"""Tests for the R4 "co-occurring query pattern" cluster finding
(DESIGN-v2.md AGENT B, sections B3/B2; fixed in DESIGN-v3.md AGENT B).

R4 clusters GroupSnapshots by shared call_site and fires when >= 2 distinct
groups at one site combine to > 1000 calls AND (>10% of wall, OR >10% of
"variable_wall" = wall minus one-shot (calls==1) setup time, OR an absolute
scale trigger: >=200 iterations at >=3 queries/iteration) -- even though no
single group in the cluster trips R1's own per-group thresholds (calls >
1000 AND total_time > 10% wall AND median < 5ms). This is the "N+1 spread
across several query groups" case R1 structurally cannot see on its own.

v0.3 FIX (feedback-driven): on a bounded run, one-time fixed costs (a big
calls==1 preload, calls==1 gate scans) can dominate the clock and suppress a
per-iteration cluster that scales linearly and would dominate at full
volume, even though it "loses" this run's clock. So a cluster below 10% of
RAW wall must still fire when it clears 10% of wall-with-setup-excluded, or
when it is simply large/frequent enough (absolute scale trigger) -- see
test_r4_fires_below_10pct_raw_wall_when_it_scales below, which replaces the
old (now-incorrect) "R4 does NOT fire below 10% of wall" expectation.

NOTE: at the time this file was written, rules.py's R4-fix is being landed by
a parallel agent (DESIGN-v3.md AGENT B), in parallel with this test file.
Until it lands, expect failures of the form "no R4 in findings" rather than
crashes -- that is the "contract-only, pending integration" case.

CLUSTERING-KEY ADDENDUM: R4's clustering key is *also* changing, from
`(file, line, function)` to `(file, function)`. Real ORM/helper code that
issues several co-located statements naturally spreads them across several
source lines of one function (a SELECT, then an UPDATE, then an audit
INSERT, each its own line) -- it does not funnel them through one shared
line. Fixtures below that are meant to prove clustering therefore use
DISTINCT line numbers sharing one (file, function) (see SITE_SELECT /
SITE_UPDATE / SITE_INSERT), never a single shared exact tuple collapsed onto
one line -- collapsing them would hide the exact bug the (file, function)
key change exists to fix. Because rules.py's CURRENT clustering still keys
on the full exact tuple (including line), those same fixtures do NOT
cluster yet, so the tests built on them are marked
`xfail(strict=False, reason="pending rules.py (file,function) clustering-key
fix")` -- expected and correct until that fix lands alongside this file, at
which point they should start passing (and strict=False lets that happen
quietly rather than erroring the suite).
"""
import pytest

from wherewent.rules import evaluate
from wherewent.stats import GroupSnapshot, RunSnapshot


# --------------------------------------------------------------------------
# Fixture builders -- mirrors test_rules.py's factory style.
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


SHARED_SITE = ("worker.py", 88, "process_chain_state")

# Multi-line, same-function sites: a SELECT, an UPDATE, and an audit INSERT,
# each issued from its OWN line of `process_chain_state` -- the realistic
# shape (see module docstring's CLUSTERING-KEY ADDENDUM). Under rules.py's
# CURRENT exact-tuple clustering key these three are DISTINCT call sites; a
# fixture built from them deliberately does not cluster until the
# (file, function) key fix lands.
SITE_SELECT = ("worker.py", 88, "process_chain_state")
SITE_UPDATE = ("worker.py", 90, "process_chain_state")
SITE_INSERT = ("worker.py", 93, "process_chain_state")


