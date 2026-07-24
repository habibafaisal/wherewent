"""Deterministic heuristics that turn a RunSnapshot into human findings.

Every threshold and merge rule here is part of the build contract. Numbers must
stay honest: a rule only fires when its inputs are actually measurable.
"""

from .stats import Finding, RunSnapshot

# --- module constants (v0.3 hardening contract) -------------------------------
# R5 must not inherit the scale-blindness R4 had: a 20.7s one-shot is 25% of a
# bounded sample run but <1% of the full run, so a pure "% of wall" gate goes
# SILENT on exactly the run that matters. An absolute floor keeps it honest.
R5_ABS_SECONDS = 10.0     # one-shot heavyweight absolute floor (seconds)
KEEP_ABS_SECONDS = 10.0   # trivia-filter escape hatch: keep anything this big

# D11: rules whose entire claim is about SCALE/TREND rather than about owning
# THIS bounded run's clock. Asking them to clear a "% of current wall" bar asks
# the wrong question by construction, so the final trivia filter must not delete
# them -- their own firing gates are what decide whether they are real. See the
# long D11 note at the filter itself.
_WALL_PCT_EXEMPT_RULES = frozenset({"R4", "R6"})


def _exempt_from_wall_pct_bar(finding) -> bool:
    """True when the trivia filter's %-of-wall bar must not apply to *finding*."""
    try:
        return finding.rule in _WALL_PCT_EXEMPT_RULES
    except Exception:
        return False

# SQL shapes that an ORM unit-of-work emits at flush()/commit() time. Used only
# by R4's flush-attribution guard (D6) -- deliberately simple prefix matching;
# perfect flush detection is out of scope.
_WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE")

# A cluster counts as "write dominated" when this share of its calls are writes.
_WRITE_DOMINANCE = 0.80


def _site_str(group) -> str:
    if group.call_site:
        f, ln, _fn = group.call_site
        return f"{f}:{ln}"
    return "unknown"


def _site_line(group):
    """The group's OWN source line, or None when unresolved."""
    try:
        if group.call_site:
            return group.call_site[1]
    except Exception:
        pass
    return None


def _is_write(sql) -> bool:
    try:
        return sql.strip().upper().startswith(_WRITE_PREFIXES)
    except Exception:
        return False


