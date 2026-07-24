"""Tests for --unit-function / wherewent.unit() work-unit profiling and R6
"Rising per-unit cost" (DESIGN-v3.md, "unit-aware profiling").

Mechanism under test (per the build contract):
  * `WHEREWENT_UNIT_FUNCTION=pkg.mod:func` + `Recorder.install()` (or the
    `install_from_env()` entry point) monkeypatches the named callable so
    each top-level (non-recursive) call becomes one "unit", accumulating
    into `RunSnapshot.unit_stats` (a `stats.UnitStats`).
  * `wherewent.unit(name="unit")` is a public context manager that stamps the
    SAME per-unit accounting from user code, and is a safe no-op (still runs
    the body) when the recorder isn't installed.
  * R6 fires when unit_stats.count >= 20, both growth windows are present,
    and the last-100 mean duration is >= 1.5x the first-100 mean.

Because the target function is monkeypatched by attribute on its owning
MODULE object, each env-var-driven test below uses its OWN uniquely-named
module-level target function (sync_process_one_*, async_process_one_*)
rather than sharing one -- the wrapper is documented as idempotent (a
`_wherewent_unit_wrapped` marker, same convention as the existing async
call-site wrappers in recorder.py), so re-wrapping an already-wrapped name
across several fresh Recorder() instances in the same test process would
silently keep recording into whichever Recorder wrapped it FIRST. Distinct
target names sidestep that ambiguity entirely.

NOTE: at the time this file was written, none of `RunSnapshot.unit_stats`,
`WHEREWENT_UNIT_FUNCTION` handling, or `wherewent.unit()` exist yet -- Agent A
is landing them in parallel (DESIGN-v3.md). Until they land, expect
AttributeError / None-shaped failures here, not a crash of the test session --
the same "contract-only, pending integration" convention used throughout this
suite (see test_async_callsite.py, test_rules_cluster.py).

CROSS-TEST ISOLATION NOTE: `Recorder.install()` deliberately registers its
SQLAlchemy hooks at the `Engine` CLASS level ("every engine in the process is
captured, zero config" -- by design, since a real job only ever installs one
recorder for its whole process). But per-unit accounting lives in a single
MODULE-level ContextVar (`_current_unit`) that every Recorder instance reads
through, not per-instance state. So a Recorder from an EARLIER test that is
still installed and still `_unit_enabled` keeps reacting to every query
executed by a LATER test in this same pytest process, and double-counts into
whatever unit is active there -- there's no supported multi-Recorder-per-
process story (nor a public uninstall/disable), so `unit_recorder` below
pokes `_unit_enabled` off at teardown purely for test-to-test isolation, not
as a src/ change. Confirmed empirically: without this, tests pass or fail
depending on how many *other* unit-enabled Recorders happened to run earlier
in the same pytest invocation -- report this to Agent A as a real
characteristic worth a public teardown hook.
"""
import json

import pytest

from sqlalchemy import Float, Integer, String, create_engine, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

import wherewent
import wherewent.recorder as recorder_mod
from wherewent import rules
from wherewent.recorder import Recorder


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "unit_profiling_widgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)


class GrowthHistory(Base):
    """A real, ever-growing table for the R6 growth fixture below to insert
    into and re-scan -- deliberately NOT a Python list + time.sleep stand-in
    (see growth_process_one's docstring for why that distinction matters).
    """

    __tablename__ = "unit_profiling_growth_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    i: Mapped[int] = mapped_column(Integer)


def _contains_number(text, n):
    return str(n) in text or f"{n:,}" in text


@pytest.fixture
def unit_recorder(request):
    """A fresh Recorder(), auto-`_unit_enabled = False`'d at teardown --
    see the module docstring's CROSS-TEST ISOLATION NOTE for why that
    matters. Use for any test that enables unit mode on an instance it owns
    (not the `recorder_mod.recorder` singleton -- those tests swap it back
    in a try/finally and disable it there instead)."""
    rec = Recorder()
    request.addfinalizer(lambda: setattr(rec, "_unit_enabled", False))
    return rec


# --------------------------------------------------------------------------
# Distinct module-level unit targets -- one name per env-var-driven test, see
# module docstring for why they must not be shared.
# --------------------------------------------------------------------------