# --------------------------------------------------------------------------
# R4 fires: co-occurring groups, none individually tripping R1
#
# The tests below are marked xfail(strict=False): their fixtures use
# SITE_SELECT/SITE_UPDATE/SITE_INSERT (distinct lines, same file+function) on
# purpose, per the CLUSTERING-KEY ADDENDUM above. They are expected to start
# passing once rules.py's clustering key moves from (file, line, function) to
# (file, function); until then, "no R4 in findings" here is correct, not a
# regression -- do not collapse these back onto one shared line to make them
# pass early, that would hide the exact bug the key change fixes.
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    reason="pending rules.py R4 clustering-key change (file,line,func) -> (file,func); "
           "fixture deliberately spreads SELECT/UPDATE/INSERT across 3 lines of one function",
    strict=False,
)
def test_r4_fires_for_cooccurring_groups_below_r1_individually():
    # 3 groups sharing one call SITE (same file+function, different lines) --
    # a SELECT + UPDATE + audit INSERT that fire together once per loop
    # iteration, an N+1 spread across groups. Each is well under R1's
    # calls>1000 bar on its own (800 each), so R1 cannot see it, but combined
    # they clear 1000 calls and 10% of a 20s wall.
    groups = [
        make_group(key="select_chain_state", normalized_sql="SELECT * FROM chain_state WHERE id = ?",
                   calls=800, total_time=0.8, median=0.001, call_site=SITE_SELECT),
        make_group(key="update_chain_state", normalized_sql="UPDATE chain_state SET state = ? WHERE id = ?",
                   calls=800, total_time=0.8, median=0.001, call_site=SITE_UPDATE),
        make_group(key="insert_audit", normalized_sql="INSERT INTO audit_log VALUES (?, ?, ?)",
                   calls=800, total_time=0.8, median=0.001, call_site=SITE_INSERT),
    ]
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=2.4, groups=groups)

    findings = evaluate(run)

    r4 = [f for f in findings if "R4" in f.rule]
    assert len(r4) == 1, f"expected exactly one R4 finding, got {[f.rule for f in findings]}"
    finding = r4[0]
    assert finding.seconds == pytest.approx(2.4)

    detail = finding.detail
    for g in groups:
        assert g.normalized_sql[:15] in detail or _contains_number(detail, g.calls), (
            f"detail does not mention group {g.key!r}: {detail!r}"
        )
    # file + function must appear -- not asserting a specific line number
    # since (file, function) clustering may span several lines in the detail.
    assert "worker.py" in detail and "process_chain_state" in detail, (
        "shared call site (file + function) must appear in the detail"
    )
    assert "per iteration" in detail.lower()
    # no individual R1 -- each group is under R1's calls>1000 bar.
    assert not any(f.rule == "R1" for f in findings)


@pytest.mark.xfail(
    reason="pending rules.py R4 clustering-key change (file,line,func) -> (file,func); "
           "fixture deliberately spreads SELECT/UPDATE across 2 lines of one function",
    strict=False,
)
def test_r4_detail_reports_per_iteration_ratio_when_groups_near_equal():
    groups = [
        make_group(key="k1", normalized_sql="SELECT 1 FROM a WHERE id = ?",
                   calls=1000, total_time=1.5, call_site=SITE_SELECT),
        make_group(key="k2", normalized_sql="UPDATE a SET x = ? WHERE id = ?",
                   calls=1000, total_time=1.5, call_site=SITE_UPDATE),
    ]
    run = make_run(wall_time=25.0, cpu_time=2.0, db_time=3.0, groups=groups)

    findings = evaluate(run)

    r4 = [f for f in findings if "R4" in f.rule]
    assert len(r4) == 1
    detail = r4[0].detail
    # 2 groups x 1000 calls each, perfectly equal -> ~1000 iterations, ~2
    # queries/iteration. Must be labelled as an estimate (this codebase's
    # existing convention is the ASCII "~=" marker R1/R2/R3 already use --
    # not necessarily the unicode "≈" glyph), never a bare guess.
    assert "~=" in detail or "≈" in detail, f"per-iteration line must be labelled as an estimate: {detail!r}"
    assert "per iteration" in detail.lower()
    assert _contains_number(detail, 1000)


# --------------------------------------------------------------------------
# Negative: a single-group hotspot is R1's job, not R4's
# --------------------------------------------------------------------------

def test_r4_does_not_fire_for_single_group_at_a_site():
    group = make_group(calls=20000, median=0.0004, total_time=8.0, call_site=SHARED_SITE)
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=8.0, groups=[group])

    findings = evaluate(run)

    assert not any("R4" in f.rule for f in findings)
    assert any(f.rule == "R1" for f in findings)


