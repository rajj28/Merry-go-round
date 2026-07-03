"""Property-based test for the Action Agent's autonomous-closure timestamp (task 10.4 — Property 15).

This is the single numbered correctness property for the ISO 8601 UTC closure
timestamp recorded on an autonomous closure (Req 8.5), complementing the
example-based unit tests in ``test_action_auto_close.py`` and the safety-gate
property in ``test_action_auto_close_properties.py`` (Property 14).

It exercises ``ActionAgent.auto_close`` end to end against the REAL collaborators —
the real :class:`~loop.verifier.verifier.Verifier`, the real in-memory
:class:`~loop.graph.sqlite_store.SqliteObligationGraph`, and the real
:class:`~loop.action.action_agent.ActionAgent` — mocking only the external GitHub MCP
boundary (the injected PR-status client). The PR-status client always returns a merged
payload, which the real Verifier maps to RESOLVED, so every generated obligation is
actually auto-closed.

The property asserts that for **any** PR-referencing obligation auto-closed by the
Action Agent, the stored ``closure_timestamp`` parses as a valid ISO 8601 UTC timestamp
and the closure retains the source artifact reference.

Runs >=100 Hypothesis examples (enforced by the workspace conftest profile).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

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
# Test double — only the external GitHub MCP boundary is mocked
# --------------------------------------------------------------------------- #
class _FixedClient:
    """A mock PR-status client returning a fixed payload (successful retrieval)."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    def __call__(
        self, owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        return self._payload


# A merged payload forces the real Verifier to report RESOLVED, so auto-close fires.
_MERGED_PAYLOAD: dict[str, Any] = {
    "number": 42,
    "state": "closed",
    "merged": True,
    "merged_at": "2025-01-08T12:30:00Z",
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

# A variety of well-formed ISO 8601 UTC closure instants the injectable clock can
# return, so the property exercises the timestamp parsing across the offset/`Z` space
# rather than a single literal — the recorded value must always parse as UTC.
_ISO_UTC_NOWS = st.sampled_from(
    [
        "2025-02-01T09:30:00+00:00",
        "2025-02-01T09:30:00Z",
        "2025-12-31T23:59:59+00:00",
        "2025-06-15T00:00:00Z",
        "2024-02-29T12:00:00.500000+00:00",
        "2025-01-08T12:30:00.123456Z",
    ]
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
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.tzinfo is not None and dt.utcoffset() == timezone.utc.utcoffset(None)


# --------------------------------------------------------------------------- #
# Property 15: Autonomous closures record an ISO 8601 UTC timestamp
# --------------------------------------------------------------------------- #
# Feature: loop-obligation-agent, Property 15: Autonomous closures record an ISO 8601 UTC timestamp
# For any obligation auto-closed by the Action Agent, the recorded closure timestamp is
# a valid ISO 8601 UTC timestamp and the closure is recorded with its source artifact
# reference.
# Validates: Requirements 8.5
@given(
    obligation=pr_obligations(),
    closure_now=_ISO_UTC_NOWS,
)
def test_autonomous_closure_records_iso_utc_timestamp(
    obligation: Obligation, closure_now: str
) -> None:
    """Every autonomous closure stamps a valid ISO 8601 UTC timestamp + source artifact ref."""
    # REAL collaborators; only the external GitHub MCP boundary (client) is mocked.
    # The merged payload makes the real Verifier report RESOLVED, so auto-close fires.
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    assert is_ok(graph.upsert(obligation))
    agent = ActionAgent(
        graph, Verifier(_FixedClient(_MERGED_PAYLOAD)), now=lambda: closure_now
    )

    result = agent.auto_close(obligation)
    stored = graph.get(obligation.obligation_id)

    # Precondition for this property: the obligation was actually auto-closed.
    assert result.verification is VerificationResult.RESOLVED
    assert result.closed is True
    assert stored.loop_state is LoopState.HEALED
    assert stored.closure_kind is ClosureKind.AUTONOMOUS
    assert stored.closure_reason == AUTO_CLOSE_REASON

    # ===== Property: the recorded closure timestamp is a valid ISO 8601 UTC instant.
    assert stored.closure_timestamp is not None
    assert _is_iso_utc(stored.closure_timestamp)
    # The returned obligation agrees with what was persisted.
    assert result.obligation.closure_timestamp == stored.closure_timestamp
    assert _is_iso_utc(result.obligation.closure_timestamp)

    # ===== Property: the closure is recorded WITH its source artifact reference.
    assert stored.artifact_type is ArtifactType.GITHUB_PR
    assert stored.artifact_ref == obligation.artifact_ref