def sync_process_one_basic(session, i):
    """2 queries/call: 1 INSERT (via add()+commit()/flush) + 1 SELECT."""
    session.add(Widget(name=f"w-{i}", value=float(i)))
    session.commit()
    session.execute(select(Widget).where(Widget.name == f"w-{i}")).scalar_one()


def sync_process_one_small(session, i):
    session.add(Widget(name=f"small-{i}", value=float(i)))
    session.commit()


def sync_process_one_json(session, i):
    session.add(Widget(name=f"json-{i}", value=float(i)))
    session.commit()


def sync_process_one_env(session, i):
    session.add(Widget(name=f"env-{i}", value=float(i)))
    session.commit()
    session.execute(select(Widget).where(Widget.name == f"env-{i}")).scalar_one()


async def async_process_one_basic(session: AsyncSession, i: int) -> None:
    """2 queries/call, mirrors sync_process_one_basic for the async path."""
    session.add(Widget(name=f"aw-{i}", value=float(i)))
    await session.commit()
    await session.execute(select(Widget).where(Widget.name == f"aw-{i}"))


# --------------------------------------------------------------------------
# sync --unit-function
# --------------------------------------------------------------------------

def test_sync_unit_function_counts_and_attribution(tmp_path, monkeypatch, unit_recorder):
    # `Recorder().install()` itself does not read env vars (only the
    # `install_from_env()` free function -- covered separately below -- and
    # the module-level `recorder` singleton do). A fresh, explicitly
    # `enable_unit()`-configured instance is how a caller wires
    # --unit-function onto an instance they own; setenv here documents the
    # spec this mirrors, and `enable_unit` reads it back out for clarity.
    spec = "tests.test_unit_profiling:sync_process_one_basic"
    monkeypatch.setenv("WHEREWENT_UNIT_FUNCTION", spec)
    rec = unit_recorder
    rec.enable_unit(spec)
    rec.install(None)

    engine = create_engine(f"sqlite:///{tmp_path / 'sync.db'}", future=True)
    Base.metadata.create_all(engine)

    n = 30
    with Session(engine, expire_on_commit=False) as session:
        for i in range(n):
            sync_process_one_basic(session, i)

    run = rec.snapshot()

    assert run.unit_stats is not None, "unit mode was enabled via WHEREWENT_UNIT_FUNCTION"
    us = run.unit_stats
    assert us.wrapped is True
    assert us.count == n, f"expected {n} recorded units, got {us.count}"
    assert us.median_queries == pytest.approx(2.0), (
        f"process_one issues exactly 2 queries/call (insert+select), got median_queries={us.median_queries}"
    )
    assert us.mean_queries == pytest.approx(2.0)
    assert us.mean_commits == pytest.approx(1.0)
    assert us.mean_duration > 0
    assert us.median_duration > 0


# --------------------------------------------------------------------------
# async --unit-function, driven sequentially by asyncio.run
# --------------------------------------------------------------------------

def test_async_unit_function_counts_exact(tmp_path, monkeypatch, unit_recorder):
    import asyncio

    spec = "tests.test_unit_profiling:async_process_one_basic"
    monkeypatch.setenv("WHEREWENT_UNIT_FUNCTION", spec)
    rec = unit_recorder
    rec.enable_unit(spec)
    rec.install(None)

    async def _run(db_path, n):
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            for i in range(n):
                # sequential, awaited in order -> exact per-unit accounting
                await async_process_one_basic(session, i)
        await engine.dispose()

    n = 25
    db_path = tmp_path / "async.db"
    asyncio.run(_run(str(db_path), n))

    run = rec.snapshot()

    assert run.unit_stats is not None
    us = run.unit_stats
    assert us.wrapped is True
    assert us.count == n, f"expected {n} recorded async units, got {us.count}"
    assert us.median_queries == pytest.approx(2.0)
    assert us.mean_commits == pytest.approx(1.0)


# --------------------------------------------------------------------------
# install_from_env(): the actual WHEREWENT_UNIT_FUNCTION-reading entry point
# (the shim's `python -m wherewent.cli run --unit-function ...` calls this,
# not Recorder().install() directly -- that method never reads env vars
# itself. Runs against the module-level `recorder` singleton, since that is
# what install_from_env() operates on.)
# --------------------------------------------------------------------------

