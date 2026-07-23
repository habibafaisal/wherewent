"""Tests for the v0.2 #4 mid-run "peek" snapshot (DESIGN-v2.md AGENT A,
section A4): Recorder.peek() must render a partial report to stderr without
ending the recording session, so the real atexit/final report still fires
later. Deliberately resilient to the exact banner wording -- only checks
that *something* was written to stderr and that `finalized` stays False.

NOTE: peek() is being added by a parallel agent alongside this test file.
Until it lands, expect the `hasattr(rec, "peek")` check to fail cleanly
(contract-only, pending integration), not a crash elsewhere.
"""
import time

from wherewent.recorder import Recorder


def _fabricate_some_state(rec: Recorder) -> None:
    """Poke enough internal state that snapshot()/render() has something to
    show, without spinning up a real SQLAlchemy engine. Mirrors the internal
    accumulator shape Recorder._after() builds (see recorder.py): a dict per
    group keyed by group key, with normalized_sql/calls/durations/total_time/
    rows/executemany_calls/call_sites/sample_stacks.
    """
    rec.installed = True
    rec.start_perf = time.perf_counter() - 1.0
    rec.total_queries = 5
    rec.total_commits = 2
    rec.db_time = 0.4
    rec.sqlalchemy_active = True
    rec.groups["fabricated"] = {
        "normalized_sql": "SELECT * FROM t WHERE id = ?",
        "calls": 5,
        "durations": [0.08] * 5,
        "total_time": 0.4,
        "rows": 5,
        "executemany_calls": 0,
        "call_sites": {},
        "sample_stacks": [],
    }


def test_peek_exists_and_is_callable():
    rec = Recorder()
    assert hasattr(rec, "peek"), "Recorder.peek() (DESIGN-v2 A4) is not implemented yet"
    assert callable(rec.peek)


def test_peek_prints_to_stderr_without_raising(capsys):
    rec = Recorder()
    _fabricate_some_state(rec)

    rec.peek()  # must not raise -- "recorder can never crash the host job"

    captured = capsys.readouterr()
    assert captured.out == "", "peek() must write to stderr, not stdout"
    assert captured.err.strip() != "", "peek() produced no output at all"


def test_peek_does_not_finalize_the_run(capsys):
    rec = Recorder()
    _fabricate_some_state(rec)

    assert rec.finalized is False
    rec.peek()
    assert rec.finalized is False, (
        "peek() must not set finalized=True -- the real final report "
        "(atexit/signal) still needs to fire later in the job's life"
    )

    capsys.readouterr()  # drain peek's output before inspecting finalize()'s

    rec.finalize()
    assert rec.finalized is True
    assert capsys.readouterr().err.strip() != "", "finalize() must still print its own report after a peek()"


def test_peek_can_be_called_repeatedly_without_finalizing(capsys):
    rec = Recorder()
    _fabricate_some_state(rec)

    for _ in range(3):
        rec.peek()

    capsys.readouterr()
    assert rec.finalized is False
