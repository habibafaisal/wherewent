# Contributing to wherewent

Thanks for helping out! wherewent is a small, focused tool and it wants to stay that way —
which makes it a great codebase to contribute to. This guide covers setup, the design
contract, and the one hard rule every change must respect.

## Getting set up

```bash
git clone https://github.com/habibafaisal/wherewent && cd wherewent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                      # unit tests — no database required
python demo/benchmark.py    # end-to-end: naive vs fixed, asserts the overhead gate
```

Python 3.10+ is required. The recorder itself uses **only the standard library** — please
keep it that way. SQLAlchemy is a dev/demo dependency, not a runtime one.

## The one hard rule: the < 15% overhead gate

wherewent runs on your hot path — once per query. If recording is expensive, nobody will
leave it on, and the tool is pointless. So:

> **Any capture path must add less than 15% to the wall time of a chatty (per-row) job.**

`python demo/benchmark.py` measures this and fails the build if it's exceeded. If your
change pushes overhead up, optimize *before* adding anything else — frame-walk caching and
sampling stacks above N calls per group are the levers that already exist.

## Architecture at a glance

Seven of the eight modules are framework-agnostic; only `recorder.py` knows about
SQLAlchemy.

| Module | Responsibility | Framework-specific? |
|---|---|---|
| `normalize.py` | Collapse a SQL string into a query-group shape | No |
| `callsite.py` | Walk the stack to the first user-code frame | No |
| `stats.py` | The `RunSnapshot` / `GroupSnapshot` / `Finding` data model | No |
| `rules.py` | Turn a `RunSnapshot` into ranked findings | No |
| `report.py` | Render a `RunSnapshot` + findings as text | No |
| `cli.py` | `wherewent run ...` and process injection | No |
| `_shim/` | `PYTHONPATH` sitecustomize that installs the recorder | No |
| `recorder.py` | Capture query events from SQLAlchemy | **Yes** |

## Adding a new framework backend

This is the highest-value contribution, and it's well-contained. A backend's whole job is
to observe query execution and populate a `RunSnapshot`. If you feed the same snapshot, the
normalization, rules, report, and CLI all work unchanged.

A backend must, per statement execution:

1. Capture a **monotonic start/end** (`time.perf_counter`) around the DB round-trip.
2. Record **rowcount** (when available) and whether it was an `executemany`.
3. Track **transaction** begin/commit/rollback so commit time and rows-per-commit are known.
4. Call `normalize.normalize_sql` + `normalize.group_key` to bucket the statement.
5. Call `callsite.resolve_call_site()` on each execution (cheap), and `capture_stack()`
   only for the first ~5 executions per group.
6. **Never** store parameter values — counts and shapes only.
7. Wrap **every** capture callback in `try/except` so the recorder can never crash the job.

Open an issue describing the backend before you start so we can agree on the hook mechanism
(e.g. Django's `connection.execute_wrapper`, a DB-API cursor proxy, the asyncpg path).

## Adding a findings rule

Rules live in `rules.py` and operate purely on a `RunSnapshot` — no I/O, fully
deterministic. A good rule:

- Shows its **arithmetic** in the finding detail (calls × median ≈ seconds, % of wall).
- Estimates **seconds attributable** so it can be ranked and suppressed (< 5% of wall is dropped).
- Has a **golden unit test** built from an in-memory `RunSnapshot` fixture — no DB needed.
  See `tests/test_rules.py` for the pattern.

## Testing & style

- `pytest` must stay green; add tests for anything you change.
- Match the existing style: plain, commented where a constraint isn't obvious, no new deps.
- Keep every reported number **honest** — if something can't be measured, emit `—`, never a guess.

## Reporting bugs

Open an issue with the command you ran, the report output (redact anything sensitive — though
wherewent already strips values), your Python and SQLAlchemy versions, and what you expected.

## License

By contributing, you agree your contributions are licensed under the [MIT License](LICENSE).
