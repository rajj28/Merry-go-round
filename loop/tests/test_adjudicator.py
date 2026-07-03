"""Example-based unit tests for the Adjudicator (tasks 5.1 + 5.2).

Covers ``Adjudicator.adjudicate`` with a mocked Opus reasoning port (a thin
injectable callable) and an in-memory Obligation Graph (``:memory:``):

Task 5.1 (happy-path write):
  * user owes  -> LoopState.BLOCKED_ON_YOU, edge user->other   (Req 3.2)
  * other owes -> LoopState.WAITING_ON_OTHER, edge other->user (Req 3.2)
  * the confidence and obligation are persisted via the graph  (Req 3.3)
  * out-of-range confidence is clamped into [0.0, 1.0]         (Req 3.3)

Task 5.2 (discard / error / quiet-by-default eligibility):
  * not-a-loop / no-user / unknown-direction -> DISCARDED, no graph write (Req 3.4)
  * Opus port raising -> ERROR, graph unchanged, error recorded            (Req 3.7)
  * surfacing_eligible reflects confidence >= current threshold            (Req 3.5, 3.6)

The direction property test (Property 8 / task 5.3) and the discard property test
(task 5.4) are separate tasks and intentionally NOT included here.
"""

from __future__ import annotations

from loop.adjudicator.adjudicator import (
    AdjudicationOutcome,
    Adjudicator,
    Direction,
    OpusAdjudication,
)
from loop.graph.models import ArtifactType, LoopState
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import ObligationFilter
from loop.watcher.rts_contract import CandidateMessage

USER_ID = "U_USER"
OTHER_ID = "U_OTHER"


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class StubReasoningClient:
    """A mock Opus port that returns a fixed judgement and records calls."""

    def __init__(self, judgement: OpusAdjudication) -> None:
        self._judgement = judgement
        self.calls: list[dict] = []

    def __call__(self, candidate: CandidateMessage, *, user_id: str) -> OpusAdjudication:
        self.calls.append({"candidate": candidate, "user_id": user_id})
        return self._judgement


