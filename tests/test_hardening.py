"""Tests for the v0.3.0 HARDENING PASS (.handoff/DESIGN-v3-hardening.md, AGENT H-C).

Covers the defects/gaps assigned to this file specifically:
  D1 - bounded per-group duration reservoir (recorder.py)
  D2 - commit_time / rollback_time split (recorder.py + report.py)
  D6 - R4 flush-attribution guard + caveat label (rules.py)
  D7 - R5 absolute floor + trivia-filter escape hatch (rules.py)
  D8 - CPU-bound honesty framing for R4/R6 (rules.py)
  D9 - R6 queries/unit slope (rules.py + wording)

D1/D2 drive a REAL Recorder + a real sqlite SQLAlchemy engine (not hand-built
snapshots) per the build contract, since those are capture-path mechanics
that only a real event-hook round trip can prove. D6/D7/D8/D9 are pure
rules.evaluate() arithmetic/wording and, matching the existing convention in
test_rules.py / test_rules_cluster.py / test_one_shot.py, are exercised
against hand-built RunSnapshot/GroupSnapshot/UnitStats fixtures -- there is
no capture-path mechanic left to prove there, only threshold and string
logic that a fixture pins precisely.

R6's true end-to-end path (install a real Recorder, wrap a real per-unit
function, run a real growing-query-count loop, snapshot(), evaluate()) is
already covered by test_unit_profiling.py's
test_growth_triggers_r6_via_unit_function -- not duplicated here.

CROSS-TEST ISOLATION: install() registers SQLAlchemy hooks at the Engine
CLASS level (global), and per-unit state lives in module-level ContextVars
shared by every Recorder instance (see test_unit_profiling.py's module
docstring for the same characteristic). The `fresh_recorder` fixture below
always calls the D10 `Recorder.disable()` teardown hook so a Recorder created
by one test can never keep reacting to a later test's queries.
"""
import json

import pytest
from sqlalchemy import create_engine, text

from wherewent import report, rules
from wherewent.recorder import Recorder, _DUR_RESERVOIR_CAP
from wherewent.rules import KEEP_ABS_SECONDS, evaluate
from wherewent.stats import GroupSnapshot, RunSnapshot, UnitStats


# --------------------------------------------------------------------------
# Fixture builders -- mirrors test_rules.py / test_rules_cluster.py's style,
# extended with unit_stats/rollback_time (D8/D9/D2 need them; the older
# copies in sibling test files predate those RunSnapshot fields).
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
    unit_stats=None,
    rollback_time=None,
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
        unit_stats=unit_stats,
        rollback_time=rollback_time,
    )


def make_unit_stats(
    name="unit",
    wrapped=True,
    count=0,
    median_duration=0.0,
    mean_duration=0.0,
    median_queries=0.0,
    mean_queries=0.0,
    mean_commits=0.0,
    mean_rollbacks=0.0,
    mean_rows=0.0,
    first_window_n=0,
    first_window_mean_duration=None,
    last_window_n=0,
    last_window_mean_duration=None,
    first_window_mean_queries=None,
    last_window_mean_queries=None,
):
    return UnitStats(
        name=name,
        wrapped=wrapped,
        count=count,
        median_duration=median_duration,
        mean_duration=mean_duration,
        median_queries=median_queries,
        mean_queries=mean_queries,
        mean_commits=mean_commits,
        mean_rollbacks=mean_rollbacks,
        mean_rows=mean_rows,
        first_window_n=first_window_n,
        first_window_mean_duration=first_window_mean_duration,
        last_window_n=last_window_n,
        last_window_mean_duration=last_window_mean_duration,
        first_window_mean_queries=first_window_mean_queries,
        last_window_mean_queries=last_window_mean_queries,
    )


_ENGINE_EVENT_NAMES = (
    "before_cursor_execute", "after_cursor_execute", "begin",
    "commit", "rollback", "engine_connect", "handle_error",
)


