"""Plain-text report renderer (~100 columns, stderr-friendly).

Golden rule: never print a guessed number. Anything unmeasurable prints as "—".
"""

from .stats import Finding, RunSnapshot

_WIDTH = 100
_SQL_COL = 48


def _pct(part: float, whole: float) -> str:
    return f"{part / whole * 100:.1f}%" if whole > 0 else "—"


def _qty(v) -> str:
    """A count printed as an integer when whole, else to 1 decimal; '—' if None."""
    if v is None:
        return "—"
    return f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def render(run: RunSnapshot, findings: "list[Finding]") -> str:
    wall = run.wall_time
    lines = []
    lines.append("=" * _WIDTH)
    lines.append("wherewent — SQL flight recorder")
    lines.append("-" * _WIDTH)

    if run.cpu_time is not None and wall > 0:
        cpu_str = f"{run.cpu_time:.2f}s ({run.cpu_time / wall * 100:.0f}% CPU busy)"
    else:
        cpu_str = "—"
    lines.append(
        f"wall: {wall:.2f}s   cpu: {cpu_str}   "
        f"queries: {run.total_queries:,}   commits: {run.total_commits:,}   "
        f"rollbacks: {run.total_rollbacks:,}"
    )
    lines.append(
        f"in-DB time: {run.db_time:.2f}s ({_pct(run.db_time, wall)} of wall; "
        f"app-observed: includes network+driver+server)"
    )
    commit_str = f"{run.commit_time:.2f}s" if run.commit_time is not None else "—"
    qpc = f"{run.total_queries / run.total_commits:.1f}" if run.total_commits > 0 else "—"
    lines.append(
        f"commit time: {commit_str}   total rows: {run.total_rows:,}   "
        f"queries per commit: {qpc}"
    )
    lines.append(
        f"recording added ~{run.overhead_time:.2f}s (~{_pct(run.overhead_time, wall)} of wall)"
    )
    lines.append("=" * _WIDTH)

    if not run.sqlalchemy_active:
        lines.append("SQLAlchemy was not imported by this process — nothing recorded.")
        lines.append("=" * _WIDTH)
        return "\n".join(lines)

    # --- query group table (top 15 by total app time) -------------------------
    groups = sorted(run.groups, key=lambda g: g.total_time, reverse=True)[:15]
    lines.append(
        f"{'QUERY GROUP':<{_SQL_COL}} {'CALLS':>8} {'TOTAL':>9} {'MEDIAN':>10}  CALL SITE"
    )
    lines.append("-" * _WIDTH)
    if not groups:
        lines.append("(no queries recorded)")
    for g in groups:
        sql = g.normalized_sql
        if len(sql) > _SQL_COL:
            sql = sql[: _SQL_COL - 3] + "..."
        if g.call_site:
            site = f"{g.call_site[0]}:{g.call_site[1]} in {g.call_site[2]}"
        else:
            site = "—"
        tot = f"{g.total_time:.2f}s"
        med = f"{g.median * 1000:.2f}ms"
        lines.append(f"{sql:<{_SQL_COL}} {g.calls:>8,} {tot:>9} {med:>10}  {site}")

    # --- unit economics (only when --unit-function / wherewent.unit() ran) -----
    us = getattr(run, "unit_stats", None)
    if us is not None:
        lines.append("=" * _WIDTH)
        if not us.wrapped:
            lines.append(f"UNIT: {us.name} — target function was never imported/wrapped")
        elif us.count == 0:
            lines.append(f"UNIT: {us.name} — target not called (0 units recorded)")
        else:
            lines.append(f"UNIT: {us.name}   ({us.count:,} units)")
            lines.append("-" * _WIDTH)
            md = f"{us.median_duration * 1000:.0f} ms"
            lines.append(
                f"  {'median duration':<18} {md:<14} "
                f"{'queries/unit':<15} {_qty(us.median_queries)} (median)"
            )
            lines.append(
                f"  {'commits/unit':<18} {us.mean_commits:<14.1f} "
                f"{'rows/unit':<15} {us.mean_rows:.1f}"
            )
            first = us.first_window_mean_duration
            last = us.last_window_mean_duration
            if first is not None and last is not None and us.count >= 20:
                lines.append("  GROWTH")
                lines.append(f"    {'units 1–100':<20} {first * 1000:.0f} ms/unit")
                lines.append(f"    {'units (last 100)':<20} {last * 1000:.0f} ms/unit")
                delta = (last - first) / first if first > 0 else 0.0
                if delta > 0.10:
                    trend = f"+{delta * 100:.0f}% slower over the run"
                elif delta < -0.10:
                    trend = f"-{abs(delta) * 100:.0f}% faster over the run"
                else:
                    trend = "flat"
                lines.append(f"    {'trend':<20} {trend}")
            else:
                lines.append("  GROWTH   — (need ≥20 units)")

    # --- findings -------------------------------------------------------------
    lines.append("=" * _WIDTH)
    lines.append("FINDINGS")
    lines.append("-" * _WIDTH)
    if not findings:
        lines.append("No findings fired (thresholds: see README).")
    else:
        for i, f in enumerate(findings, 1):
            lines.append(f"{i}. [{f.rule}] {f.title}")
            for dl in f.detail.split("\n"):
                lines.append(f"   {dl}")
            lines.append(f"   ~= {f.seconds:.1f}s attributable")
    lines.append("=" * _WIDTH)
    return "\n".join(lines)
