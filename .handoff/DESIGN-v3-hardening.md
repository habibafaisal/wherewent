# wherewent v0.3.0 — HARDENING PASS contract (post-review)

Nine verified defects/gaps from two review rounds. All confirmed against real code.
Disjoint file ownership so 3 agents run in parallel. Ship v0.3.0 only after this lands.

Invariants (unchanged, non-negotiable):
- Never crash the host job (every hook body try/except).
- Never store parameter VALUES — counts/durations/normalized SQL only.
- Every number honest: unmeasurable prints "—", never a guess.
- Hot path stays cheap when a feature is disabled (unit mode already gated).
- < 15% self-measured overhead gate must still pass.

Module constants to add (top of rules.py):
    R5_ABS_SECONDS = 10.0     # one-shot heavyweight absolute floor
    KEEP_ABS_SECONDS = 10.0   # trivia-filter escape hatch (see R5)

===================================================================
AGENT H-A — capture path.  OWNS: src/wherewent/recorder.py, src/wherewent/stats.py
===================================================================

### D1  Reservoir-sample per-group durations (round1 #1)
Problem: `g["durations"]` is an unbounded list (one float/execution). On a 500k-call
group that is ~14MB + an O(n) sort in statistics.median at every snapshot()/peek().
Fix: replace the per-group unbounded list with a bounded reservoir (reuse the exact
approach in _UnitAccumulator: cap 5000, exact under cap, algorithm-R above).
- Per group keep: exact `calls` (already), exact `total_time` (already, summed), and
  a reservoir `dur_reservoir` (cap 5000) + `dur_seen` counter for the MEDIAN only.
- snapshot(): median = statistics.median(dur_reservoir) if dur_reservoir else 0.0.
  (Sample median — honest and bounded. total_time stays exact.)
- Keep sample_stacks logic (first-5) untouched.
- --save JSON: durations were never dumped; unaffected.