def _clear_stale_engine_listeners():
    """Defensive-only sweep of Engine-class-level SQLAlchemy event listeners.

    NOT a fix for a bug in this file -- a workaround for one confirmed
    upstream: tests/test_async_callsite.py (not owned by this file) builds
    two `Recorder(); rec.install(None)` instances and never calls
    `rec.disable()`. Those two Recorders' listeners (registered at Engine
    CLASS level, i.e. process-global -- see the module docstring) stay live
    for the rest of the pytest process once that file has run.

    That leak is normally harmless (a leaked Recorder just keeps quietly
    counting queries nobody reads), EXCEPT for recorder.py's
    `_wrap_dialect()`: the "idempotent per dialect" guard
    (`dialect._wherewent_wrapped`) is scoped to the DIALECT INSTANCE, not to
    a Recorder. Whichever Recorder's `engine_connect` listener fires first
    on a fresh engine wins the one-time commit/rollback timing wrap; every
    OTHER Recorder observing that same dialect is then permanently denied
    `commit_measurable`/`rollback_measurable`. Because the leaked listeners
    were registered earliest, they always fire first and always win --
    starving every Recorder created by a LATER test (this file's D2 tests
    included) of commit_time/rollback_time for the rest of the run, in an
    order-dependent way (fails only when test_async_callsite.py ran first).

    Confirmed via bisection: tests/test_hardening.py's D2 tests pass in
    isolation, pass paired with any other single test file, and only fail
    inside the full suite -- specifically because test_async_callsite.py's
    two orphaned Recorders precede them in collection order. This is a
    genuine cross-file test-isolation bug outside this file's ownership
    (reported, not fixed there); this sweep keeps this file's own D1/D2
    coverage deterministic and order-independent regardless of what ran
    before it, without touching any other file.
    """
    try:
        from sqlalchemy.engine import Engine
    except Exception:
        return
    for name in _ENGINE_EVENT_NAMES:
        dispatch = getattr(Engine.dispatch, name, None)
        if dispatch is not None:
            try:
                dispatch.clear()
            except Exception:
                pass


@pytest.fixture
def fresh_recorder():
    """A brand-new Recorder(), guaranteed-disabled at teardown.

    D10's Recorder.disable() removes the Engine-class-level event listeners,
    restores wrapped dialect commit/rollback, and drops any meta_path import
    hooks this instance installed -- so it cannot keep reacting to queries
    issued by a LATER test in the same pytest process. See the module
    docstring's CROSS-TEST ISOLATION note.

    Also sweeps any STALE listeners left by an unrelated leaking test file
    before creating this Recorder -- see `_clear_stale_engine_listeners`.
    """
    _clear_stale_engine_listeners()
    rec = Recorder()
    try:
        yield rec
    finally:
        rec.disable()


# ==========================================================================
# D1 -- bounded per-group duration reservoir
# ==========================================================================

def test_d1_reservoir_capped_calls_and_total_time_exact_median_sane(tmp_path, fresh_recorder):
    rec = fresh_recorder
    rec.install(None)

    engine = create_engine(f"sqlite:///{tmp_path / 'reservoir.db'}", future=True)
    n = _DUR_RESERVOIR_CAP + 1200  # comfortably past the cap: several overflow replacements
    with engine.connect() as conn:
        for _ in range(n):
            conn.execute(text("SELECT 1"))

    run = rec.snapshot()
    assert len(run.groups) == 1, (
        f"expected exactly one query group (same statement every time), "
        f"got {[g.normalized_sql for g in run.groups]}"
    )
    g = run.groups[0]

    # `calls` is EXACT -- every execution is counted, nothing sampled away.
    assert g.calls == n

    # `total_time` is EXACT too: it is the run's only group, so it must equal
    # db_time bit-for-bit -- both are unconditional running sums built from
    # the SAME per-execution durations in the SAME order (recorder.py's
    # `_after()` does `self.db_time += duration` and `g["total_time"] +=
    # duration` for every single execution, never gated by the reservoir).
    assert g.total_time == run.db_time

    # the reservoir is the actual bounded-memory mechanism -- assert the cap
    # directly against the recorder's internal accumulator.
    raw = rec.groups[g.key]
    assert raw["dur_seen"] == n, "dur_seen must count every execution exactly, even past the cap"
    assert len(raw["dur_reservoir"]) == _DUR_RESERVOIR_CAP, (
        "the per-group duration reservoir must never grow past its cap"
    )

    # median is a SAMPLE median drawn from a still-nonempty, bounded
    # reservoir -- must be a real, sane number (not zero, not absurd).
    assert 0.0 < g.median < 1.0


