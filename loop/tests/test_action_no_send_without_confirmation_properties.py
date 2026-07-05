"""Property-based test for Property 16 (SAFETY-CRITICAL) — task 12.4.

**Property 16: No message is sent as the user without confirmation.**

For any action that sends a message *as the user* — a Polite Nudge
(:meth:`loop.action.action_agent.ActionAgent.send_polite_nudge`), a delegation
(:meth:`loop.action.action_agent.ActionAgent.delegate`), or a Conversational
send command routed through
:class:`loop.conversational.conversational_agent.ConversationalAgent` — **no
message is sent unless a one-tap confirmation has been received; when
confirmation is received the message is sent exactly once.**

The single safety-critical invariant proven here, across all three send paths:

  * **No confirmation ⟹ zero sends.** Before the user confirms (the
    ``confirmed=False`` default on the Action Agent, and the pre-``confirm`` /
    declined / 60s-timed-out states in the pane), the injected Slack "send as
    user" port is invoked exactly **0** times.
  * **Confirmation ⟹ exactly one send.** When the user confirms and the send is
    not otherwise cancelled (the only cancellation is the PR-already-resolved
    Verifier gate on a nudge, Req 7.5), the port is invoked exactly **1** time.

These tests run against the *real* Action Agent and Conversational Agent and the
*real* in-memory Obligation Graph; only the outward Slack "send as user" port,
the Verifier, and LLM drafting are mocked so the send count can be asserted
exactly. Each property runs ≥100 Hypothesis examples (enforced by the root
``conftest.py``) and carries the Property 16 traceability tag.

Validates: Requirements 7.2, 7.3, 10.1, 12.6, 13.2.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from loop.action.action_agent import ActionAgent, NudgeDraft
from loop.conversational.conversational_agent import (
    CommandAction,
    ConversationalAgent,
    ParsedCommand,
    Selector,
)
from loop.graph.models import ArtifactType, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import ObligationFilter
from loop.verifier.types import VerificationResult, VerifyPurpose


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _RecordingSender:
    """A mock "send as user" port that counts every send and always succeeds.

    The whole property reduces to assertions on :attr:`count`: it must be 0
    before confirmation and exactly 1 once confirmation drives a send.
    """

    def __init__(self) -> None:
        self.count = 0
        self.calls: list[tuple[str, str]] = []

    def __call__(self, recipient_id: str, text: str) -> None:
        self.count += 1
        self.calls.append((recipient_id, text))


class _FakeVerifier:
    """A mock Verifier returning a fixed three-valued result for any PR.

    Lets the PR-referencing nudge gate (Req 7.4, 7.5) be driven deterministically
    so the "exactly once on confirm" branch and the RESOLVED-cancel branch are
    both exercised.
    """

    def __init__(self, result: VerificationResult) -> None:
        self.result = result

    def verify_pr(
        self, obligation: Obligation, *, purpose: VerifyPurpose
    ) -> VerificationResult:
        return self.result


class _Clock:
    """A mutable ISO 8601 UTC clock for driving the 60s confirmation window."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def iso(self) -> str:
        return self._t.isoformat()

    def advance(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


# --------------------------------------------------------------------------- #
# Hypothesis strategies
# --------------------------------------------------------------------------- #
_IDS = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=12,
)

_SAFE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=40,
)

_UNIT_FLOAT = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)

_FIXED_TS = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat()


