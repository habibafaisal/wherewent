# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-07-24

Driven by production feedback showing R4 missed the very N+1 pattern it exists to
catch — because it gated on percent of *this run's* wall clock, and on a bounded
run one-time fixed costs dominate the clock and suppress the per-iteration pattern
that actually scales.

### Fixed
- **R4 no longer gates purely on percent of wall.** A co-occurring cluster now also
  fires on an absolute scale trigger (many co-located queries per iteration across
  many iterations) and against a wall baseline that **excludes one-shot (`calls==1`)
  fixed costs** — so an N+1 that scales linearly is flagged even when a one-time
  20-second preload made it look like 6% of a short run. The finding now shows both
  "% of wall" and "% once one-time setup is excluded", plus a note that it dominates
  at full volume.

### Added
- **R5 — one-shot heavyweight.** A single statement (`calls==1`) that eats more than
  15% of wall is invisible to R1/R3/R4 (they all look for chattiness) yet is often
  the single most fixable thing. R5 surfaces it.
- **Work-unit-aware profiling.** Name the unit your job processes and get its
  economics — median duration, queries/commits/rows per unit, and a growth trend
  across the run — instead of only program-wide totals ("135 queries per receivable"
  beats "81,749 queries"). Two entry points, zero data recorded:
  - `wherewent run --unit-function myapp.jobs:process_receivable python run.py` (zero-config)
  - `with wherewent.unit("receivable"):` in your loop body
- **R6 — rising per-unit cost.** When per-unit time climbs across the run (later
  units markedly slower than early ones), R6 flags the growing-per-item-work smell.
- `demo/unit_job.py` plus unit / one-shot / R4-scale tests.

### Hardened (post-review)
A review pass against real workloads found nine defects; all are fixed in this release.

- **R4 clustered by source LINE, not by function** — the highest-impact bug. A helper that
  issued its INSERT/SELECT/UPDATE on three different lines of one function fragmented into
  three sub-threshold clusters, so R4 never fired on exactly the N+1 pattern it exists to
  catch. Clusters are now keyed by `(file, function)` and recombine. One-shot (`calls == 1`)
  statements are excluded from clustering — they are fixed cost, and R5's job.
- **R4 flush-attribution guard.** Coarsening the key can merge writes that a single
  `session.flush()`/`commit()` emitted. When a cluster's writes share one source line,
  wherewent labels it as possibly one flush and will not advise "collapse into one
  round-trip" for something already batched.
- **R5 gained an absolute floor** (`> 15% of wall` **or** `> 10s`). A 20s one-shot is worth
  cutting whether it is 24% of a sampled run or 1% of the full one; the previous
  percent-only gate went silent on exactly the large runs that matter. The final
  suppression filter gained a matching escape hatch so such findings are not re-dropped.
- **R6 also fires on the queries/unit slope**, not only duration, and reports it — so a
  compute-bound job whose per-unit query cost is growing is still caught.
- **CPU-bound honesty framing.** When a run is compute-bound, R4/R6 say the pattern is a
  scalability risk that dominates at full volume rather than implying it is the current
  wall-clock bottleneck.
- **Bounded memory.** Per-group durations were an unbounded list (one float per execution).
  They are now a 5,000-sample reservoir: memory stays flat on million-query runs, `calls`
  and `total_time` stay exact, and the median is honestly a *sample* median.
- **Commit vs rollback time are no longer conflated.** SQLAlchemy's pool issues a rollback
  on every connection check-in, which was inflating commit time; rollback time is now
  reported separately and labelled *(incl. pool resets)*.
- **Execution-context leak on error paths.** A failed statement never fires
  `after_cursor_execute`, leaking a timing entry per error; a `handle_error` listener now
  cleans up.
- **Silent internal failures.** `peek()`/`finalize()` wrapped rendering in a bare
  `except: pass`, so an internal error printed as empty output — indistinguishable from
  "nothing to report". They now emit a clearly-marked internal-error line and keep
  recording, still without ever raising into the host job.
- **`Recorder.disable()`** added to fully unwind class-level hooks, dialect wrappers and
  import hooks — for embedders and test isolation.
- **R4 under-counted queries-per-iteration, and missed the growing N+1 entirely.** The
  iteration count was estimated as the *largest* group's call count, which silently assumes
  every group fires at most once per iteration. When one group fires several times per
  iteration — precisely the growing-N+1 shape R4 exists to catch — that group inflates the
  denominator and deflates the ratio, so the estimator is least accurate exactly when the
  problem is worst. On CI the demo cluster read `1920 / 720 = 2.67` queries/iteration against
  a bar of 3 and disqualified itself; the truth is `1920 / 400 = 4.8` across 400 units. The
  estimate is now the **median** group call count, which resists both a group that fires many
  times per iteration and one that fires only conditionally. Thresholds are unchanged — this
  corrects the estimator, not the bar. A per-iteration display line is now suppressed when it
  would contradict the estimate the rule actually used.