def test_d1_reservoir_cap_holds_far_past_the_cap(tmp_path, fresh_recorder):
    """A second, larger pass -- proves the cap holds under repeated overflow
    replacement (algorithm-R keeps replacing forever), not just just past it."""
    rec = fresh_recorder
    rec.install(None)

    engine = create_engine(f"sqlite:///{tmp_path / 'reservoir2.db'}", future=True)
    n = _DUR_RESERVOIR_CAP * 2 + 500
    with engine.connect() as conn:
        for _ in range(n):
            conn.execute(text("SELECT 1"))

    run = rec.snapshot()
    g = run.groups[0]
    assert g.calls == n
    assert g.total_time == run.db_time
    raw = rec.groups[g.key]
    assert len(raw["dur_reservoir"]) == _DUR_RESERVOIR_CAP


# ==========================================================================
# D2 -- commit_time / rollback_time split
# ==========================================================================

def test_d2_rollback_only_run_leaves_commit_time_at_zero(tmp_path, fresh_recorder):
    rec = fresh_recorder
    rec.install(None)

    engine = create_engine(f"sqlite:///{tmp_path / 'rollback_only.db'}", future=True)
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER)"))
        conn.rollback()  # no commit() anywhere in this run

    run = rec.snapshot()
    assert run.commit_time is not None, "a connection was made -- the dialect must be wrapped/measurable"
    assert run.commit_time == 0.0, (
        "no commit occurred: commit_time must stay commit-ONLY, never absorb rollback time"
    )
    assert run.rollback_time is not None
    assert run.rollback_time > 0.0, "the explicit rollback() (plus any pool-reset rollback) must be measured"


def test_d2_commit_time_unaffected_by_a_later_rollback(tmp_path, fresh_recorder):
    rec = fresh_recorder
    rec.install(None)

    engine = create_engine(f"sqlite:///{tmp_path / 'mixed.db'}", future=True)
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER)"))
        conn.commit()
    commit_time_after_commit = rec.commit_time
    assert commit_time_after_commit > 0.0

    with engine.connect() as conn:
        conn.execute(text("INSERT INTO t (id) VALUES (1)"))
        conn.rollback()

    run = rec.snapshot()
    assert run.commit_time == pytest.approx(commit_time_after_commit), (
        "a rollback happening AFTER the commit must never add into commit_time"
    )
    assert run.rollback_time > 0.0


def test_d2_save_json_reports_commit_and_rollback_time_separately(tmp_path, fresh_recorder):
    rec = fresh_recorder
    save_path = tmp_path / "out.json"
    rec.install(str(save_path))

    engine = create_engine(f"sqlite:///{tmp_path / 'json_rb.db'}", future=True)
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER)"))
        conn.commit()
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO t (id) VALUES (1)"))
        conn.rollback()
    rec.finalize()

    with open(save_path) as fh:
        payload = json.load(fh)

    assert "rollback_time" in payload
    assert "commit_time" in payload
    assert payload["commit_time"] > 0.0
    assert payload["rollback_time"] > 0.0


def _report_run(**overrides):
    base = dict(
        wall_time=10.0, cpu_time=1.0, total_queries=0, total_commits=0,
        total_rollbacks=0, commit_time=1.0, total_rows=0, db_time=0.5,
        overhead_time=0.01, sqlalchemy_active=True, groups=[],
    )
    base.update(overrides)
    return RunSnapshot(**base)


def test_d2_report_shows_rollback_line_labelled_pool_resets_when_measurable():
    run = _report_run(rollback_time=0.35)
    text_out = report.render(run, [])
    rollback_lines = [
        ln for ln in text_out.splitlines() if ln.strip().lower().startswith("rollback time")
    ]
    assert len(rollback_lines) == 1, f"expected exactly one rollback-time header line: {text_out!r}"
    assert "0.35" in rollback_lines[0]
    assert "incl. pool resets" in rollback_lines[0]