def test_install_from_env_reads_unit_function_and_wraps(tmp_path, monkeypatch):
    monkeypatch.setenv("WHEREWENT_UNIT_FUNCTION", "tests.test_unit_profiling:sync_process_one_env")
    monkeypatch.delenv("WHEREWENT_SAVE", raising=False)

    original = recorder_mod.recorder
    fresh = recorder_mod.Recorder()
    recorder_mod.recorder = fresh
    try:
        recorder_mod.install_from_env()

        engine = create_engine(f"sqlite:///{tmp_path / 'env.db'}", future=True)
        Base.metadata.create_all(engine)

        n = 12
        with Session(engine, expire_on_commit=False) as session:
            for i in range(n):
                sync_process_one_env(session, i)

        run = fresh.snapshot()
        assert run.unit_stats is not None, "install_from_env() should have read WHEREWENT_UNIT_FUNCTION and enabled unit mode"
        us = run.unit_stats
        assert us.wrapped is True
        assert us.count == n
        assert us.median_queries == pytest.approx(2.0)
    finally:
        fresh._unit_enabled = False  # see module docstring's CROSS-TEST ISOLATION NOTE
        recorder_mod.recorder = original


# --------------------------------------------------------------------------
# growth -> R6 "Rising per-unit cost"
# --------------------------------------------------------------------------

GROWTH_TOTAL = 300
GROWTH_BLOCK = 30


def growth_process_one(session, i):
    """Query COUNT rises with i, genuinely -- not a sleep stand-in.

    Unit `i` inserts one row into a real, ever-growing `growth_history`
    table, then issues `1 + i // GROWTH_BLOCK` real COUNT(*) scans over that
    table (1 scan for the first block of units, climbing to 10 scans/unit by
    the end of a 300-unit run). Both the query COUNT and each individual
    query's real scan cost rise with `i`, so later units do measurably more
    real query work than earlier ones -- exactly the shape R6 exists to
    name, produced without any artificial time.sleep (reviewer feedback: a
    duration-only, sleep-based stand-in doesn't prove the per-unit query
    load itself is rising).
    """
    session.execute(insert(GrowthHistory).values(i=i))
    reads = 1 + i // GROWTH_BLOCK
    for _ in range(reads):
        session.execute(select(func.count()).select_from(GrowthHistory)).scalar_one()
    session.commit()