class RaisingReasoningClient:
    """A mock Opus port that simulates an unreachable/erroring Opus (Req 3.7)."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or RuntimeError("opus unreachable")
        self.calls: list[dict] = []

    def __call__(self, candidate: CandidateMessage, *, user_id: str) -> OpusAdjudication:
        self.calls.append({"candidate": candidate, "user_id": user_id})
        raise self._exc


def _candidate(
    channel_id: str = "C123",
    message_ts: str = "1700000000.000100",
    author_id: str = OTHER_ID,
    text: str = "Hey, can you review my PR?",
) -> CandidateMessage:
    return CandidateMessage(
        channel_id=channel_id,
        message_ts=message_ts,
        author_id=author_id,
        text=text,
        permalink="https://example.slack.com/archives/C123/p1700000000000100",
    )


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


# --------------------------------------------------------------------------- #
# Direction mapping (Req 3.2)
# --------------------------------------------------------------------------- #
def test_user_owes_maps_to_blocked_on_you() -> None:
    # Req 3.2: when the user owes the response -> blocked-on-you, edge user->other.
    client = StubReasoningClient(
        OpusAdjudication(
            is_loop=True,
            involves_user=True,
            direction=Direction.USER_OWES,
            confidence=0.8,
            subject_summary="Owe a reply about the deploy",
        )
    )
    graph = _graph()
    adjudicator = Adjudicator(client, graph)

    result = adjudicator.adjudicate(_candidate(author_id=OTHER_ID), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.CREATED
    ob = result.obligation
    assert ob is not None
    assert ob.loop_state is LoopState.BLOCKED_ON_YOU
    assert ob.owes_person_id == USER_ID       # user owes
    assert ob.owed_person_id == OTHER_ID      # other is owed
    assert ob.owner_person_id == USER_ID      # owner = who owes


def test_other_owes_maps_to_waiting_on_other() -> None:
    # Req 3.2: when the other party owes -> waiting-on-other, edge other->user.
    client = StubReasoningClient(
        OpusAdjudication(
            is_loop=True,
            involves_user=True,
            direction=Direction.OTHER_OWES,
            confidence=0.7,
            subject_summary="Waiting on a PR review",
        )
    )
    graph = _graph()
    adjudicator = Adjudicator(client, graph)

    result = adjudicator.adjudicate(_candidate(author_id=OTHER_ID), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.CREATED
    ob = result.obligation
    assert ob is not None
    assert ob.loop_state is LoopState.WAITING_ON_OTHER
    assert ob.owes_person_id == OTHER_ID      # other owes
    assert ob.owed_person_id == USER_ID       # user is owed
    assert ob.owner_person_id == OTHER_ID


# --------------------------------------------------------------------------- #
# Persistence of confidence + obligation via the graph (Req 3.3)
# --------------------------------------------------------------------------- #
def test_obligation_and_confidence_persisted_via_graph() -> None:
    client = StubReasoningClient(
        OpusAdjudication(
            is_loop=True,
            involves_user=True,
            direction=Direction.OTHER_OWES,
            confidence=0.42,
            subject_summary="PR review pending",
        )
    )
    graph = _graph()
    adjudicator = Adjudicator(client, graph)
    candidate = _candidate()

    result = adjudicator.adjudicate(candidate, user_id=USER_ID)

    # The returned obligation is readable back from the graph (read-after-write).
    stored = graph.get(result.obligation.obligation_id)
    assert stored is not None
    assert stored.confidence_score == 0.42
    assert stored.subject_summary == "PR review pending"
    # Source message reference is preserved (Req 1.6).
    assert stored.source_msg_channel == candidate.channel_id
    assert stored.source_msg_ts == candidate.message_ts


def test_re_adjudicating_same_message_updates_not_duplicates() -> None:
    # A stable obligation_id derived from the source message keeps re-adjudication
    # idempotent: the second pass UPDATEs the same edge rather than duplicating it.
    graph = _graph()
    candidate = _candidate(message_ts="1700000500.000200")

    first = Adjudicator(
        StubReasoningClient(
            OpusAdjudication(True, True, Direction.OTHER_OWES, 0.6, "v1")
        ),
        graph,
    ).adjudicate(candidate, user_id=USER_ID)
    assert first.outcome is AdjudicationOutcome.CREATED

    second = Adjudicator(
        StubReasoningClient(
            OpusAdjudication(True, True, Direction.OTHER_OWES, 0.9, "v2")
        ),
        graph,
    ).adjudicate(candidate, user_id=USER_ID)
    assert second.outcome is AdjudicationOutcome.UPDATED
    assert first.obligation.obligation_id == second.obligation.obligation_id
    assert len(graph.query(ObligationFilter())) == 1


# --------------------------------------------------------------------------- #
# Confidence clamping into [0.0, 1.0] (Req 3.3)
# --------------------------------------------------------------------------- #
def test_confidence_above_one_is_clamped() -> None:
    client = StubReasoningClient(
        OpusAdjudication(True, True, Direction.USER_OWES, 1.7, "over")
    )
    graph = _graph()
    result = Adjudicator(client, graph).adjudicate(_candidate(), user_id=USER_ID)

    assert result.obligation.confidence_score == 1.0


def test_confidence_below_zero_is_clamped() -> None:
    client = StubReasoningClient(
        OpusAdjudication(True, True, Direction.OTHER_OWES, -0.5, "under")
    )
    graph = _graph()
    result = Adjudicator(client, graph).adjudicate(_candidate(), user_id=USER_ID)

    assert result.obligation.confidence_score == 0.0


# --------------------------------------------------------------------------- #
# Task 5.2 — discard paths leave the graph unchanged (Req 3.4)
# --------------------------------------------------------------------------- #
def test_not_a_loop_is_discarded_with_no_graph_write() -> None:
    client = StubReasoningClient(
        OpusAdjudication(
            is_loop=False,
            involves_user=True,
            direction=Direction.USER_OWES,
            confidence=0.9,
            subject_summary="not really a loop",
        )
    )
    graph = _graph()
    result = Adjudicator(client, graph).adjudicate(_candidate(), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.DISCARDED
    assert result.discard_reason == "not a loop"
    assert result.obligation is None
    assert result.surfacing_eligible is None
    # No graph change (Req 3.4).
    assert graph.query(ObligationFilter()) == []


def test_does_not_involve_user_is_discarded_with_no_graph_write() -> None:
    client = StubReasoningClient(
        OpusAdjudication(
            is_loop=True,
            involves_user=False,
            direction=Direction.OTHER_OWES,
            confidence=0.9,
            subject_summary="two other people",
        )
    )
    graph = _graph()
    result = Adjudicator(client, graph).adjudicate(_candidate(), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.DISCARDED
    assert result.discard_reason == "does not involve user"
    assert result.obligation is None
    assert graph.query(ObligationFilter()) == []


def test_unknown_direction_is_discarded_with_no_graph_write() -> None:
    client = StubReasoningClient(
        OpusAdjudication(
            is_loop=True,
            involves_user=True,
            direction=Direction.UNKNOWN,
            confidence=0.9,
            subject_summary="ambiguous direction",
        )
    )
    graph = _graph()
    result = Adjudicator(client, graph).adjudicate(_candidate(), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.DISCARDED
    assert result.discard_reason == "unknown direction"
    assert result.obligation is None
    assert graph.query(ObligationFilter()) == []


# --------------------------------------------------------------------------- #
# Task 5.2 — Opus error/unreachable: ERROR, graph unchanged, error recorded (Req 3.7)
# --------------------------------------------------------------------------- #
def test_opus_error_returns_error_outcome_and_leaves_graph_unchanged() -> None:
    client = RaisingReasoningClient(RuntimeError("opus 503"))
    graph = _graph()
    result = Adjudicator(client, graph).adjudicate(_candidate(), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.ERROR
    assert result.obligation is None
    # An error indication is recorded (Req 3.7).
    assert result.error_message is not None
    assert "opus 503" in result.error_message
    # The graph is left unchanged — no obligation created.
    assert graph.query(ObligationFilter()) == []


# --------------------------------------------------------------------------- #
# Task 5.2 — quiet-by-default eligibility = confidence >= threshold (Req 3.5, 3.6)
# --------------------------------------------------------------------------- #
def test_surfacing_eligible_true_when_confidence_at_or_above_threshold() -> None:
    # Default threshold is 0.5; confidence 0.8 is at/above -> eligible (Req 3.6).
    client = StubReasoningClient(
        OpusAdjudication(True, True, Direction.OTHER_OWES, 0.8, "above threshold")
    )
    graph = _graph()
    result = Adjudicator(client, graph).adjudicate(_candidate(), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.CREATED
    assert result.surfacing_eligible is True


def test_surfacing_eligible_true_at_exact_threshold_boundary() -> None:
    # Inclusive gate: confidence == threshold surfaces (Req 3.6).
    graph = _graph()
    graph.set_threshold(0.7)
    client = StubReasoningClient(
        OpusAdjudication(True, True, Direction.OTHER_OWES, 0.7, "exactly at threshold")
    )
    result = Adjudicator(client, graph).adjudicate(_candidate(), user_id=USER_ID)

    assert result.surfacing_eligible is True


def test_surfacing_eligible_false_when_confidence_below_threshold() -> None:
    # Default threshold is 0.5; confidence 0.3 is strictly below -> quiet (Req 3.5).
    client = StubReasoningClient(
        OpusAdjudication(True, True, Direction.USER_OWES, 0.3, "below threshold")
    )
    graph = _graph()
    result = Adjudicator(client, graph).adjudicate(_candidate(), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.CREATED
    assert result.surfacing_eligible is False
    # Below-threshold obligations are still written, just kept unsurfaced (Req 3.5).
    assert result.obligation is not None
    assert graph.get(result.obligation.obligation_id) is not None


# --------------------------------------------------------------------------- #
# Live auto-close wiring — a PR reference in the message text grounds the
# obligation (artifact_type=GITHUB_PR, artifact_ref="owner/repo#number"); without
# one, both stay None. Required for the live GitHub-MCP auto-close beat (Req 4, 8).
# --------------------------------------------------------------------------- #
def _real_loop_client() -> StubReasoningClient:
    """A stub Opus that always reports a real blocked-on-you loop involving the user."""
    return StubReasoningClient(
        OpusAdjudication(
            is_loop=True,
            involves_user=True,
            direction=Direction.USER_OWES,
            confidence=0.9,
            subject_summary="Bob is blocked on you merging the PR.",
        )
    )


def test_pr_url_in_message_sets_artifact_ref_normalized() -> None:
    graph = _graph()
    candidate = _candidate(
        text="gentle nudge on my PR https://github.com/rajj28/loop-demo/pull/2 🙏"
    )
    result = Adjudicator(_real_loop_client(), graph).adjudicate(
        candidate, user_id=USER_ID
    )

    assert result.outcome is AdjudicationOutcome.CREATED
    ob = result.obligation
    assert ob is not None
    assert ob.artifact_type is ArtifactType.GITHUB_PR
    assert ob.artifact_ref == "rajj28/loop-demo#2"
    # Durable in the graph.
    stored = graph.get(ob.obligation_id)
    assert stored is not None
    assert stored.artifact_type is ArtifactType.GITHUB_PR
    assert stored.artifact_ref == "rajj28/loop-demo#2"


def test_pr_shorthand_in_message_sets_artifact_ref_normalized() -> None:
    graph = _graph()
    candidate = _candidate(text="nudge on acme/widgets#42, can't merge without you")
    result = Adjudicator(_real_loop_client(), graph).adjudicate(
        candidate, user_id=USER_ID
    )

    ob = result.obligation
    assert ob is not None
    assert ob.artifact_type is ArtifactType.GITHUB_PR
    assert ob.artifact_ref == "acme/widgets#42"


def test_no_pr_ref_in_message_leaves_artifact_fields_none() -> None:
    graph = _graph()
    # The default candidate text mentions "PR" but contains no parseable reference.
    candidate = _candidate(text="Hey, can you review my PR when you get a sec?")
    result = Adjudicator(_real_loop_client(), graph).adjudicate(
        candidate, user_id=USER_ID
    )

    ob = result.obligation
    assert ob is not None
    assert ob.artifact_type is None
    assert ob.artifact_ref is None
