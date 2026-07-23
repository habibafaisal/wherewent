# RESUME HANDOFF — wherewent v0.3.0 hardening wave

Read this + DESIGN-v3-hardening.md (same dir) to resume. Standing constraint: ORCHESTRATE
ONLY — never write wherewent's src yourself; dispatch Opus/Sonnet background agents with
disjoint file ownership. Do NOT modify publish.yml's `environment: pypi`.

## STATE AT HANDOFF  (updated — Mac being powered off mid-hardening)
- v0.2.0 already live on PyPI. Working on v0.3.0.
- v0.3 build agents A (capture), B (rules/report), C (tests/demo) ALL DONE + integrated.
- `wherewent.unit` re-export wired into src/wherewent/__init__.py; version = 0.3.0 in
  __init__.py + pyproject.toml; CHANGELOG 0.3.0 entry + README (findings table R4/R5/R6,
  "Name your unit of work", Feature 2 roadmap) already prepped by orchestrator.
- NINE verified defects across 2 review rounds -> hardening contract = DESIGN-v3-hardening.md.

### !!! CURRENT WORKING-TREE STATE (READ CAREFULLY) !!!
- Everything (A/B/C v0.3 work + prepped README/CHANGELOG/pyproject + partial H-A) is
  UNCOMMITTED working tree, and ALSO committed to branch `wip/v0.3-hardening` as insurance.
- The clean A/B/C baseline (70 passed / 4 xfailed) was NEVER committed separately. H-A began
  editing recorder.py/stats.py ON TOP of it, so there is NO clean snapshot to `git checkout`
  back to. Do NOT `git checkout -- recorder.py` — HEAD is v0.2.0 and that would DESTROY all of
  Agent A's v0.3 capture work.
- H-A (agentId af8a632d11848ca4a) was DISPATCHED then STOPPED mid-verification. It had already
  written ~+695 lines to recorder.py and +26 to stats.py (D1 reservoir, etc.). Result: compiles
  + imports fine, but REGRESSED 2 peek tests -> current suite = **68 passed, 2 FAILED, 4 xfailed**
  (failures: tests/test_peek.py::test_peek_prints_to_stderr_without_raising and
  ::test_peek_does_not_finalize_the_run — "peek() produced no output"). H-A's edits are
  INCOMPLETE/BROKEN, not finished.

### RESUME OPTIONS for H-A (pick one):
  (A) RECOMMENDED — dispatch a fresh finish-agent owning recorder.py+stats.py: "H-A's prior run
      left recorder.py/stats.py partially edited per DESIGN-v3-hardening.md (D1,D2,D3,D4,D10) and
      REGRESSED the 2 peek tests. Diagnose why peek() now produces no output, finish any unstarted
      contract items, get `pytest tests/` back to all-green (the 4 xfails stay). Do NOT revert to
      HEAD — that loses Agent A's v0.3 work; fix forward." Verify against the contract.
  (B) If forward-fix looks risky, reconstruct clean A/B/C recorder.py/stats.py from Agent A's
      completion report diffs in the prior session transcript, then re-run H-A fresh.

## HARDENING WAVE — 3 agents, disjoint ownership, all vs DESIGN-v3-hardening.md
- H-A (capture): src/wherewent/recorder.py + stats.py. Items D1,D2,D3,D4,D10. Model opus.
    *** PARTIALLY APPLIED + BROKEN (2 peek regressions). See RESUME OPTIONS above. H-A prompt below.
- H-B (rules): src/wherewent/rules.py + report.py. Items D5,D6,D7,D8,D9. Model opus. NOT YET SENT.
- H-C (tests): NEW test files only + un-xfail + demo tweaks. Model sonnet. NOT YET SENT.

Dispatch order on resume: (1) finish/fix H-A to green, (2) then H-B, (3) after H-B, H-C (so it
can un-xfail against real rules.py). H-B and H-C own files disjoint from H-A and each other.

