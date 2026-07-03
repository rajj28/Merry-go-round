"""Example-based unit tests for the Action Agent's auto-close (task 10.1 / 10.2).

Covers ``ActionAgent.auto_close`` against the Verifier's three-valued result, using
a mocked PR-status client (the Verifier's thin injectable port) and an isolated
in-memory Obligation Graph:

  * RESOLVED (PR merged)      -> loop healed + autonomous + closure metadata stamped
                                 with a valid ISO 8601 UTC timestamp (Req 8.2, 8.5)
  * UNRESOLVED (PR open)      -> Loop_State unchanged, no autonomous closure (Req 8.3)
  * UNRESOLVED (closed w/o    -> Loop_State unchanged, never autonomous (Req 8.7)
    merge)
  * UNVERIFIED (transport     -> Loop_State unchanged, safety interlock (Req 8.4, 8.6)
    failure)

The property tests (Properties 14/15, tasks 10.3/10.4) are separate tasks and
intentionally NOT included here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from loop.action.action_agent import AUTO_CLOSE_REASON, ActionAgent
from loop.graph.models import (
    ArtifactType,
    ClosureKind,
    LoopState,
    Obligation,
)
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import is_ok
from loop.verifier.mcp_contract import (
    SAMPLE_CLOSED_WITHOUT_MERGE,
    SAMPLE_MERGED,
    SAMPLE_OPEN,
)
from loop.verifier.types import VerificationResult
from loop.verifier.verifier import Verifier


# --------------------------------------------------------------------------- #
# Test doubles / fixtures
# --------------------------------------------------------------------------- #
class _FixedClient:
    """A mock PR-status client returning a fixed payload (success path)."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    def __call__(
        self, owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        return self._payload


class _RaisingClient:
    """A mock client that always raises — simulates unreachable/timeout/error."""

    def __call__(
        self, owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        raise ConnectionError("GitHub MCP unreachable")


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


def _pr_obligation(obligation_id: str = "OBL_PR") -> Obligation:
    """A PR-referencing, blocked-on-you obligation seeded into the graph."""
    return Obligation(
        obligation_id=obligation_id,
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.9,
        last_touch_timestamp=datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary="Review the throwaway PR",
        artifact_type=ArtifactType.GITHUB_PR,
        artifact_ref="acme/throwaway#42",
    )


def _agent(graph: SqliteObligationGraph, payload: Mapping[str, Any] | None, *, raising: bool = False,
           now: str = "2025-02-01T09:30:00+00:00") -> ActionAgent:
    client = _RaisingClient() if raising else _FixedClient(payload or {})
    verifier = Verifier(client)
    return ActionAgent(graph, verifier, now=lambda: now)


def _is_iso_utc(value: str) -> bool:
    """True iff ``value`` parses as an ISO 8601 timestamp anchored to UTC."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.tzinfo is not None and dt.utcoffset() == timezone.utc.utcoffset(None)


# --------------------------------------------------------------------------- #
# RESOLVED -> healed + autonomous + closure metadata (Req 8.2, 8.5)
# --------------------------------------------------------------------------- #
def test_resolved_merge_closes_loop_as_autonomous() -> None:
    graph = _graph()
    obligation = _pr_obligation()
    assert is_ok(graph.upsert(obligation))

    closure_now = "2025-02-01T09:30:00+00:00"
    agent = _agent(graph, SAMPLE_MERGED, now=closure_now)

    result = agent.auto_close(obligation)

    assert result.closed is True
    assert result.verification is VerificationResult.RESOLVED
    assert result.obligation.loop_state is LoopState.HEALED
    assert result.obligation.closure_kind is ClosureKind.AUTONOMOUS

    # The healed state is persisted and observable from the graph (Req 8.2).
    stored = graph.get(obligation.obligation_id)
    assert stored.loop_state is LoopState.HEALED
    assert stored.closure_kind is ClosureKind.AUTONOMOUS


def test_resolved_merge_records_closure_metadata() -> None:
    graph = _graph()
    obligation = _pr_obligation()
    assert is_ok(graph.upsert(obligation))

    closure_now = "2025-02-01T09:30:00+00:00"
    agent = _agent(graph, SAMPLE_MERGED, now=closure_now)

    agent.auto_close(obligation)
    stored = graph.get(obligation.obligation_id)

    # Source artifact reference retained (Req 8.5).
    assert stored.artifact_ref == "acme/throwaway#42"
    assert stored.artifact_type is ArtifactType.GITHUB_PR
    # Closure reason recorded (Req 8.5).
    assert stored.closure_reason == AUTO_CLOSE_REASON
    # Closure timestamp in ISO 8601 UTC (Req 8.5).
    assert stored.closure_timestamp == closure_now
    assert _is_iso_utc(stored.closure_timestamp)


# --------------------------------------------------------------------------- #
# UNRESOLVED -> Loop_State unchanged, no autonomous closure (Req 8.3, 8.7)
# --------------------------------------------------------------------------- #
def test_open_pr_leaves_loop_unchanged() -> None:
    graph = _graph()
    obligation = _pr_obligation()
    assert is_ok(graph.upsert(obligation))

    agent = _agent(graph, SAMPLE_OPEN)
    result = agent.auto_close(obligation)

    assert result.closed is False
    assert result.verification is VerificationResult.UNRESOLVED

    stored = graph.get(obligation.obligation_id)
    assert stored.loop_state is LoopState.BLOCKED_ON_YOU
    assert stored.closure_kind is None
    assert stored.closure_timestamp is None
    assert stored.closure_reason is None


def test_closed_without_merge_never_recorded_as_autonomous() -> None:
    graph = _graph()
    obligation = _pr_obligation()
    assert is_ok(graph.upsert(obligation))

    agent = _agent(graph, SAMPLE_CLOSED_WITHOUT_MERGE)
    result = agent.auto_close(obligation)

    # Closed-without-merge maps to UNRESOLVED upstream — never auto-closed (Req 8.7).
    assert result.closed is False
    assert result.verification is VerificationResult.UNRESOLVED

    stored = graph.get(obligation.obligation_id)
    assert stored.loop_state is LoopState.BLOCKED_ON_YOU
    assert stored.closure_kind is None


# --------------------------------------------------------------------------- #
# UNVERIFIED -> Loop_State unchanged, safety interlock (Req 8.4, 8.6)
# --------------------------------------------------------------------------- #
def test_unverified_leaves_loop_unchanged() -> None:
    graph = _graph()
    obligation = _pr_obligation()
    assert is_ok(graph.upsert(obligation))

    agent = _agent(graph, None, raising=True)
    result = agent.auto_close(obligation)

    assert result.closed is False
    assert result.verification is VerificationResult.UNVERIFIED

    stored = graph.get(obligation.obligation_id)
    assert stored.loop_state is LoopState.BLOCKED_ON_YOU
    assert stored.closure_kind is None
    assert stored.closure_timestamp is None
