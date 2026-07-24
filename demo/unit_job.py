"""wherewent demo: the feedback-shaped unit-aware job (v0.3, DESIGN-v3.md).

Models the exact production shape v0.3 was built for, in one job:

  1. A ONE-SHOT heavyweight statement at startup (calls == 1, deliberately
     slow -- an unindexed self-join over a seeded ledger table forces SQLite
     to do O(n^2) row-pair work before it can return its single aggregate
     row). On a small run this one statement can dwarf everything else --
     the case R5 exists to name, and the case that (pre-v0.3) suppressed R4
     by inflating the wall%% denominator.
  2. A per-unit function, `process_one(session, i)`, issuing a SELECT +
     UPDATE + INSERT from the SAME function every call -- individually
     modest per query group, but combined they clear R4's scale trigger even
     though the cluster stays a small slice of a wall dominated by #1. The
     three statements are DELIBERATELY on three different source lines (real
     ORM/helper code naturally spans several lines -- collapsing them onto
     one line via a shared execute() choke point would hide the exact bug
     R4's clustering key is being fixed for: it is moving from
     (file, line, function) to (file, function) so same-function,
     different-line statements recombine into one cluster. See
     tests/test_rules_cluster.py for fixtures mirroring this shape.)
  3. A real, ever-growing history table the per-unit function re-scans (via
     COUNT(*), not a sleep) once the run passes its halfway point, and more
     times the later the unit runs -- so later units do genuinely more real
     query work than earlier ones. R6's "rising per-unit cost" trend.

Not runnable directly as `python demo/unit_job.py` -- see demo/run_units.py,
the actual entry point, for why (module identity matters for
`--unit-function demo.unit_job:process_one` to actually patch what this loop
calls through).

Env vars (all optional):
  WHEREWENT_UNIT_ROWS         per-unit iterations (default 400)
  WHEREWENT_UNIT_LEDGER_ROWS  rows seeded for the one-shot self-join (default 5500 --
                              tuned so the O(n^2) self-join takes ~1-1.5s, comfortably
                              dominant enough to clear R5's >15%-of-wall bar and push
                              the R4 cluster below 10% of RAW wall, reproducing the
                              feedback fixture's shape at this run's actual timings)
"""
import os

from sqlalchemy import (
    Float,
    Integer,
    String,
    create_engine,
    event,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Ledger(Base):
    """Seeded once, read once (by the one-shot heavyweight query)."""

    __tablename__ = "unit_job_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    amount: Mapped[float] = mapped_column(Float)


class Receivable(Base):
    """One row per unit -- process_one's SELECT + UPDATE target."""

    __tablename__ = "unit_job_receivables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    amount: Mapped[float] = mapped_column(Float)


class History(Base):
    """Grows by one row per unit -- process_one's INSERT target, and (once
    the run passes its halfway point) also the target of the growing
    COUNT(*) re-scans that drive R6's growth signal.
    """

    __tablename__ = "unit_job_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receivable_id: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(64))


def make_engine(db_path):
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        # Same rationale as naive_job.py: skip the fsync-per-commit disk
        # flush so hundreds of per-unit commits stay fast enough for a
        # demo/test loop. Commit *overhead* (the round-trip) is untouched.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.close()

    return engine


def seed(engine, ledger_rows, unit_rows):
    """Fast bulk seed -- a single executemany-style INSERT per table, so this
    setup cost stays small and does not itself become the R5 one-shot (that
    role belongs to `run_one_shot_heavyweight`, below).
    """
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(Ledger),
            [{"id": i, "amount": float(i % 997)} for i in range(ledger_rows)],
        )
        conn.execute(
            insert(Receivable),
            [{"id": i, "status": "open", "amount": float(100 + i)} for i in range(unit_rows)],
        )


def run_one_shot_heavyweight(engine):
    """A single deliberately expensive statement, run exactly once.

    An unindexed self-join (`a.amount < b.amount`) over the ledger table is
    O(n^2) in the seeded row count, and SQLite must do that comparison work
    before it can produce the single COUNT(*) row -- so almost all of the
    cost lands inside ONE cursor.execute() call (calls == 1), exactly the
    shape R1/R3/R4 (which all assume repeated/chatty statements) cannot see,
    and R5 exists to name.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "SELECT COUNT(*) FROM unit_job_ledger a "
                "JOIN unit_job_ledger b ON a.amount < b.amount"
            )
        )
        return result.scalar_one()


# -- growth tuning, self-configured from the same env var run_units.py reads
# for the loop length, so the "back 40%%" always lines up with the actual run.
_UNIT_ROWS = int(os.environ.get("WHEREWENT_UNIT_ROWS", "400"))
# Growth confined to the back ~40%% of units: with >=50%% of units at the base
# query count (3), the MEDIAN queries/unit over the whole run stays exactly 3
# regardless of how far the tail climbs -- it's a genuinely rising TAIL (real
# COUNT(*) scans over a real, ever-growing table -- not a sleep), not a shift
# of the whole distribution, so the run still reads as "3 queries/unit" while
# the LAST-100-units window mean climbs sharply above the FIRST-100 window.
_GROWTH_STARTS_AT = int(_UNIT_ROWS * 0.6)
_GROWTH_BLOCK = max(1, (_UNIT_ROWS - _GROWTH_STARTS_AT) // 8)


def process_one(session, i):
    """The per-unit target: SELECT + UPDATE + INSERT, one call each, on
    three different source lines of this SAME function (the real-world
    shape -- see module docstring for why that matters).

    `--unit-function demo.unit_job:process_one` wraps exactly this callable,
    so each call here becomes one unit in wherewent's per-unit report.
    """
    row = session.execute(select(Receivable).where(Receivable.id == i)).scalar_one()
    amount = row.amount  # noqa: F841 (kept for realism; a real job would use it)

    session.execute(update(Receivable).where(Receivable.id == i).values(status="processed"))

    session.execute(insert(History).values(receivable_id=i, note=f"processed #{i}"))
    session.commit()

    if i >= _GROWTH_STARTS_AT:
        reads = 1 + (i - _GROWTH_STARTS_AT) // _GROWTH_BLOCK
        for _ in range(reads):
            session.execute(select(func.count()).select_from(History)).scalar_one()
