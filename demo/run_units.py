"""wherewent demo: entry point that drives demo/unit_job.py's process_one.

`demo/unit_job.py` is imported here as a module (`import demo.unit_job`) and
called through via the module attribute (`unit_job.process_one(...)`) on
every iteration -- deliberately NOT `from demo.unit_job import process_one`
bound once, and unit_job.py is deliberately not runnable directly as
`python demo/unit_job.py`. Both choices exist for the same reason:

`wherewent run --unit-function demo.unit_job:process_one` patches the
`process_one` ATTRIBUTE on the `demo.unit_job` MODULE OBJECT (immediately, if
that module is already in sys.modules; otherwise via a post-import hook). If
this script imported unit_job "fresh" as `__main__` (i.e. if unit_job.py were
launched directly as the child process's script), or bound a local name to
`process_one` before the patch could apply, the loop below would keep calling
the ORIGINAL function forever and `unit_stats.count` would stay 0 -- the
patch would have landed on a module object nothing here ever calls through.
Routing every call through `unit_job.process_one(...)` (attribute lookup, not
a bound local) sidesteps both failure modes.

Run directly: python -m demo.run_units   (needs the repo root on sys.path;
python -m already does this automatically as long as it's invoked from, or
with PYTHONPATH containing, the repo root -- see demo/benchmark.py.)

Env vars: see demo/unit_job.py's module docstring, plus
  WHEREWENT_UNIT_DEMO_DB   sqlite file path (default ./unit_job_demo.db)
"""
import os
import time

from sqlalchemy.orm import Session

import demo.unit_job as unit_job


def main():
    db_path = os.environ.get("WHEREWENT_UNIT_DEMO_DB", "./unit_job_demo.db")
    unit_rows = int(os.environ.get("WHEREWENT_UNIT_ROWS", "400"))
    ledger_rows = int(os.environ.get("WHEREWENT_UNIT_LEDGER_ROWS", "5500"))

    if os.path.exists(db_path):
        os.remove(db_path)

    engine = unit_job.make_engine(db_path)
    unit_job.seed(engine, ledger_rows, unit_rows)

    print(f"[unit-job] one-shot heavyweight: self-join over {ledger_rows} ledger rows ...")
    t0 = time.perf_counter()
    pairs = unit_job.run_one_shot_heavyweight(engine)
    print(f"[unit-job] one-shot done in {time.perf_counter() - t0:.2f}s (pairs={pairs:,})")

    print(f"[unit-job] processing {unit_rows} units through demo.unit_job.process_one ...")
    start = time.perf_counter()
    with Session(engine, expire_on_commit=False) as session:
        for i in range(unit_rows):
            # module-attribute call: picks up whatever `wherewent run
            # --unit-function` patched onto demo.unit_job, not a stale local
            # reference bound at import time (see module docstring above).
            unit_job.process_one(session, i)
            if (i + 1) % max(unit_rows // 10, 1) == 0:
                elapsed = time.perf_counter() - start
                print(f"[unit-job] {i + 1}/{unit_rows} units processed ({elapsed:.1f}s elapsed)")

    elapsed = time.perf_counter() - start
    print(f"[unit-job] done in {elapsed:.2f}s")


if __name__ == "__main__":
    main()
