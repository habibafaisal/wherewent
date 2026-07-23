"""wherewent demo: the FIXED job.

Same data, same schema as naive_job.py, but rows are batched: each batch of
BATCH_SIZE rows is inserted with ONE Core-style bulk insert --
`session.execute(insert(Event), list_of_param_dicts)` -- then ONE commit.
Passing a list of dicts to a Core insert() lets the driver executemany (or
SQLAlchemy's insertmanyvalues) the whole batch in a single cursor call.
NOTE: session.add_all() + commit does NOT achieve this -- the ORM still
emits one INSERT per row (to fetch each row's generated PK); it only fixes
the commit count, not the query count. The Core-style bulk insert is what
collapses per-row INSERTs, not just commits.

Run directly: python demo/fixed_job.py
Env vars:
  WHEREWENT_DEMO_DB   sqlite file path (default ./demo_events.db)
  WHEREWENT_DEMO_ROWS number of rows to insert (default 100000)
"""
import os
import time

from sqlalchemy import Integer, String, Float, create_engine, event, func, insert, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session

BATCH_SIZE = 5000


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
        # Same rationale as naive_job.py: keeps the demo's disk I/O fast
        # without hiding the (much smaller, here) commit round-trip cost.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.close()

    return engine


def insert_batch(session, batch):
    session.execute(insert(Event), batch)
    session.commit()


def main():
    db_path = os.environ.get("WHEREWENT_DEMO_DB", "./demo_events.db")
    rows = int(os.environ.get("WHEREWENT_DEMO_ROWS", "100000"))

    if os.path.exists(db_path):
        os.remove(db_path)

    engine = make_engine(db_path)
    Base.metadata.create_all(engine)

    print(f"[fixed] inserting {rows} rows into {db_path}, batches of {BATCH_SIZE}")
    start = time.perf_counter()

    with Session(engine) as session:
        batch = []
        for i in range(rows):
            batch.append({"name": f"evt-{i}", "value": float(i)})
            if len(batch) >= BATCH_SIZE:
                insert_batch(session, batch)
                batch = []
                elapsed = time.perf_counter() - start
                print(f"[fixed] {i + 1}/{rows} rows committed ({elapsed:.1f}s elapsed)")
        if batch:
            insert_batch(session, batch)

    with Session(engine) as session:
        count = session.execute(select(func.count()).select_from(Event)).scalar_one()

    elapsed = time.perf_counter() - start
    print(f"[fixed] done in {elapsed:.2f}s -- sanity check count(*) = {count}")
    assert count == rows, f"expected {rows} rows, found {count}"


if __name__ == "__main__":
    main()
