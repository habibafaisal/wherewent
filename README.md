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

## Name your unit of work

"81,749 queries" is hard to judge. **"135 queries per receivable"** tells an engineer
instantly that the architecture is chatty. Name the unit your job processes and wherewent
reports the economics of *one* — median duration, queries/commits/rows per unit, and how
the cost trends as the run progresses:

```bash
# Zero-config: name a function; every top-level call is one unit
wherewent run --unit-function myapp.jobs:process_receivable python run.py
```

```python
# Or mark the unit in code (same machinery, same report)
import wherewent
for receivable in book:
    with wherewent.unit("receivable"):
        process(receivable)
```

```
UNIT: myapp.jobs:process_receivable   (1,203 units)
----------------------------------------------------------------------------------------------------
  median duration    341 ms         queries/unit    135 (median)
  commits/unit       1.0            rows/unit       46.0
  GROWTH
    units 1–100          220 ms/unit
    units (last 100)     379 ms/unit
    queries 1–100        98 queries/unit
    queries (last 100)   171 queries/unit
    trend                +72% slower over the run
    query trend          +74% more queries/unit over the run     ← R6 fires
```

R6 fires on **either** slope. That matters for a compute-bound job: if the clock stays flat but
queries/unit climbs, the duration trend reads `flat` and only the query trend exposes the problem —
so wherewent reports both and says plainly that the pattern is a *scalability* risk rather than the
current wall-clock bottleneck.

The growth trend is why a *sampled* run is honest: it shows cost-per-unit **rising**, so you
know the full run will be worse than a linear extrapolation — the thing a totals-only profiler
can never tell you. Per-unit counts are exact even under concurrent async units; nothing but
shapes and counts is ever recorded.

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
| **R4 — co-occurring pattern** | ≥ 2 query groups fire from the **same function** AND the pattern **scales** — many queries/iteration across many iterations, *or* > 10% of wall once one-time setup is excluded | Several queries fire together every iteration (SELECT + UPDATE + INSERT) — collapse them into one round-trip. Clusters by *function*, not by line, so a helper that issues its statements on three different lines is still seen as **one** operation. One-shot (`calls == 1`) statements are excluded — they're fixed cost, and R5's job. Reports estimated *queries-per-iteration*, and flags patterns that scale even when a bounded run's clock hides them. |
| **R5 — one-shot heavyweight** | a single `calls==1` statement > 15% of wall **or > 10s absolute** | One statement is a huge fixed cost. R1/R3/R4 all look for chattiness and miss it — R5 catches the single most fixable line. The absolute floor matters: 20s is worth cutting whether it's 24% of a sampled run or 1% of the full one. |
| **R6 — rising per-unit cost** | per-unit **time** *or* **queries/unit** climbs ≥ 1.5× from the first 100 units to the last 100 (needs `--unit-function`/`wherewent.unit()`) | Cost per item grows as the run progresses — accumulating state, unbatched history reads, or a list that grows each loop. Reports the slope (queries/unit early vs late), so a compute-bound job whose *query* cost is growing still gets caught. |

Findings that share a root cause **merge** (e.g. `R1+R2`), everything under 5% of wall is
suppressed, and at most the top 3 are shown — ranked by seconds attributable. **R4** catches
the case a per-group threshold can't: an N+1 pattern spread across a SELECT + UPDATE + INSERT
that individually look innocent but fire as one unit each loop — and, since v0.3, it fires on
patterns that **scale** even when one-time setup costs make them look small on a short sample run.

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
- [x] **Work-unit-aware profiling** — per-unit economics + growth trend *(v0.3.0)*
- [ ] **Execution-pattern findings** *(the next big one — help wanted)* — today wherewent
  clusters the queries that fire together each iteration (R4). Next: **reconstruct the ordered,
  possibly nested workflow** behind them and name it, e.g.
  ```
  For each receivable:
    For each audit event ×23:
      SELECT chain_state → SELECT payload → INSERT payload → INSERT audit_event → UPDATE chain_state
  Finding: serialized audit-append loop — 23 repetitions/receivable, ≈115 statements/receivable,
           58% of DB activity, at process_receivable → emit_firing → append_event.
  ```
  This is a real step past ordinary N+1 detection (Sentry/Scout find repeated single-shape
  queries; this would find multi-operation workflows spanning several SQL shapes and functions):
  read→modify→write loops, serialize→insert→commit per item, whole-state snapshots after every
  mutation, growing-history scans, and CPU rising with item position. Needs an ordered per-unit
  event log + repeated-subsequence mining, kept under the overhead gate.
- [ ] **Raw `psycopg` / `psycopg2`** — cursor subclass or connection factory hook
- [ ] **Raw `asyncpg`** (outside SQLAlchemy) — the async execution path
- [ ] **Django ORM** — via `connection.execute_wrapper`
- [ ] **Generic DB-API 2.0** — a monkeypatch-free `Cursor` proxy
- [ ] More findings rules (lock-wait, seq-scan heuristics)

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
- Per-unit **counts** (`--unit-function` / `wherewent.unit()`) are exact even under concurrent
  async units; per-unit **duration** is wall time and may overlap when units run concurrently —
  the common sequential-loop case is exact.
- **R6's attributed seconds are a deterministic lower-bound estimate**, not a measurement. The
  excess queries per unit are priced at the run's *mean* per-query DB time, so if the extra
  queries are cheaper than average the true cost is higher (and vice versa). It is computed from
  exact integer query counts rather than the clock, so it is reproducible run to run — but R6's
  claim is the *slope*, not the seconds.
- **ORM flush attribution.** Queries emitted by a `session.flush()`/`commit()` all resolve to that
  one call site, so R4 can group unrelated writes under a single "workflow". When a cluster's
  writes share one source line, wherewent labels it as possibly a single flush rather than
  claiming you can collapse it — it will not tell you to batch something already batched.
- Per-group **median** is a bounded *sample* median (reservoir of 5,000 executions per group) so
  memory stays flat on million-query runs. `calls` and `total_time` remain exact.
- **Commit vs rollback time are reported separately.** SQLAlchemy's pool issues a rollback on
  every connection check-in, so rollback time is labelled *(incl. pool resets)* and is never
  folded into commit time.
- Findings describe **where the time goes and how it scales** — on a CPU-bound run they say so
  explicitly, rather than implying that fixing the SQL will speed up this run.

These are the honest edges of a validation prototype, not permanent walls — see the roadmap.

## Contributing

Contributions are very welcome — new backends, new rules, docs, bug reports. Start with
[`CONTRIBUTING.md`](CONTRIBUTING.md), open an issue to discuss anything substantial, and
run `pytest && python demo/benchmark.py` before you push.

## License

[MIT](LICENSE) © 2026 Habiba Faisal
