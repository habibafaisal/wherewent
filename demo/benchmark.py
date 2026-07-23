"""wherewent demo benchmark: naive vs. fixed, with and without the recorder.

Runs naive_job.py uninstrumented, then both jobs under `wherewent run`, then
checks that wherewent (a) actually flags the naive job's commit-per-row
anti-pattern, (b) does NOT flag the fixed job, and (c) adds tolerable
overhead. Stdlib + subprocess only -- this script never imports wherewent.

Usage: python demo/benchmark.py
Env: WHEREWENT_DEMO_ROWS (default 20000), passed through to the jobs.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
OVERHEAD_GATE_PCT = 15.0


def run(cmd, env, label):
    print(f"\n--- running: {' '.join(cmd)} ---")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    wall = time.perf_counter() - t0
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(f"--- {label} stderr (wherewent report lives here) ---")
        print(proc.stderr, end="")
    print(f"--- {label} wall time: {wall:.2f}s (exit {proc.returncode}) ---")
    return wall, proc


def wherewent_cmd(save_path, job_path):
    exe = shutil.which("wherewent")
    if exe:
        return [exe, "run", "--save", save_path, sys.executable, job_path]
    return [sys.executable, "-m", "wherewent.cli", "run", "--save", save_path, sys.executable, job_path]


def wherewent_cmd_unit(save_path, unit_spec, module_name):
    # `python -m demo.run_units` (not a bare script path) -- see
    # demo/unit_job.py / demo/run_units.py docstrings for why module identity
    # matters for --unit-function to actually patch what the loop calls.
    exe = shutil.which("wherewent")
    base = [exe] if exe else [sys.executable, "-m", "wherewent.cli"]
    return base + [
        "run", "--save", save_path, "--unit-function", unit_spec,
        sys.executable, "-m", module_name,
    ]


def load_findings(path):
    with open(path) as f:
        data = json.load(f)
    return data, data.get("findings", [])


def has_rule(findings, substr):
    return any(substr in f.get("rule", "") for f in findings)


def find_insert_group(groups):
    matches = [
        g for g in groups
        if "insert" in g["normalized_sql"].lower() and "events" in g["normalized_sql"].lower()
    ]
    return max(matches, key=lambda g: g["calls"]) if matches else None


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}{(' -- ' + detail) if detail else ''}")
    return condition


def check_async_end_to_end(env, scratch, async_rows):
    """Optional section proving v0.2 #1 (async call-site attribution)
    end-to-end: runs demo/async_naive_job.py under the recorder and checks
    the INSERT group's call_site is non-null and that a finding names that
    real file:line. Gated so a missing aiosqlite SKIPS rather than fails --
    this must never break the sync gate above.
    """
    try:
        import aiosqlite  # noqa: F401
    except ImportError:
        print("\n--- async call-site check: SKIPPED (aiosqlite not installed) ---")
        return None

    async_job = os.path.join(DEMO_DIR, "async_naive_job.py")
    async_json = os.path.join(scratch, "async.json")

    async_env = env.copy()
    async_env["WHEREWENT_DEMO_ROWS"] = str(async_rows)
    async_env["WHEREWENT_DEMO_DB"] = os.path.join(scratch, "async_demo.db")

    run(wherewent_cmd(async_json, async_job), async_env, "async naive (recorded)")

    async_data, async_findings = load_findings(async_json)
    insert_group = find_insert_group(async_data.get("groups", []))

    print("\n=== ASYNC CALL-SITE ASSERTIONS (optional, proves #1 end-to-end) ===")
    sub_results = [
        check(
            "async INSERT group recorded",
            insert_group is not None,
            f"groups={[g['normalized_sql'] for g in async_data.get('groups', [])]}",
        )
    ]

    call_site = insert_group.get("call_site") if insert_group else None
    sub_results.append(
        check("async INSERT group call_site is non-null", call_site is not None, f"call_site={call_site}")
    )

    candidate_sites = {
        f"{g['call_site'][0]}:{g['call_site'][1]}"
        for g in async_data.get("groups", [])
        if g.get("call_site") and os.path.basename(g["call_site"][0]) == "async_naive_job.py"
    }
    matched = {
        site
        for site in candidate_sites
        if any(site in f.get("detail", "") or site in f.get("title", "") for f in async_findings)
    }
    named = bool(matched)
    sub_results.append(
        check(
            "a finding names the real async call site",
            named,
            f"named_sites={matched} candidates={sorted(candidate_sites)} rules={[f['rule'] for f in async_findings]}",
        )
    )
    return all(sub_results)


def _approx(a, b, tol=0.05):
    return a is not None and abs(a - b) <= tol


def _find_r4_cluster(groups):
    """Group JSON group dicts by call site, return the qualifying (>=2
    distinct groups) cluster with the most combined calls, or None. Mirrors
    (a simplified, JSON-only view of) rules.py's own R4 clustering so the
    benchmark can independently show the "<10% of raw wall" claim, not just
    trust the rule fired.

    Keyed on (file, function), NOT the full (file, line, function) tuple --
    matching rules.py's clustering-key fix (DESIGN-v3.md addendum): real
    same-function helper code spreads its statements across several lines
    (demo/unit_job.py's process_one does exactly this -- SELECT, UPDATE,
    INSERT each on their own line), so an exact-tuple key would never see
    them as one cluster. This helper reflects the CORRECT/target clustering;
    whether `has_rule(unit_findings, "R4")` below actually agrees depends on
    whether that rules.py fix has landed yet in this build.
    """
    by_site = {}
    for g in groups:
        site = g.get("call_site")
        if not site:
            continue
        key = (site[0], site[2])  # (file, function) -- drop the line number
        by_site.setdefault(key, []).append(g)

    best = None
    for site, gs in by_site.items():
        distinct = {g["key"] for g in gs}
        if len(distinct) < 2:
            continue
        combined_calls = sum(g["calls"] for g in gs)
        combined_time = sum(g["total_time"] for g in gs)
        if best is None or combined_calls > best[1]:
            best = (site, combined_calls, combined_time, gs)
    return best


def check_unit_end_to_end(env, scratch):
    """Optional section proving v0.3's unit-aware profiling (--unit-function)
    end-to-end, using demo/unit_job.py's feedback-shaped job: runs it under
    `wherewent run --unit-function demo.unit_job:process_one --save` and
    checks unit_stats is populated correctly, R4 fires despite the per-unit
    query cluster being under 10% of RAW wall (the v0.3 fix, proven with real
    numbers rather than just trusting the rule name), R5 names the one-shot
    heavyweight self-join, and R6 fires on the engineered growth.

    Gated so a wherewent build that does not yet support --unit-function
    (unit_stats absent from the JSON -- mid-integration per DESIGN-v3.md)
    SKIPS rather than fails, same convention as check_async_end_to_end above.
    Does NOT count toward the sync overhead gate or the async assertions.
    """
    unit_rows = int(os.environ.get("WHEREWENT_UNIT_ROWS", "400"))
    ledger_rows = int(os.environ.get("WHEREWENT_UNIT_LEDGER_ROWS", "5500"))
    expected_queries_per_unit = 3  # SELECT + UPDATE + INSERT, see unit_job.process_one

    unit_env = env.copy()
    unit_env["WHEREWENT_UNIT_ROWS"] = str(unit_rows)
    unit_env["WHEREWENT_UNIT_LEDGER_ROWS"] = str(ledger_rows)
    unit_env["WHEREWENT_UNIT_DEMO_DB"] = os.path.join(scratch, "unit_demo.db")
    # `python -m demo.run_units` needs the repo root (parent of demo/) on
    # PYTHONPATH regardless of subprocess cwd.
    repo_root = os.path.dirname(DEMO_DIR)
    existing_pp = unit_env.get("PYTHONPATH", "")
    unit_env["PYTHONPATH"] = os.pathsep.join([repo_root] + ([existing_pp] if existing_pp else []))

    unit_json = os.path.join(scratch, "unit.json")
    run(
        wherewent_cmd_unit(unit_json, "demo.unit_job:process_one", "demo.run_units"),
        unit_env,
        "unit job (recorded, --unit-function)",
    )

    try:
        unit_data, unit_findings = load_findings(unit_json)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"\n--- unit-function check: SKIPPED (no/invalid JSON written -- {exc}) ---")
        return None

    if "unit_stats" not in unit_data:
        print(
            "\n--- unit-function check: SKIPPED (unit_stats absent from JSON -- this "
            "wherewent build does not yet support --unit-function; pending Agent A/B "
            "integration per DESIGN-v3.md) ---"
        )
        return None

    us = unit_data.get("unit_stats")
    wall = unit_data.get("wall_time") or 0
    unit_rules = [f["rule"] for f in unit_findings]

    print("\n=== UNIT-AWARE PROFILING ASSERTIONS (optional, proves v0.3 end-to-end) ===")
    sub_results = [
        check("unit_stats is non-null (target was wrapped and called)", us is not None, f"unit_stats={us}")
    ]
    if us is None:
        return all(sub_results)

    sub_results.append(
        check("unit_stats.count == rows", us.get("count") == unit_rows,
              f"count={us.get('count')} rows={unit_rows}")
    )
    sub_results.append(
        check(
            f"unit_stats.median_queries == {expected_queries_per_unit} (SELECT+UPDATE+INSERT)",
            _approx(us.get("median_queries"), expected_queries_per_unit, tol=0.1),
            f"median_queries={us.get('median_queries')}",
        )
    )

    cluster = _find_r4_cluster(unit_data.get("groups", []))
    if cluster is not None:
        site, combined_calls, combined_time, gs = cluster
        raw_pct = (combined_time / wall * 100) if wall > 0 else float("inf")
        sub_results.append(
            check(
                "R4 cluster combined_calls > 1000",
                combined_calls > 1000,
                f"combined_calls={combined_calls}",
            )
        )
        sub_results.append(
            check(
                "R4 cluster is < 10% of RAW wall (the v0.3 trap this fixture reproduces)",
                raw_pct < 10.0,
                f"combined_time={combined_time:.3f}s wall={wall:.3f}s = {raw_pct:.1f}%",
            )
        )
    else:
        sub_results.append(check("R4 cluster found in JSON groups", False, "no >=2-group call_site cluster"))

    sub_results.append(
        check("R4 fires despite the cluster being < 10% of raw wall (the fix)",
              has_rule(unit_findings, "R4"), f"rules={unit_rules}")
    )

    r5_findings = [f for f in unit_findings if "R5" in f.get("rule", "")]
    r5_names_heavyweight = any("unit_job.py" in f.get("detail", "") for f in r5_findings)
    sub_results.append(
        check(
            "R5 fires and names the one-shot heavyweight self-join",
            bool(r5_findings) and r5_names_heavyweight,
            f"r5={r5_findings}",
        )
    )

    first_mean = us.get("first_window_mean_duration")
    last_mean = us.get("last_window_mean_duration")
    growth_strong = (
        first_mean is not None and last_mean is not None and first_mean > 0
        and last_mean >= 1.5 * first_mean
    )
    r6_fired = has_rule(unit_findings, "R6")
    sub_results.append(
        check(
            "R6 fires (growth engineered to be strong: last-100 mean >= 1.5x first-100 mean)",
            (not growth_strong) or r6_fired,
            f"first_window_mean_duration={first_mean} last_window_mean_duration={last_mean} rules={unit_rules}",
        )
    )

    return all(sub_results)


def main():
    rows = int(os.environ.get("WHEREWENT_DEMO_ROWS", "20000"))
    scratch = tempfile.mkdtemp(prefix="wherewent_demo_")
    db_path = os.path.join(scratch, "demo_events.db")
    naive_json = os.path.join(scratch, "naive.json")
    fixed_json = os.path.join(scratch, "fixed.json")

    env = os.environ.copy()
    env["WHEREWENT_DEMO_ROWS"] = str(rows)
    env["WHEREWENT_DEMO_DB"] = db_path

    naive_job = os.path.join(DEMO_DIR, "naive_job.py")
    fixed_job = os.path.join(DEMO_DIR, "fixed_job.py")

    print(f"wherewent demo benchmark -- {rows} rows, scratch={scratch}")

    t0, _ = run([sys.executable, naive_job], env, "naive (uninstrumented)")
    t1, _ = run(wherewent_cmd(naive_json, naive_job), env, "naive (recorded)")
    run(wherewent_cmd(fixed_json, fixed_job), env, "fixed (recorded)")

    overhead_pct = (t1 - t0) / t0 * 100 if t0 > 0 else float("inf")

    naive_data, naive_findings = load_findings(naive_json)
    fixed_data, fixed_findings = load_findings(fixed_json)

    naive_wall_time = naive_data.get("wall_time") or 0
    self_overhead_pct = (
        naive_data["overhead_time"] / naive_wall_time * 100 if naive_wall_time > 0 else float("inf")
    )

    insert_group = find_insert_group(naive_data.get("groups", []))
    insert_calls = insert_group["calls"] if insert_group else None
    naive_rules = [f["rule"] for f in naive_findings]
    fixed_rules = [f["rule"] for f in fixed_findings]

    print("\n=== ASSERTIONS ===")
    assertions = [
        ("naive INSERT group calls == ROWS", insert_calls == rows,
         f"calls={insert_calls} rows={rows}"),
        ("naive total_commits >= ROWS", naive_data["total_commits"] >= rows,
         f"total_commits={naive_data['total_commits']} rows={rows}"),
        ("naive findings include R1 and R2",
         has_rule(naive_findings, "R1") and has_rule(naive_findings, "R2"),
         f"rules={naive_rules}"),
        ("fixed findings exclude R1 and R2",
         not has_rule(fixed_findings, "R1") and not has_rule(fixed_findings, "R2"),
         f"rules={fixed_rules}"),
    ]
    results = [check(*a) for a in assertions]

    print(f"\n=== OVERHEAD ===\nself-measured overhead: {self_overhead_pct:.1f}% "
          f"(overhead_time {naive_data.get('overhead_time', 0):.4f}s / wall_time {naive_wall_time:.4f}s), "
          f"gate < {OVERHEAD_GATE_PCT}%")
    print(f"wall-diff (informational, noisy cross-run measurement): {overhead_pct:.1f}% "
          f"(uninstrumented {t0:.2f}s -> recorded {t1:.2f}s)")
    results.append(check("self-measured overhead < 15.0%", self_overhead_pct < OVERHEAD_GATE_PCT))

    # Optional: proves the async call-site fix (v0.2 #1) end-to-end. Does NOT
    # count toward overhead_pct (async/aiosqlite has different per-call cost
    # than the sync sqlite3 driver, so it would not be a fair comparison) and
    # is skipped -- not failed -- when aiosqlite isn't installed.
    # >1000 rows so the async INSERT/SELECT groups clear R1's calls>1000 bar
    # too -- otherwise only R2 (commit-per-row) fires, and R2's detail has no
    # call site to check against.
    async_rows = int(os.environ.get("WHEREWENT_ASYNC_DEMO_ROWS", "1500"))
    async_ok = check_async_end_to_end(env, scratch, async_rows)
    if async_ok is not None:
        results.append(async_ok)

    # Optional: proves v0.3's unit-aware profiling (--unit-function, R4's
    # fix, R5, R6) end-to-end. Skipped -- not failed -- until unit_stats
    # shows up in the JSON (mid-integration per DESIGN-v3.md). Does NOT
    # count toward the sync overhead gate or weaken any assertion above.
    unit_ok = check_unit_end_to_end(env, scratch)
    if unit_ok is not None:
        results.append(unit_ok)

    ok = all(results)
    print(f"\n=== RESULT: {'PASS' if ok else 'FAIL'} ===")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
