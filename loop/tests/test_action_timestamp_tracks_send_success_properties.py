"""Property-based test for Property 18 — task 12.6.

**Property 18: Timestamp updates track send success.**

For any Polite Nudge or delegation, the obligation's Last_Touch_Timestamp (and,
for delegation, its owner) is updated *if and only if* the send succeeds after
confirmation; on cancel or send failure the obligation is left unchanged.

The single biconditional invariant proven here, across both send-as-user paths
on the real :class:`loop.action.action_agent.ActionAgent`:

  * **Polite Nudge** (:meth:`ActionAgent.send_polite_nudge`) — the
    Last_Touch_Timestamp advances to the send time *exactly* when the nudge is
    confirmed, not cancelled by the PR-already-resolved Verifier gate (Req 7.5),
    and the send succeeds (Req 7.6). On no-confirm, a RESOLVED-PR cancel, or a
    send failure the obligation is returned unchanged (Req 7.7).
  * **Delegation** (:meth:`ActionAgent.delegate`) — the owner is reassigned to
    the teammate and the Last_Touch_Timestamp advances to the confirmation time
    *exactly* when the delegation is confirmed and the send succeeds within the
    bounded retries (Req 10.4). On cancel (Req 10.2) or a send failure after all
    attempts (Req 10.5) the obligation — owner and timestamp — is unchanged.

These tests run against the *real* Action Agent and the *real* in-memory
Obligation Graph; only the outward Slack "send as user" port and the Verifier
are mocked, so send success/failure can be driven deterministically. The clock
is injected to a send time distinct from the obligation's initial
Last_Touch_Timestamp, so a successful send is observable as a concrete change.
Each property runs >=100 Hypothesis examples (enforced by the root
``conftest.py``).

Validates: Requirements 7.6, 7.7, 10.2, 10.4, 10.5.
"""

from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import given
from hypothesis import strategies as st

from loop.action.action_agent import ActionAgent, NudgeDraft
from loop.graph.models import ArtifactType, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.verifier.types import VerificationResult, VerifyPurpose


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _ConfigurableSender:
    """A mock "send as user" port that succeeds or always raises.

    When ``succeeds`` is False every invocation raises, which (for delegation)
    exhausts the bounded retries and drives the send-failure branch; for a nudge
    the single attempt fails. This is what lets the "send failure ⟹ unchanged"
    half of the biconditional be exercised deterministically.
    """

    def __init__(self, succeeds: bool) -> None:
        self.succeeds = succeeds
        self.count = 0

    def __call__(self, recipient_id: str, text: str) -> None:
        self.count += 1
        if not self.succeeds:
            raise RuntimeError("send failed (injected)")


class _FakeVerifier:
    """A mock Verifier returning a fixed three-valued result for any PR."""

    def __init__(self, result: VerificationResult) -> None:
        self.result = result

    def verify_pr(
        self, obligation: Obligation, *, purpose: VerifyPurpose
    ) -> VerificationResult:
        return self.result


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


# The obligation's initial Last_Touch_Timestamp and the (distinct) injected send
# time. Because they differ, a successful send is observable as a concrete change
# while a no-send path leaves the original value verbatim.
_INITIAL_TS = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat()
_SEND_TS = datetime(2025, 6, 1, 9, 30, 0, tzinfo=timezone.utc).isoformat()


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


@st.composite
def _send_obligations(draw: st.DrawFn) -> Obligation:
    """A valid Obligation that may or may not reference a GitHub PR.

    The optional PR artifact lets the nudge's RESOLVED-cancel gate (Req 7.5) be
    exercised; the send paths otherwise do not gate on loop_state/confidence, so
    those range freely. The Last_Touch_Timestamp starts at :data:`_INITIAL_TS`.
    """
    has_pr = draw(st.booleans())
    return Obligation(
        obligation_id=draw(_IDS),
        owes_person_id=draw(_IDS),
        owed_person_id=draw(_IDS),
        owner_person_id=draw(_IDS),
        loop_state=draw(st.sampled_from(list(LoopState))),
        confidence_score=draw(_UNIT_FLOAT),
        last_touch_timestamp=_INITIAL_TS,
        source_msg_channel=draw(_IDS),
        source_msg_ts=draw(_SAFE_TEXT),
        subject_summary=draw(_SAFE_TEXT),
        artifact_type=ArtifactType.GITHUB_PR if has_pr else None,
        artifact_ref="octo/repo#1" if has_pr else None,
    )


