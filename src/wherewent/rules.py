"""Deterministic heuristics that turn a RunSnapshot into human findings.

Every threshold and merge rule here is part of the build contract. Numbers must
stay honest: a rule only fires when its inputs are actually measurable.
"""

from .stats import Finding, RunSnapshot


def _site_str(group) -> str:
    if group.call_site:
        f, ln, _fn = group.call_site
        return f"{f}:{ln}"
    return "unknown"


def _within(a: float, b: float, frac: float) -> bool:
    """True if a and b are within *frac* of each other (relative)."""
    hi = max(a, b)
    if hi <= 0:
        return False
    return (min(a, b) / hi) >= (1.0 - frac)


def evaluate(run: RunSnapshot) -> "list[Finding]":
    wall = run.wall_time
    if wall <= 0:
        return []

    # One-time (calls==1) statements dominate a bounded run's clock but do NOT
    # scale with input size. Exclude them to get a "variable" baseline against
    # which a scaling N+1 cluster can be judged (R4-fix, below).
    fixed_cost_time = sum(g.total_time for g in run.groups if g.calls == 1)
    variable_wall = max(wall - fixed_cost_time, 1e-9)  # wall minus one-shot setup

    cpu_busy = (run.cpu_time / wall) if (run.cpu_time is not None and wall > 0) else None

    r1 = r1_group = None
    r2 = None
    r3 = None

    # --- R1: chatty query group (worst by total_time) -------------------------
    candidates = [
        g for g in run.groups
        if g.calls > 1000 and g.total_time > 0.10 * wall and g.median < 0.005
    ]
    if candidates:
        g = max(candidates, key=lambda x: x.total_time)
        r1_group = g
        detail = (
            f"{g.calls:,} calls x {g.median * 1000:.2f}ms median "
            f"~= {g.total_time:.1f}s = {g.total_time / wall * 100:.0f}% of {wall:.1f}s wall, "
            f"at {_site_str(g)}. Batch it (executemany / IN-list / JOIN)."
        )
        r1 = Finding("R1", "Chatty query group", detail, g.total_time)

    # --- R2: commit-per-row ---------------------------------------------------
    if (
        run.total_commits > 100
        and run.commit_time is not None
        and (run.total_rows / run.total_commits) < 10
        and run.commit_time > 0.05 * wall
    ):
        avg = run.total_rows / run.total_commits
        detail = (
            f"{run.total_commits:,} commits for {run.total_rows:,} rows "
            f"({avg:.1f} rows/commit), {run.commit_time:.1f}s in commit "
            f"= {run.commit_time / wall * 100:.0f}% of wall. "
            f"Batch to 1,000+ rows per transaction."
        )
        r2 = Finding("R2", "Commit-per-row", detail, run.commit_time)

    # --- R3: DB-wait dominates ------------------------------------------------
    if run.db_time > 0.60 * wall and cpu_busy is not None and cpu_busy < 0.30:
        detail = (
            f"DB {run.db_time:.1f}s of {wall:.1f}s wall "
            f"({run.db_time / wall * 100:.0f}%), CPU busy only {cpu_busy * 100:.0f}% "
            f"-> round-trip bound, not compute bound."
        )
        r3 = Finding("R3", "DB-wait dominates", detail, run.db_time)

    # --- merge / fold ---------------------------------------------------------
    findings: "list[Finding]" = []
    consumed = set()

    # R1 + R2 same-loop merge (calls ~= commits within 25%)
    if r1 is not None and r2 is not None and _within(r1_group.calls, run.total_commits, 0.25):
        consumed.update({"r1", "r2"})
        rule = "R1+R2"
        detail = r1.detail + "\n" + r2.detail
        seconds = r1.seconds + r2.seconds
        if r3 is not None:
            consumed.add("r3")
            rule = "R1+R2+R3"
            detail = detail + "\n" + r3.detail  # R3 overlaps R1's query time; don't add seconds
        findings.append(Finding(rule, "commit-per-row loop", detail, seconds))

    # R3 + exactly one other, where that other explains >= 80% of the DB wait
    if "r3" not in consumed and r3 is not None:
        other = other_name = None
        if r1 is not None and r2 is None:
            other, other_name = r1, "r1"
        elif r2 is not None and r1 is None:
            other, other_name = r2, "r2"
        if other is not None and other.seconds >= 0.8 * run.db_time:
            consumed.update({"r3", other_name})
            if other_name == "r1":
                # R1's query time is a subset of db_time: attribute db_time, no double-count
                seconds = max(other.seconds, r3.seconds)
                rule = "R1+R3"
            else:
                # commit time is disjoint from cursor-execute db_time: additive
                seconds = other.seconds + r3.seconds
                rule = "R2+R3"
            findings.append(Finding(rule, other.title, other.detail + "\n" + r3.detail, seconds))

    # emit any survivors that were not folded into a merge
    for name, f in (("r1", r1), ("r2", r2), ("r3", r3)):
        if f is not None and name not in consumed:
            findings.append(f)

    # --- R4: co-occurring query pattern (cluster groups by call_site) ---------
    # Additive, additional to R1/R2/R3 above: an N+1 pattern spread across
    # several query groups (e.g. SELECT + UPDATE + INSERT all fired once per
    # loop iteration from the same call site) can be individually under R1's
    # per-group bars yet obviously bad in aggregate. Cluster by call_site,
    # THEN threshold the cluster.
    clusters: "dict[tuple, list]" = {}
    for g in run.groups:
        if g.call_site is None:
            continue  # can't attribute -> can't cluster
        clusters.setdefault(g.call_site, []).append(g)

    r1_absorbed = False
    for site, gs in clusters.items():
        distinct = {g.key for g in gs}
        if len(distinct) < 2:
            continue  # single-group hotspots are R1's job, not R4's
        combined_calls = sum(g.calls for g in gs)
        combined_time = sum(g.total_time for g in gs)
        iterations = max(g.calls for g in gs)
        per_iter = combined_calls / iterations if iterations else 0
        # FIX R4: fire on SCALE, not just this run's wall%. A cluster that is a
        # small share of the raw clock can still be the dominant cost at full
        # volume: it may clear 10% once one-time setup is excluded, or it may be
        # obviously per-iteration (>=200 iterations, >=3 queries each).
        qualifies = combined_calls > 1000 and (
            combined_time > 0.10 * wall                      # raw wall%
            or combined_time > 0.10 * variable_wall          # setup-excluded wall%
            or (iterations >= 200 and per_iter >= 3)         # absolute scale trigger
        )
        if not qualifies:
            continue

        gs_sorted = sorted(gs, key=lambda g: g.total_time, reverse=True)
        site_str = _site_str(gs_sorted[0])
        excluded_pct = combined_time / variable_wall * 100
        detail_lines = [f"{len(gs_sorted)} query groups fire together from {site_str} in {site[2]}:"]
        for g in gs_sorted:
            detail_lines.append(f"  - {g.normalized_sql} ({g.calls:,} calls, {g.total_time:.1f}s)")
        detail_lines.append(
            f"combined: {combined_calls:,} calls, {combined_time:.1f}s "
            f"= {combined_time / wall * 100:.0f}% of {wall:.1f}s wall "
            f"({excluded_pct:.0f}% once {fixed_cost_time:.1f}s of one-time setup is excluded)."
        )
        detail_lines.append(
            f"~= {per_iter:.1f} queries/iteration across ~{iterations:,} iterations from {site_str} "
            f"-- this scales linearly with units, so it becomes the dominant cost at full "
            f"volume even though it did not win this bounded run's clock."
        )

        # B2: honest per-iteration estimate, only when the signal is strong
        # (>= 2 groups whose call counts are near-equal, i.e. within 25% of
        # the cluster's max group calls) -- never guess otherwise.
        max_calls = max(g.calls for g in gs_sorted)
        near_equal = [g for g in gs_sorted if _within(g.calls, max_calls, 0.25)]
        if max_calls > 0 and len(near_equal) >= 2:
            per_iter = combined_calls / max_calls
            detail_lines.append(
                f"~= {per_iter:.1f} queries per iteration (~{max_calls:,} iterations)."
            )

        detail_lines.append(
            f"{len(gs_sorted)} queries fire together per iteration from {site_str} "
            f"-> collapse into one round-trip (JOIN / batch / eager-load)."
        )
        findings.append(Finding("R4", "Co-occurring query pattern", "\n".join(detail_lines), combined_time))

        # Prevent double-counting with R1: if R1 fired STANDALONE on a group
        # that lives inside this qualifying cluster, R4 strictly contains
        # that group plus its siblings and tells the bigger story, so drop
        # the standalone R1 (never sum the overlapping seconds). A group
        # already folded into a merged "R1+R2"/"R1+R3"/"R1+R2+R3" story is
        # left untouched -- that merge logic is not R4's to alter.
        if r1_group is not None and r1_group in gs:
            r1_absorbed = True

    if r1_absorbed:
        findings = [f for f in findings if f.rule != "R1"]

    # --- R5: one-shot heavyweight statement -----------------------------------
    # A single calls==1 statement over 15% of wall. Invisible to R1/R3/R4, which
    # all assume chattiness. Independent & additive: never merged or suppressed
    # against R1-R4; ranks in top-3 by seconds like any other finding.
    try:
        one_shots = [g for g in run.groups if g.calls == 1]
        if one_shots:
            g = max(one_shots, key=lambda x: x.total_time)
            if g.total_time > 0.15 * wall:
                where = _site_str(g) if g.call_site else g.normalized_sql
                detail = (
                    f"1 statement at {where} took {g.total_time:.1f}s "
                    f"= {g.total_time / wall * 100:.0f}% of {wall:.1f}s wall.\n"
                    f"It runs once regardless of input size, so R1/R3/R4 (which look for "
                    f"chattiness) miss it -- but it is the single biggest fixed cost. "
                    f"Cache, narrow, or stream it."
                )
                findings.append(
                    Finding("R5", "One-shot heavyweight statement", detail, g.total_time)
                )
    except Exception:
        pass

    # --- R6: rising per-unit cost (unit-aware) --------------------------------
    # Only when unit mode was on. Cost per unit climbing over the run is a strong
    # "growing per-item work" signal that program-wide totals hide entirely.
    try:
        us = run.unit_stats
    except AttributeError:
        us = None
    if us is not None:
        try:
            first = us.first_window_mean_duration
            last = us.last_window_mean_duration
            if (
                us.count >= 20
                and first is not None
                and last is not None
                and first > 0
                and last >= 1.5 * first
            ):
                delta_pct = (last - first) / first * 100
                detail = (
                    f"first 100 units averaged {first * 1000:.0f} ms; "
                    f"last 100 averaged {last * 1000:.0f} ms (+{delta_pct:.0f}%). "
                    f"Cost per unit is climbing -- likely growing per-item work "
                    f"(accumulating state, unbatched history reads, or a list that "
                    f"grows each iteration)."
                )
                seconds = max(0.0, (us.mean_duration - first) * us.count)
                findings.append(Finding("R6", "Rising per-unit cost", detail, seconds))
        except Exception:
            pass

    # suppress trivia, sort, cap at 3
    findings = [f for f in findings if f.seconds >= 0.05 * wall]
    findings.sort(key=lambda f: f.seconds, reverse=True)
    return findings[:3]