def _expected_unit_queries(i):
    """Exact query count growth_process_one issues for unit `i`.

    1 INSERT + (1 + i // GROWTH_BLOCK) COUNT(*) scans. Commits are accounted
    separately by the recorder, so they are not queries. Kept in lockstep with
    growth_process_one above so the windows below are derived, not magic.
    """
    return 1 + (1 + i // GROWTH_BLOCK)


def _window_mean_queries(lo, hi):
    return sum(_expected_unit_queries(i) for i in range(lo, hi)) / (hi - lo)


def test_growth_triggers_r6_via_unit_function(tmp_path, monkeypatch, unit_recorder):
    """End-to-end: a real Recorder + real --unit-function wrapping + a real
    SQLAlchemy loop -> snapshot() -> rules.evaluate(). Deliberately NOT a
    hand-built RunSnapshot -- a hand-built snapshot proves the arithmetic but
    not the wiring (reviewer feedback on DESIGN-v3.md); this proves both.
    """
    spec = "tests.test_unit_profiling:growth_process_one"
    monkeypatch.setenv("WHEREWENT_UNIT_FUNCTION", spec)
    rec = unit_recorder
    rec.enable_unit(spec)
    rec.install(None)

    engine = create_engine(f"sqlite:///{tmp_path / 'growth.db'}", future=True)
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as session:
        for i in range(GROWTH_TOTAL):
            growth_process_one(session, i)

    run = rec.snapshot()
    us = run.unit_stats
    assert us is not None
    assert us.count == GROWTH_TOTAL

    # The fixture is built so the per-unit query COUNT genuinely rises
    # (2 -> 11 real queries/unit across the run). Query counts are exact
    # integers the recorder attributes directly, so every assertion below is
    # deterministic -- unlike per-unit DURATION, which is wall time and drifts
    # with system noise plus first-window statement-compilation warmup (observed
    # last/first duration ratios of 1.72x-2.43x run to run, i.e. straddling a
    # 1.5x bar). R6 fires on EITHER the duration slope OR the query slope (D9),
    # so this drives and asserts the deterministic half.
    exp_first_q = _window_mean_queries(0, 100)                          # 3.2
    exp_last_q = _window_mean_queries(GROWTH_TOTAL - 100, GROWTH_TOTAL)  # 9.8
    exp_mean_q = _window_mean_queries(0, GROWTH_TOTAL)                  # 6.5

    assert us.mean_queries == pytest.approx(exp_mean_q), (
        f"growth fixture issues an exactly-known rising 2..11 queries/unit; "
        f"expected mean_queries={exp_mean_q}, got {us.mean_queries}"
    )

    assert us.first_window_n == 100
    assert us.last_window_n == 100
    first_q = us.first_window_mean_queries
    last_q = us.last_window_mean_queries
    assert first_q == pytest.approx(exp_first_q), (
        f"first 100 units issue exactly {exp_first_q} queries/unit on average, got {first_q}"
    )
    assert last_q == pytest.approx(exp_last_q), (
        f"last 100 units issue exactly {exp_last_q} queries/unit on average, got {last_q}"
    )
    # This is the exact bar R6's query slope tests. 9.8 / 3.2 = 3.06x, cleared
    # by a factor of two on integers that cannot move -- no wall clock involved.
    assert last_q >= 1.5 * first_q, (
        f"first_window_mean_queries={first_q} last_window_mean_queries={last_q} "
        "-- the rising per-unit query load must clear R6's 1.5x slope bar"
    )

    # Duration windows must still be populated and positive (that much IS
    # deterministic); their RATIO is deliberately not asserted -- it is real
    # wall time, and R6 does not need it to fire here.
    assert us.first_window_mean_duration is not None
    assert us.last_window_mean_duration is not None
    assert us.first_window_mean_duration > 0
    assert us.last_window_mean_duration > 0

    findings = rules.evaluate(run)
    r6 = [f for f in findings if f.rule == "R6"]
    assert len(r6) == 1, f"expected R6 to fire, got {[f.rule for f in findings]}"
    finding = r6[0]
    assert finding.title.lower().startswith("rising")

    # R6's attributed seconds must be the QUERY-derived estimate, priced at the
    # run's mean per-query DB time -- not the duration-derived one. Per-unit
    # query counts are exact integers the recorder attributes directly (zero
    # clock involvement), so this number is reproducible run to run. The
    # duration windows are NOT a clean baseline: the first window pays
    # one-time SQLAlchemy statement-compilation warmup, which inflates
    # `first_window_mean_duration` and biases a duration-derived estimate low
    # -- by enough, on real runs, to flip whether R6 clears the trivia filter
    # from one byte-identical run to the next. Compute the expectation from
    # the snapshot's own public numbers, independent of the rule's source.
    per_q = (run.db_time / run.total_queries) if run.total_queries > 0 else 0.0
    excess = max(0.0, (us.mean_queries - first_q)) * us.count
    expected_seconds = max(0.0, excess * per_q)
    assert finding.seconds == pytest.approx(expected_seconds, rel=1e-6)

    # Determinism guard: this pins the actual defect. The duration-derived
    # quantity below must NOT be what R6 reported -- if a regression makes R6
    # prefer the warmup-contaminated duration estimate again, this fails
    # loudly instead of only flaking on other machines/runs.
    duration_derived_seconds = max(
        0.0, (us.mean_duration - us.first_window_mean_duration) * us.count
    )
    assert finding.seconds != pytest.approx(duration_derived_seconds, rel=1e-6), (
        f"R6 seconds ({finding.seconds}) matches the warmup-contaminated "
        f"duration-derived estimate ({duration_derived_seconds}) -- expected the "
        "deterministic query-derived attribution instead"
    )

    detail = finding.detail
    assert "100" in detail, f"detail should name the 100-unit windows: {detail!r}"
    import re
    assert re.search(r"\d+%", detail), f"detail should show a growth percentage: {detail!r}"


# --------------------------------------------------------------------------
# context-manager form: `with wherewent.unit("receivable"):`
# --------------------------------------------------------------------------

def test_context_manager_attributes_units_over_sequential_loop(tmp_path):
    original = recorder_mod.recorder
    fresh = recorder_mod.Recorder()
    recorder_mod.recorder = fresh
    try:
        fresh.install(None)

        engine = create_engine(f"sqlite:///{tmp_path / 'cm.db'}", future=True)
        Base.metadata.create_all(engine)

        n = 15
        with Session(engine, expire_on_commit=False) as session:
            for i in range(n):
                with wherewent.unit("receivable"):
                    session.add(Widget(name=f"cm-{i}", value=float(i)))
                    session.commit()
                    session.execute(select(Widget).where(Widget.name == f"cm-{i}")).scalar_one()

        run = fresh.snapshot()
        assert run.unit_stats is not None
        us = run.unit_stats
        assert us.count == n
        assert us.median_queries == pytest.approx(2.0)
        assert us.mean_commits == pytest.approx(1.0)
    finally:
        fresh._unit_enabled = False  # see module docstring's CROSS-TEST ISOLATION NOTE
        recorder_mod.recorder = original


def test_context_manager_is_noop_when_recorder_not_installed():
    original = recorder_mod.recorder
    # a definitely-uninstalled fresh singleton -- wherewent.unit() must still
    # run the body and never raise.
    recorder_mod.recorder = recorder_mod.Recorder()
    try:
        ran = []
        with wherewent.unit("receivable"):
            ran.append(1)
        assert ran == [1], "unit() must still execute the body when unit mode/recorder is not installed"
    finally:
        recorder_mod.recorder = original


# --------------------------------------------------------------------------
# negatives
# --------------------------------------------------------------------------

def test_no_unit_set_yields_none_unit_stats_and_no_r6(tmp_path):
    rec = Recorder()
    rec.install(None)

    engine = create_engine(f"sqlite:///{tmp_path / 'none.db'}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.execute(select(1))

    run = rec.snapshot()
    assert run.unit_stats is None
    assert not any(f.rule == "R6" for f in rules.evaluate(run))


def test_count_below_20_suppresses_r6(tmp_path, monkeypatch, unit_recorder):
    spec = "tests.test_unit_profiling:sync_process_one_small"
    monkeypatch.setenv("WHEREWENT_UNIT_FUNCTION", spec)
    rec = unit_recorder
    rec.enable_unit(spec)
    rec.install(None)

    engine = create_engine(f"sqlite:///{tmp_path / 'small.db'}", future=True)
    Base.metadata.create_all(engine)

    n = 10
    with Session(engine, expire_on_commit=False) as session:
        for i in range(n):
            sync_process_one_small(session, i)

    run = rec.snapshot()
    assert run.unit_stats is not None
    assert run.unit_stats.count == n
    assert run.unit_stats.count < 20
    assert not any(f.rule == "R6" for f in rules.evaluate(run))


# --------------------------------------------------------------------------
# JSON (--save) round-trip
# --------------------------------------------------------------------------

_UNIT_STATS_FIELDS = (
    "name", "wrapped", "count", "median_duration", "mean_duration",
    "median_queries", "mean_queries", "mean_commits", "mean_rollbacks",
    "mean_rows", "first_window_n", "first_window_mean_duration",
    "last_window_n", "last_window_mean_duration",
)


def test_json_save_emits_unit_stats_object(tmp_path, monkeypatch, unit_recorder):
    spec = "tests.test_unit_profiling:sync_process_one_json"
    monkeypatch.setenv("WHEREWENT_UNIT_FUNCTION", spec)
    rec = unit_recorder
    rec.enable_unit(spec)
    save_path = tmp_path / "out.json"
    rec.install(str(save_path))

    engine = create_engine(f"sqlite:///{tmp_path / 'json.db'}", future=True)
    Base.metadata.create_all(engine)

    n = 25
    with Session(engine, expire_on_commit=False) as session:
        for i in range(n):
            sync_process_one_json(session, i)

    rec.finalize()

    with open(save_path) as f:
        payload = json.load(f)

    assert "unit_stats" in payload
    us = payload["unit_stats"]
    assert us is not None
    for field in _UNIT_STATS_FIELDS:
        assert field in us, f"unit_stats JSON missing field {field!r}: {us}"
    assert us["count"] == n


def test_json_save_emits_null_unit_stats_when_disabled(tmp_path):
    rec = Recorder()
    save_path = tmp_path / "out_disabled.json"
    rec.install(str(save_path))

    engine = create_engine(f"sqlite:///{tmp_path / 'disabled.db'}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        session.execute(select(1))

    rec.finalize()

    with open(save_path) as f:
        payload = json.load(f)

    assert payload["unit_stats"] is None
