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

    print(f"\n=== OVERHEAD ===\nmeasured overhead: {overhead_pct:.1f}% "
          f"(naive uninstrumented {t0:.2f}s -> recorded {t1:.2f}s), gate < {OVERHEAD_GATE_PCT}%")
    results.append(check(f"overhead_pct < {OVERHEAD_GATE_PCT}%", overhead_pct < OVERHEAD_GATE_PCT))

    ok = all(results)
    print(f"\n=== RESULT: {'PASS' if ok else 'FAIL'} ===")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