### D2  Split rollback_time out of commit_time (round1 #2)
Problem: timed_rollback adds into rec.commit_time; SQLAlchemy pool fires do_rollback as
a reset on every checkin -> commit_time (and R2's evidence, and the report line) inflated
by non-commits on any pooled workload.
Fix:
- New accumulator field self.rollback_time = 0.0 ; self.rollback_measurable mirrors commit.
- timed_commit -> rec.commit_time (commit only).  timed_rollback -> rec.rollback_time.
- RunSnapshot gains `rollback_time: "float | None" = None` (None when not measurable).
  commit_time stays commit-ONLY.
- snapshot(): rollback_time = self.rollback_time if self.commit_measurable else None
  (same dialect wrap makes both measurable together).
- Reset-on-return exclusion is best-effort/hard; DO NOT try to perfectly detect. Instead
  report rollback_time separately and let report.py LABEL it "(incl. pool resets)". This
  satisfies the honesty rule. R2 must use commit_time only (already does).
- --save JSON payload: add "rollback_time": run.rollback_time.

### D3  handle_error cleanup for _exec_start leak (round1 #4)
Problem: _before inserts _exec_start[id(ctx)]; _after pops it. On a failed execute,
after_cursor_execute never fires -> entry leaks (unbounded on error-heavy jobs).
Fix: in install(), also `event.listen(Engine, "handle_error", self._on_error)`.
    def _on_error(self, exception_context):  # wrapped in try/except, times overhead
        ctx = getattr(exception_context, "execution_context", None)
        if ctx is not None: self._exec_start.pop(id(ctx), None)
Do not swallow/alter the exception — SQLAlchemy re-raises; just clean up and return None.

### D4  Per-unit query-slope windows (round2 #5)  — enables R6 queries/unit slope
_UnitAccumulator additions:
- self.first_sum_queries = 0        # paired with the existing FIRST window (first_n<=W)
- self.last_queries = collections.deque(maxlen=self.W)   # paired with last_durations
- in add(): when incrementing first window, also first_sum_queries += queries.
            always last_queries.append(queries).
UnitStats NEW fields (append AFTER existing ones so positional consumers unaffected):
- first_window_mean_queries: "float | None"
- last_window_mean_queries: "float | None"
_build_unit_stats():
- first_window_mean_queries = (first_sum_queries/first_n) if first_n else None
- last_window_mean_queries  = (sum(last_queries)/len(last_queries)) if last_queries else None
- set BOTH in the count==0 branch (None) and the normal branch.
_unit_stats_to_json(): add the two new fields.

### D10  Public disable()/reset hook (test hygiene + host safety)  (Agent C found)
Problem: install() registers hooks at the Engine CLASS level (global) and per-unit state
lives in a MODULE-level ContextVar shared by every Recorder instance. With no disable API,
a still-_unit_enabled Recorder keeps reacting to later work in the same process (cross-test
unit-count pollution; also a foot-gun for any embedder).
Fix: add a small, guarded `Recorder.disable()` that:
- sets self._unit_enabled = False and self.installed = False,
- removes the SQLAlchemy event listeners it added (event.remove(...) for each, guarded),
- restores wrapped dialect do_commit/do_rollback if feasible, else leaves them (idempotent),
- does NOT reset accumulated counters (a caller may still want snapshot()).
Keep it best-effort and fully try/except — never raise. This is additive; existing behavior
unchanged when disable() is never called.

H-A verification: compileall clean; pytest tests/ still green; a smoke run on a
500k-row loop shows flat memory (reservoir capped) and the report unchanged in shape.

===================================================================
AGENT H-B — findings + report.  OWNS: src/wherewent/rules.py, src/wherewent/report.py
===================================================================

### D5  R4 cluster granularity: key by (file, function), NOT (file, line, function)  (round2 #1)  ** highest leverage **
Problem: clusters keyed on full g.call_site incl. LINE. A helper that issues INSERT/
SELECT/UPDATE on 3 source lines of ONE function fragments into 3 sub-threshold clusters.
Fix: cluster key = (g.call_site[0], g.call_site[2]) = (file, function). Groups on
different lines of the same function recombine. Track each group's own line for display
(the representative site_str = dominant group's own call_site file:line).
- detail header "N query groups fire together from <dominant file:line> in <function>:".

### D6  R4 flush-attribution guard + label (round1 #5)  — MUST ship WITH D5
Tension (found during verification): coarsening the key to (file, function) can newly
MERGE unrelated groups — e.g. a function that does a direct session.execute(select) on
line 60 AND a session.commit() flush on line 88 would now cluster together.
Requirement (honest, not over-claimed — reviewer explicitly accepted "at minimum label"):
- Within a function-keyed cluster, compute the set of distinct SOURCE LINES.
- If the cluster is dominated by write shapes (INSERT/UPDATE/DELETE) that all share ONE
  source line -> that is the session.flush()/commit() signature. Append a caveat line:
  "Note: these statements share one call site and may be emitted by a single
   session.flush()/commit() (ORM unit-of-work) — verify they are not already batched
   before splitting." and DO NOT let the pure scale-trigger alone promote such a
   single-line write cluster; require it to also clear raw-wall or variable-wall %.
- If the cluster spans >=2 distinct source lines -> genuine multi-line workflow; fire R4
  as normal (this is the fragmentation case D5 exists to catch).
- Keep it SIMPLE and documented; perfect flush detection is out of scope. Honesty over
  cleverness: never tell the user to "collapse into one round-trip" for something that is
  already one flush.

### D7  R5 absolute floor + trivia-filter escape hatch (round2 #3)
- R5 fires if `g.total_time > 0.15*wall OR g.total_time > R5_ABS_SECONDS` (10s).
- The final trivia filter currently drops f.seconds < 0.05*wall — which would re-suppress
  an absolute-floor R5 on a huge run. Change it to keep anything big in absolute terms too:
  findings = [f for f in findings if f.seconds >= 0.05*wall or f.seconds >= KEEP_ABS_SECONDS]
- R5 detail on the absolute path must say "= X% of wall (Ns absolute)" so a small % but
  large-seconds finding reads honestly.

### D8  CPU-bound honesty framing for R4/R6 (round2 #4)
cpu_busy is already computed (run.cpu_time/wall). When cpu_busy is not None and >= 0.5
(compute-bound), R4 and R6 must append a caveat instead of implying the SQL is the current
bottleneck, e.g.:
  "This run is CPU-bound (CPU busy {pct}%): this pattern is a SCALABILITY risk that will
   dominate at full volume, NOT the current wall-clock bottleneck. Fixing it speeds up
   large runs, not this sample."
Never imply "fix your SQL and it's fast" when the clock is in compute.

### D9  R6 queries/unit slope (round2 #5)
- Read us.first_window_mean_queries / us.last_window_mean_queries (new H-A fields).
- Extend the TRIGGER: fire when count>=20 AND
    ( (dur windows present and last_dur >= 1.5*first_dur)
      OR (query windows present and last_q >= 1.5*first_q) )
  so a CPU-bound-but-query-growing job (the VFE case) still fires on the query slope.
- Detail must REPORT the slope reviewers want, when present:
    "queries/unit rose from {first_q:.0f} (first 100) to {last_q:.0f} (last 100) (+{pct}%)"
  alongside the existing duration line. Whichever windows are present drive the wording;
  never print a slope for a window that is None.
- seconds attribution unchanged in spirit (max(0.0,(mean_duration-first_dur)*count)); if
  only the query slope fired and first_dur is None, fall back to a conservative estimate
  or 0.0 — never crash, never negative.

### report.py
- Header: keep "commit time: <commit_time>" as commit-ONLY. Add a "rollback time:
  <rollback_time> (incl. pool resets)" line when rollback_time is not None; print "—"
  semantics when None (omit or dash per existing style).
- UNIT GROWTH block: add the queries/unit early-vs-late numbers next to the duration
  windows (first-100 vs last-100 queries/unit), only when those windows are present.

H-B verification: pytest tests/ green (existing cluster tests may need the multi-line
fixture from H-C — coordinate: a currently-line-fragmented fixture SHOULD now cluster).
Run scratchpad isolation checks: (a) 3 groups on 3 lines of one function -> ONE R4;
(b) direct-execute + flush single-line writes -> flush caveat, not a false "collapse";
(c) R5 20s on a huge synthetic wall -> fires via absolute floor and survives the filter;
(d) CPU-bound run -> R4/R6 carry the scalability caveat; (e) query-slope-only R6 fires.

===================================================================
AGENT H-C — tests + demo.  OWNS: NEW test files only (disjoint from Agent C's files) + demo helpers
===================================================================
IMPORTANT: Agent C is concurrently authoring tests/test_one_shot.py and demo files.
H-C must create NEW files with distinct names (e.g. tests/test_hardening.py,
tests/test_r4_granularity.py, tests/test_unit_slope.py) and NOT edit Agent C's files.
Coordinate via the contract shapes above; write tests against the contract, not by reading
half-written files.

Tests to add (all must exercise real code paths, NOT hand-built RunSnapshots where noted):
1. R4 multi-line-same-function: a fixture whose co-occurring queries are issued on
   DIFFERENT source lines of ONE function -> asserts a SINGLE R4 cluster fires. (This is
   the exact real-ORM shape; a single-line fixture would hide the bug.)
2. R4 flush guard: direct execute on one line + flush-style single-line writes -> assert
   the flush caveat text present and no bogus "collapse into one round-trip" claim.
3. R5 absolute floor: a 20s calls==1 group on a synthetic large wall where 20s < 5% of
   wall -> assert R5 STILL fires and survives the trivia filter.
4. CPU-bound framing: a run with high cpu_busy -> assert R4/R6 detail contains the
   scalability caveat.
5. R6 END-TO-END through snapshot() (round1 #3): install recorder, run wrapped units whose
   per-unit query count genuinely RISES across the run, call recorder.snapshot(), assert
   R6 in rules.evaluate(snapshot). NOT a hand-built snapshot.
6. R6 query-slope: assert the queries/unit early-vs-late slope appears in the R6 detail and
   that query-slope-only (flat duration, rising queries) still fires.
7. Bounded memory: drive a group past the reservoir cap (>5000 execs) and assert the
   per-group reservoir length is capped and median is still sane.
8. commit/rollback split: assert commit_time excludes rollback time and rollback_time is
   reported separately.

Demo: ensure demo (Agent C's unit demo, or a new demo/vfe_like.py) reproduces the VFE
shape — a one-shot preload + a per-unit multi-line audit helper across a growing history —
so R4(fragmented)+R5(one-shot)+R6(rising, incl query slope) all fire on a real run. Put
co-occurring queries on DIFFERENT lines of one function. Use `python -m` or the context
manager (NOT bare `python job.py`, whose funcs live in __main__ and won't wrap via SPEC).

===================================================================
ORCHESTRATOR (me) — README + integration
===================================================================
- README Limitations: document the flush-attribution edge (R4 may cluster flush-emitted
  writes at one commit site; labeled "via session flush"); note per-group median is now a
  bounded SAMPLE median; note rollback time reported separately (incl. pool resets).
- README findings table: R5 "> 15% of wall OR > 10s absolute"; R6 mention queries/unit slope.
- Reconcile the README unit-report example numbers with the real demo output.
- After integration: full pytest + demo, verify overhead gate < 15%, commit, push, CI green,
  gh release create v0.3.0 (target main; publish.yml keeps environment: pypi — DO NOT touch),
  verify 0.3.0 on PyPI. THEN deliver the codebase walkthrough.
