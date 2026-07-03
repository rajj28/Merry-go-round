"""Unit tests for the authoritative Verifier result contract (task 2.4).

Covers the enum members of ``VerificationResult`` / ``VerifyPurpose`` and the
``from_intent`` adapter that lines ``mcp_contract.VerificationIntent`` up with the
authoritative ``VerificationResult`` (Req 4.3, 4.4, 4.5).
"""

from __future__ import annotations

import pytest

from loop.verifier.mcp_contract import VerificationIntent
from loop.verifier.types import VerificationResult, VerifyPurpose, from_intent


def test_verification_result_members() -> None:
    # Exactly the three safety-interlock values, with stable string values.
    assert {r.value for r in VerificationResult} == {
        "resolved",
        "unresolved",
        "unverified",
    }
    assert VerificationResult.RESOLVED.value == "resolved"      # Req 4.3
    assert VerificationResult.UNRESOLVED.value == "unresolved"  # Req 4.4
    assert VerificationResult.UNVERIFIED.value == "unverified"  # Req 4.5


def test_verify_purpose_members() -> None:
    assert {p.value for p in VerifyPurpose} == {"auto_close", "nudge"}
    assert VerifyPurpose.AUTO_CLOSE.value == "auto_close"  # Req 8.1 (30s budget)
    assert VerifyPurpose.NUDGE.value == "nudge"            # Req 4.2 (10s budget)


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        (VerificationIntent.RESOLVED, VerificationResult.RESOLVED),      # Req 4.3
        (VerificationIntent.UNRESOLVED, VerificationResult.UNRESOLVED),  # Req 4.4, 8.7
        (VerificationIntent.UNVERIFIED, VerificationResult.UNVERIFIED),  # Req 4.5 (defensive)
    ],
)
def test_from_intent_maps_each_value(
    intent: VerificationIntent, expected: VerificationResult
) -> None:
    assert from_intent(intent) is expected


def test_from_intent_is_total_over_all_intents() -> None:
    # Every VerificationIntent member maps to some VerificationResult member.
    for intent in VerificationIntent:
        assert isinstance(from_intent(intent), VerificationResult)


def test_contract_mapping_never_yields_unverified() -> None:
    # The contract mapping only classifies *retrieved* statuses; UNVERIFIED is the
    # Verifier-owned transport-failure value (Req 4.5/8.6) and must not arise from a
    # successfully-mapped PR status.
    from loop.verifier.mcp_contract import (
        SAMPLE_CLOSED_WITHOUT_MERGE,
        SAMPLE_MERGED,
        SAMPLE_OPEN,
        PullRequestStatus,
        to_verification_intent,
    )

    for payload in (SAMPLE_MERGED, SAMPLE_OPEN, SAMPLE_CLOSED_WITHOUT_MERGE):
        status = PullRequestStatus.from_mcp_response(payload)
        result = from_intent(to_verification_intent(status))
        assert result is not VerificationResult.UNVERIFIED

    # And the merge gate lines up end-to-end: merged -> RESOLVED, others -> UNRESOLVED.
    merged = from_intent(to_verification_intent(PullRequestStatus.from_mcp_response(SAMPLE_MERGED)))
    assert merged is VerificationResult.RESOLVED
    open_pr = from_intent(to_verification_intent(PullRequestStatus.from_mcp_response(SAMPLE_OPEN)))
    assert open_pr is VerificationResult.UNRESOLVED