### H-B PROMPT (Agent tool: subagent_type general-purpose, model opus)
"""
You are Agent H-B in an orchestrated hardening pass for the Python project "wherewent"
(zero-config SQL flight recorder for SQLAlchemy 2.x). You OWN exactly two files, touch no
others:
- /Users/mac/Documents/PersonalProjects/wherewent/src/wherewent/rules.py
- /Users/mac/Documents/PersonalProjects/wherewent/src/wherewent/report.py
READ THE FULL CONTRACT FIRST (authoritative shapes/thresholds):
<repo>/.handoff/DESIGN-v3-hardening.md
Implement ONLY the "AGENT H-B" items: D5 (R4 re-key clusters by (file, function) =
(call_site[0], call_site[2]) instead of full call_site incl. line — THE highest-leverage
fix; groups on different lines of one function must recombine; representative site_str =
dominant group's own file:line), D6 (flush guard+label, MUST ship with D5 — a function-keyed
cluster whose writes all share ONE source line gets a "may be one session.flush()/commit()"
caveat and must NOT be promoted by the scale-trigger alone; >=2 distinct lines = genuine
workflow, fire normally; keep simple + honest, never say "collapse into one round-trip" for a
single flush), D7 (R5 absolute floor: fire if total_time > 0.15*wall OR > R5_ABS_SECONDS=10.0;
add module consts R5_ABS_SECONDS + KEEP_ABS_SECONDS=10.0; change final trivia filter to keep
f.seconds >= 0.05*wall OR >= KEEP_ABS_SECONDS so the floor is not re-suppressed; R5 detail
shows "X% of wall (Ns absolute)"), D8 (CPU-bound framing: when cpu_busy not None and >=0.5,
R4+R6 append a caveat that this is a SCALABILITY risk not the current wall bottleneck), D9 (R6
also fires on the query slope: read us.first_window_mean_queries/last_window_mean_queries [new
H-A fields]; trigger when count>=20 AND (last_dur>=1.5*first_dur OR last_q>=1.5*first_q); detail
reports "queries/unit rose from A to B (+P%)" when present; never print a None-window slope;
seconds must never be negative/crash). report.py: header keeps commit-only "commit time",
adds "rollback time: X (incl. pool resets)" when rollback_time not None; UNIT GROWTH block adds
queries/unit early-vs-late next to duration windows.
INVARIANTS: never crash; every number honest ("—" not a guess); do not reorder Finding fields.
UnitStats has NEW appended fields first_window_mean_queries/last_window_mean_queries from H-A —
access via getattr defensively so you compile even if run before H-A lands.
VERIFY: cd /Users/mac/Documents/PersonalProjects/wherewent && ./.venv/bin/python -m compileall
src -q (clean); ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider — after your re-key
the 4 xfailed R4 tests in test_rules_cluster.py should now PASS (xpass, strict=False = ok) and
all others stay green; ./.venv/bin/python demo/benchmark.py should flip the R4 check to PASS.
Report files changed, the exact cluster-key change, how you handled the D6 flush tension, what
you verified, any deviation. Your final message is consumed by the orchestrator — return raw data.
"""

### H-C PROMPT (Agent tool: subagent_type general-purpose, model sonnet) — send AFTER H-A+H-B land
"""
You are Agent H-C in an orchestrated hardening pass for "wherewent". You may create NEW test
files and edit demo/ helpers, but must NOT edit src/ or Agent C's existing files
(tests/test_rules_cluster.py, tests/test_unit_profiling.py, tests/test_one_shot.py,
demo/unit_job.py, demo/run_units.py) beyond removing xfail markers that are now xpassing.
READ THE FULL CONTRACT FIRST:
<repo>/.handoff/DESIGN-v3-hardening.md
Do the "AGENT H-C" test list that Agent C did NOT already cover: (a) remove the now-obsolete
xfail markers on the 4 R4 tests in tests/test_rules_cluster.py IF they now pass (verify first);
(b) NEW tests/test_hardening.py covering: R4 flush guard/caveat (D6), R5 absolute floor on a
huge synthetic wall where 20s < 5% of wall still fires and survives the trivia filter (D7),
CPU-bound framing caveat in R4/R6 detail (D8), R6 query-slope wording + query-slope-only firing
(D9), bounded-memory per-group reservoir cap (>5000 execs -> capped, median sane) (D1),
commit/rollback split (commit_time excludes rollback, rollback_time reported separately) (D2).
Prefer real code paths; where a hand-built RunSnapshot is unavoidable use the real dataclasses.
INVARIANTS: tests must be deterministic + order-independent; use a fixture that disables any
prior Recorder's _unit_enabled at teardown (module-level ContextVar pollution — see H-A's new
disable() if present).
VERIFY: cd /Users/mac/Documents/PersonalProjects/wherewent && ./.venv/bin/python -m pytest
tests/ -q -p no:cacheprovider — all green (0 xfail ideally, or only genuinely-pending ones);
./.venv/bin/python demo/benchmark.py => RESULT: PASS. Report files added/changed, what you
verified, any deviation. Return raw data to the orchestrator.
"""

## AFTER THE WAVE (orchestrator does this)
1. Integrate; run full pytest (all green) + demo/benchmark.py (RESULT: PASS, overhead <15%).
2. Capture REAL R4/R5/R6 + unit-report numbers from the demo; reconcile the README unit-report
   example + findings-table wording to match reality (R5 "> 15% wall OR > 10s"; R6 mentions
   queries/unit slope). Add to README Limitations: flush-attribution edge ("via session flush"),
   per-group median is now a bounded SAMPLE median, rollback time reported separately (incl.
   pool resets). Add a 0.3.0 CHANGELOG note for the hardening if warranted.
3. git add -A && commit (v0.3.0 hardening) && push origin main. Confirm CI green.
4. gh release create v0.3.0 --repo habibafaisal/wherewent --target main (triggers publish.yml
   trusted publishing with environment: pypi — DO NOT modify). Verify 0.3.0 live on PyPI
   (clean venv: pip install wherewent==0.3.0; import; wherewent --help).
5. THEN deliver the user's requested codebase walkthrough: how it works end-to-end, how it
   genuinely helps, whether it is unique. (Original ask, still pending.)

## OPEN NOTES
- Agent A deviation: install() does NOT read WHEREWENT_UNIT_FUNCTION; only install_from_env()
  does (calls enable_unit then install on the module singleton). By design (shim path). Fine.
- Agent A extra: _unit_note writes a stderr line for EVERY wrap (contract only required it for
  bare names). Additive; leave unless you want it suppressed for explicit SPEC.
