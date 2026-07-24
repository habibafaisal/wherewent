"""Tests for async call-site attribution (v0.2 #1, DESIGN-v2.md AGENT A).

In async SQLAlchemy, before_cursor_execute runs inside a greenlet whose sync
stack has no user frames, so the sync-only resolve_call_site() returns None.
Agent A's fix stamps a contextvar at the async entry boundary (wrapping
AsyncSession/AsyncConnection methods in Recorder.install()) that survives
into the hook. This file exercises that end-to-end: install the recorder,
run a small real AsyncSession job over aiosqlite, and check the resulting
GroupSnapshot.call_site is attributed to *this* module's user frame.

NOTE: at the time this file was written, the async wrappers in recorder.py
do not exist yet (a parallel agent is adding them per DESIGN-v2.md). Until
they land, expect the call_site assertions below to fail with `None`, not
with an ImportError/crash -- that failure mode is the "contract-only,
pending integration" case, not a bug in this test.
"""
import asyncio
import os

import pytest

pytest.importorskip("aiosqlite")

from sqlalchemy import Float, Integer, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from wherewent.recorder import Recorder

ROWS = 30


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widgets_async_callsite_test"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)


async def _run_async_job(db_path: str, rows: int) -> None:
    """The user-code entry point the recorder must attribute queries to.

    Mirrors demo/async_naive_job.py's process_one(): one insert, one commit,
    one lookup per row -- so it exhibits both the INSERT and SELECT groups
    the recorder needs to attribute.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        for i in range(rows):
            await _process_one(session, i)

    await engine.dispose()


async def _process_one(session: AsyncSession, i: int) -> None:
    session.add(Widget(name=f"w-{i}", value=float(i)))
    await session.commit()
    await session.execute(select(Widget).where(Widget.name == f"w-{i}"))


@pytest.fixture
def installed_recorder(request):
    """A fresh Recorder(), install()ed and GUARANTEED disable()d at teardown.

    Recorder.install() registers its SQLAlchemy listeners at the Engine CLASS
    level (process-global by design), and recorder.py's one-time commit/rollback
    timing wrap is guarded per DIALECT INSTANCE (`dialect._wherewent_wrapped`),
    not per Recorder. So a recorder from this module that was never disabled
    would keep winning that one-time wrap on every engine created afterwards,
    permanently denying commit_measurable/rollback_measurable to any Recorder
    built by a LATER test in the same process (see the D2 tests in
    tests/test_hardening.py). disable() removes the class-level listeners,
    restores wrapped dialects and drops our sys.meta_path finders, so tearing
    down here keeps this module order-independent.
    """
    rec = Recorder()
    rec.install(None)
    request.addfinalizer(rec.disable)
    return rec


def _find_group(groups, keyword):
    for g in groups:
        sql = g.normalized_sql.lower()
        if keyword in sql and "widgets_async_callsite_test" in sql:
            return g
    return None


def test_async_insert_and_select_groups_get_a_real_call_site(tmp_path, installed_recorder):
    # A fresh Recorder(), not the module singleton: DESIGN-v2.md documents
    # the async wrappers as global, idempotent monkeypatches installed
    # inside Recorder.install() (guarded by a sentinel attr), so constructing
    # our own instance is safe and keeps this test's accounting isolated from
    # any other test that touches the shared `recorder` singleton. install()
    # must still be called -- the async wrappers are only installed there.
    # The fixture owns install()/disable() so the process-global hooks never
    # leak into a later test.
    rec = installed_recorder

    db_path = tmp_path / "async_callsite.db"
    asyncio.run(_run_async_job(str(db_path), ROWS))

    run = rec.snapshot()
    insert_group = _find_group(run.groups, "insert")
    select_group = _find_group(run.groups, "select")

    all_sql = [g.normalized_sql for g in run.groups]
    assert insert_group is not None, f"no INSERT group recorded; groups={all_sql}"
    assert select_group is not None, f"no SELECT group recorded; groups={all_sql}"

    for label, group in (("insert", insert_group), ("select", select_group)):
        assert group.call_site is not None, (
            f"{label} group.call_site is None -- async call-site attribution "
            f"(DESIGN-v2 AGENT A #1) is not wired up yet, or the contextvar "
            f"wasn't stamped for this entrypoint"
        )
        file, line, func_name = group.call_site
        assert os.path.basename(file) == os.path.basename(__file__), (
            f"{label} group attributed to {file!r} (expected this test module's "
            f"user frame, not a sqlalchemy/greenlet internal frame)"
        )
        assert func_name == "_process_one"
        assert isinstance(line, int) and line > 0


def test_async_call_site_survives_commit_before_execute_boundary(tmp_path, installed_recorder):
    # The addendum explicitly calls out that INSERTs issued from
    # session.commit()/flush bypass execute(), so *every* async entrypoint
    # that can trigger a flush must stamp the contextvar, not just execute().
    # This re-asserts that specifically for the commit-triggered INSERT.
    rec = installed_recorder

    db_path = tmp_path / "async_callsite_commit.db"
    asyncio.run(_run_async_job(str(db_path), ROWS))

    run = rec.snapshot()
    insert_group = _find_group(run.groups, "insert")
    assert insert_group is not None
    assert insert_group.call_site is not None, (
        "INSERT emitted via session.commit()/flush was not attributed -- only "
        "explicit execute() calls appear to stamp the async call-site contextvar"
    )