@st.composite
def _send_obligations(draw: st.DrawFn) -> Obligation:
    """A valid Obligation that may or may not reference a GitHub PR.

    The send paths under test do not gate on loop_state/confidence (only the
    nudge's PR Verifier gate matters), so those fields range freely; the optional
    PR artifact lets the RESOLVED-cancel branch be exercised.
    """
    has_pr = draw(st.booleans())
    return Obligation(
        obligation_id=draw(_IDS),
        owes_person_id=draw(_IDS),
        owed_person_id=draw(_IDS),
        owner_person_id=draw(_IDS),
        loop_state=draw(st.sampled_from(list(LoopState))),
        confidence_score=draw(_UNIT_FLOAT),
        last_touch_timestamp=_FIXED_TS,
        source_msg_channel=draw(_IDS),
        source_msg_ts=draw(_SAFE_TEXT),
        subject_summary=draw(_SAFE_TEXT),
        artifact_type=ArtifactType.GITHUB_PR if has_pr else None,
        artifact_ref="octo/repo#1" if has_pr else None,
    )


def _surfaced_obligation(obligation_id: str = "OBL_PANE") -> Obligation:
    """A single surfaced, active, non-PR obligation for the pane flow.

    Surfaced (active state, high confidence, not dismissed/snoozed) so the
    Conversational Agent resolves it as the command's target; non-PR so the nudge
    path is not affected by the Verifier gate and a confirmed send is exactly one.
    """
    return Obligation(
        obligation_id=obligation_id,
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.95,
        last_touch_timestamp=_FIXED_TS,
        source_msg_channel="C_PANE",
        source_msg_ts="1700000000.000100",
        subject_summary="Ship the release notes",
    )


# =========================================================================== #
# Path 1 — Polite Nudge (ActionAgent.send_polite_nudge): Req 7.2, 7.3, 13.2
# =========================================================================== #
# Feature: loop-obligation-agent, Property 16: No message is sent as the user without confirmation
@given(
    obligation=_send_obligations(),
    confirmed=st.booleans(),
    verifier_result=st.sampled_from(list(VerificationResult)),
)
def test_property_16_polite_nudge_no_send_without_confirmation(
    obligation: Obligation,
    confirmed: bool,
    verifier_result: VerificationResult,
) -> None:
    """Validates: Requirements 7.2, 7.3, 13.2.

    A Polite Nudge is posted as the user only after a one-tap confirm:
    ``confirmed=False`` sends nothing; ``confirmed=True`` sends exactly once
    unless the PR Verifier gate cancels it because the loop is already resolved
    (Req 7.5).
    """
    graph = _graph()
    sender = _RecordingSender()
    agent = ActionAgent(
        graph,
        verifier=_FakeVerifier(verifier_result),  # type: ignore[arg-type]
        slack_send_as_user=sender,
    )

    draft = NudgeDraft(
        obligation=obligation,
        text="Just a friendly nudge on this.",
        channel=obligation.source_msg_channel,
    )

    result = agent.send_polite_nudge(draft, confirmed=confirmed)

    is_pr = obligation.artifact_type is ArtifactType.GITHUB_PR and bool(
        obligation.artifact_ref
    )
    if not confirmed:
        # SAFETY: no confirmation ⟹ no message is ever posted as the user.
        expected_sends = 0
    elif is_pr and verifier_result is VerificationResult.RESOLVED:
        # The only confirmed-but-cancelled path: PR already resolved (Req 7.5).
        expected_sends = 0
    else:
        # Confirmation received and not cancelled ⟹ sent exactly once.
        expected_sends = 1

    assert sender.count == expected_sends
    # The core safety invariant, asserted independently of the gate outcome.
    if not confirmed:
        assert sender.count == 0
        assert result.sent is False


