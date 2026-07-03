"""Unit tests for Polite Nudge drafting constraints — task 12.7.

Covers :meth:`loop.action.action_agent.ActionAgent.draft_polite_nudge` with a
*mocked* Claude drafting port, asserting the two contractual constraints:

  * **Req 7.1** — a successfully drafted nudge is no more than
    :data:`~loop.action.action_agent.NUDGE_DRAFT_MAX_CHARS` (1000) characters and
    references the sender, the summarized subject, and the timestamp of the source
    Slack message.
  * **Req 7.8** — when the Claude port fails or times out, the draft path reports
    failure and nothing is sent (no message ever reaches the Slack "send as user"
    port).

Only the outward ports are mocked (Claude drafting + Slack send-as-user); the
Action Agent itself and its real length-clipping logic are exercised directly.

Validates: Requirements 7.1, 7.8.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from loop.action.action_agent import (
    NUDGE_DRAFT_MAX_CHARS,
    ActionAgent,
)
from loop.graph.models import LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.verifier.types import VerificationResult, VerifyPurpose


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _RecordingSender:
    """A mock "send as user" port that records every send.

    Used to prove the draft-failure path sends nothing: :attr:`count` must stay 0.
    """

    def __init__(self) -> None:
        self.count = 0
        self.calls: list[tuple[str, str]] = []

    def __call__(self, recipient_id: str, text: str) -> None:
        self.count += 1
        self.calls.append((recipient_id, text))


class _UnusedVerifier:
    """A Verifier stand-in; drafting never consults the Verifier (the gate is on send)."""

    def verify_pr(
        self, obligation: Obligation, *, purpose: VerifyPurpose
    ) -> VerificationResult:  # pragma: no cover - must not be called during drafting
        raise AssertionError("draft_polite_nudge must not call the Verifier")


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
# Distinctive, easy-to-find field values so the "references sender/subject/timestamp"
# assertion is unambiguous.
SENDER = "U_ALICE"
SUBJECT = "ship the Q3 launch checklist"
SOURCE_TS = datetime(2025, 1, 8, 9, 30, 0, tzinfo=timezone.utc).isoformat()


def _obligation() -> Obligation:
    """A blocked-on-you obligation carrying a recognisable sender/subject/timestamp."""
    return Obligation(
        obligation_id="OB1",
        owes_person_id=SENDER,
        owed_person_id="U_BOB",
        owner_person_id=SENDER,
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.9,
        last_touch_timestamp=SOURCE_TS,
        source_msg_channel="C_GENERAL",
        source_msg_ts=SOURCE_TS,
        subject_summary=SUBJECT,
        artifact_type=None,
        artifact_ref=None,
    )


def _agent(graph: SqliteObligationGraph, *, claude_draft, sender) -> ActionAgent:
    return ActionAgent(
        graph,
        verifier=_UnusedVerifier(),  # type: ignore[arg-type]
        slack_send_as_user=sender,
        claude_draft=claude_draft,
    )


# =========================================================================== #
# Req 7.1 — draft references sender/subject/timestamp and is <= 1000 chars
# =========================================================================== #
def test_draft_references_sender_subject_timestamp_and_within_limit() -> None:
    """Validates: Requirement 7.1.

    With a faithful mocked Claude that builds the reminder from the source message,
    the drafted nudge references the sender, the summarized subject, and the source
    timestamp, and is no more than 1000 characters.
    """
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    sender = _RecordingSender()

    def fake_claude(ob: Obligation) -> str:
        # A realistic context-aware reminder drawing on the source message fields.
        return (
            f"Hi {ob.owes_person_id}, just a gentle nudge on "
            f"\"{ob.subject_summary}\" — your message from {ob.source_msg_ts} is "
            "still open on our side. Could you take a look when you get a moment? Thanks!"
        )

    agent = _agent(graph, claude_draft=fake_claude, sender=sender)
    result = agent.draft_polite_nudge(_obligation())

    assert result.drafted is True
    assert result.draft is not None
    text = result.draft.text

    # Req 7.1: references sender, summarized subject, and the source timestamp.
    assert SENDER in text
    assert SUBJECT in text
    assert SOURCE_TS in text

    # Req 7.1: no more than 1000 characters.
    assert len(text) <= NUDGE_DRAFT_MAX_CHARS

    # The draft is posted to the source channel and nothing is sent during drafting.
    assert result.draft.channel == "C_GENERAL"
    assert sender.count == 0


def test_draft_is_clipped_to_max_chars_when_model_overshoots() -> None:
    """Validates: Requirement 7.1.

    Even if Claude returns more than 1000 characters, the Action Agent clips the
    draft so the 1000-character ceiling always holds.
    """
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    sender = _RecordingSender()

    overlong = "x" * (NUDGE_DRAFT_MAX_CHARS + 500)
    agent = _agent(graph, claude_draft=lambda ob: overlong, sender=sender)

    result = agent.draft_polite_nudge(_obligation())

    assert result.drafted is True
    assert result.draft is not None
    assert len(result.draft.text) == NUDGE_DRAFT_MAX_CHARS
    assert sender.count == 0


# =========================================================================== #
# Req 7.8 — draft failure / timeout reports failure and sends nothing
# =========================================================================== #
@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("Claude is unavailable"),
        TimeoutError("drafting exceeded the 10s budget"),
    ],
    ids=["error", "timeout"],
)
def test_draft_failure_reports_failure_and_sends_nothing(boom: Exception) -> None:
    """Validates: Requirement 7.8.

    When the Claude drafting port raises (a plain failure or a timeout), the draft
    path reports failure, produces no draft, and nothing is ever sent as the user.
    """
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    sender = _RecordingSender()

    def failing_claude(ob: Obligation) -> str:
        raise boom

    agent = _agent(graph, claude_draft=failing_claude, sender=sender)
    result = agent.draft_polite_nudge(_obligation())

    # Req 7.8: failure is reported, no draft is produced.
    assert result.drafted is False
    assert result.draft is None
    assert result.message is not None  # user is informed drafting failed
    assert result.error is not None

    # Req 7.8: nothing is sent on the draft-failure path.
    assert sender.count == 0
