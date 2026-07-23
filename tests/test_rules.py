"""Tests for wherewent.rules.evaluate() against in-memory RunSnapshot fixtures."""

import pytest

from wherewent.rules import evaluate
from wherewent.stats import GroupSnapshot, RunSnapshot


# --------------------------------------------------------------------------
# Fixture builders — small, overridable factories keep each case readable.
# --------------------------------------------------------------------------

def make_group(
    key="g1",
    normalized_sql="INSERT INTO t VALUES (?, ?)",
    calls=0,
    total_time=0.0,
    median=0.0,
    rows=0,
    executemany_calls=0,
    call_site=("job.py", 42, "loop_over_rows"),
):
    return GroupSnapshot(
        key=key,
        normalized_sql=normalized_sql,
        calls=calls,
        total_time=total_time,
        median=median,
        rows=rows,
        executemany_calls=executemany_calls,
        call_site=call_site,
    )


def make_run(
    wall_time=0.0,
    cpu_time=None,
    total_queries=0,
    total_commits=0,
    total_rollbacks=0,
    commit_time=None,
    total_rows=0,
    db_time=0.0,
    overhead_time=0.0,
    sqlalchemy_active=True,
    groups=None,
):
    return RunSnapshot(
        wall_time=wall_time,
        cpu_time=cpu_time,
        total_queries=total_queries,
        total_commits=total_commits,
        total_rollbacks=total_rollbacks,
        commit_time=commit_time,
        total_rows=total_rows,
        db_time=db_time,
        overhead_time=overhead_time,
        sqlalchemy_active=sqlalchemy_active,
        groups=groups if groups is not None else [],
    )


def _contains_number(text, n):
    """Match a count regardless of thousands-separator formatting."""
    return str(n) in text or f"{n:,}" in text


# --------------------------------------------------------------------------
# R1 — chatty group
# --------------------------------------------------------------------------

def test_r1_fires():
    group = make_group(calls=20000, median=0.0004, total_time=8.0)
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=8.0, groups=[group])

    findings = evaluate(run)

    assert len(findings) == 1
    assert findings[0].rule == "R1"
    assert findings[0].seconds == pytest.approx(8.0)


def test_r1_blocked_by_calls_threshold():
    group = make_group(calls=1000, median=0.0004, total_time=8.0)
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=8.0, groups=[group])

    assert evaluate(run) == []


def test_r1_blocked_by_total_time_threshold():
    group = make_group(calls=20000, median=0.0004, total_time=1.0)
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=1.0, groups=[group])

    assert evaluate(run) == []


def test_r1_blocked_by_median_threshold():
    group = make_group(calls=20000, median=0.005, total_time=8.0)
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=8.0, groups=[group])

    assert evaluate(run) == []


# --------------------------------------------------------------------------
# R2 — commit-per-row
# --------------------------------------------------------------------------

def test_r2_fires():
    run = make_run(
        wall_time=20.0,
        total_commits=200,
        total_rows=1000,
        commit_time=2.0,
        db_time=1.0,
    )

    findings = evaluate(run)

    assert len(findings) == 1
    assert findings[0].rule == "R2"
    assert findings[0].seconds == pytest.approx(2.0)


def test_r2_blocked_by_commits_threshold():
    run = make_run(
        wall_time=20.0,
        total_commits=100,
        total_rows=500,
        commit_time=2.0,
        db_time=1.0,
    )

    assert evaluate(run) == []


def test_r2_blocked_by_rows_per_commit_threshold():
    run = make_run(
        wall_time=20.0,
        total_commits=200,
        total_rows=2000,  # 10 rows/commit, not < 10
        commit_time=2.0,
        db_time=1.0,
    )

    assert evaluate(run) == []


def test_r2_blocked_by_commit_time_none_no_crash():
    run = make_run(
        wall_time=20.0,
        total_commits=200,
        total_rows=1000,
        commit_time=None,
        db_time=1.0,
    )

    assert evaluate(run) == []


# --------------------------------------------------------------------------
# R3 — DB-wait dominates
# --------------------------------------------------------------------------

def test_r3_fires():
    run = make_run(wall_time=20.0, cpu_time=3.0, db_time=15.0)

    findings = evaluate(run)

    assert len(findings) == 1
    assert findings[0].rule == "R3"
    assert findings[0].seconds == pytest.approx(15.0)


def test_r3_blocked_by_cpu_time_none_no_crash():
    run = make_run(wall_time=20.0, cpu_time=None, db_time=15.0)

    assert evaluate(run) == []


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------