def test_d2_report_omits_rollback_line_when_not_measurable():
    run = _report_run(rollback_time=None)
    text_out = report.render(run, [])
    assert "rollback time" not in text_out.lower()


# ==========================================================================
# D6 -- R4 flush-attribution guard + caveat label
# ==========================================================================
# All three fixtures share wall=80.0 so the trivia-suppression floor
# (>= 5% of wall, i.e. >= 4.0s) and the qualifying wall% gate (> 10% of wall,
# i.e. > 8.0s) sit on either side of the "scale trigger alone" combined_time
# (4.5s) -- proving R4 can qualify AND survive the final trivia filter via
# scale alone, distinct from clearing the wall% bar outright.

_FLUSH_SITE = ("worker.py", 88, "flush_writes")
_MULTI_LINE_A = ("worker.py", 60, "multi_line_writes")
_MULTI_LINE_B = ("worker.py", 70, "multi_line_writes")
_MULTI_LINE_C = ("worker.py", 80, "multi_line_writes")


def test_d6_single_line_write_cluster_not_promoted_by_scale_trigger_alone():
    # 3 write-shaped groups, ALL at the SAME source line -- the ORM
    # unit-of-work flush/commit signature. Combined calls clear 1000 and the
    # absolute scale trigger (iterations=1000>=200, per_iter=3>=3), but
    # combined_time (4.5s) stays well under 10% of an 80s wall (8.0s) -- so
    # the flush-signature guard must block the scale trigger from promoting
    # it alone. R4 (and everything else) must be entirely absent.
    groups = [
        make_group(key="ins", normalized_sql="INSERT INTO ledger_state (status) VALUES (?)",
                   calls=1000, total_time=1.5, call_site=_FLUSH_SITE),
        make_group(key="upd", normalized_sql="UPDATE ledger_state SET status = ? WHERE id = ?",
                   calls=1000, total_time=1.5, call_site=_FLUSH_SITE),
        make_group(key="del", normalized_sql="DELETE FROM ledger_state WHERE id = ?",
                   calls=1000, total_time=1.5, call_site=_FLUSH_SITE),
    ]
    run = make_run(wall_time=80.0, cpu_time=None, db_time=4.5, groups=groups)

    findings = evaluate(run)
    assert findings == [], (
        f"a single-line write cluster below the wall%% bar must not fire via "
        f"scale alone, got {[f.rule for f in findings]}"
    )


def test_d6_single_line_write_cluster_fires_with_flush_caveat_when_it_clears_wallpct():
    # Same single-line write shape, but bumped so combined_time (9.0s) clears
    # 10% of the 80s wall -- R4 now qualifies via the wall%% gate, NOT the
    # scale trigger. It must carry the flush caveat and must NOT tell the
    # user to "collapse into one round-trip" (that would be false: a single
    # flush()/commit() already merged these into one call site).
    groups = [
        make_group(key="ins", normalized_sql="INSERT INTO ledger_state (status) VALUES (?)",
                   calls=1000, total_time=3.0, call_site=_FLUSH_SITE),
        make_group(key="upd", normalized_sql="UPDATE ledger_state SET status = ? WHERE id = ?",
                   calls=1000, total_time=3.0, call_site=_FLUSH_SITE),
        make_group(key="del", normalized_sql="DELETE FROM ledger_state WHERE id = ?",
                   calls=1000, total_time=3.0, call_site=_FLUSH_SITE),
    ]
    run = make_run(wall_time=80.0, cpu_time=None, db_time=9.0, groups=groups)

    findings = evaluate(run)
    r4 = [f for f in findings if "R4" in f.rule]
    assert len(r4) == 1, f"expected R4 to fire via the wall%% gate, got {[f.rule for f in findings]}"
    detail = r4[0].detail
    # H-B note: assert a stable SUBSTRING, never a whole line (the caveat
    # uses a Unicode em dash while surrounding text uses ASCII "--").
    assert "session.flush()/commit()" in detail
    assert "collapse into one round-trip" not in detail


