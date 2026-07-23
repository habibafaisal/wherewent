"""wherewent demo: the ANTI-PATTERN job.

Inserts rows one at a time, calling session.commit() after every single
session.add(). This is the classic "commit-per-row" mistake: each commit is
a full transaction round-trip, so the job pays that cost N times instead of
once. It exists so `wherewent` has something obviously bad to catch.

Run directly: python demo/naive_job.py
Env vars:
  WHEREWENT_DEMO_DB   sqlite file path (default ./demo_events.db)
  WHEREWENT_DEMO_ROWS number of rows to insert (default 100000)
"""
import os
import time

from sqlalchemy import Integer, String, Float, create_engine, event, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)


def make_engine(db_path):
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        # synchronous=OFF skips the fsync-per-commit disk flush. Without it the
        # naive job's ~100k individual commits would take many minutes on real
        # disks, which is too slow for a demo loop. Commit *overhead* (the
        # transaction round-trip itself) is untouched -- that's what we want
        # wherewent to catch, so this pragma keeps the demo honest, just fast.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.close()

    return engine


def main():
    db_path = os.environ.get("WHEREWENT_DEMO_DB", "./demo_events.db")
    rows = int(os.environ.get("WHEREWENT_DEMO_ROWS", "100000"))

    if os.path.exists(db_path):
        os.remove(db_path)

    engine = make_engine(db_path)
    Base.metadata.create_all(engine)

    print(f"[naive] inserting {rows} rows into {db_path}, one commit per row")
    start = time.perf_counter()

    with Session(engine) as session:
        for i in range(rows):
            session.add(Event(name=f"evt-{i}", value=float(i)))
            session.commit()  # ANTI-PATTERN: one transaction per row
            if (i + 1) % max(rows // 10, 1) == 0:
                elapsed = time.perf_counter() - start
                print(f"[naive] {i + 1}/{rows} rows committed ({elapsed:.1f}s elapsed)")

    with Session(engine) as session:
        count = session.execute(select(func.count()).select_from(Event)).scalar_one()

    elapsed = time.perf_counter() - start
    print(f"[naive] done in {elapsed:.2f}s -- sanity check count(*) = {count}")
    assert count == rows, f"expected {rows} rows, found {count}"


if __name__ == "__main__":
    main()
