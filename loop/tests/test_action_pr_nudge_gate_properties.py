"""Property-based test for Property 17 — task 12.5.

**Property 17: PR-referencing nudge requires an unresolved loop.**

For any obligation that references a GitHub pull request, a Polite Nudge
(:meth:`loop.action.action_agent.ActionAgent.send_polite_nudge`) proceeds only
when the Verifier reports the obligation as **not** RESOLVED; if the Verifier
reports RESOLVED, the nudge is **cancelled** and nothing is sent.

The single invariant proven here, over all PR-referencing obligations and every
``VerificationResult`` value, with the send already confirmed by the user:

  * **Verifier RESOLVED ⟹ nudge cancelled, zero sends.** A confirmed nudge on a
    PR-referencing obligation whose loop the Verifier reports as already resolved
    (merged) is cancelled before it reaches the wire — the injected Slack
    "send as user" port is invoked **0** times and the result is flagged
    ``cancelled`` (Req 7.5).
  * **Verifier UNRESOLVED / UNVERIFIED ⟹ nudge proceeds, exactly one send.** A
    confirmed nudge on a PR-referencing obligation whose loop is *not* RESOLVED
    clears the gate and is posted as the user exactly **1** time; a reminder on an
    open or unknown PR state is safe and still user-confirmed (Req 7.4).

These tests run against the *real* Action Agent and the *real* in-memory
Obligation Graph; only the outward Slack "send as user" port and the Verifier are
mocked so the send count and the gate decision can be asserted exactly. Each
property runs ≥100 Hypothesis examples (enforced by the root ``conftest.py``) and
carries the Property 17 traceability tag.

Validates: Requirements 7.4, 7.5.
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
# Test doubles (mirrored from test_action_no_send_without_confirmation_properties)
# --------------------------------------------------------------------------- #
class _RecordingSender:
    """A mock "send as user" port that counts every send and always succeeds.

    The whole property reduces to assertions on :attr:`count`: it must be 0 when
    the Verifier gate cancels the nudge (RESOLVED) and exactly 1 when the gate is
    cleared (UNRESOLVED / UNVERIFIED).
    """

    def __init__(self) -> None:
        self.count = 0
        self.calls: list[tuple[str, str]] = []

    def __call__(self, recipient_id: str, text: str) -> None:
        self.count += 1
        self.calls.append((recipient_id, text))


class _FakeVerifier:
    """A mock Verifier returning a fixed three-valued result for any PR.

    Records the purpose it was called with so the test can confirm the nudge gate
    invokes the Verifier with :data:`VerifyPurpose.NUDGE` (Req 7.4).
    """

    def __init__(self, result: VerificationResult) -> None:
        self.result = result
        self.calls: list[VerifyPurpose] = []

    def verify_pr(
        self, obligation: Obligation, *, purpose: VerifyPurpose
    ) -> VerificationResult:
        self.calls.append(purpose)
        return self.result


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

# PR artifact references like "octo/repo#123" — exercises a range of valid refs.
_PR_REFS = st.builds(
    lambda owner, repo, num: f"{owner}/{repo}#{num}",
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=8),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=8),
    st.integers(min_value=1, max_value=99999),
)

_FIXED_TS = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat()


@st.composite
def _pr_obligations(draw: st.DrawFn) -> Obligation:
    """A valid Obligation that ALWAYS references a GitHub PR.

    The nudge gate keys solely off ``artifact_type == GITHUB_PR`` with a non-empty
    ``artifact_ref``; loop_state/confidence do not affect the gate, so they range
    freely to prove the PR gate is independent of surfacing fields.
    """
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
        artifact_type=ArtifactType.GITHUB_PR,
        artifact_ref=draw(_PR_REFS),
    )


# =========================================================================== #
# Property 17 — PR-referencing nudge requires an unresolved loop (Req 7.4, 7.5)
# =========================================================================== #
# Feature: loop-obligation-agent, Property 17: PR-referencing nudge requires an unresolved loop
@given(
    obligation=_pr_obligations(),
    verifier_result=st.sampled_from(list(VerificationResult)),
)
def test_property_17_pr_nudge_requires_unresolved_loop(
    obligation: Obligation,
    verifier_result: VerificationResult,
) -> None:
    """Validates: Requirements 7.4, 7.5.

    A confirmed Polite Nudge on a PR-referencing obligation consults the Verifier
    (purpose NUDGE) before sending:

      * RESOLVED → the nudge is cancelled and nothing is posted as the user
        (Req 7.5).
      * UNRESOLVED / UNVERIFIED → the gate is cleared and the nudge is posted as
        the user exactly once (Req 7.4).
    """
    graph = _graph()
    sender = _RecordingSender()
    verifier = _FakeVerifier(verifier_result)
    agent = ActionAgent(
        graph,
        verifier=verifier,  # type: ignore[arg-type]
        slack_send_as_user=sender,
    )

    draft = NudgeDraft(
        obligation=obligation,
        text="Just a friendly nudge on this PR.",
        channel=obligation.source_msg_channel,
    )

    # The send is confirmed; the only thing that may stop it is the PR Verifier gate.
    result = agent.send_polite_nudge(draft, confirmed=True)

    # The PR gate must always consult the Verifier with the NUDGE purpose (Req 7.4).
    assert verifier.calls == [VerifyPurpose.NUDGE]
    assert result.verification is verifier_result

    if verifier_result is VerificationResult.RESOLVED:
        # Req 7.5: the loop is already resolved — cancel, send nothing.
        assert result.cancelled is True
        assert result.sent is False
        assert sender.count == 0
    else:
        # Req 7.4: not RESOLVED (UNRESOLVED / UNVERIFIED) — the nudge proceeds and
        # is posted as the user exactly once.
        assert result.cancelled is False
        assert result.sent is True
        assert sender.count == 1
        # It is posted in the source channel, as the user (Req 7.3 edge, exercised
        # here for the proceed branch).
        assert sender.calls == [(draft.channel, draft.text)]
