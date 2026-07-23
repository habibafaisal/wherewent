# wherewent v0.3.0 build contract — work-unit-aware profiling (`--unit-function`)

This is the pinned contract for three parallel agents. Obey it EXACTLY. File
ownership is disjoint (below): only your files, nothing else. The v0.1/v0.2
invariants still hold: **never crash the host job** (every new hook body in a
try/except), **never record parameter values**, **sync jobs that do NOT pass
`--unit-function` pay ZERO extra cost** (all new machinery is behind a flag).

## The feature

Let a user name the "unit of work" their batch job processes, so wherewent can
report the economics of ONE unit ("135 SQL calls per invoice", "341 ms/unit",
"units 1–100: 220 ms/unit → last 100: 379 ms/unit, +72% slower") instead of only
program-wide totals. TWO entry points, both feeding the SAME accumulator:

1. Zero-config CLI (preferred): `wherewent run --unit-function myapp.jobs:process_receivable python run.py`
2. In-code context manager: `with wherewent.unit("receivable"):` inside the loop body.

## v0.3 SCOPE (feedback-driven — three items, all in this release)

Real production feedback on v0.2 exposed that R4 misses the very pattern it exists
to catch, because it gates on % of THIS run's wall. On a bounded run, one-time
fixed costs (a 20.7s one-shot preload `calls==1`, ~8s of `calls==1` gate scans)
dominate the clock and suppress the per-iteration cluster (~5.2s = 6% of 84s) —
even though that cluster scales linearly and dominates at full volume. So v0.3 is:

- **FIX R4** — add an absolute scale trigger AND exclude one-shot (`calls==1`) time
  from the wall baseline, so a scaling N+1 fires regardless of this run's clock.
- **ADD R5 "one-shot heavyweight"** — a single `calls==1` statement over 15% of
  wall (invisible to R1/R3/R4, which all assume chattiness). Catches the 20.66s
  preload — the single most fixable thing on that run.
- **ADD unit-aware profiling** (`--unit-function` + `wherewent.unit()`), with a
  per-unit report and **R6 "rising per-unit cost"** (growth trend).

Exact thresholds/formulas for R4-fix, R5, R6 are in the rules.py section below.

## Mechanism (KEYSTONE — read carefully)

`--unit-function SPEC` is passed to the child via env var
`WHEREWENT_UNIT_FUNCTION`. The recorder, at install(), reads it and wraps the
named callable so **each top-level call is one unit**. Constraints:

- **NO global `sys.setprofile`/`settrace` on the steady-state hot path.** Wrap by
  monkeypatching the target callable, via a post-import hook. This keeps per-query
  overhead unchanged.
- **SPEC forms:** `pkg.mod:func` or `pkg.mod:Class.method` (preferred, unambiguous).
  Also accept a bare `func` (best-effort). Parse on `:` → (module, attr-path).
- **Patch timing:** if the SPEC module is already in `sys.modules`, patch the
  attribute immediately. Otherwise install a `sys.meta_path` finder that patches
  the attribute right after that module is executed. For a bare name (no module),
  install a post-import hook that, as each *user* (non-library) module finishes
  importing, patches the first top-level callable whose `__name__` matches; write
  one line to stderr naming what was wrapped (honesty: the user must know).
- Recommended proven import-hook shape (you may adapt):
  ```python
  import importlib.abc, importlib.util, sys
  class _PostImportPatcher(importlib.abc.MetaPathFinder, importlib.abc.Loader):
      def __init__(self, target_module, patch):
          self.target_module = target_module; self.patch = patch; self._busy = False
      def find_spec(self, name, path, target=None):
          if name != self.target_module or self._busy:
              return None
          self._busy = True
          try:
              spec = importlib.util.find_spec(name)   # real spec, other finders
          finally:
              self._busy = False
          if spec is None or spec.loader is None:
              return None
          spec.loader = _WrapLoader(spec.loader, self.patch)   # delegate + patch
          return spec
  class _WrapLoader(importlib.abc.Loader):
      def __init__(self, inner, patch): self.inner = inner; self.patch = patch
      def create_module(self, spec): return self.inner.create_module(spec)
      def exec_module(self, module):
          self.inner.exec_module(module)
          try: self.patch(module)
          except Exception: pass
  ```
  Insert the finder at `sys.meta_path.insert(0, ...)`. All of this is guarded so
  any failure leaves the job running exactly as without wherewent (the shim's
  outer try/except is a backstop, but guard here too).