def test_d6_multi_line_write_cluster_fires_normally_via_scale_trigger_alone():
    # The SAME write statements, but spread across 3 DISTINCT source lines of
    # one function -- a genuine multi-line workflow (D5's fragmentation
    # case), not an ORM flush. combined_time (4.5s) stays below 10% of the
    # 80s wall (same numbers as the blocked case above) but clears the 5%
    # trivia floor (4.0s) and the absolute scale trigger, so R4 must fire via
    # scale alone -- and with the ORDINARY advice, no flush caveat.
    groups = [
        make_group(key="ins", normalized_sql="INSERT INTO ledger_state (status) VALUES (?)",
                   calls=1000, total_time=1.5, call_site=_MULTI_LINE_A),
        make_group(key="upd", normalized_sql="UPDATE ledger_state SET status = ? WHERE id = ?",
                   calls=1000, total_time=1.5, call_site=_MULTI_LINE_B),
        make_group(key="del", normalized_sql="DELETE FROM ledger_state WHERE id = ?",
                   calls=1000, total_time=1.5, call_site=_MULTI_LINE_C),
    ]
    run = make_run(wall_time=80.0, cpu_time=None, db_time=4.5, groups=groups)

    findings = evaluate(run)
    r4 = [f for f in findings if "R4" in f.rule]
    assert len(r4) == 1, (
        f"a genuine multi-line cluster must fire via the scale trigger alone, "
        f"got {[f.rule for f in findings]}"
    )
    detail = r4[0].detail
    assert "session.flush()/commit()" not in detail
    assert "collapse into one round-trip" in detail


# ==========================================================================
# D7 -- R5 absolute floor + trivia-filter escape hatch
# ==========================================================================

def test_d7_absolute_floor_fires_far_below_5pct_of_a_huge_wall():
    # 20s on a 1500s wall is ~1.3%%, nowhere near R5's 15%% percentage gate
    # AND nowhere near the trivia filter's normal 5%% floor (75s) -- only the
    # KEEP_ABS_SECONDS escape hatch (>= 10s absolute) keeps it alive.
    heavy = make_group(
        key="preload", normalized_sql="SELECT * FROM huge_table",
        calls=1, total_time=20.0, median=20.0,
        call_site=("app/big.py", 5, "one_shot_fn"),
    )
    run = make_run(wall_time=1500.0, cpu_time=None, db_time=20.0, groups=[heavy])

    findings = evaluate(run)
    r5 = [f for f in findings if f.rule == "R5"]
    assert len(r5) == 1, (
        f"a 20s one-shot on a 1500s wall must still fire via the absolute floor "
        f"and survive the trivia filter, got {[f.rule for f in findings]}"
    )
    finding = r5[0]
    assert finding.seconds == pytest.approx(20.0)
    detail = finding.detail
    # the absolute-path arithmetic must say BOTH the (small) percentage and
    # the (large) absolute seconds, so the number reads honestly.
    assert "20.0s absolute" in detail
    assert "10s floor" in detail
    assert "1500.0s wall" in detail


def test_d7_absolute_floor_negative_control_strict_greater_than():
    # 10.0s on a 100s wall: 10%% (<= 15%% pct gate) AND exactly
    # R5_ABS_SECONDS (10.0, NOT > 10.0 -- the floor is a strict > comparison)
    # -- must NOT fire.
    borderline = make_group(
        key="preload", calls=1, total_time=10.0,
        call_site=("app/big.py", 5, "one_shot_fn"),
    )
    run = make_run(wall_time=100.0, cpu_time=None, db_time=10.0, groups=[borderline])

    findings = evaluate(run)
    assert not any(f.rule == "R5" for f in findings), (
        "10.0s is not strictly > the 10.0s absolute floor -- R5 must not fire"
    )


def test_d7_just_over_the_absolute_floor_does_fire():
    # The mirror image of the negative control: 10.01s (strictly > 10.0s)
    # still nowhere near 15%% of a 1000s wall -- must fire via the floor.
    just_over = make_group(
        key="preload", calls=1, total_time=10.01,
        call_site=("app/big.py", 5, "one_shot_fn"),
    )
    run = make_run(wall_time=1000.0, cpu_time=None, db_time=10.01, groups=[just_over])

    findings = evaluate(run)
    assert any(f.rule == "R5" for f in findings)


