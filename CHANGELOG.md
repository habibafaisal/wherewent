# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/habibafaisal/wherewent/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/habibafaisal/wherewent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/habibafaisal/wherewent/releases/tag/v0.1.0