- **The wrapper** (replaces the target callable, `functools.wraps`, idempotent via
  a `_wherewent_unit_wrapped` marker):
  - Handle BOTH a plain function and a coroutine function. If calling `orig`
    returns a coroutine/awaitable, return an async wrapper that does the exit
    accounting after `await`; else do it inline. Detect via
    `inspect.iscoroutinefunction(orig)` AND a runtime `inspect.isawaitable(result)`
    fallback (decorated/partial cases).
  - **Recursion guard:** maintain a depth counter (a `contextvars.ContextVar` int).
    Only the OUTERMOST call is a unit (increment on entry; treat as a unit only
    when depth goes 0→1; record on 1→0). Nested self-calls are NOT new units.
  - On entry (outermost): create a mutable unit record dict
    `{"queries":0,"commits":0,"rollbacks":0,"rows":0,"t0":perf_counter()}`, set it
    as the current-unit contextvar, and remember the unit's ordinal index.
  - On exit (outermost, in `finally` so exceptions still record): compute
    `duration = perf_counter() - t0`, then hand the record to the recorder's unit
    accumulator (below). NEVER change the wrapped call's return value; on
    exception, record the unit then re-raise (do not swallow the job's exception).

- **Per-unit query attribution (concurrency-correct):** add a
  `contextvars.ContextVar` holding the current unit record (default None). In the
  recorder's `_after`, `_on_commit`, `_on_rollback` hooks, add — ONLY when unit
  mode is enabled — a cheap read: `u = self._current_unit.get(); if u is not None:
  u["queries"] += 1` (and rows/commits/rollbacks respectively). This makes
  per-unit COUNTS correct even if async units overlap. Guard the whole addition
  with `if self._unit_enabled:` so a run WITHOUT `--unit-function` executes the
  identical hot path as v0.2 (no contextvar touch, zero cost).
  - Per-unit DURATION is the wrapper's own wall (`perf_counter` delta). Under
    concurrent (overlapping) async units these wall spans overlap — that is
    inherent; document it as a limitation (see README task, orchestrator-owned).
    The common batch case (sequential loop, sync or awaited in order) is exact.

## Bounded-memory unit accumulator (in recorder)

Do NOT store every unit (jobs may process millions). Keep:
- Exact running aggregates: `count`, `sum_duration`, `sum_queries`, `sum_commits`,
  `sum_rollbacks`, `sum_rows`.
- A bounded reservoir sample (cap 5000) of `duration` and of `queries` for the
  MEDIANS (use `statistics.median` on the sample; under the cap it is exact —
  keep tests below the cap so they are deterministic).
- Two ordinal windows for the growth trend, both bounded:
  - FIRST window: accumulate `(duration, queries)` for the first `W` units,
    `W = 100`. Keep count + sum_duration for that window only.
  - LAST window: a `collections.deque(maxlen=100)` of recent `duration`s; its mean
    is the "last 100 units" figure.

## stats.py — OWNED BY AGENT A. Add (do not remove/rename existing fields):

```python
@dataclass
class UnitStats:
    name: str                    # the SPEC string as given, e.g. "myapp.jobs:process_receivable"
    wrapped: bool                # True once the target was actually patched (else nothing ran)
    count: int                   # number of top-level unit executions recorded
    median_duration: float       # seconds
    mean_duration: float         # seconds
    median_queries: float
    mean_queries: float
    mean_commits: float
    mean_rollbacks: float
    mean_rows: float
    first_window_n: int          # units in the FIRST window (<= 100)
    first_window_mean_duration: "float | None"   # None if no units yet
    last_window_n: int           # units in the LAST window (<= 100)
    last_window_mean_duration: "float | None"    # None if no units yet
```
Add to `RunSnapshot`: `unit_stats: "UnitStats | None" = None` (LAST field, keep
existing order). `snapshot()` builds a `UnitStats` when unit mode is enabled
(even if `count == 0`, so an unwrapped/never-called target reports `wrapped`
honestly), else leaves `unit_stats=None`.

## report.py — OWNED BY AGENT B. Insert a UNIT section

After the query-group table and BEFORE the FINDINGS block, iff
`run.unit_stats is not None`. Layout (100-col, "—" for any None; ms with 0
decimals, counts with 1 decimal). Example when populated:
```
====================================================================================================
UNIT: myapp.jobs:process_receivable   (1,203 units)
----------------------------------------------------------------------------------------------------
  median duration     341 ms         queries/unit    135 (median)
  commits/unit        1.0            rows/unit        46.0
  GROWTH
    units 1–100          220 ms/unit
    units (last 100)     379 ms/unit
    trend                +72% slower over the run
```
Rules for the section:
- If `unit_stats.wrapped` is False OR `count == 0`: print a single honest line
  instead of the table, e.g. `UNIT: <name> — target not called (0 units recorded)`
  (when wrapped but never called) or `... — target function was never imported/wrapped`
  (when not wrapped). Do not invent numbers.
- GROWTH block only when BOTH window means are non-None AND `count >= 20`; else
  print `GROWTH   — (need ≥20 units)`. Compute trend from the two window means:
  `delta = (last-first)/first`; render `+NN% slower` if delta > 0.10, `-NN% faster`
  if delta < -0.10, else `flat`.
- Durations shown in ms (`x*1000`), 0 decimals. queries/commits/rows to 1 decimal.
  median_queries printed as an integer if whole, else 1 decimal.

## rules.py — OWNED BY AGENT B. FIX R4, ADD R5 (one-shot), ADD R6 (rising cost)

Keep R1, R2, R3 and their merge logic byte-for-byte unchanged. Compute once near
the top of `evaluate`, after `wall`:
```python
fixed_cost_time = sum(g.total_time for g in run.groups if g.calls == 1)
variable_wall = max(wall - fixed_cost_time, 1e-9)   # wall minus one-shot setup
```

### FIX R4 — fire on scale, not just this run's wall%
Inside the existing cluster loop, KEEP the current requirements (≥2 groups sharing
a non-None call_site; `combined_calls > 1000`). REPLACE the single wall% gate with:
```python
iterations = max(g.calls for g in gs)          # gs = groups in this cluster
per_iter   = combined_calls / iterations if iterations else 0
qualifies = combined_calls > 1000 and (
    combined_time > 0.10 * wall
    or combined_time > 0.10 * variable_wall          # (b) exclude one-shot setup
    or (iterations >= 200 and per_iter >= 3)         # (a) absolute scale trigger
)
```
When it fires, the detail MUST show BOTH ratios and the scaling note, e.g.:
```
combined: 13,072 calls, 5.2s = 6% of 84.1s wall (16% once 28.4s of one-time setup is excluded).
~= 3.0 queries/iteration across ~430 iterations from app/audit.py:193 — this scales
linearly with units, so it becomes the dominant cost at full volume even though it
did not win this bounded run's clock.
```
Compute the "excluded" ratio as `combined_time / variable_wall`. Keep the existing
per-iteration estimate line and the existing standalone-R1 double-count guard.
`seconds = combined_time` (unchanged). The negative guards still hold: single-group
clusters and call_site=None never qualify; `combined_calls <= 1000` never fires.
(NOTE for Agent C: the old "does NOT fire below 10% wall" test must be updated —
firing below 10% wall on a scaling cluster is now correct behavior.)

### ADD R5 — one-shot heavyweight statement
After R4. Independent, additive. Among groups with `g.calls == 1`, take the one with
the largest `total_time`; fire if that `total_time > 0.15 * wall`:
- `rule="R5"`, `title="One-shot heavyweight statement"`.
- `detail` shows arithmetic, e.g.:
  ```
  1 statement at app/preload.py:44 took 20.7s = 24% of 84.1s wall.
  It runs once regardless of input size, so R1/R3/R4 (which look for chattiness)
  miss it — but it is the single biggest fixed cost. Cache, narrow, or stream it.
  ```
  (Include the call_site if resolved, else the normalized SQL.)
- `seconds = group.total_time`. Do NOT merge/suppress vs R1–R4; ranks in top-3 by seconds.

### ADD R6 — rising per-unit cost (unit-aware)
Only when `run.unit_stats` is not None. Fires when `us.count >= 20` AND both window
means present AND `us.first_window_mean_duration > 0` AND
`us.last_window_mean_duration >= 1.5 * us.first_window_mean_duration`.
- `rule="R6"`, `title="Rising per-unit cost"`.
- `detail` shows both window numbers, e.g.:
  `first 100 units averaged 220 ms; last 100 averaged 379 ms (+72%). Cost per unit
  is climbing — likely growing per-item work (accumulating state, unbatched history
  reads, or a list that grows each iteration).`
- `seconds = max(0.0, (us.mean_duration - us.first_window_mean_duration) * us.count)`.
- Independent of R1–R5; participates in the `findings[:3]` top-3 ranking by seconds.

All new rules stay inside the existing `try/except` discipline of `evaluate`; a
malformed field must never raise. The final `return findings[:3]` is unchanged.

## JSON (`--save`) — OWNED BY AGENT A (recorder `_write_json`)

Add `"unit_stats"` to the payload: `null` when `run.unit_stats is None`, else an
object with every `UnitStats` field (windows as numbers or null). Everything else
in the payload unchanged.

## cli.py — OWNED BY AGENT A

Add `--unit-function SPEC` parsing to the `run` arg loop (same style as `--save`;
it takes one value). Thread it into `_child_env` as
`env["WHEREWENT_UNIT_FUNCTION"] = spec or ""`. Update the USAGE string with an
example line. `install_from_env()` (recorder) reads `WHEREWENT_UNIT_FUNCTION` and
enables unit mode when non-empty.

## Public `wherewent.unit()` context manager — OWNED BY AGENT A

Provide a context manager `unit(name="unit")` that stamps the SAME per-unit record
onto the SAME current-unit contextvar the `--unit-function` wrapper uses, so units
opened in user code are recorded identically:
```python
with wherewent.unit("receivable"):
    process(receivable)
```
- Define it where the unit machinery lives (recorder.py or a small helper module
  YOU own). It must: on `__enter__` push a unit record (respecting the same
  recursion-depth guard so a `unit()` inside a `--unit-function` unit does not
  double-count — outermost wins), on `__exit__` record duration+counts even if the
  body raised (then propagate the exception), and be a **no-op that still runs the
  body** when unit mode/recorder is not installed (so code using it is safe in
  production without wherewent). Enabling unit mode via the context manager (first
  use) is fine even if `--unit-function` was not passed; `snapshot()` then emits
  `unit_stats` with `name` = the first/most-common unit name (or "unit").
- Also support `async with`? Not required for v0.3 — a sync `with` inside an async
  loop body is enough; keep scope tight. Document sync-only if you don't add it.
- **Orchestrator (not Agent A)** adds the `from .<module> import unit` re-export to
  `__init__.py` so `wherewent.unit` resolves. Agent A: just make sure the callable
  exists and is importable from the module you put it in; state the import path in
  your report so the orchestrator wires it.

## FILE OWNERSHIP (disjoint — touch ONLY yours)

- **Agent A (capture path):** `src/wherewent/stats.py`, `src/wherewent/recorder.py`,
  `src/wherewent/cli.py`, `src/wherewent/callsite.py` (only if you need a shared
  contextvar helper; otherwise keep new contextvars local to recorder.py).
- **Agent B (findings + report):** `src/wherewent/rules.py`, `src/wherewent/report.py`.
  You import `UnitStats` from `.stats` — rely on the field list above; do not edit stats.py.
- **Agent C (tests + demo):** everything under `tests/` and `demo/` (incl.
  `demo/benchmark.py`). Do NOT touch `src/`.
- **Orchestrator (NOT you):** `src/wherewent/__init__.py`, `pyproject.toml`,
  `CHANGELOG.md`, `README.md`. Do not touch these.

## Agent C specifics

- **Update `tests/test_rules_cluster.py` for the new R4 semantics.** The old test
  asserting R4 does NOT fire when a cluster is below 10% of wall is now WRONG — a
  scaling cluster (`iterations >= 200 and per_iter >= 3`, or > 10% of variable_wall)
  SHOULD fire even below 10% of raw wall. Rewrite that case to build a fixture
  mimicking the feedback scenario: a big one-shot group (`calls==1`, huge
  total_time, e.g. a 20s preload) PLUS a co-located multi-group cluster (2–3 groups,
  ~430 iterations, ~3 queries/iter, modest total_time that is <10% of raw wall but
  >10% of variable_wall) → assert R4 now fires AND its detail contains both the raw
  wall% and the setup-excluded% and the "scales" language. Keep the other R4
  negative guards (single-group→R1, call_site=None, combined_calls<=1000) passing.
- New `tests/test_one_shot.py` (R5): a fixture with one `calls==1` group whose
  total_time > 15% of wall → assert R5 fires, names the site/SQL, seconds == that
  group's total_time; and a negative (no calls==1 group over the bar → no R5).
- New `tests/test_unit_profiling.py`:
  - sync target: define a module-level function, run a small loop under a fresh
    `Recorder()` with unit mode on (set `WHEREWENT_UNIT_FUNCTION` and call
    `install_from_env()`, OR call the recorder's unit-enable path directly — match
    however Agent A exposes it; coordinate via the public shape: env var +
    `install`/`snapshot`). Assert `unit_stats.count == loop_n`, `median_queries`
    equals the per-call query count, counts attributed correctly.
  - async target: an `async def` unit wrapped, driven by `asyncio.run` over a
    sequential loop; assert per-unit counts are exact.
  - growth: craft units whose later iterations issue more queries / take longer so
    `last_window_mean_duration >= 1.5 * first_window_mean_duration`; assert **R6**
    fires and its detail shows both window numbers.
  - context-manager form: drive `with wherewent.unit("receivable"):` over a small
    sequential loop (no `--unit-function`); assert `unit_stats.count` matches and
    per-unit counts are attributed. Also assert the context manager is a safe no-op
    (runs the body, no error) when the recorder is not installed.
  - negative: with NO unit function/context set, `run.unit_stats is None` and R6
    never fires; and a run with `count < 20` does not fire R6.
  - JSON round-trip: `--save` (or `_write_json`) emits a `unit_stats` object with the
    right fields; `null` when disabled.
  - Reuse the fixture style in existing `tests/test_rules.py` / `test_async_callsite.py`.
- `demo/unit_job.py`: a SQLAlchemy job modeled on the feedback scenario so it
  exercises R4-fix, R5 and R6 together:
  - a ONE-SHOT heavyweight at startup (`calls==1`, deliberately slow — e.g. a big
    single SELECT/aggregate over a seeded table) that is a large share of a SMALL
    run's wall → triggers **R5** and inflates the wall denominator (the R4 trap);
  - a per-unit function `process_one(...)` issuing several CO-LOCATED queries
    (SELECT + UPDATE + INSERT from one call site) each iteration → the R4 cluster
    that is <10% of raw wall but scales → triggers the fixed **R4**;
  - later iterations re-read a growing history so per-unit time climbs → **R6**.
  Env-configurable rows. Include a `process_one` importable as `demo.unit_job:process_one`.
- `demo/benchmark.py`: add an OPTIONAL unit section (like the async one) that runs
  `unit_job.py` under `wherewent run --unit-function demo.unit_job:process_one`
  with `--save`, and asserts: `unit_stats.count` matches the row count;
  `median_queries` equals the expected per-unit count; **R4 fires despite the
  cluster being <10% of raw wall** (proving the fix); **R5 names the one-shot
  heavyweight**; and R6 fires if the growth is strong enough. Skip cleanly (not
  fail) if unavailable. Do NOT weaken the existing sync self-measured overhead gate
  or the async assertions.

## Verification each agent runs before returning
`cd /Users/mac/Documents/PersonalProjects/wherewent && ./.venv/bin/python -m compileall src tests demo`
and the relevant `./.venv/bin/python -m pytest tests/ -q`. Report exact numbers,
any deviation from this contract, and paste your unified diffs.