def test_r4_ignores_groups_with_no_call_site_for_clustering():
    # call_site=None groups can't be attributed to a shared site, so they
    # must never cluster into an R4 finding no matter how chatty.
    groups = [
        make_group(key="k1", calls=800, total_time=0.8, call_site=None),
        make_group(key="k2", normalized_sql="UPDATE t SET x = ? WHERE id = ?",
                   calls=800, total_time=0.8, call_site=None),
    ]
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=1.6, groups=groups)

    findings = evaluate(run)
    assert not any("R4" in f.rule for f in findings)


def test_r4_blocked_by_combined_calls_threshold():
    groups = [
        make_group(key="k1", calls=400, total_time=3.0, call_site=SHARED_SITE),
        make_group(key="k2", normalized_sql="UPDATE t SET x = ? WHERE id = ?",
                   calls=400, total_time=3.0, call_site=SHARED_SITE),
    ]
    # combined calls = 800 (<= 1000), even though combined seconds is large.
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=6.0, groups=groups)

    findings = evaluate(run)
    assert not any("R4" in f.rule for f in findings)


def test_r4_still_blocked_below_10pct_when_it_does_not_scale():
    # A genuine negative under the NEW (v0.3) formula: combined calls clear
    # 1000, but the cluster is small relative to wall (5%), small relative to
    # variable_wall too (there's no one-shot setup here to exclude, so
    # variable_wall == wall), and the absolute scale trigger also fails
    # (per_iter == 2, below the >=3 bar). None of the three qualifying
    # conditions hold, so R4 must still NOT fire -- the fix adds new ways to
    # qualify, it does not make every modest cluster fire.
    groups = [
        make_group(key="k1", calls=800, total_time=0.5, call_site=SHARED_SITE),
        make_group(key="k2", normalized_sql="UPDATE t SET x = ? WHERE id = ?",
                   calls=800, total_time=0.5, call_site=SHARED_SITE),
    ]
    # combined calls = 1600 (> 1000) but combined total_time = 1.0s = 5% of a
    # 20s wall (<= 10%), and 5% of variable_wall (no one-shot groups present).
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=1.0, groups=groups)

    findings = evaluate(run)
    assert not any("R4" in f.rule for f in findings)


@pytest.mark.xfail(
    reason="pending rules.py R4 clustering-key change (file,line,func) -> (file,func); "
           "fixture deliberately spreads SELECT/UPDATE/INSERT across 3 lines of one function",
    strict=False,
)
def test_r4_fires_below_10pct_raw_wall_when_it_scales():
    # The feedback fixture (DESIGN-v3.md): a big one-shot preload/gate-scan
    # (calls == 1, ~29s) inflates a bounded run's wall so much that a
    # per-iteration cluster which obviously scales linearly (3 co-located
    # query groups firing together, ~430 iterations, ~3 queries/iteration)
    # only amounts to ~7% of the RAW wall -- below the OLD single wall% gate.
    # It is nonetheless >10% of wall-with-one-shot-setup-excluded
    # (variable_wall) AND clears the absolute scale trigger
    # (iterations >= 200 and per_iter >= 3), so the FIXED R4 must fire, and
    # its detail must show both ratios plus the "scales at volume" framing.
    #
    # The 3 co-located groups are, again, on 3 different lines of one
    # function (SITE_SELECT/SITE_UPDATE/SITE_INSERT) -- not funnelled
    # through one shared line -- since this is precisely the fixture the
    # (file, function) clustering-key fix targets.
    one_shot = make_group(
        key="preload",
        normalized_sql="SELECT * FROM accounts WHERE region = ?",
        calls=1,
        total_time=29.0,
        median=29.0,
        call_site=("app/preload.py", 12, "warm_cache"),
    )
    cluster_groups = [
        make_group(key="select_chain_state", normalized_sql="SELECT * FROM chain_state WHERE id = ?",
                   calls=430, total_time=2.0, median=0.0047, call_site=SITE_SELECT),
        make_group(key="update_chain_state", normalized_sql="UPDATE chain_state SET state = ? WHERE id = ?",
                   calls=430, total_time=2.0, median=0.0047, call_site=SITE_UPDATE),
        make_group(key="insert_audit", normalized_sql="INSERT INTO audit_log VALUES (?, ?, ?)",
                   calls=430, total_time=2.0, median=0.0047, call_site=SITE_INSERT),
    ]
    wall = 84.0
    combined_calls = 430 * 3   # 1290 > 1000
    combined_time = 2.0 * 3    # 6.0s
    variable_wall = wall - 29.0  # 55.0
    assert combined_time / wall < 0.10          # below the OLD/raw-wall gate (~7.1%)
    assert combined_time / variable_wall > 0.10  # above the NEW excluded-wall gate (~10.9%)

    groups = [one_shot] + cluster_groups
    run = make_run(wall_time=wall, cpu_time=8.0, db_time=combined_time + 29.0, groups=groups)

    findings = evaluate(run)

    r4 = [f for f in findings if "R4" in f.rule]
    assert len(r4) == 1, f"expected R4 to fire despite being under 10% of raw wall, got {[f.rule for f in findings]}"
    finding = r4[0]
    assert finding.seconds == pytest.approx(combined_time)

    detail = finding.detail
    raw_pct = f"{combined_time / wall * 100:.0f}%"
    excluded_pct = f"{combined_time / variable_wall * 100:.0f}%"
    assert raw_pct in detail, f"raw wall%% ({raw_pct}) must appear in detail: {detail!r}"
    assert excluded_pct in detail, f"setup-excluded wall%% ({excluded_pct}) must appear in detail: {detail!r}"
    assert f"{wall:.1f}" in detail
    assert "scale" in detail.lower(), f"detail must explain this scales at volume: {detail!r}"
    assert _contains_number(detail, combined_calls)


