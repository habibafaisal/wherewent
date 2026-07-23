<div align="center">

# wherewent

### Where did the time go? Find out in one command.

**A zero-config recorder that answers "why did this Python batch job take so long?"**

[![PyPI version](https://img.shields.io/pypi/v/wherewent.svg)](https://pypi.org/project/wherewent/)
[![Python versions](https://img.shields.io/pypi/pyversions/wherewent.svg)](https://pypi.org/project/wherewent/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/habibafaisal/wherewent/actions/workflows/ci.yml/badge.svg)](https://github.com/habibafaisal/wherewent/actions/workflows/ci.yml)

```bash
wherewent run python your_job.py
```

</div>

---

## The 0.4ms query that costs you 5 minutes

A query can be individually fast — `0.4ms` — and still sink your job, because it's
called **500,000 times from a single line of code**. Your app burns 300 seconds on
round-trips while Postgres itself only worked for 80. Every profiler you've tried shows
you *"time spent in psycopg"* and stops there.

**wherewent shows you the calling pattern.** It groups queries by shape, counts how
often each shape ran, sums the wall time, and points at the exact `file:line` in *your*
code that fired it — then tells you, in plain English with the arithmetic shown, what to
do about it.

```
====================================================================================================
wherewent — SQL flight recorder
----------------------------------------------------------------------------------------------------
wall: 26.15s   cpu: 25.41s (97% CPU busy)   queries: 20,004   commits: 20,001   rollbacks: 1
in-DB time: 5.46s (20.9% of wall; app-observed: includes network+driver+server)
commit time: 9.06s   total rows: 20,000
recording added ~1.81s (~6.9% of wall)
====================================================================================================
QUERY GROUP                                         CALLS     TOTAL     MEDIAN  CALL SITE
----------------------------------------------------------------------------------------------------
INSERT INTO events (name, value) VALUES (?, ?)     20,000     5.46s     0.24ms  demo/naive_job.py:65 in main
SELECT count(*) AS count_1 FROM events                  1     0.00s     0.16ms  demo/naive_job.py:71 in main
====================================================================================================
FINDINGS
----------------------------------------------------------------------------------------------------
1. [R1+R2] commit-per-row loop
   20,000 calls x 0.24ms median ~= 5.5s = 21% of 26.1s wall, at demo/naive_job.py:65. Batch it.
   20,001 commits for 20,000 rows (1.0 rows/commit), 9.1s in commit = 35% of wall. Batch to 1,000+ rows/txn.
   ~= 14.5s attributable
====================================================================================================
```

## Why it's different

| | Sampling profilers | APM / tracing | **wherewent** |
|---|:---:|:---:|:---:|
| Zero code changes | ✅ | ❌ | ✅ |
| Groups queries by shape | ❌ | ⚠️ | ✅ |
| Blames *your* call site | ⚠️ | ⚠️ | ✅ |
| Tells you the fix | ❌ | ❌ | ✅ |
| Runs anywhere, no server | ✅ | ❌ | ✅ |
| Works on a Ctrl-C'd partial run | ❌ | ⚠️ | ✅ |

## Install

```bash
pip install wherewent
```

That's it — the recorder is **pure standard library**. You only need SQLAlchemy
because *your job* already uses it.

## Use it

Wrap any command. Your script runs **completely unmodified** — no imports, no decorators,
no config:

```bash
wherewent run python your_job.py --some arg
wherewent run python -m your_package
wherewent run --save run.json python your_job.py   # also dump machine-readable JSON
```

- The report prints to **stderr** at exit; your job's own stdout/stderr pass through untouched.
- **Ctrl-C still produces a report.** Sampling the first 5 minutes of a 14-hour job is the
  main use case — partial data is the point.
- **Peek without stopping.** Send `SIGUSR1` (`kill -USR1 <pid>`) for a partial snapshot mid-run,
  or run with `WHEREWENT_INTERVAL=30` to print one every 30s. The job keeps going.
- **Works on async SQLAlchemy.** Queries run inside a greenlet with no user frames on the
  stack, so naive stack-walking blames nothing; wherewent attributes them to your real call
  site anyway (`AsyncSession` / `AsyncConnection`).
- **It can never crash or corrupt your job.** Every hook body is wrapped so the recorder
  fails silent rather than taking your run down with it.
- **It never records your data.** Only query *shapes* and *counts* are kept — literal
  values and bind parameters are stripped before anything is stored.

### Try the built-in demo

```bash
git clone https://github.com/habibafaisal/wherewent && cd wherewent
pip install -e ".[dev]"
wherewent run python demo/naive_job.py     # watch the R1+R2 finding fire
python demo/benchmark.py                    # naive vs fixed, with the overhead gate
```

## How it works

1. **Injects itself** into the target process via a `PYTHONPATH` sitecustomize shim — no
   changes to your code, no wrapper imports.
2. **Listens at the class level** — `event.listen(sqlalchemy.engine.Engine, ...)` — so
   *every* engine your app creates is captured automatically, config-free.
3. **Normalizes each statement** into a query *group*: literals, bind params, `IN`-lists
   and multi-row `VALUES` collapse, so a million distinct inserts become one honest row.
4. **Resolves the call site** by walking the stack past library frames to the first line
   of *your* code — cheaply: cached by filename, full stacks only for the first 5 samples
   per group, so the hot path stays cheap enough to hit its overhead budget.
5. **Fires deterministic findings** from three rules, each showing its arithmetic.

### The findings engine

| Rule | Fires when | Tells you |
|---|---|---|
| **R1 — chatty group** | > 1,000 calls, > 10% of wall, median < 5ms | A fast query is called too many times — batch it (`executemany` / `IN`-list / `JOIN`). |
| **R2 — commit-per-row** | > 100 commits, < 10 rows/commit, > 5% of wall in commit | You're committing per row — batch to 1,000+ rows per transaction. |
| **R3 — DB-wait bound** | in-DB time > 60% of wall, CPU busy < 30% | The job is round-trip bound, not compute bound. |
| **R4 — co-occurring pattern** | ≥ 2 query groups share a call site, > 1,000 combined calls, > 10% of wall | Several queries fire together every iteration — collapse them into one round-trip. Reports an estimated *queries-per-iteration*. |

Findings that share a root cause **merge** (e.g. `R1+R2`), everything under 5% of wall is
suppressed, and at most the top 3 are shown — ranked by seconds attributable. **R4** catches
the case a per-group threshold can't: an N+1 pattern spread across a SELECT + UPDATE + INSERT
that individually look innocent but fire as one unit each loop.

**Every number is honest.** Query times are labelled *app-observed* (they include network,
driver, and server time — not just Postgres). Anything that can't be measured prints `—`,
never a guess. wherewent even times *its own hooks* and reports the overhead it added.

## Roadmap — help wanted 🙌

wherewent is built to grow **beyond SQLAlchemy**. Seven of its eight modules —
normalization, call-site resolution, the stats model, the rules engine, the report, the
CLI, and the injection shim — are already **framework-agnostic**. They operate on a plain
`RunSnapshot` of query events. Only `recorder.py`, which binds SQLAlchemy's event system,
is framework-specific.

**That means a new backend is a well-contained contribution:** capture query
start/end/rowcount/txn events from another driver, feed the same `RunSnapshot`, and the
entire findings-and-report pipeline works for free. Good first backends:

- [x] **Async SQLAlchemy** — call-site attribution through the greenlet boundary *(v0.2.0)*
- [ ] **Raw `psycopg` / `psycopg2`** — cursor subclass or connection factory hook
- [ ] **Raw `asyncpg`** (outside SQLAlchemy) — the async execution path
- [ ] **Django ORM** — via `connection.execute_wrapper`
- [ ] **Generic DB-API 2.0** — a monkeypatch-free `Cursor` proxy
- [ ] New findings rules (N+1 `SELECT` detection, lock-wait, seq-scan heuristics)

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the backend contract and the **< 15% overhead
gate** that every capture path must pass.

## Limitations (today)

- SQLAlchemy **2.x** (sync **and** async ORM/Core; 1.4 may work). Raw `asyncpg` *outside*
  SQLAlchemy is not attributed yet.
- Single process — no multiprocessing fan-out.
- Query times are app-observed (network + driver + server), by design.
- Commit timing is obtained by wrapping the dialect's commit; if that wrap fails it prints `—`.
- Per-iteration ratios are **estimates** (labelled `≈`) inferred from co-occurring query
  counts — shown only when the signal is strong, never guessed.

These are the honest edges of a validation prototype, not permanent walls — see the roadmap.

## Contributing

Contributions are very welcome — new backends, new rules, docs, bug reports. Start with
[`CONTRIBUTING.md`](CONTRIBUTING.md), open an issue to discuss anything substantial, and
run `pytest && python demo/benchmark.py` before you push.

## License

[MIT](LICENSE) © 2026 Habiba Faisal
