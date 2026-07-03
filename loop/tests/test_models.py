"""Unit tests for the frozen Obligation-Graph data model (task 2.1).

These construct each model and assert the frozen enum values and field defaults.
They are pure value-object tests: no DB engine, no connection — only model
construction and the lightweight, non-enforcing helpers. Persistence and
authoritative validation are exercised by task 3's store tests.
"""

from __future__ import annotations

from loop.graph.models import (
    ARTIFACT_TYPE_VALUES,
    CLOSURE_KIND_VALUES,
    DEFAULT_CONFIDENCE_THRESHOLD,
    FEEDBACK_POLARITY_VALUES,
    LOOP_STATE_VALUES,
    ArtifactType,
    ClosureKind,
    FeedbackEvent,
    FeedbackPolarity,
    GraphConfig,
    LoopState,
    Obligation,
    Person,
    clamp_confidence,
    is_valid_confidence,
    is_valid_loop_state,
    utc_now_iso,
)


# --- frozen enum values -----------------------------------------------------
def test_loop_state_values_are_exact() -> None:
    assert LoopState.BLOCKED_ON_YOU.value == "blocked-on-you"
    assert LoopState.WAITING_ON_OTHER.value == "waiting-on-other"
    assert LoopState.HEALED.value == "healed"
    assert LOOP_STATE_VALUES == {"blocked-on-you", "waiting-on-other", "healed"}


def test_closure_kind_values_are_exact() -> None:
    assert ClosureKind.AUTONOMOUS.value == "autonomous"
    assert ClosureKind.MANUAL.value == "manual"
    assert CLOSURE_KIND_VALUES == {"autonomous", "manual"}


def test_artifact_and_polarity_values_are_exact() -> None:
    assert ArtifactType.GITHUB_PR.value == "github_pr"
    assert ARTIFACT_TYPE_VALUES == {"github_pr"}
    assert FeedbackPolarity.POSITIVE.value == "positive"
    assert FeedbackPolarity.NEGATIVE.value == "negative"
    assert FEEDBACK_POLARITY_VALUES == {"positive", "negative"}


# --- model construction + field defaults ------------------------------------
def test_person_construction_and_default() -> None:
    p = Person(person_id="U1", display_name="Ada Lovelace")
    assert p.person_id == "U1"
    assert p.display_name == "Ada Lovelace"
    assert p.is_user is False  # default

    user = Person(person_id="U0", display_name="The User", is_user=True)
    assert user.is_user is True


def test_obligation_construction_and_defaults() -> None:
    o = Obligation(
        obligation_id="OB1",
        owes_person_id="U0",
        owed_person_id="U1",
        owner_person_id="U0",
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.82,
        last_touch_timestamp="2025-01-08T12:00:00+00:00",
        source_msg_channel="C123",
        source_msg_ts="123456.7890",
        subject_summary="Review the deploy runbook",
    )

    # required fields round-trip
    assert o.loop_state is LoopState.BLOCKED_ON_YOU
    assert o.confidence_score == 0.82
    assert o.source_msg_channel == "C123"
    assert o.source_msg_ts == "123456.7890"

    # surfacing-control defaults
    assert o.dismissed is False
    assert o.snoozed_until is None

    # artifact + closure metadata default to null until the lifecycle event
    assert o.artifact_type is None
    assert o.artifact_ref is None
    assert o.closure_kind is None
    assert o.closure_timestamp is None
    assert o.closure_reason is None


def test_obligation_accepts_artifact_and_closure_metadata() -> None:
    o = Obligation(
        obligation_id="OB2",
        owes_person_id="U1",
        owed_person_id="U0",
        owner_person_id="U1",
        loop_state=LoopState.HEALED,
        confidence_score=0.99,
        last_touch_timestamp="2025-01-08T12:00:00+00:00",
        source_msg_channel="C123",
        source_msg_ts="123456.7891",
        subject_summary="Merge PR #42",
        artifact_type=ArtifactType.GITHUB_PR,
        artifact_ref="acme/throwaway#42",
        closure_kind=ClosureKind.AUTONOMOUS,
        closure_timestamp="2025-01-08T12:30:00+00:00",
        closure_reason="PR merged",
    )
    assert o.artifact_type is ArtifactType.GITHUB_PR
    assert o.artifact_ref == "acme/throwaway#42"
    assert o.closure_kind is ClosureKind.AUTONOMOUS
    assert o.closure_reason == "PR merged"


def test_feedback_event_construction() -> None:
    ev = FeedbackEvent(
        event_id="EV1",
        obligation_id="OB1",
        polarity=FeedbackPolarity.NEGATIVE,
        created_at="2025-01-08T12:00:00+00:00",
    )
    assert ev.obligation_id == "OB1"
    assert ev.polarity is FeedbackPolarity.NEGATIVE


def test_graph_config_default_threshold() -> None:
    cfg = GraphConfig(user_id="U0")
    assert cfg.confidence_threshold == DEFAULT_CONFIDENCE_THRESHOLD

    custom = GraphConfig(user_id="U0", confidence_threshold=0.7)
    assert custom.confidence_threshold == 0.7


# --- lightweight, non-enforcing helpers -------------------------------------
def test_is_valid_loop_state() -> None:
    assert is_valid_loop_state(LoopState.HEALED)
    assert is_valid_loop_state("waiting-on-other")
    assert not is_valid_loop_state("done")
    assert not is_valid_loop_state(None)


def test_is_valid_confidence() -> None:
    assert is_valid_confidence(0.0)
    assert is_valid_confidence(1.0)
    assert is_valid_confidence(0.5)
    assert not is_valid_confidence(-0.01)
    assert not is_valid_confidence(1.01)
    assert not is_valid_confidence(True)  # bool is not a confidence score
    assert not is_valid_confidence("0.5")


def test_clamp_confidence() -> None:
    assert clamp_confidence(-1.0) == 0.0
    assert clamp_confidence(2.0) == 1.0
    assert clamp_confidence(0.4) == 0.4


def test_utc_now_iso_is_iso_utc() -> None:
    ts = utc_now_iso()
    # ISO 8601 UTC ends with an offset; datetime.now(timezone.utc) yields +00:00
    assert "T" in ts
    assert ts.endswith("+00:00")
