"""Example-based unit tests for the Verifier's three-valued result (task 8.1).

Covers ``Verifier.verify_pr`` with a mocked GitHub MCP client (a thin injectable
port), exercising the four observable outcomes and the retry/timeout policy:

  * merged                       -> RESOLVED     (Req 4.3)
  * open                         -> UNRESOLVED   (Req 4.4)
  * closed-without-merge         -> UNRESOLVED   (Req 4.4, 8.7)
  * unreachable/timeout/error    -> UNVERIFIED after <=3 attempts (Req 4.5, 8.6)

Also asserts the bounded retry count and that the correct per-purpose timeout
budget (NUDGE 10s / AUTO_CLOSE 30s) is applied (Req 4.2, 8.1), plus that
UNVERIFIED forbids auto-close (Req 4.6, 8.6).

The property test (Property 13, task 8.2) and the live/recorded MCP smoke test
(task 8.3) are separate tasks and intentionally NOT included here.
"""

from __future__ import annotations

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
    PrRef,
    Verifier,
    is_auto_close_permitted,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class RecordingClient:
    """A mock PR-status client that returns a fixed payload and records calls."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        self.calls.append(
            {"owner": owner, "repo": repo, "pull_number": pull_number, "timeout": timeout}
        )
        return self._payload


class RaisingClient:
    """A mock client that always raises, simulating unreachable/timeout/error."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc or ConnectionError("GitHub MCP unreachable")
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        self.calls.append(
            {"owner": owner, "repo": repo, "pull_number": pull_number, "timeout": timeout}
        )
        raise self._exc


def _zero_clock() -> float:
    """A fixed monotonic clock so the per-call timeout always equals the full budget."""
    return 0.0


def _pr_obligation(artifact_ref: str = "acme/throwaway#42") -> Obligation:
    """A minimal PR-referencing Obligation for verification tests."""
    return Obligation(
        obligation_id="ob-1",
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
# Status mapping: merged / open / closed-without-merge
# --------------------------------------------------------------------------- #
def test_merged_pr_reports_resolved() -> None:
    # Req 4.3: merged PR -> RESOLVED.
    client = RecordingClient(SAMPLE_MERGED)
    verifier = Verifier(client)

    result = verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.AUTO_CLOSE)

    assert result is VerificationResult.RESOLVED
    assert len(client.calls) == 1  # one successful retrieval, no retries


def test_open_pr_reports_unresolved() -> None:
    # Req 4.4: a reachable, non-merged (open) PR -> UNRESOLVED.
    client = RecordingClient(SAMPLE_OPEN)
    verifier = Verifier(client)

    result = verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.NUDGE)

    assert result is VerificationResult.UNRESOLVED
    assert len(client.calls) == 1


def test_closed_without_merge_reports_unresolved() -> None:
    # Req 4.4 / 8.7: closed-without-merge is NOT a merge -> UNRESOLVED.
    client = RecordingClient(SAMPLE_CLOSED_WITHOUT_MERGE)
    verifier = Verifier(client)

    result = verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.AUTO_CLOSE)

    assert result is VerificationResult.UNRESOLVED
    assert len(client.calls) == 1


# --------------------------------------------------------------------------- #
# Transport failure: unreachable / timeout / error -> UNVERIFIED after <=3 tries
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("unreachable"),
        TimeoutError("timed out"),
        RuntimeError("server error"),
    ],
)
def test_transport_failure_reports_unverified_after_three_attempts(
    exc: BaseException,
) -> None:
    # Req 4.5 / 8.6: unreachable, timeout, or error after at most 3 attempts -> UNVERIFIED.
    client = RaisingClient(exc)
    verifier = Verifier(client)  # default max_attempts == 3

    result = verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.AUTO_CLOSE)

    assert result is VerificationResult.UNVERIFIED
    assert len(client.calls) == 3  # bounded retry: exactly 3 attempts


