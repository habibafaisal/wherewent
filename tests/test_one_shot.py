"""Tests for R5 "One-shot heavyweight statement" (DESIGN-v3.md AGENT B).

R5 fires independently of R1-R4: among groups with calls == 1, take the one
with the largest total_time; fire if that total_time > 15% of wall. This
catches a single, deliberately-expensive statement (e.g. a big preload or
aggregate query run once at startup) that R1/R3/R4 structurally cannot see,
because all three assume "chattiness" (calls > 1000, or several groups
repeating together) -- a statement that runs exactly once, however slow, is
invisible to them.

NOTE: at the time this file was written, R5 is being added to rules.py by a
parallel agent (DESIGN-v3.md AGENT B), in parallel with this test file. Until
it lands, expect "no R5 in findings" rather than a crash -- the contract-only,
pending-integration case (same convention as test_rules_cluster.py).
"""
import pytest

from wherewent.rules import evaluate
from wherewent.stats import GroupSnapshot, RunSnapshot


# --------------------------------------------------------------------------
# Fixture builders -- mirrors test_rules.py / test_rules_cluster.py's style.
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
    return str(n) in text or f"{n:,}" in text


# --------------------------------------------------------------------------
# R5 fires
# --------------------------------------------------------------------------

def test_r5_fires_for_one_shot_heavyweight_statement():
    # 20.7s / 84.1s wall = 24.6% > 15% -- the DESIGN-v3.md feedback example.
    heavy = make_group(
        key="preload",
        normalized_sql="SELECT * FROM accounts WHERE region = ?",
        calls=1,
        total_time=20.7,
        median=20.7,
        call_site=("app/preload.py", 44, "warm_cache"),
    )
    run = make_run(wall_time=84.1, cpu_time=10.0, db_time=20.7, groups=[heavy])

    findings = evaluate(run)

    r5 = [f for f in findings if f.rule == "R5"]
    assert len(r5) == 1, f"expected exactly one R5 finding, got {[f.rule for f in findings]}"
    finding = r5[0]
    assert finding.seconds == pytest.approx(20.7)
    assert "one-shot" in finding.title.lower() or "heavyweight" in finding.title.lower()
    # site must be named
    assert "app/preload.py" in finding.detail and "44" in finding.detail
    assert "20.7" in finding.detail
    assert "84.1" in finding.detail


def test_r5_names_normalized_sql_when_call_site_unresolved():
    heavy = make_group(
        key="preload",
        normalized_sql="SELECT sum(balance) FROM ledger",
        calls=1,
        total_time=15.0,
        median=15.0,
        call_site=None,
    )
    run = make_run(wall_time=90.0, cpu_time=10.0, db_time=15.0, groups=[heavy])

    findings = evaluate(run)

    r5 = [f for f in findings if f.rule == "R5"]
    assert len(r5) == 1
    detail_lower = r5[0].detail.lower()
    assert "sum(balance)" in detail_lower or "ledger" in detail_lower


def test_r5_picks_the_largest_calls_eq_1_group():
    small_one_shot = make_group(key="small", calls=1, total_time=1.0, call_site=("a.py", 1, "f"))
    big_one_shot = make_group(key="big", calls=1, total_time=20.0, call_site=("b.py", 2, "g"))
    run = make_run(wall_time=100.0, cpu_time=10.0, db_time=21.0,
                    groups=[small_one_shot, big_one_shot])

    findings = evaluate(run)

    r5 = [f for f in findings if f.rule == "R5"]
    assert len(r5) == 1
    assert r5[0].seconds == pytest.approx(20.0)
    assert "b.py" in r5[0].detail
    assert "a.py" not in r5[0].detail


def test_r5_is_additive_not_merged_or_suppressed_by_r1():
    heavy = make_group(key="preload", calls=1, total_time=20.0,
                        call_site=("preload.py", 9, "warm"))
    chatty = make_group(key="chatty", calls=20000, median=0.0004, total_time=15.0,
                         call_site=("job.py", 42, "loop_over_rows"))
    run = make_run(wall_time=100.0, cpu_time=10.0, db_time=35.0, groups=[heavy, chatty])

    findings = evaluate(run)

    rules_fired = {f.rule for f in findings}
    assert "R5" in rules_fired, f"R5 must fire alongside R1, got {rules_fired}"
    assert "R1" in rules_fired, f"R5 must not suppress R1, got {rules_fired}"
    r5 = next(f for f in findings if f.rule == "R5")
    assert r5.seconds == pytest.approx(20.0), "R5's seconds must not be folded into R1's"


# --------------------------------------------------------------------------
# Negative: nothing clears the 15%-of-wall bar
# --------------------------------------------------------------------------

def test_r5_does_not_fire_below_threshold():
    heavy = make_group(key="preload", calls=1, total_time=10.0,
                        call_site=("preload.py", 9, "warm"))
    # 10.0 / 100.0 = 10% <= 15%
    run = make_run(wall_time=100.0, cpu_time=10.0, db_time=10.0, groups=[heavy])

    findings = evaluate(run)
    assert not any(f.rule == "R5" for f in findings)


def test_r5_does_not_fire_with_no_calls_eq_1_groups():
    group = make_group(key="chatty", calls=20000, median=0.0004, total_time=8.0)
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=8.0, groups=[group])

    findings = evaluate(run)
    assert not any(f.rule == "R5" for f in findings)


def test_r5_does_not_fire_on_empty_snapshot():
    run = make_run(wall_time=10.0, cpu_time=1.0, groups=[])
    assert not any(f.rule == "R5" for f in evaluate(run))
