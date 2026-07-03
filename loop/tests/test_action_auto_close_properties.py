"""Property-based test for the Action Agent's auto-close safety gate (task 10.3 — Property 14).

This is the single numbered correctness property for the SAFETY-CRITICAL auto-close
gate, complementing the example-based unit tests in ``test_action_auto_close.py``.

It exercises ``ActionAgent.auto_close`` end to end against the REAL collaborators —
the real :class:`~loop.verifier.verifier.Verifier`, the real in-memory
:class:`~loop.graph.sqlite_store.SqliteObligationGraph`, and the real
:class:`~loop.action.action_agent.ActionAgent` — mocking only the external GitHub MCP
boundary (the injected PR-status client). The PR-status client is driven across the
whole reachable outcome space:

  * a merged payload            -> Verifier reports RESOLVED
  * an open payload             -> Verifier reports UNRESOLVED
  * a closed-without-merge load -> Verifier reports UNRESOLVED (Req 8.7)
  * a raising (transport-fail)  -> Verifier reports UNVERIFIED after <=3 attempts

The property asserts the biconditional safety gate: the loop is set to ``healed`` with
an autonomous closure **if and only if** the Verifier reports RESOLVED; on UNRESOLVED
or UNVERIFIED the Loop_State (and every other field) is left exactly unchanged and no
autonomous closure is ever recorded.

Runs >=100 Hypothesis examples (enforced by the workspace conftest profile).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from hypothesis import given
from hypothesis import strategies as st

from loop.action.action_agent import AUTO_CLOSE_REASON, ActionAgent
from loop.graph.models import (
    ArtifactType,
    ClosureKind,
    LoopState,
    Obligation,
)
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import is_ok
from loop.verifier.types import VerificationResult
from loop.verifier.verifier import Verifier


# --------------------------------------------------------------------------- #
# Test doubles — only the external GitHub MCP boundary is mocked
# --------------------------------------------------------------------------- #
class _FixedClient:
    """A mock PR-status client returning a fixed payload (successful retrieval)."""

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


# A merged / open / closed-without-merge payload for each successful outcome, plus a
# sentinel selecting the raising (transport-failure -> UNVERIFIED) client.
_MERGED_PAYLOAD: dict[str, Any] = {
    "number": 42,
    "state": "closed",
    "merged": True,
    "merged_at": "2025-01-08T12:30:00Z",
}
_OPEN_PAYLOAD: dict[str, Any] = {
    "number": 42,
    "state": "open",
    "merged": False,
    "merged_at": None,
}
_CLOSED_NO_MERGE_PAYLOAD: dict[str, Any] = {
    "number": 42,
    "state": "closed",
    "merged": False,
    "merged_at": None,
}

# outcome label -> (client factory, expected VerificationResult)
_OUTCOMES = {
    "merged": (lambda: _FixedClient(_MERGED_PAYLOAD), VerificationResult.RESOLVED),
    "open": (lambda: _FixedClient(_OPEN_PAYLOAD), VerificationResult.UNRESOLVED),
    "closed": (lambda: _FixedClient(_CLOSED_NO_MERGE_PAYLOAD), VerificationResult.UNRESOLVED),
    "unverified": (lambda: _RaisingClient(), VerificationResult.UNVERIFIED),
}


# --------------------------------------------------------------------------- #
# Smart generators — constrained to the real auto-close input space
# --------------------------------------------------------------------------- #
# Auto-close only ever evaluates a PR-referencing open loop, so every generated
# obligation carries a valid GitHub PR ref and an open Loop_State.
_OPEN_STATES = st.sampled_from([LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER])
_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=12,
)


@st.composite
def pr_obligations(draw: st.DrawFn) -> Obligation:
    """Generate a PR-referencing, open-loop Obligation eligible for auto-close."""
    owner = draw(_IDENT)
    repo = draw(_IDENT)
    number = draw(st.integers(min_value=1, max_value=1_000_000))
    return Obligation(
        obligation_id=draw(_IDENT),
        owes_person_id=draw(_IDENT),
        owed_person_id=draw(_IDENT),
        owner_person_id=draw(_IDENT),
        loop_state=draw(_OPEN_STATES),
        confidence_score=draw(st.floats(min_value=0.0, max_value=1.0)),
        last_touch_timestamp="2025-01-08T12:00:00+00:00",
        source_msg_channel=draw(_IDENT),
        source_msg_ts="1700000000.000100",
        subject_summary=draw(st.text(max_size=80)),
        artifact_type=ArtifactType.GITHUB_PR,
        artifact_ref=f"{owner}/{repo}#{number}",
    )


def _is_iso_utc(value: str) -> bool:
    """True iff ``value`` parses as an ISO 8601 timestamp anchored to UTC."""
    from datetime import datetime, timezone

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.tzinfo is not None and dt.utcoffset() == timezone.utc.utcoffset(None)


# --------------------------------------------------------------------------- #
# Property 14: Auto-close only on a verified merge (SAFETY-CRITICAL)
# --------------------------------------------------------------------------- #
# Feature: loop-obligation-agent, Property 14: Auto-close only on a verified merge
# For any obligation evaluated for closure, the Action Agent sets its Loop_State to
# `healed` with an autonomous closure if and only if the Verifier reports RESOLVED;
# when the Verifier reports UNRESOLVED or UNVERIFIED, the obligation's Loop_State is
# left unchanged.
# Validates: Requirements 4.5, 4.6, 8.2, 8.3, 8.4, 8.6
@given(
    obligation=pr_obligations(),
    outcome=st.sampled_from(list(_OUTCOMES)),
)
def test_auto_close_only_on_verified_merge(obligation: Obligation, outcome: str) -> None:
    """healed+autonomous iff RESOLVED; UNRESOLVED/UNVERIFIED leave the loop unchanged."""
    client_factory, expected_result = _OUTCOMES[outcome]

    # REAL collaborators; only the external GitHub MCP boundary (client) is mocked.
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    assert is_ok(graph.upsert(obligation))
    closure_now = "2025-02-01T09:30:00+00:00"
    agent = ActionAgent(graph, Verifier(client_factory()), now=lambda: closure_now)

    result = agent.auto_close(obligation)
    stored = graph.get(obligation.obligation_id)

    # The Verifier resolves to exactly the expected three-valued result.
    assert result.verification is expected_result

    if expected_result is VerificationResult.RESOLVED:
        # ===== Forward direction: RESOLVED => healed + autonomous closure =====
        assert result.closed is True
        assert result.obligation.loop_state is LoopState.HEALED
        assert result.obligation.closure_kind is ClosureKind.AUTONOMOUS
        # The healed write is persisted and observable from the graph (Req 8.2).
        assert stored.loop_state is LoopState.HEALED
        assert stored.closure_kind is ClosureKind.AUTONOMOUS
        # Closure metadata stamped: reason + ISO 8601 UTC timestamp; artifact retained.
        assert stored.closure_reason == AUTO_CLOSE_REASON
        assert stored.closure_timestamp is not None and _is_iso_utc(stored.closure_timestamp)
        assert stored.artifact_ref == obligation.artifact_ref
    else:
        # ===== Reverse direction: NOT RESOLVED => Loop_State left unchanged =====
        # (UNRESOLVED — open or closed-without-merge — and UNVERIFIED both block close,
        #  the latter being the safety interlock against an unknown PR state, Req 4.6/8.6.)
        assert result.closed is False
        # The returned obligation is the unchanged input.
        assert result.obligation.loop_state is obligation.loop_state
        # The persisted obligation is left exactly as it was — no field mutated,
        # and crucially never an autonomous closure recorded without a merge (Req 8.4/8.7).
        assert stored.loop_state is obligation.loop_state
        assert stored.closure_kind is None
        assert stored.closure_timestamp is None
        assert stored.closure_reason is None

    # Biconditional restated: an autonomous heal occurred iff the Verifier said RESOLVED.
    healed_autonomously = (
        stored.loop_state is LoopState.HEALED
        and stored.closure_kind is ClosureKind.AUTONOMOUS
    )
    assert healed_autonomously is (expected_result is VerificationResult.RESOLVED)