def test_retry_count_respects_custom_max_attempts() -> None:
    # The retry bound is configurable but still terminates in UNVERIFIED.
    client = RaisingClient()
    verifier = Verifier(client, max_attempts=2)

    result = verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.NUDGE)

    assert result is VerificationResult.UNVERIFIED
    assert len(client.calls) == 2


def test_recovers_on_second_attempt_after_transient_failure() -> None:
    # A transient failure followed by success still yields the mapped result.
    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, owner, repo, pull_number, *, timeout):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("transient")
            return SAMPLE_MERGED

    client = FlakyClient()
    verifier = Verifier(client)

    result = verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.AUTO_CLOSE)

    assert result is VerificationResult.RESOLVED
    assert client.calls == 2


# --------------------------------------------------------------------------- #
# Per-purpose timeout budget is applied (Req 4.2 / 8.1)
# --------------------------------------------------------------------------- #
def test_nudge_applies_ten_second_timeout() -> None:
    # Req 4.2: NUDGE path uses a 10s timeout budget.
    client = RecordingClient(SAMPLE_OPEN)
    verifier = Verifier(client, monotonic=_zero_clock)

    verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.NUDGE)

    assert client.calls[0]["timeout"] == pytest.approx(10.0)
    assert Verifier.timeout_budget(VerifyPurpose.NUDGE) == 10.0


def test_auto_close_applies_thirty_second_timeout() -> None:
    # Req 8.1: AUTO_CLOSE path resolves within a 30s budget.
    client = RecordingClient(SAMPLE_MERGED)
    verifier = Verifier(client, monotonic=_zero_clock)

    verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.AUTO_CLOSE)

    assert client.calls[0]["timeout"] == pytest.approx(30.0)
    assert Verifier.timeout_budget(VerifyPurpose.AUTO_CLOSE) == 30.0


def test_budget_exhausted_before_any_attempt_reports_unverified() -> None:
    # If the wall-clock budget elapses before a definite answer, report UNVERIFIED
    # without forcing further calls (Req 4.5 / 8.6).
    ticks = iter([0.0, 100.0, 200.0, 300.0])
    client = RecordingClient(SAMPLE_MERGED)
    verifier = Verifier(client, monotonic=lambda: next(ticks))

    result = verifier.verify_pr(_pr_obligation(), purpose=VerifyPurpose.AUTO_CLOSE)

    assert result is VerificationResult.UNVERIFIED
    assert client.calls == []  # deadline already passed -> no retrieval attempted


# --------------------------------------------------------------------------- #
# Auto-close safety interlock (Req 4.6 / 8.6)
# --------------------------------------------------------------------------- #
def test_unverified_forbids_auto_close() -> None:
    assert is_auto_close_permitted(VerificationResult.UNVERIFIED) is False
    assert is_auto_close_permitted(VerificationResult.RESOLVED) is True
    assert is_auto_close_permitted(VerificationResult.UNRESOLVED) is True


# --------------------------------------------------------------------------- #
# artifact_ref parsing
# --------------------------------------------------------------------------- #
def test_prref_parses_owner_repo_number() -> None:
    ref = PrRef.parse("acme/throwaway#42")
    assert (ref.owner, ref.repo, ref.number) == ("acme", "throwaway", 42)


@pytest.mark.parametrize("bad", [None, "", "not-a-ref", "acme/throwaway", "acme#42"])
def test_prref_rejects_bad_refs(bad: str | None) -> None:
    with pytest.raises(ValueError):
        PrRef.parse(bad)


def test_verify_pr_raises_on_missing_artifact_ref() -> None:
    client = RecordingClient(SAMPLE_MERGED)
    verifier = Verifier(client)
    obligation = _pr_obligation()
    object.__setattr__(obligation, "artifact_ref", None)

    with pytest.raises(ValueError):
        verifier.verify_pr(obligation, purpose=VerifyPurpose.NUDGE)