def _median(values) -> float:
    """Median of *values*; 0.0 for an empty input. Never raises."""
    try:
        vals = sorted(values)
    except Exception:
        return 0.0
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


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

    # D8: when the process is compute-bound, R4/R6 must NOT imply that the SQL
    # is the current wall-clock bottleneck. They still matter -- both scale with
    # input size -- so frame them as a SCALABILITY risk, honestly.
    cpu_caveat = None
    if cpu_busy is not None and cpu_busy >= 0.5:
        cpu_caveat = (
            f"This run is CPU-bound (CPU busy {cpu_busy * 100:.0f}%): this pattern is a "
            f"SCALABILITY risk that will dominate at full volume, NOT the current "
            f"wall-clock bottleneck. Fixing it speeds up large runs, not this sample."
        )

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
    # per-group bars yet obviously bad in aggregate. Cluster by call site,
    # THEN threshold the cluster.
    #
    # D5: the cluster KEY is (file, function) -- NOT (file, line, function).
    # Real helper code issues its co-occurring statements on several different
    # source LINES of ONE function (a SELECT on line 88, an UPDATE on 90, an
    # audit INSERT on 93). Keying on the exact line fragmented that single N+1
    # workflow into three sub-threshold clusters, so R4 never fired on the very
    # pattern it exists to catch. Each group's own line is still kept for
    # DISPLAY (the representative site is the dominant group's own file:line).
    #
    # D6 (companion guard, same "coarsening merges unrelated groups" family):
    # one-shot (calls == 1) statements are fixed cost by this module's own
    # definition -- they feed `fixed_cost_time` above and are R5's job. They do
    # NOT co-occur per iteration, so a big loop plus the DDL/PRAGMA/count(*)
    # that happen to live in the same function must not be reported as "N query
    # groups fire together". Exclude them from clustering entirely.
    clusters: "dict[tuple, list]" = {}
    for g in run.groups:
        if g.call_site is None:
            continue  # can't attribute -> can't cluster
        if g.calls <= 1:
            continue  # one-shot fixed cost, not a co-occurring per-iteration group
        clusters.setdefault((g.call_site[0], g.call_site[2]), []).append(g)

    r1_absorbed = False
    for (site_file, site_func), gs in clusters.items():
        distinct = {g.key for g in gs}
        if len(distinct) < 2:
            continue  # single-group hotspots are R1's job, not R4's
        combined_calls = sum(g.calls for g in gs)
        combined_time = sum(g.total_time for g in gs)
        # D12: estimate the ITERATION COUNT from the MEDIAN of the cluster's
        # per-group call counts, not the max. `max` silently assumes every
        # group fires AT MOST ONCE per iteration; when one group fires SEVERAL
        # times per iteration -- precisely the growing-N+1 shape R4 exists to
        # catch -- that group's call count inflates the denominator and
        # DEFLATES per_iter, so the estimator is least accurate exactly when
        # the pattern is worst and R4 under-claims itself out of firing. In a
        # genuine co-occurring workflow most groups fire about once per
        # iteration, so the median group's call count estimates how many times
        # the pattern repeated: the max is skewed UP by any group that fires
        # several times per iteration, the min is skewed DOWN by any group that
        # fires only conditionally, and the median resists both.
        # Observed on CI (demo unit fixture, 400 units): calls
        # [400, 400, 400, 720] -> max gives 720 iterations x 2.67 queries
        # (scale trigger OFF, R4 silent); the median gives the TRUE 400
        # iterations x 4.8 queries (scale trigger ON). Rounded to a whole
        # number so the ratio and the "~N iterations" it is rendered with stay
        # arithmetically consistent. The >=200 / >=3 bars are UNCHANGED -- this
        # corrects the estimator, it does not lower a threshold.
        iterations = int(round(_median([g.calls for g in gs])))
        per_iter = combined_calls / iterations if iterations > 0 else 0

        # D6: coarsening the key to (file, function) can newly MERGE unrelated
        # statements -- e.g. a direct session.execute(select(...)) on line 60
        # plus the writes a session.commit() flushes on line 88. Distinguish
        # the two honestly using the distinct SOURCE LINES in the cluster:
        #   >= 2 lines            -> a genuine multi-line workflow (the D5 case)
        #   1 line + write-shaped -> the ORM unit-of-work flush signature
        # A single-line write cluster is NOT told to "collapse into one
        # round-trip" (it is already one flush) and is not promoted by the pure
        # scale trigger alone -- it must also clear a wall% bar.
        distinct_lines = {ln for ln in (_site_line(g) for g in gs) if ln is not None}
        write_calls = sum(g.calls for g in gs if _is_write(g.normalized_sql))
        write_dominated = combined_calls > 0 and write_calls >= _WRITE_DOMINANCE * combined_calls
        flush_signature = len(distinct_lines) <= 1 and write_dominated

        # FIX R4: fire on SCALE, not just this run's wall%. A cluster that is a
        # small share of the raw clock can still be the dominant cost at full
        # volume: it may clear 10% once one-time setup is excluded, or it may be
        # obviously per-iteration (>=200 iterations, >=3 queries each).
        wall_pct_trigger = (
            combined_time > 0.10 * wall                      # raw wall%
            or combined_time > 0.10 * variable_wall          # setup-excluded wall%
        )
        scale_trigger = iterations >= 200 and per_iter >= 3  # absolute scale trigger
        qualifies = combined_calls > 1000 and (
            wall_pct_trigger or (scale_trigger and not flush_signature)
        )
        if not qualifies:
            continue

        gs_sorted = sorted(gs, key=lambda g: g.total_time, reverse=True)
        site_str = _site_str(gs_sorted[0])   # dominant group's OWN file:line
        excluded_pct = combined_time / variable_wall * 100
        detail_lines = [
            f"{len(gs_sorted)} query groups fire together from {site_str} in {site_func}:"
        ]
        for g in gs_sorted:
            ln = _site_line(g)
            where = f" at {site_file}:{ln}" if ln is not None else ""
            detail_lines.append(
                f"  - {g.normalized_sql} ({g.calls:,} calls, {g.total_time:.1f}s){where}"
            )
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
        # D12: this block is a SECOND, max-based reading of the same question the
        # line above already answers from the median. While both used the max
        # they always agreed; now they can disagree (calls [1000, 1000, 100, 100]
        # renders "~= 4.0 queries/iteration across ~550 iterations" from the
        # median and "~= 2.2 queries per iteration (~1,000 iterations)" from the
        # max -- two different answers in one report). Emit it only when the two
        # readings AGREE, i.e. when the max IS the median-based estimate. This
        # changes no threshold and no firing gate: it only drops a display line
        # that would otherwise contradict the estimate this rule actually used.
        if max_calls > 0 and len(near_equal) >= 2 and max_calls == iterations:
            per_iter = combined_calls / max_calls
            detail_lines.append(
                f"~= {per_iter:.1f} queries per iteration (~{max_calls:,} iterations)."
            )

        if flush_signature:
            # Honesty over cleverness: never tell a user to merge round-trips
            # that a single flush already merged.
            detail_lines.append(
                "Note: these statements share one call site and may be emitted by a single "
                "session.flush()/commit() (ORM unit-of-work) — verify they are not already "
                "batched before splitting."
            )
            detail_lines.append(
                f"{len(gs_sorted)} write shapes are emitted together at {site_str} "
                f"-> if they already leave the app in one flush, the win is FEWER/BULKIER "
                f"statements per unit of work (bulk insert / bulk update), not merging "
                f"round-trips."
            )
        else:
            detail_lines.append(
                f"{len(gs_sorted)} queries fire together per iteration from {site_str} "
                f"-> collapse into one round-trip (JOIN / batch / eager-load)."
            )
        if cpu_caveat is not None:
            detail_lines.append(cpu_caveat)
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
    # A single calls==1 statement over 15% of wall OR over R5_ABS_SECONDS in
    # absolute terms. Invisible to R1/R3/R4, which all assume chattiness.
    # Independent & additive: never merged or suppressed against R1-R4; ranks in
    # top-3 by seconds like any other finding.
    #
    # D7: the absolute floor exists because a percentage-only gate is
    # scale-blind in the opposite direction to R4's bug -- a 20.7s preload is
    # 25% of a bounded sample run but <1% of the full run, so a %-only R5 goes
    # silent on the run that actually matters.
    try:
        one_shots = [g for g in run.groups if g.calls == 1]
        if one_shots:
            g = max(one_shots, key=lambda x: x.total_time)
            over_pct = g.total_time > 0.15 * wall
            over_abs = g.total_time > R5_ABS_SECONDS
            if over_pct or over_abs:
                where = _site_str(g) if g.call_site else g.normalized_sql
                pct = g.total_time / wall * 100
                if over_pct:
                    arithmetic = f"= {pct:.0f}% of {wall:.1f}s wall."
                else:
                    # absolute path: a small % but a large number of seconds --
                    # say both, so the number reads honestly.
                    arithmetic = (
                        f"= {pct:.0f}% of wall ({g.total_time:.1f}s absolute, over the "
                        f"{R5_ABS_SECONDS:.0f}s floor) on a {wall:.1f}s wall."
                    )
                detail = (
                    f"1 statement at {where} took {g.total_time:.1f}s "
                    f"{arithmetic}\n"
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
            # D9: duration is only half the story. A CPU-bound job whose
            # queries/unit climbs is exactly the shape reviewers care about, and
            # a duration-only trigger misses it. Read the query windows
            # DEFENSIVELY -- they are appended fields and may be absent on an
            # older snapshot.
            first = getattr(us, "first_window_mean_duration", None)
            last = getattr(us, "last_window_mean_duration", None)
            first_q = getattr(us, "first_window_mean_queries", None)
            last_q = getattr(us, "last_window_mean_queries", None)

            dur_present = first is not None and last is not None and first > 0
            q_present = first_q is not None and last_q is not None and first_q > 0
            dur_ok = dur_present and last >= 1.5 * first
            q_ok = q_present and last_q >= 1.5 * first_q

            if us.count >= 20 and (dur_ok or q_ok):
                parts: "list[str]" = []
                if dur_present:
                    delta_pct = (last - first) / first * 100
                    parts.append(
                        f"first 100 units averaged {first * 1000:.0f} ms; "
                        f"last 100 averaged {last * 1000:.0f} ms ({delta_pct:+.0f}%)."
                    )
                if q_present:
                    q_pct = (last_q - first_q) / first_q * 100
                    verb = "rose" if q_pct >= 0 else "fell"
                    parts.append(
                        f"queries/unit {verb} from {first_q:.0f} (first 100) to "
                        f"{last_q:.0f} (last 100) ({q_pct:+.0f}%)."
                    )
                parts.append(
                    "Cost per unit is climbing -- likely growing per-item work "
                    "(accumulating state, unbatched history reads, or a list that "
                    "grows each iteration)."
                )
                if cpu_caveat is not None:
                    parts.append(cpu_caveat)
                detail = " ".join(parts)

                # D10: PREFER the query-derived attribution over the
                # duration-derived one whenever it is computable. Per-unit query
                # counts are exact integers the recorder attributes directly,
                # with zero clock involvement, so the same fixture yields the
                # same number every run. `first_window_mean_duration` is NOT a
                # clean baseline: the first window pays one-time SQLAlchemy
                # statement-compilation warmup, which is not part of the
                # steady-state per-unit price. That inflates the baseline and so
                # biases the attributed seconds LOW by construction -- enough,
                # on real runs, to push R6 under the trivia filter on some runs
                # and not others for a byte-identical workload. Duration stays
                # as the fallback for snapshots with no query windows.
                dur_seconds = None
                if first is not None and first > 0:
                    dur_seconds = max(0.0, (us.mean_duration - first) * us.count)

                q_seconds = None
                try:
                    if q_present:
                        # Conservative pricing: the EXTRA queries per unit above
                        # the first window, charged at the run's mean per-query
                        # DB time. Never negative, never a crash.
                        per_q = (
                            (run.db_time / run.total_queries) if run.total_queries > 0 else 0.0
                        )
                        excess = max(0.0, (us.mean_queries - first_q)) * us.count
                        q_seconds = max(0.0, excess * per_q)
                except Exception:
                    q_seconds = None

                if q_seconds is not None and q_seconds > 0.0:
                    seconds = q_seconds
                elif dur_seconds is not None:
                    seconds = dur_seconds
                else:
                    seconds = 0.0
                findings.append(Finding("R6", "Rising per-unit cost", detail, seconds))
        except Exception:
            pass

    # suppress trivia, sort, cap at 3.
    # D7: the "< 5% of wall" bar would re-suppress an absolute-floor R5 (or any
    # genuinely large finding) on a huge run, so anything big in ABSOLUTE terms
    # survives too.
    #
    # D10: R6 is a TREND finding, so a share-of-CURRENT-wall bar is the wrong
    # question to ask it. Its whole claim is that the per-unit price is rising
    # and will dominate at full volume, NOT that it owns this bounded run's
    # clock -- its current-run magnitude is small by construction. Gating it on
    # 5% of wall meant a real, reproducible per-unit regression went silent
    # whenever the sample run happened to be short. Same spirit as D7's absolute
    # escape hatch: don't let a %-of-wall bar silence a scale signal. R6's own
    # 1.5x firing bar remains the thing that decides whether it is real.
    #
    # D11: R4 is the SAME shape of finding as R6 and needs the SAME treatment.
    # R4 deliberately fires on an absolute SCALE trigger (>= 200 iterations x
    # >= 3 co-located queries each) precisely so that an N+1 spread across
    # several query groups is reported even when it is a small share of a
    # bounded run's clock -- on a sampled or partial run, one-time fixed costs
    # own the clock while the per-iteration pattern is the thing that scales.
    # Re-applying the generic 5%-of-wall bar here re-imposed the very bar the
    # scale trigger exists to bypass and SILENTLY DELETED the finding after it
    # had correctly fired (observed on CI: combined_time 0.078s on a 2.653s
    # wall = 3.0%, R4 computed then dropped; the same fixture kept R4 locally at
    # 6.5%, so the bug was disk-speed-dependent). An R4 that qualified via the
    # wall% path already clears this bar on its own, so exempting R4 outright
    # changes nothing for it and only rescues the scale-triggered case. R4's own
    # firing gates (combined_calls > 1000, the calls<=1 one-shot exclusion, and
    # the D6 flush-attribution guard) remain the things that decide whether it
    # is real. Same defect family as D7 (R5's absolute escape hatch) and D10.
    findings = [
        f for f in findings
        if _exempt_from_wall_pct_bar(f)
        or f.seconds >= 0.05 * wall
        or f.seconds >= KEEP_ABS_SECONDS
    ]
    findings.sort(key=lambda f: f.seconds, reverse=True)
    return findings[:3]