# =========================================================================== #
# Path 1 — Polite Nudge (ActionAgent.send_polite_nudge): Req 7.6, 7.7
# =========================================================================== #
# Feature: loop-obligation-agent, Property 18: Timestamp updates track send success
@given(
    obligation=_send_obligations(),
    confirmed=st.booleans(),
    verifier_result=st.sampled_from(list(VerificationResult)),
    send_succeeds=st.booleans(),
)
def test_property_18_nudge_timestamp_tracks_send_success(
    obligation: Obligation,
    confirmed: bool,
    verifier_result: VerificationResult,
    send_succeeds: bool,
) -> None:
    """Validates: Requirements 7.6, 7.7.

    The nudge advances Last_Touch_Timestamp to the send time *iff* it is
    confirmed, not cancelled by a RESOLVED PR, and the send succeeds; otherwise
    the obligation is returned unchanged.
    """
    graph = _graph()
    sender = _ConfigurableSender(send_succeeds)
    agent = ActionAgent(
        graph,
        verifier=_FakeVerifier(verifier_result),  # type: ignore[arg-type]
        now=lambda: _SEND_TS,
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
    cancelled = is_pr and verifier_result is VerificationResult.RESOLVED
    should_update = confirmed and not cancelled and send_succeeds

    if should_update:
        # Req 7.6: send succeeded after confirmation — the timestamp advanced to
        # the send time and nothing else about the obligation changed.
        assert result.sent is True
        assert result.obligation.last_touch_timestamp == _SEND_TS
        assert result.obligation == obligation.model_copy(
            update={"last_touch_timestamp": _SEND_TS}
        )
    else:
        # Req 7.7 (and no-confirm / RESOLVED-cancel): the obligation is unchanged.
        assert result.sent is False
        assert result.obligation == obligation
        assert result.obligation.last_touch_timestamp == _INITIAL_TS


# =========================================================================== #
# Path 2 — Delegation (ActionAgent.delegate): Req 10.2, 10.4, 10.5
# =========================================================================== #
# Feature: loop-obligation-agent, Property 18: Timestamp updates track send success
@given(
    obligation=_send_obligations(),
    teammate_id=_IDS,
    confirmed=st.booleans(),
    send_succeeds=st.booleans(),
)
def test_property_18_delegate_timestamp_and_owner_track_send_success(
    obligation: Obligation,
    teammate_id: str,
    confirmed: bool,
    send_succeeds: bool,
) -> None:
    """Validates: Requirements 10.2, 10.4, 10.5.

    Delegation reassigns the owner to the teammate and advances
    Last_Touch_Timestamp to the confirmation time *iff* it is confirmed and the
    send succeeds; on cancel (Req 10.2) or send failure after all attempts
    (Req 10.5) the owner and timestamp are unchanged.
    """
    graph = _graph()
    sender = _ConfigurableSender(send_succeeds)
    agent = ActionAgent(
        graph,
        verifier=_FakeVerifier(VerificationResult.UNRESOLVED),  # type: ignore[arg-type]
        now=lambda: _SEND_TS,
        slack_send_as_user=sender,
    )

    result = agent.delegate(obligation, teammate_id, confirmed=confirmed)

    should_update = confirmed and send_succeeds

    if should_update:
        # Req 10.4: send succeeded after confirmation — owner reassigned to the
        # teammate and timestamp advanced to the confirmation time.
        assert result.delegated is True
        assert result.sent is True
        assert result.obligation.owner_person_id == teammate_id
        assert result.obligation.last_touch_timestamp == _SEND_TS
        assert result.obligation == obligation.model_copy(
            update={
                "owner_person_id": teammate_id,
                "last_touch_timestamp": _SEND_TS,
            }
        )
    else:
        # Req 10.2 (cancel) / Req 10.5 (send failure): obligation unchanged.
        assert result.delegated is False
        assert result.obligation == obligation
        assert result.obligation.owner_person_id == obligation.owner_person_id
        assert result.obligation.last_touch_timestamp == _INITIAL_TS