- **R4 was deleted by the suppression filter on exactly the runs it exists for.** R4's whole
  premise is that a scaling N+1 must be reported even when it is a small share of a bounded
  run's wall clock — that is why it has a scale trigger alongside its percent-of-wall path.
  But the final trivia filter then re-imposed a `≥ 5% of wall` bar and dropped the finding, so
  the rule fired and was silently undone. Caught by CI on all four Python versions: the demo
  cluster measured `0.078s / 2.653s = 3.0%` there versus 6.5% on the developer's machine, so
  it vanished on the slower-relative-disk runner. Scale and trend findings (R4, R6) are now
  exempt from the percent-of-wall bar; their own firing gates remain the quality bar. Anyone
  profiling a short sample of a long job — the primary use case — would have hit this.
- **R6 fired nondeterministically on identical input.** Its attributed seconds were derived
  from `(mean_duration − first_window_mean_duration) × count`, but the first window pays
  one-time SQLAlchemy statement-compilation warmup, which is not part of the steady-state
  per-unit price. That inflated the baseline and biased the estimate low — enough that on
  ten runs of a byte-identical workload the value landed as close as 1.02× the suppression
  threshold, so R6 appeared in only some runs. It now prefers a **query-derived**
  attribution (excess queries per unit, priced at the run's mean per-query DB time), which
  uses exact integer counts and no clock at all: the query windows were identical across all
  ten runs while the duration windows spread 46%. R6 is also **exempt from the
  share-of-wall trivia filter** — it is a trend finding whose current-run magnitude is small
  by construction, which is precisely what it is telling you. Its 1.5× firing bar is
  unchanged and remains the thing that decides whether it is real.

### Notes
- Per-unit COUNTS are exact even under concurrent async units (attributed via a
  contextvar); per-unit DURATION is wall time and may overlap for concurrently-running
  units — the common sequential-loop case is exact.

## [0.2.0] - 2026-07-24

Driven by feedback from running v0.1.0 on a real async workload. The headline fix:
call-site attribution now works on async SQLAlchemy, where it previously collapsed to "—".

### Added
- **Async call-site attribution.** On the async path, queries execute inside a greenlet
  whose stack has no user frames, so the call site was always "—". The recorder now stamps
  the issuing call site at the `AsyncSession` / `AsyncConnection` entry boundary (execute,
  scalars, scalar, stream, get, commit, flush, refresh) and reads it back inside the cursor
  hook via a `contextvar`. Sync jobs are unaffected and pay no extra cost.
- **Mid-run snapshots.** Long jobs are no longer blind until exit: send `SIGUSR1` to the job
  for an on-demand PARTIAL SNAPSHOT, or set `WHEREWENT_INTERVAL=<seconds>` for periodic ones.
  The job keeps running; the final report still prints at exit.
- **Co-occurring pattern findings (R4).** An N+1 pattern spread across several query groups
  (e.g. a SELECT + UPDATE + INSERT firing together each iteration) previously tripped no
  single-group threshold and produced "no findings". R4 clusters groups by call site, then
  thresholds the cluster — and reports an estimated *queries-per-iteration* ratio when the
  signal is strong. R1/R2/R3 are unchanged; R4 is purely additive.
- `demo/async_naive_job.py` and async / cluster / peek tests.

### Notes
- Async attribution covers the SQLAlchemy async ORM/Core boundary; raw asyncpg outside
  SQLAlchemy is still out of scope (see the roadmap).

## [0.1.0] - 2026-07-24

Initial public release — a validation prototype of the core idea: surface the *calling
pattern* behind slow SQLAlchemy batch jobs, not just "time spent in the driver".

### Added
- `wherewent run python job.py [...]` CLI, with `-m module` and bare-script forms.
- Zero-config SQL flight recorder via class-level SQLAlchemy 2.x engine hooks — the target
  job runs unmodified.
- Query-group normalization (literals, bind params, `IN`-lists, multi-row `VALUES`).
- Cheap call-site resolution (filename-cached library detection; stacks sampled for the
  first 5 executions per group).
- Per-connection transaction tracking and dialect-level commit timing.
- Terminal report with a call-pattern table and a deterministic findings engine (rules
  R1 chatty-group, R2 commit-per-row, R3 DB-wait-bound, with merging and suppression).
- Self-measured recorder overhead, reported on every run.
- `--save PATH` JSON dump of raw run data.
- Report on `atexit` **and** on Ctrl-C / SIGTERM, so partial runs still report.
- `demo/` with naive + fixed jobs and a benchmark that asserts query counts, rule firing,
  and the < 15% overhead gate.

### Known limitations
- SQLAlchemy 2.x synchronous only; single process; no async, multiprocessing, or Django ORM.
- Query times are app-observed (network + driver + server).

[Unreleased]: https://github.com/habibafaisal/wherewent/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/habibafaisal/wherewent/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/habibafaisal/wherewent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/habibafaisal/wherewent/releases/tag/v0.1.0
