# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/habibafaisal/wherewent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/habibafaisal/wherewent/releases/tag/v0.1.0