# =========================================================================== #
# Path 2 — Delegation (ActionAgent.delegate): Req 10.1
# =========================================================================== #
# Feature: loop-obligation-agent, Property 16: No message is sent as the user without confirmation
@given(
    obligation=_send_obligations(),
    confirmed=st.booleans(),
    teammate_id=_IDS,
)
def test_property_16_delegate_no_send_without_confirmation(
    obligation: Obligation,
    confirmed: bool,
    teammate_id: str,
) -> None:
    """Validates: Requirements 10.1.

    A delegation handoff message is authored as the user only after a one-tap
    confirm: ``confirmed=False`` sends nothing; ``confirmed=True`` sends exactly
    once (the send succeeds on the first attempt).
    """
    graph = _graph()
    sender = _RecordingSender()
    agent = ActionAgent(
        graph,
        verifier=_FakeVerifier(VerificationResult.UNRESOLVED),  # type: ignore[arg-type]
        slack_send_as_user=sender,
    )

    result = agent.delegate(obligation, teammate_id, confirmed=confirmed)

    if confirmed:
        assert sender.count == 1            # confirmation ⟹ exactly one send
        assert result.sent is True
    else:
        assert sender.count == 0            # SAFETY: no confirm ⟹ no send
        assert result.sent is False
        assert result.delegated is False


# =========================================================================== #
# Path 3 — Conversational send command (ConversationalAgent): Req 12.6
# =========================================================================== #
# The four ways a pending send-as-user command can resolve in the pane.
_PANE_OUTCOME = st.sampled_from(["confirm", "decline", "timeout", "no_action"])


# Feature: loop-obligation-agent, Property 16: No message is sent as the user without confirmation
@given(
    action=st.sampled_from([CommandAction.NUDGE, CommandAction.DELEGATE]),
    outcome=_PANE_OUTCOME,
    delay_seconds=st.floats(min_value=0.0, max_value=600.0),
)
def test_property_16_conversational_send_requires_confirmation(
    action: CommandAction,
    outcome: str,
    delay_seconds: float,
) -> None:
    """Validates: Requirements 12.6, 7.2, 13.2.

    A send-as-user command issued in the Assistant pane (nudge or delegate) never
    posts as the user when it is merely prepared; it posts exactly once only when
    the user confirms within the 60-second window. A decline, a too-late confirm,
    or no response at all sends nothing.
    """
    graph = _graph()
    graph.upsert(_surfaced_obligation())
    sender = _RecordingSender()
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))

    action_agent = ActionAgent(
        graph,
        verifier=_FakeVerifier(VerificationResult.UNRESOLVED),  # type: ignore[arg-type]
        now=clock.iso,
        slack_send_as_user=sender,
        draft=lambda obligation: "Drafted nudge text.",
    )

    # A deterministic parser that maps any text to the chosen send-as-user
    # command, targeting the single surfaced obligation (selector collapses the
    # candidate set to one, so no disambiguation).
    def parser(text: str) -> ParsedCommand:
        return ParsedCommand(
            action=action,
            filter=ObligationFilter(
                loop_states=frozenset(
                    {LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER}
                ),
                surfaced_only=True,
            ),
            selector=Selector.OLDEST,
            teammate_id="U_TEAMMATE" if action is CommandAction.DELEGATE else None,
        )

    convo = ConversationalAgent(
        graph, action_agent, parser=parser, now=clock.iso
    )

    user = "U_USER"

    # Preparing the command must never send before the user confirms (Req 12.6).
    prepare_reply = convo.handle_command(user, "nudge the oldest one")
    assert prepare_reply.requires_confirmation is True
    assert sender.count == 0  # SAFETY: nothing sent on preparation

    # Resolve the pending confirmation one of four ways.
    if outcome == "confirm":
        within_window = delay_seconds < 60.0
        clock.advance(delay_seconds)
        convo.confirm(user)
        # A confirm inside the 60s window sends exactly once; a too-late confirm
        # is treated as a timeout and cancels with no send (Req 12.7).
        assert sender.count == (1 if within_window else 0)
    elif outcome == "decline":
        convo.decline(user)
        assert sender.count == 0  # explicit decline ⟹ no send (Req 12.7)
    elif outcome == "timeout":
        clock.advance(delay_seconds + 60.0)  # guaranteed past the window
        convo.check_timeout(user)
        assert sender.count == 0  # 60s silence ⟹ no send (Req 12.7)
    else:  # no_action — the user simply never responds
        assert sender.count == 0  # still pending ⟹ nothing sent
