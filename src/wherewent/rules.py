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
        if not (combined_calls > 1000 and combined_time > 0.10 * wall):
            continue

        gs_sorted = sorted(gs, key=lambda g: g.total_time, reverse=True)
        site_str = _site_str(gs_sorted[0])
        detail_lines = [f"{len(gs_sorted)} query groups fire together from {site_str} in {site[2]}:"]
        for g in gs_sorted:
            detail_lines.append(f"  - {g.normalized_sql} ({g.calls:,} calls, {g.total_time:.1f}s)")
        detail_lines.append(
            f"combined: {combined_calls:,} calls, {combined_time:.1f}s "
            f"= {combined_time / wall * 100:.0f}% of {wall:.1f}s wall."
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

    # suppress trivia, sort, cap at 3
    findings = [f for f in findings if f.seconds >= 0.05 * wall]
    findings.sort(key=lambda f: f.seconds, reverse=True)
    return findings[:3]