# --------------------------------------------------------------------------
# R4 subsumes R1 when R1's group sits inside an R4 cluster (no double count)
# --------------------------------------------------------------------------

@pytest.mark.xfail(
    reason="pending rules.py R4 clustering-key change (file,line,func) -> (file,func); "
           "fixture deliberately spreads its 3 groups across 3 lines of one function",
    strict=False,
)
def test_r4_subsumes_r1_when_r1_group_is_inside_the_cluster():
    r1_group = make_group(key="hot", calls=20000, median=0.0004, total_time=8.0, call_site=SITE_SELECT)
    sibling_a = make_group(key="sib_a", normalized_sql="UPDATE t SET x = ? WHERE id = ?",
                            calls=800, total_time=0.5, call_site=SITE_UPDATE)
    sibling_b = make_group(key="sib_b", normalized_sql="INSERT INTO audit VALUES (?, ?)",
                            calls=800, total_time=0.5, call_site=SITE_INSERT)
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=9.0,
                    groups=[r1_group, sibling_a, sibling_b])

    findings = evaluate(run)

    # R1 alone would fire on `r1_group`; because it shares a call site with
    # siblings forming a qualifying R4 cluster, only R4 survives (it strictly
    # contains R1's story), with seconds = the cluster total, not R1's alone.
    assert not any(f.rule == "R1" for f in findings)
    r4 = [f for f in findings if "R4" in f.rule]
    assert len(r4) == 1
    assert r4[0].seconds == pytest.approx(8.0 + 0.5 + 0.5)


# --------------------------------------------------------------------------
# Regression: R4 must not duplicate or suppress the existing R1+R2 story
# --------------------------------------------------------------------------

def test_r4_does_not_disturb_naive_like_r1_r2_merge():
    # Same fixture shape as test_rules.py's R1+R2 merge case. Only ONE query
    # group is present -> R4 structurally cannot fire (it needs >= 2 distinct
    # groups at a site), so the naive job's R1+R2 story must be unchanged.
    group = make_group(calls=9000, median=0.0004, total_time=4.0)
    run = make_run(
        wall_time=20.0,
        cpu_time=2.0,
        total_commits=8000,
        total_rows=32000,
        commit_time=3.0,
        db_time=5.0,
        groups=[group],
    )

    findings = evaluate(run)

    assert len(findings) == 1
    assert findings[0].rule == "R1+R2"
    assert findings[0].seconds == pytest.approx(4.0 + 3.0)
    assert not any("R4" in f.rule for f in findings)
