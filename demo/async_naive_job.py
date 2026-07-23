"""wherewent demo: the ASYNC anti-pattern job.

Same commit-per-row anti-pattern as naive_job.py, but exercised through
AsyncSession over sqlite+aiosqlite instead of the sync ORM. This is the
artifact that proves wherewent's async call-site fix (v0.2 #1): in async
SQLAlchemy, before_cursor_execute runs inside a greenlet whose sync stack has
no user frames, so the old sync-only resolve_call_site() returns None and the
CALL SITE column shows "--". The fix stamps a contextvar at the async entry
boundary (AsyncSession.execute/commit/flush/... wrappers) that survives into
the hook, so queries issued from process_one() below get attributed correctly.

Run directly: python demo/async_naive_job.py
Env vars:
  WHEREWENT_DEMO_DB   sqlite file path (default ./async_demo.db)
  WHEREWENT_DEMO_ROWS number of rows to insert (default 2000)
"""
import asyncio
import os
import time

from sqlalchemy import Integer, String, Float, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)


def make_engine(db_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)

    # "connect" fires at the DBAPI level even for an async engine -- listen on
    # the underlying sync_engine, same rationale as naive_job.py: skip the
    # fsync-per-commit disk flush so ~2000 individual commits stay fast enough
    # for a demo/test loop. Commit *overhead* (the round-trip itself) is
    # untouched -- that's what wherewent is meant to catch.
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.close()

    return engine


async def process_one(session: AsyncSession, i: int) -> None:
    """The user function: one insert, one commit, one lookup -- per row.

    This is the clearly-named async call site the recorder must attribute the
    INSERT and SELECT query groups back to (instead of "--"/unknown).
    """
    session.add(Event(name=f"evt-{i}", value=float(i)))
    await session.commit()  # ANTI-PATTERN: one transaction per row
    await session.execute(select(Event).where(Event.name == f"evt-{i}"))


async def main_async():
    db_path = os.environ.get("WHEREWENT_DEMO_DB", "./async_demo.db")
    rows = int(os.environ.get("WHEREWENT_DEMO_ROWS", "2000"))

    if os.path.exists(db_path):
        os.remove(db_path)

    engine = make_engine(db_path)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    print(f"[async-naive] inserting {rows} rows into {db_path}, one commit per row")
    start = time.perf_counter()

    async with session_factory() as session:
        for i in range(rows):
            await process_one(session, i)
            if (i + 1) % max(rows // 10, 1) == 0:
                elapsed = time.perf_counter() - start
                print(f"[async-naive] {i + 1}/{rows} rows committed ({elapsed:.1f}s elapsed)")

    async with session_factory() as session:
        count = (await session.execute(select(func.count()).select_from(Event))).scalar_one()

    elapsed = time.perf_counter() - start
    print(f"[async-naive] done in {elapsed:.2f}s -- sanity check count(*) = {count}")
    assert count == rows, f"expected {rows} rows, found {count}"

    await engine.dispose()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