# ==========================================================================
# D8 -- CPU-bound honesty framing for R4/R6
# ==========================================================================

_R4_SITE_SELECT = ("worker.py", 88, "process_chain_state")
_R4_SITE_UPDATE = ("worker.py", 90, "process_chain_state")
_R4_SITE_INSERT = ("worker.py", 93, "process_chain_state")


def _r4_cluster_groups():
    return [
        make_group(key="select_chain_state", normalized_sql="SELECT * FROM chain_state WHERE id = ?",
                   calls=800, total_time=0.8, median=0.001, call_site=_R4_SITE_SELECT),
        make_group(key="update_chain_state", normalized_sql="UPDATE chain_state SET state = ? WHERE id = ?",
                   calls=800, total_time=0.8, median=0.001, call_site=_R4_SITE_UPDATE),
        make_group(key="insert_audit", normalized_sql="INSERT INTO audit_log VALUES (?, ?, ?)",
                   calls=800, total_time=0.8, median=0.001, call_site=_R4_SITE_INSERT),
    ]


def test_d8_r4_carries_scalability_caveat_when_cpu_bound():
    # cpu_busy = 18/20 = 90%% -- comfortably compute-bound (>= 0.5 gate).
    run = make_run(wall_time=20.0, cpu_time=18.0, db_time=2.4, groups=_r4_cluster_groups())

    findings = evaluate(run)
    r4 = [f for f in findings if "R4" in f.rule]
    assert len(r4) == 1
    detail = r4[0].detail
    assert "CPU-bound (CPU busy 90%)" in detail
    assert "SCALABILITY risk" in detail
    assert "NOT the current wall-clock bottleneck" in detail


def test_d8_r4_no_scalability_caveat_when_not_cpu_bound():
    # cpu_busy = 2/20 = 10%% -- not compute-bound.
    run = make_run(wall_time=20.0, cpu_time=2.0, db_time=2.4, groups=_r4_cluster_groups())

    findings = evaluate(run)
    r4 = [f for f in findings if "R4" in f.rule]
    assert len(r4) == 1
    assert "CPU-bound" not in r4[0].detail


def _r6_growth_unit_stats():
    return make_unit_stats(
        count=25, mean_duration=0.08,
        first_window_n=100, first_window_mean_duration=0.01,
        last_window_n=100, last_window_mean_duration=0.1,
    )


def test_d8_r6_carries_scalability_caveat_when_cpu_bound():
    # cpu_busy = 15/20 = 75%%.
    run = make_run(wall_time=20.0, cpu_time=15.0, db_time=1.0, unit_stats=_r6_growth_unit_stats())

    findings = evaluate(run)
    r6 = [f for f in findings if f.rule == "R6"]
    assert len(r6) == 1
    detail = r6[0].detail
    assert "CPU-bound (CPU busy 75%)" in detail
    assert "SCALABILITY risk" in detail
    assert "NOT the current wall-clock bottleneck" in detail


def test_d8_r6_no_scalability_caveat_when_cpu_time_unmeasurable():
    run = make_run(wall_time=20.0, cpu_time=None, db_time=1.0, unit_stats=_r6_growth_unit_stats())

    findings = evaluate(run)
    r6 = [f for f in findings if f.rule == "R6"]
    assert len(r6) == 1
    assert "CPU-bound" not in r6[0].detail


# ==========================================================================
# D9 -- R6 queries/unit slope
# ==========================================================================

def test_d9_query_slope_wording_appears_with_signed_percentage():
    us = make_unit_stats(
        count=25, mean_duration=0.05,
        first_window_n=100, first_window_mean_duration=0.01,
        last_window_n=100, last_window_mean_duration=0.02,
        first_window_mean_queries=2.0, last_window_mean_queries=8.0,
        mean_queries=5.0,
    )
    run = make_run(wall_time=10.0, cpu_time=None, db_time=1.0, unit_stats=us)

    findings = evaluate(run)
    r6 = [f for f in findings if f.rule == "R6"]
    assert len(r6) == 1
    detail = r6[0].detail
    assert "queries/unit rose from 2 (first 100) to 8 (last 100) (+300%)." in detail


