"""Example-based unit tests for the Learn feedback recorder (task 6.1).

Covers the task 6.1 deliverables only — confirm/dismiss recording, dismissal
state change, the dismissed-never-surfaces guarantee, and the failure path —
against an isolated in-memory store per test:

  * confirm records a positive FeedbackEvent and changes no state (Req 5.1);
  * dismiss sets ``dismissed`` AND records a negative FeedbackEvent, after which
    the obligation no longer surfaces (Req 5.2, 5.3, 14.3, 14.5);
  * the user may dismiss any obligation, surfaced or not (Req 14.4);
  * a feedback-record failure returns ``not saved`` and leaves state, surfacing
    eligibility, and the threshold unchanged (Req 5.7, 14.7);
  * a dismiss state-update failure leaves the obligation surfacing (Req 14.7).

Property-based coverage (Properties 11/12) and the dedicated fault-injection test
(task 6.6) are separate tasks; this file is deliberately example-based.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loop.graph.models import FeedbackPolarity, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.learn.feedback import LearnEngine

NOW = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


def _make_obligation(
    obligation_id: str = "OBL1",
    *,
    confidence: float = 0.9,
    dismissed: bool = False,
    loop_state: LoopState = LoopState.BLOCKED_ON_YOU,
) -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=loop_state,
        confidence_score=confidence,
        last_touch_timestamp=NOW,
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary="needs a review",
        dismissed=dismissed,
    )


def _failing_commit(_session) -> None:
    raise RuntimeError("simulated commit failure")


# ---------------------------------------------------------------------------
# Confirm records positive feedback (Req 5.1)
# ---------------------------------------------------------------------------
def test_confirm_records_positive_feedback() -> None:
    graph = _graph()
    graph.set_threshold(0.5)
    graph.upsert(_make_obligation())
    engine = LearnEngine(graph)

    result = engine.record_confirm("OBL1")

    assert result.saved is True
    events = graph.get_feedback("OBL1")
    assert len(events) == 1
    assert events[0].polarity is FeedbackPolarity.POSITIVE
    assert events[0].obligation_id == "OBL1"


def test_confirm_tunes_threshold_down_and_keeps_state() -> None:
    graph = _graph()
    graph.set_threshold(0.5)
    graph.upsert(_make_obligation())
    engine = LearnEngine(graph)

    engine.record_confirm("OBL1")

    fetched = graph.get("OBL1")
    assert fetched is not None
    assert fetched.dismissed is False
    # Positive feedback nudges the threshold down by the bounded step (task 6.2).
    assert graph.get_threshold() == 0.45
    # Still surfaces (confidence 0.9 >= 0.45).
    assert engine.is_surfaced("OBL1", NOW) is True


def test_confirm_unknown_obligation_not_saved() -> None:
    graph = _graph()
    engine = LearnEngine(graph)

    result = engine.record_confirm("MISSING")

    assert result.saved is False
    assert graph.get_feedback("MISSING") == []


# ---------------------------------------------------------------------------
# Dismiss sets dismissed + records negative + no longer surfaces (Req 5.2/5.3/14.5)
# ---------------------------------------------------------------------------
def test_dismiss_sets_dismissed_and_records_negative_feedback() -> None:
    graph = _graph()
    graph.set_threshold(0.5)
    graph.upsert(_make_obligation())
    engine = LearnEngine(graph)

    # Surfaced before dismissal.
    assert engine.is_surfaced("OBL1", NOW) is True

    result = engine.record_dismiss("OBL1")

    assert result.saved is True
    fetched = graph.get("OBL1")
    assert fetched is not None
    assert fetched.dismissed is True

    events = graph.get_feedback("OBL1")
    assert len(events) == 1
    assert events[0].polarity is FeedbackPolarity.NEGATIVE


def test_dismissed_obligation_no_longer_surfaces() -> None:
    graph = _graph()
    graph.set_threshold(0.5)
    graph.upsert(_make_obligation())
    engine = LearnEngine(graph)

    engine.record_dismiss("OBL1")

    # Never surfaces anywhere after dismissal (Req 5.3, 14.5).
    assert engine.is_surfaced("OBL1", NOW) is False


def test_dismiss_tunes_threshold_up() -> None:
    graph = _graph()
    graph.set_threshold(0.5)
    graph.upsert(_make_obligation())
    engine = LearnEngine(graph)

    engine.record_dismiss("OBL1")

    # Negative feedback nudges the threshold up by the bounded step (task 6.2).
    assert graph.get_threshold() == 0.55


def test_user_can_dismiss_any_obligation_even_below_threshold() -> None:
    graph = _graph()
    graph.set_threshold(0.5)
    # A below-threshold obligation is not surfaced, but is still dismissible (Req 14.4).
    graph.upsert(_make_obligation(confidence=0.2))
    engine = LearnEngine(graph)
    assert engine.is_surfaced("OBL1", NOW) is False

    result = engine.record_dismiss("OBL1")

    assert result.saved is True
    assert graph.get("OBL1").dismissed is True


def test_dismiss_unknown_obligation_not_saved() -> None:
    graph = _graph()
    engine = LearnEngine(graph)

    result = engine.record_dismiss("MISSING")

    assert result.saved is False
    assert graph.get_feedback("MISSING") == []


# ---------------------------------------------------------------------------
# Failure path: feedback-record failure leaves everything unchanged (Req 5.7/14.7)
# ---------------------------------------------------------------------------
def test_confirm_record_failure_returns_not_saved_and_changes_nothing() -> None:
    graph = _graph()
    graph.set_threshold(0.5)
    graph.upsert(_make_obligation())
    engine = LearnEngine(graph)

    graph._commit = _failing_commit  # type: ignore[assignment]
    result = engine.record_confirm("OBL1")
    del graph._commit  # type: ignore[attr-defined]

    assert result.saved is False
    assert result.message  # a user-facing "not saved" indication is present
    assert graph.get_feedback("OBL1") == []
    assert graph.get_threshold() == 0.5
    assert engine.is_surfaced("OBL1", NOW) is True


def test_dismiss_state_update_failure_keeps_obligation_surfacing() -> None:
    graph = _graph()
    graph.set_threshold(0.5)
    graph.upsert(_make_obligation())
    engine = LearnEngine(graph)

    # The dismissal upsert (step 1) fails: nothing should change.
    graph._commit = _failing_commit  # type: ignore[assignment]
    result = engine.record_dismiss("OBL1")
    del graph._commit  # type: ignore[attr-defined]

    assert result.saved is False
    fetched = graph.get("OBL1")
    assert fetched is not None
    assert fetched.dismissed is False  # prior state retained (Req 14.7)
    assert graph.get_feedback("OBL1") == []
    assert graph.get_threshold() == 0.5
    # Still surfaces — dismissal was not saved (Req 14.7).
    assert engine.is_surfaced("OBL1", NOW) is True


def test_dismiss_feedback_failure_rolls_back_dismissal() -> None:
    graph = _graph()
    graph.set_threshold(0.5)
    graph.upsert(_make_obligation())
    engine = LearnEngine(graph)

    # Let the dismissal upsert succeed but make the feedback append fail by
    # failing only the second commit. We simulate this by counting commits.
    real_commit = SqliteObligationGraph._commit
    calls = {"n": 0}

    def _commit_fail_on_second(self, session) -> None:  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated feedback persist failure")
        real_commit(self, session)

    graph._commit = _commit_fail_on_second.__get__(graph, SqliteObligationGraph)  # type: ignore[assignment]
    result = engine.record_dismiss("OBL1")
    del graph._commit  # type: ignore[attr-defined]

    assert result.saved is False
    fetched = graph.get("OBL1")
    assert fetched is not None
    # Dismissal was rolled back to the prior state (Req 5.7, 14.7).
    assert fetched.dismissed is False
    assert engine.is_surfaced("OBL1", NOW) is True
    # No feedback event persisted.
    assert graph.get_feedback("OBL1") == []


# ---------------------------------------------------------------------------
# Task 6.6 — Fault-injection: feedback/dismissal record failure leaves state,
# surfacing eligibility, and the (now actively tuned) threshold all unchanged,
# and notifies the user (Req 5.7, 14.7).
#
# Distinct from the task-6.1 failure tests above: this verifies the failure
# contract *now that threshold tuning (task 6.2) is live* — the tuning runs only
# on the success path, so a failed record must never move the threshold.
# ---------------------------------------------------------------------------
def test_confirm_record_failure_leaves_threshold_and_eligibility_unchanged() -> None:
    """Validates: Requirements 5.7, 14.7.

    A failed confirm (feedback append fails) must report ``not saved``, notify the
    user, persist no feedback, leave surfacing eligibility intact, and — crucially
    with tuning active — leave the Confidence_Threshold exactly where it was.
    """
    graph = _graph()
    graph.set_threshold(0.6)
    graph.upsert(_make_obligation())
    engine = LearnEngine(graph)
    assert engine.is_surfaced("OBL1", NOW) is True

    graph._commit = _failing_commit  # type: ignore[assignment]
    result = engine.record_confirm("OBL1")
    del graph._commit  # type: ignore[attr-defined]

    assert result.saved is False
    assert result.message  # user is notified the feedback was not saved
    assert graph.get_feedback("OBL1") == []
    # Threshold NOT nudged down — tuning runs only on the success path (Req 5.7).
    assert graph.get_threshold() == 0.6
    # Surfacing eligibility unchanged.
    assert engine.is_surfaced("OBL1", NOW) is True


def test_dismiss_record_failure_leaves_threshold_state_and_eligibility_unchanged() -> None:
    """Validates: Requirements 5.7, 14.7.

    A failed dismiss (state-update commit fails) must report ``not saved``, notify
    the user, leave the obligation un-dismissed and still surfacing, and leave the
    Confidence_Threshold unchanged (no upward tuning on a failed dismissal).
    """
    graph = _graph()
    graph.set_threshold(0.6)
    graph.upsert(_make_obligation())
    engine = LearnEngine(graph)

    graph._commit = _failing_commit  # type: ignore[assignment]
    result = engine.record_dismiss("OBL1")
    del graph._commit  # type: ignore[attr-defined]

    assert result.saved is False
    assert result.message  # user is notified the dismissal was not saved
    fetched = graph.get("OBL1")
    assert fetched is not None
    assert fetched.dismissed is False           # prior state retained (Req 14.7)
    assert engine.is_surfaced("OBL1", NOW) is True  # still surfacing
    assert graph.get_feedback("OBL1") == []
    # Threshold NOT nudged up — tuning runs only on the success path (Req 5.7).
    assert graph.get_threshold() == 0.6
