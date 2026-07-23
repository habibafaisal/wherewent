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