def test_d9_query_slope_only_case_still_fires_r6_with_flat_duration():
    # first/last window MEAN DURATION are identical (flat) -- the duration
    # signal alone would never fire R6 -- but queries/unit triples
    # (2 -> 6, well over the 1.5x bar), so R6 must still fire on the query
    # slope alone. Because the duration-based seconds estimate comes out to
    # 0.0 (flat), rules.py falls back to a conservative query-slope estimate
    # (excess queries/unit x mean per-query DB time) -- never negative, never
    # a crash.
    us = make_unit_stats(
        count=25, mean_duration=0.01,
        first_window_n=100, first_window_mean_duration=0.01,
        last_window_n=100, last_window_mean_duration=0.01,
        first_window_mean_queries=2.0, last_window_mean_queries=6.0,
        mean_queries=8.0,
    )
    run = make_run(wall_time=10.0, cpu_time=None, total_queries=1000, db_time=5.0, unit_stats=us)

    findings = evaluate(run)
    r6 = [f for f in findings if f.rule == "R6"]
    assert len(r6) == 1, (
        f"query-slope-only (flat duration, rising queries) must still fire R6, "
        f"got {[f.rule for f in findings]}"
    )
    finding = r6[0]
    expected_seconds = (8.0 - 2.0) * 25 * (5.0 / 1000)  # excess queries/unit * mean per-query time
    assert finding.seconds == pytest.approx(expected_seconds)
    assert finding.seconds > 0.0
    detail = finding.detail
    assert "queries/unit rose from 2 (first 100) to 6 (last 100) (+200%)." in detail
    assert "first 100 units averaged 10 ms; last 100 averaged 10 ms (+0%)." in detail


def test_d9_query_trend_reports_a_negative_percentage_when_falling():
    # Duration alone drives the fire here (2x growth); queries/unit actually
    # DECLINE. The query line must still print (it is present, just not the
    # trigger) with a signed NEGATIVE percentage and "fell", proving the
    # wording is not hardcoded to a "+" sign.
    us = make_unit_stats(
        count=25, mean_duration=0.05,
        first_window_n=100, first_window_mean_duration=0.01,
        last_window_n=100, last_window_mean_duration=0.02,
        first_window_mean_queries=8.0, last_window_mean_queries=2.0,
        mean_queries=5.0,
    )
    run = make_run(wall_time=10.0, cpu_time=None, db_time=1.0, unit_stats=us)

    findings = evaluate(run)
    r6 = [f for f in findings if f.rule == "R6"]
    assert len(r6) == 1
    detail = r6[0].detail
    assert "queries/unit fell from 8 (first 100) to 2 (last 100) (-75%)." in detail
    assert "first 100 units averaged 10 ms; last 100 averaged 20 ms (+100%)." in detail


def test_d9_no_query_line_when_query_windows_absent():
    # Duration-only unit stats (no query windows at all, e.g. an older
    # snapshot) must never print a slope line for data that isn't there.
    us = make_unit_stats(
        count=25, mean_duration=0.05,
        first_window_n=100, first_window_mean_duration=0.01,
        last_window_n=100, last_window_mean_duration=0.02,
        first_window_mean_queries=None, last_window_mean_queries=None,
    )
    run = make_run(wall_time=10.0, cpu_time=None, db_time=1.0, unit_stats=us)

    findings = evaluate(run)
    r6 = [f for f in findings if f.rule == "R6"]
    assert len(r6) == 1
    assert "queries/unit" not in r6[0].detail


# ==========================================================================
# D11 -- a SCALE-triggered R4 must survive the final %-of-wall trivia filter
# ==========================================================================
# Regression for a defect reproduced deterministically on CI (all 4 Python
# versions): R4 fired correctly via its absolute SCALE trigger and was then
# silently DELETED by evaluate()'s closing suppression filter, which kept a
# finding only if `f.rule == "R6" or f.seconds >= 0.05 * wall or
# f.seconds >= KEEP_ABS_SECONDS`. R4's seconds are the cluster's combined_time,
# which is small by construction on a bounded/sampled run -- that is the exact
# reason the scale trigger exists. Numbers below mirror the observed CI run of
# demo/benchmark.py's unit section:
#     GitHub CI runner: combined_time=0.078s, wall=2.653s -> 2.9%  (R4 DELETED)
#     local Mac:        combined_time=0.232s, wall=3.571s -> 6.5%  (R4 survived)
# i.e. the same byte-identical workload reported a different rule set purely as
# a function of disk speed. Not the findings[:3] cap: only 2 findings existed.