def test_r1_r2_merge_into_single_finding_when_calls_near_commits():
    # calls=9000 vs total_commits=8000: within 25% of each other -> merge.
    group = make_group(calls=9000, median=0.0004, total_time=4.0)
    run = make_run(
        wall_time=20.0,
        cpu_time=2.0,
        total_commits=8000,
        total_rows=32000,  # 4 rows/commit, < 10
        commit_time=3.0,
        db_time=5.0,  # well under 0.60 * wall, R3 does not fire
        groups=[group],
    )

    findings = evaluate(run)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "R1+R2"
    assert "R1" in finding.rule and "R2" in finding.rule
    assert finding.seconds == pytest.approx(4.0 + 3.0)
    assert _contains_number(finding.detail, 9000)
    assert _contains_number(finding.detail, 8000)


def test_r1_r2_not_merged_when_calls_far_from_commits():
    # calls=10000 vs total_commits=6000: 40% apart -> no merge, two findings.
    group = make_group(calls=10000, median=0.0004, total_time=4.0)
    run = make_run(
        wall_time=20.0,
        cpu_time=2.0,
        total_commits=6000,
        total_rows=24000,
        commit_time=3.0,
        db_time=5.0,
        groups=[group],
    )

    findings = evaluate(run)

    rules = {f.rule for f in findings}
    assert len(findings) == 2
    assert rules == {"R1", "R2"}


def test_full_composite_r1_r2_r3():
    group = make_group(calls=9000, median=0.0004, total_time=6.0)
    run = make_run(
        wall_time=30.0,
        cpu_time=3.0,
        total_commits=8000,
        total_rows=32000,  # 4 rows/commit, < 10
        commit_time=4.0,
        db_time=20.0,  # > 0.60 * wall (18.0)
        groups=[group],
    )

    findings = evaluate(run)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule == "R1+R2+R3"
    # R3's db_time overlaps R1's query time (queries ARE the db wait), so it
    # is folded into the detail text only, not added on top of R1+R2 seconds
    # — summing it in too would double-count the same wall-clock seconds.
    assert finding.seconds == pytest.approx(6.0 + 4.0)
    assert _contains_number(finding.detail, 9000)
    assert _contains_number(finding.detail, 8000)
    # R3's arithmetic (db_time) must still show up in the detail text.
    assert f"{run.db_time:.1f}" in finding.detail


# --------------------------------------------------------------------------
# Cap and suppression
# --------------------------------------------------------------------------

def test_never_more_than_three_findings():
    # All three rules fire independently, and none of them qualifies to
    # merge with another (calls/commits far apart; neither R1 nor R2's
    # seconds reach 0.8 * db_time so R3 stays standalone too).
    group = make_group(calls=10000, median=0.0004, total_time=5.0)
    run = make_run(
        wall_time=30.0,
        cpu_time=2.0,
        total_commits=6000,
        total_rows=24000,
        commit_time=3.0,
        db_time=20.0,  # > 0.60 * 30 = 18.0
        groups=[group],
    )

    findings = evaluate(run)

    assert len(findings) <= 3
    assert len(findings) == 3
    assert {f.rule for f in findings} == {"R1", "R2", "R3"}


def test_all_zero_snapshot_yields_no_findings():
    run = make_run(
        wall_time=10.0,
        cpu_time=0.0,
        total_queries=0,
        total_commits=0,
        total_rollbacks=0,
        commit_time=None,
        total_rows=0,
        db_time=0.0,
        overhead_time=0.0,
        sqlalchemy_active=True,
        groups=[],
    )

    assert evaluate(run) == []


def test_findings_never_below_suppression_floor():
    """The 5%-of-wall floor (`Suppress any finding with seconds < 0.05 *
    wall`) must hold for everything evaluate() actually returns. Each of
    R1/R2/R3's own firing thresholds (10%, 5%, 60% of wall respectively)
    already implies this floor, and merges only sum seconds upward, so
    this is checked as an invariant across a battery of fixtures rather
    than by forcing an active finding under the floor (impossible given
    the rule thresholds as specified)."""
    scenarios = [
        make_run(wall_time=20.0, cpu_time=2.0, db_time=8.0,
                  groups=[make_group(calls=20000, median=0.0004, total_time=8.0)]),
        make_run(wall_time=20.0, total_commits=200, total_rows=1000,
                  commit_time=2.0, db_time=1.0),
        make_run(wall_time=20.0, cpu_time=3.0, db_time=15.0),
    ]
    for run in scenarios:
        for finding in evaluate(run):
            assert finding.seconds >= 0.05 * run.wall_time

    # A finding just below R2's own firing gate never fires at all — it
    # can't reach a "would be suppressed" state because the gate is
    # stricter than (equal to) the suppression floor.
    run = make_run(wall_time=1000.0, total_commits=200, total_rows=1000,
                    commit_time=49.0, db_time=0.0)
    assert evaluate(run) == []
