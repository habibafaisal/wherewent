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
    median: float                # seconds; 0.0 if no samples
    rows: int                    # summed cursor.rowcount where >= 0, else 0 contribution
    executemany_calls: int
    call_site: "tuple[str, int, str] | None"  # (file, line, function); None if unresolved


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


@dataclass
class Finding:
    rule: str        # "R1" | "R2" | "R3" | "R1+R2" | "R1+R2+R3" | "R1+R3" | "R2+R3"
    title: str       # one line
    detail: str      # multi-line: MUST show the arithmetic
    seconds: float   # estimated seconds attributable