_R4_CI_SITE_SELECT = ("demo/benchmark.py", 120, "process_one_unit")
_R4_CI_SITE_UPDATE = ("demo/benchmark.py", 123, "process_one_unit")
_R4_CI_SITE_INSERT = ("demo/benchmark.py", 127, "process_one_unit")

_R4_CI_WALL = 2.653          # observed CI wall clock
_R4_CI_COMBINED_TIME = 0.078  # observed CI cluster combined_time (2.9% of wall)


def _r4_scale_triggered_under_the_bar_run():
    """A cluster that fires via SCALE alone while sitting UNDER 5%% of wall.

    3 distinct source lines of one function x 400 iterations each:
      - combined_calls = 1200 (> the 1000 gate)
      - iterations = 400 (>= 200) and per_iter = 3.0 (>= 3) -> scale trigger ON
      - 3 distinct lines -> NOT the D6 flush signature, so scale alone promotes
      - combined_time = 0.078s, which is BELOW 10%% of wall (0.2653s) and below
        10%% of variable_wall (no calls==1 groups exist, so variable_wall ==
        wall) -> the wall%% trigger is OFF; scale is the ONLY reason R4 fires.
    """
    per_group_time = _R4_CI_COMBINED_TIME / 3
    groups = [
        make_group(key="select_unit", normalized_sql="SELECT * FROM chain_state WHERE id = ?",
                   calls=400, total_time=per_group_time, median=0.00006,
                   call_site=_R4_CI_SITE_SELECT),
        make_group(key="update_unit", normalized_sql="UPDATE chain_state SET state = ? WHERE id = ?",
                   calls=400, total_time=per_group_time, median=0.00006,
                   call_site=_R4_CI_SITE_UPDATE),
        make_group(key="insert_audit", normalized_sql="INSERT INTO audit_log VALUES (?, ?, ?)",
                   calls=400, total_time=per_group_time, median=0.00006,
                   call_site=_R4_CI_SITE_INSERT),
    ]
    return make_run(
        wall_time=_R4_CI_WALL, cpu_time=2.3, total_queries=1200,
        db_time=_R4_CI_COMBINED_TIME, groups=groups,
    )


def test_d11_scale_triggered_r4_survives_the_wall_pct_trivia_filter():
    run = _r4_scale_triggered_under_the_bar_run()
    wall = run.wall_time

    findings = evaluate(run)
    r4 = [f for f in findings if f.rule == "R4"]
    assert len(r4) == 1, (
        f"a scale-triggered R4 must survive the final trivia filter, "
        f"got {[f.rule for f in findings]}"
    )

    finding = r4[0]
    # The POINT of the regression: it is present *while* below the bar that
    # used to delete it. Asserting only "R4 in findings" would silently pass
    # again if someone re-tuned the numbers instead of exempting the rule.
    assert finding.seconds == pytest.approx(_R4_CI_COMBINED_TIME)
    assert finding.seconds < 0.05 * wall, (
        f"fixture no longer exercises the defect: {finding.seconds:.4f}s is not "
        f"below the 5%% trivia bar ({0.05 * wall:.4f}s)"
    )
    assert finding.seconds < KEEP_ABS_SECONDS, (
        "fixture no longer exercises the defect: the absolute escape hatch "
        "would have kept this finding regardless of the %-of-wall bar"
    )
    # ...and while the wall%% FIRING path is off too, so scale is provably the
    # only reason it fired at all.
    assert finding.seconds <= 0.10 * wall

    # The other half of the CI symptom: nothing else in this fixture fires, so
    # R4 is the whole difference between a useful report and an empty one --
    # and this is NOT the findings[:3] cap (there is only one finding).
    assert [f.rule for f in findings] == ["R4"], (
        f"expected exactly ['R4'], got {[f.rule for f in findings]}"
    )
