"""Integration / smoke test for GitHub MCP mapping + timeout fallback (task 8.3).

Example-based (1-3 examples) end-to-end check of the Verifier against the GitHub
MCP *port* — the injectable ``PullRequestStatusClient`` the Verifier calls. It
confirms the three demo-relevant status mappings and the transport-failure
fallback policy, exercising the same recorded payload shapes the spike captured
(``loop/spikes/mcp_spike.py``; see ``loop/spikes/SETUP_GITHUB_MCP.md``):

    merged                -> RESOLVED                              (Req 4.3 / 8.2)
    open                  -> UNRESOLVED                            (Req 4.4)
    closed-without-merge  -> UNRESOLVED                            (Req 4.4 / 8.7)
    always-failing client -> UNVERIFIED after <= 3 attempts        (Req 4.5 / 8.6)

It also asserts the per-purpose timeout budget the Verifier hands each call:
NUDGE 10s, AUTO_CLOSE 30s (Req 4.2 / 8.1).

-----------------------------------------------------------------------------
POINTING THIS AT A REAL SERVER LATER
-----------------------------------------------------------------------------
No live GitHub MCP server is available in this environment, so the test drives a
*recorded/mocked* client by default. The recorded payloads
(``RECORDED_MERGED`` / ``RECORDED_OPEN`` / ``RECORDED_CLOSED``) are the exact
shapes the spike round-trips. To run this against a real server later:

  1. Provision a throwaway repo with three PRs (merged / open / closed-no-merge)
     per ``loop/spikes/SETUP_GITHUB_MCP.md``.
  2. Set GITHUB_MCP_URL / GITHUB_MCP_TOKEN and build the live client with
     ``loop.verifier.verifier.build_github_mcp_client`` (the same port shape used
     here), then point ``_live_client_for`` at it.
  3. The ``LIVE_MCP_PR_REFS`` mapping below documents the owner/repo#number to use
     for each expected status; the live test is skipped unless that env is set.

The mapping assertions and the retry/timeout assertions are written against the
injectable port, so they hold identically for the recorded client and a real one.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

import pytest

from loop.graph.models import ArtifactType, LoopState, Obligation
from loop.verifier.mcp_contract import (
    SAMPLE_CLOSED_WITHOUT_MERGE,
    SAMPLE_MERGED,
    SAMPLE_OPEN,
)
from loop.verifier.types import VerificationResult, VerifyPurpose
from loop.verifier.verifier import (
    DEFAULT_MAX_ATTEMPTS,
    Verifier,
    is_auto_close_permitted,
)

# Recorded payload shapes (the exact bodies the spike exercises). Aliased to the
# frozen contract samples so the recorded fixtures and the contract never drift.
RECORDED_MERGED: Mapping[str, Any] = SAMPLE_MERGED
RECORDED_OPEN: Mapping[str, Any] = SAMPLE_OPEN
RECORDED_CLOSED: Mapping[str, Any] = SAMPLE_CLOSED_WITHOUT_MERGE

# Documents the throwaway-repo PR references to use when this is pointed at a real
# GitHub MCP server (see SETUP_GITHUB_MCP.md). Unused by the recorded-client run.
LIVE_MCP_PR_REFS: dict[str, str] = {
    "merged": "acme/throwaway#42",
    "open": "acme/throwaway#43",
    "closed": "acme/throwaway#44",
}


# --------------------------------------------------------------------------- #
# Recorded / mocked port implementations (swap for a live client later)
# --------------------------------------------------------------------------- #
class RecordedClient:
    """A recorded ``PullRequestStatusClient`` returning a fixed payload.

    Stands in for a real GitHub MCP round-trip; records the per-call timeout the
    Verifier passes so the budget can be asserted.
    """

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload
        self.timeouts: list[float] = []

    def __call__(
        self, owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        self.timeouts.append(timeout)
        return self._payload


class AlwaysRaisingClient:
    """A client that always raises, simulating an unreachable/timing-out server."""

    def __init__(self) -> None:
        self.attempts = 0

    def __call__(
        self, owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        self.attempts += 1
        raise ConnectionError("GitHub MCP unreachable (recorded failure)")


def _zero_clock() -> float:
    """A fixed monotonic clock so each attempt sees the full per-purpose budget."""
    return 0.0


def _pr_obligation(artifact_ref: str = "acme/throwaway#42") -> Obligation:
    return Obligation(
        obligation_id="ob-smoke",
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
        artifact_ref=artifact_ref,
    )


# --------------------------------------------------------------------------- #
# Mapping: merged / open / closed-without-merge (1-3 examples)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (RECORDED_MERGED, VerificationResult.RESOLVED),       # Req 4.3 / 8.2
        (RECORDED_OPEN, VerificationResult.UNRESOLVED),       # Req 4.4
        (RECORDED_CLOSED, VerificationResult.UNRESOLVED),     # Req 4.4 / 8.7
    ],
    ids=["merged", "open", "closed-without-merge"],
)
def test_recorded_mcp_status_maps_correctly(
    payload: Mapping[str, Any], expected: VerificationResult
) -> None:
    """End-to-end: a recorded MCP PR payload maps to the expected three-valued result."""
    client = RecordedClient(payload)
    verifier = Verifier(client)

    result = verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.AUTO_CLOSE)

    assert result is expected


# --------------------------------------------------------------------------- #
# Timeout fallback: always-failing server -> UNVERIFIED after <= 3 attempts
# --------------------------------------------------------------------------- #
def test_always_failing_client_reports_unverified_after_at_most_three_attempts() -> None:
    """Req 4.5 / 8.6: an unreachable/timing-out server yields UNVERIFIED, bounded retries."""
    client = AlwaysRaisingClient()
    verifier = Verifier(client)  # default bounded retry == 3

    result = verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.AUTO_CLOSE)

    assert result is VerificationResult.UNVERIFIED
    assert client.attempts <= DEFAULT_MAX_ATTEMPTS == 3
    assert client.attempts == 3
    # Safety interlock: UNVERIFIED forbids auto-close (Req 4.6 / 8.6).
    assert is_auto_close_permitted(result) is False


# --------------------------------------------------------------------------- #
# Per-purpose timeout budget is applied (Req 4.2 / 8.1)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("purpose", "expected_budget"),
    [
        (VerifyPurpose.NUDGE, 10.0),       # Req 4.2: nudge path 10s
        (VerifyPurpose.AUTO_CLOSE, 30.0),  # Req 8.1: auto-close path 30s
    ],
    ids=["nudge-10s", "auto_close-30s"],
)
def test_per_purpose_timeout_budget_is_applied(
    purpose: VerifyPurpose, expected_budget: float
) -> None:
    """The Verifier hands each MCP call the correct per-purpose timeout budget."""
    client = RecordedClient(RECORDED_OPEN)
    verifier = Verifier(client, monotonic=_zero_clock)

    verifier.verify_pr(_pr_obligation(), purpose=purpose)

    assert client.timeouts[0] == pytest.approx(expected_budget)
    assert Verifier.timeout_budget(purpose) == expected_budget


# --------------------------------------------------------------------------- #
# Live-server slot (skipped unless explicitly enabled) — see module docstring
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not os.environ.get("GITHUB_MCP_URL") or not os.environ.get("GITHUB_MCP_TOKEN"),
    reason="No live GitHub MCP server configured; runs against the recorded client only.",
)
def test_live_github_mcp_merged_maps_to_resolved() -> None:  # pragma: no cover - live only
    """Optional live check: a real merged PR maps to RESOLVED via the GitHub MCP.

    Enabled only when GITHUB_MCP_URL / GITHUB_MCP_TOKEN are set and the throwaway
    repo in LIVE_MCP_PR_REFS exists (see SETUP_GITHUB_MCP.md). Uses the same port
    the recorded tests use, so the assertions are identical.
    """
    from loop.verifier.verifier import build_github_mcp_client

    client = build_github_mcp_client()
    verifier = Verifier(client)
    obligation = _pr_obligation(LIVE_MCP_PR_REFS["merged"])

    result = verifier.verify_pr(obligation, purpose=VerifyPurpose.AUTO_CLOSE)

    assert result is VerificationResult.RESOLVED
