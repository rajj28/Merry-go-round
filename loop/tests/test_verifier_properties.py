"""Property-based test for the Verifier's status mapping (task 8.2 — Property 13).

This is the single numbered correctness property for the Verifier's status
mapping, complementing the example-based unit tests in ``test_verifier.py`` and
the integration/smoke test in ``test_verifier_smoke.py``.

It exercises the *successful-retrieval* branch of ``Verifier.verify_pr`` across the
whole reachable PR-status input space: for ANY Pull_Request_Status the GitHub MCP
could return, the Verifier reports RESOLVED iff the PR is merged, and UNRESOLVED
for every other reachable status (open, closed-without-merge). The transport
failure outcome (UNVERIFIED) is a separate concern owned by the call/retry logic
and is covered by the unit + smoke tests, so this property deliberately drives a
client that always succeeds.

Runs >=100 Hypothesis examples (enforced by the workspace conftest profile).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from hypothesis import given
from hypothesis import strategies as st

from loop.graph.models import ArtifactType, LoopState, Obligation
from loop.verifier.types import VerificationResult, VerifyPurpose
from loop.verifier.verifier import Verifier


# --------------------------------------------------------------------------- #
# Test double
# --------------------------------------------------------------------------- #
class FixedPayloadClient:
    """A mock PR-status client that always succeeds, returning a fixed payload.

    Drives only the successful-retrieval branch so the property isolates the
    status -> result mapping (merged -> RESOLVED, else UNRESOLVED). It records the
    per-call timeout purely so the property can assert the call actually happened.
    """

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload
        self.calls = 0

    def __call__(
        self, owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        self.calls += 1
        return self._payload


def _pr_obligation() -> Obligation:
    """A minimal PR-referencing Obligation to verify."""
    return Obligation(
        obligation_id="ob-prop-13",
        owes_person_id="U_DEV",
        owed_person_id="U_USER",
        owner_person_id="U_DEV",
        loop_state=LoopState.WAITING_ON_OTHER,
        confidence_score=0.9,
        last_touch_timestamp="2025-01-08T12:00:00+00:00",
        source_msg_channel="C123",
        source_msg_ts="1700000000.000100",
        subject_summary="PR review",
        artifact_type=ArtifactType.GITHUB_PR,
        artifact_ref="acme/throwaway#42",
    )


# --------------------------------------------------------------------------- #
# Strategy: a reachable GitHub MCP PR-status payload
# --------------------------------------------------------------------------- #
# Smart generator constrained to the real input space the GitHub MCP can return:
#   * state          : GitHub only ever reports "open" or "closed" in `state`
#                      (merge is signalled by the `merged` boolean, never by state)
#   * merged         : authoritative merge signal
#   * merged_at      : ISO-8601 UTC string when merged, else null
#   * review_decision: optional review state, orthogonal to the merge gate
# The three reachable demo outcomes that fall out of this product are:
#   merged=True (state="closed")            -> merged           -> RESOLVED
#   merged=False, state="open"              -> open             -> UNRESOLVED
#   merged=False, state="closed"            -> closed-no-merge  -> UNRESOLVED
_REVIEW_DECISIONS = st.sampled_from(
    [None, "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"]
)


@st.composite
def pr_status_payloads(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a raw ``get_pull_request`` payload spanning every reachable status."""
    merged = draw(st.booleans())
    # GitHub never reports a merged PR with state "open": a merged PR is closed.
    # An unmerged PR may be either open or closed (closed-without-merge).
    if merged:
        state = "closed"
        merged_at: Optional[str] = draw(
            st.datetimes().map(lambda d: d.isoformat() + "Z")
        )
    else:
        state = draw(st.sampled_from(["open", "closed"]))
        merged_at = None
    return {
        "number": draw(st.integers(min_value=1, max_value=1_000_000)),
        "state": state,
        "merged": merged,
        "merged_at": merged_at,
        "review_decision": draw(_REVIEW_DECISIONS),
    }


# --------------------------------------------------------------------------- #
# Property 13: Verifier status mapping
# --------------------------------------------------------------------------- #
# Feature: loop-obligation-agent, Property 13: For any Pull_Request_Status returned
# by the GitHub MCP, the Verifier reports RESOLVED iff the PR is merged, and
# UNRESOLVED for every other reachable status (open, closed-without-merge).
# Validates: Requirements 4.3, 4.4, 8.7
@given(payload=pr_status_payloads(), purpose=st.sampled_from(list(VerifyPurpose)))
def test_verifier_status_mapping(payload: dict[str, Any], purpose: VerifyPurpose) -> None:
    """merged -> RESOLVED, every other reachable status -> UNRESOLVED (Req 4.3, 4.4, 8.7)."""
    client = FixedPayloadClient(payload)
    verifier = Verifier(client)

    result = verifier.verify_pr(_pr_obligation(), purpose=purpose)

    if payload["merged"]:
        # Req 4.3: a merged PR is reported RESOLVED.
        assert result is VerificationResult.RESOLVED
    else:
        # Req 4.4 / 8.7: any reachable non-merged status (open or
        # closed-without-merge) is reported UNRESOLVED.
        assert result is VerificationResult.UNRESOLVED

    # A successful retrieval never produces UNVERIFIED (that is the transport-
    # failure outcome, not derivable from a response body).
    assert result is not VerificationResult.UNVERIFIED
    # Exactly one successful retrieval was needed (no retries on success).
    assert client.calls == 1
