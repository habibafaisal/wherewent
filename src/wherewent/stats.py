"""Shared dataclasses — the load-bearing contract that rules/report/tests depend on.

Field names and types here are frozen: do not rename or reorder without updating
every consumer.
"""

from dataclasses import dataclass, field


@dataclass
class GroupSnapshot:
    key: str                     # 12-hex-char sha1 prefix of normalized_sql
    normalized_sql: str
    calls: int
    total_time: float            # seconds, app-observed (includes driver+network+server)
    # v0.3 hardening (D1): a bounded SAMPLE median (reservoir cap 5000), not an
    # exact one, once a group exceeds the cap. `calls` and `total_time` stay
    # EXACT. 0.0 if no samples.
    median: float                # seconds; sample median; 0.0 if no samples
    rows: int                    # summed cursor.rowcount where >= 0, else 0 contribution
    executemany_calls: int
    call_site: "tuple[str, int, str] | None"  # (file, line, function); None if unresolved


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
    # v0.3 hardening (D4): per-unit query-slope windows. APPENDED after the
    # existing fields so positional consumers (rules/report/tests) are unaffected.
    first_window_mean_queries: "float | None"    # None if no units yet
    last_window_mean_queries: "float | None"     # None if no units yet


@dataclass
class RunSnapshot:
    wall_time: float             # seconds
    cpu_time: "float | None"     # process user+sys CPU seconds (os.times); None if unmeasurable
    total_queries: int
    total_commits: int
    total_rollbacks: int
    commit_time: "float | None"  # seconds inside DBAPI commit; None if not measurable
    total_rows: int
    db_time: float               # sum of all cursor-execute durations
    overhead_time: float         # recorder's own measured hook time, seconds
    sqlalchemy_active: bool      # were hooks installed and did SQLAlchemy get used
    groups: "list[GroupSnapshot]" = field(default_factory=list)
    unit_stats: "UnitStats | None" = None
    # v0.3 hardening (D2): DBAPI rollback time, split out of commit_time. None
    # when not measurable. Includes pool-reset rollbacks (report.py labels it).
    rollback_time: "float | None" = None


@dataclass
class Finding:
    rule: str        # "R1" | "R2" | "R3" | "R1+R2" | "R1+R2+R3" | "R1+R3" | "R2+R3"
    title: str       # one line
    detail: str      # multi-line: MUST show the arithmetic
    seconds: float   # estimated seconds attributable
