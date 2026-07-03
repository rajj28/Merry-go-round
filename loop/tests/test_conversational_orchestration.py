"""Tests for the Conversational Agent's DYNAMIC ORCHESTRATION layer.

These exercise the additive ReAct-style planner path of the front door
(:class:`loop.conversational.conversational_agent.ConversationalAgent`, wired with
an injectable :data:`~loop.conversational.planner.Planner`):

  (a) a deterministic multi-step plan is executed **in order**, the Tool_Use_Trace
      records each *planned* step interleaved with its *execution* in order, and a
      planned send-as-user step still requires a one-tap confirmation — nothing is
      sent before confirm (Req 12.5, 12.6);
  (b) when the planner raises or returns an unusable/garbage plan, the agent falls
      back to the deterministic parser path, still answers, and records the fallback
      in the trace (graceful degradation, Req 12.5);
  (c) the default :func:`~loop.conversational.planner.build_llm_planner` parses model
      JSON robustly (a property over randomized plans, offline via an injected chat).

The agent runs against the *real* in-memory Obligation Graph and *real* Action
Agent; only the outward "send as user" port, the Verifier, and Claude drafting are
mocked so behaviour is deterministic. The planner is an injected test double, so no
LLM/network is involved.

Validates: Requirements 12.5, 12.6.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from loop.action.action_agent import ActionAgent
from loop.conversational.conversational_agent import (
    TRACE_ACTION_AGENT,
    TRACE_GRAPH,
    TRACE_PLANNER,
    TRACE_VERIFIER,
    ConversationalAgent,
    ParsedQuery,
    ReplyKind,
)
from loop.conversational.planner import (
    KNOWN_TOOLS,
    TOOL_DRAFT_NUDGE,
    TOOL_QUERY_GRAPH,
    TOOL_VERIFY_PR,
    Plan,
    PlanStep,
    build_llm_planner,
    parse_plan,
)
from loop.graph.models import ArtifactType, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import ObligationFilter
from loop.verifier.types import VerificationResult, VerifyPurpose

USER = "U_USER"
_FIXED_TS = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat()
_ACTIVE_STATES = frozenset({LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER})


# --------------------------------------------------------------------------- #
# Test doubles (mirrors test_conversational_routing.py)
# --------------------------------------------------------------------------- #
class _RecordingSender:
    """A mock "send as user" port that records every send and always succeeds."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, recipient_id: str, text: str) -> None:
        self.calls.append((recipient_id, text))

    @property
    def count(self) -> int:
        return len(self.calls)


class _FakeVerifier:
    """A mock Verifier returning a fixed three-valued result for any PR."""

    def __init__(self, result: VerificationResult) -> None:
        self.result = result
        self.calls = 0

    def verify_pr(
        self, obligation: Obligation, *, purpose: VerifyPurpose
    ) -> VerificationResult:
        self.calls += 1
        return self.result


class _Clock:
    """A mutable ISO 8601 UTC clock."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def iso(self) -> str:
        return self._t.isoformat()

    def advance(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)


def _planner_returning(plan: Plan):
    """A deterministic planner double that always returns ``plan``."""

    def planner(message: str, tools) -> Plan:
        return plan

    return planner


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


def _obligation(
    obligation_id: str,
    *,
    ts: str = _FIXED_TS,
    pr: bool = False,
    summary: str = "Ship the release notes",
) -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.95,
        last_touch_timestamp=ts,
        source_msg_channel="C_PANE",
        source_msg_ts="1700000000.000100",
        subject_summary=summary,
        artifact_type=ArtifactType.GITHUB_PR if pr else None,
        artifact_ref="octo/repo#1" if pr else None,
    )


def _action_agent(
    graph: SqliteObligationGraph,
    *,
    verifier: _FakeVerifier,
    sender: _RecordingSender,
    clock: _Clock,
) -> ActionAgent:
    return ActionAgent(
        graph,
        verifier=verifier,  # type: ignore[arg-type]
        now=clock.iso,
        slack_send_as_user=sender,
        claude_draft=lambda obligation: "Friendly nudge text.",
    )


def _active_query_parser():
    """A parser stub yielding an active+surfaced query filter (the plan's base scope)."""

    def parser(text: str) -> ParsedQuery:
        return ParsedQuery(
            filter=ObligationFilter(loop_states=_ACTIVE_STATES, surfaced_only=True)
        )

    return parser


def _tools(reply) -> list[str]:
    return [e.tool for e in reply.trace.entries]


def _details(reply) -> list[tuple[str, str | None]]:
    return [(e.tool, e.detail) for e in reply.trace.entries]


# =========================================================================== #
# (a) Deterministic multi-step plan: ordered execution + trace + confirm gate
# =========================================================================== #
def test_planned_multistep_executes_in_order_and_gates_send_as_user() -> None:
    """Validates: Requirements 12.5, 12.6.

    A fixed plan [query_graph → verify_pr → draft_nudge] runs in order; the trace
    interleaves each planned step with its execution in order; and the send-as-user
    (nudge) step prepares only — nothing is sent until a one-tap confirm.
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_PR", pr=True))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)

    plan = Plan(
        summary="nudge the blocking loop",
        steps=(
            PlanStep(TOOL_QUERY_GRAPH, {}, "find the blocking loop"),
            PlanStep(TOOL_VERIFY_PR, {}, "confirm the PR isn't already merged"),
            PlanStep(TOOL_DRAFT_NUDGE, {}, "draft a polite nudge as the user"),
        ),
    )
    convo = ConversationalAgent(
        graph,
        action,
        parser=_active_query_parser(),
        now=clock.iso,
        planner=_planner_returning(plan),
    )

    reply = convo.handle(USER, "nudge whoever is blocking me")

    # Send-as-user is gated: confirmation required, nothing sent yet (Req 12.6).
    assert reply.kind is ReplyKind.CONFIRM_REQUIRED
    assert reply.requires_confirmation is True
    assert sender.count == 0

    # The trace shows the plan interleaved with execution, in order (Req 12.5).
    assert _tools(reply) == [
        TRACE_PLANNER,        # plan formed
        TRACE_PLANNER,        # planned step 1: query_graph
        TRACE_GRAPH,          # executed: graph query
        TRACE_PLANNER,        # planned step 2: verify_pr
        TRACE_VERIFIER,       # executed: verify the PR
        TRACE_PLANNER,        # planned step 3: draft_nudge
        TRACE_ACTION_AGENT,   # executed: draft (no send)
    ]
    # Execution sub-details are the real invocations.
    assert (TRACE_GRAPH, "query") in _details(reply)
    assert (TRACE_VERIFIER, "verify_pr") in _details(reply)
    assert (TRACE_ACTION_AGENT, "draft_polite_nudge") in _details(reply)
    # Planned-step entries name the tools, in order.
    planned = [e.detail or "" for e in reply.trace.entries if e.tool == TRACE_PLANNER]
    step_order = [d for d in planned if d.startswith("step ")]
    assert len(step_order) == 3
    assert "query_graph" in step_order[0]
    assert "verify_pr" in step_order[1]
    assert "draft_nudge" in step_order[2]

    # The confirmation completes the send only after the one-tap confirm (Req 12.6).
    confirm = convo.confirm(USER)
    assert confirm.kind is ReplyKind.ACTION_DONE
    assert sender.count == 1


def test_planned_snooze_executes_and_persists() -> None:
    """Validates: Requirements 12.5.

    A plan [query_graph → snooze] routes the autonomous snooze through the Action
    Agent and persists the mutation, with the trace showing plan + execution.
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_ONLY"))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)

    plan = Plan(
        summary="snooze it for a day",
        steps=(
            PlanStep(TOOL_QUERY_GRAPH, {}, "find the loop"),
            PlanStep("snooze", {"duration_days": 1}, "mute for a day"),
        ),
    )
    convo = ConversationalAgent(
        graph,
        action,
        parser=_active_query_parser(),
        now=clock.iso,
        planner=_planner_returning(plan),
    )

    reply = convo.handle(USER, "snooze this for a day")

    assert reply.kind is ReplyKind.ACTION_DONE
    assert _tools(reply) == [TRACE_PLANNER, TRACE_PLANNER, TRACE_GRAPH, TRACE_PLANNER, TRACE_ACTION_AGENT]
    assert (TRACE_ACTION_AGENT, "snooze") in _details(reply)
    stored = graph.get("OBL_ONLY")
    assert stored is not None and stored.snoozed_until is not None


# =========================================================================== #
# (b) Graceful degradation: planner raises / returns garbage → parser fallback
# =========================================================================== #
def test_planner_raises_falls_back_to_parser_and_answers() -> None:
    """Validates: Requirements 12.5.

    When the planner raises, the agent falls back to the deterministic parser path,
    still answers, and records the fallback in the trace.
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_Q"))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)

    def exploding_planner(message: str, tools) -> Plan:
        raise RuntimeError("LLM unreachable")

    convo = ConversationalAgent(
        graph,
        action,
        parser=_active_query_parser(),  # answers as a query
        now=clock.iso,
        planner=exploding_planner,
    )

    reply = convo.handle(USER, "who is blocked on me?")

    # Still answered via the parser path.
    assert reply.kind is ReplyKind.QUERY_RESULT
    assert {o.obligation_id for o in reply.obligations} == {"OBL_Q"}
    # The fallback is recorded first, then the parser-path graph query (Req 12.5).
    assert reply.trace.entries[0].tool == TRACE_PLANNER
    assert "fallback" in (reply.trace.entries[0].detail or "")
    assert _tools(reply) == [TRACE_PLANNER, TRACE_GRAPH]


@pytest.mark.parametrize(
    "bad_plan",
    [
        Plan(steps=()),                                  # empty plan
        Plan(steps=(PlanStep("teleport", {}, "nope"),)),  # only unknown tools
        "not a plan object",                             # wrong type entirely
        None,                                            # nothing
    ],
)
def test_unusable_plan_falls_back_to_parser(bad_plan) -> None:
    """Validates: Requirements 12.5.

    An empty plan, a plan of only unknown tools, or a non-Plan value all degrade
    gracefully to the parser path with the fallback recorded.
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_Q"))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)

    convo = ConversationalAgent(
        graph,
        action,
        parser=_active_query_parser(),
        now=clock.iso,
        planner=_planner_returning(bad_plan),  # type: ignore[arg-type]
    )

    reply = convo.handle(USER, "who is blocked on me?")

    assert reply.kind is ReplyKind.QUERY_RESULT
    assert reply.trace.entries[0].tool == TRACE_PLANNER
    assert "fallback" in (reply.trace.entries[0].detail or "")


def test_planner_none_preserves_parser_path_unchanged() -> None:
    """Validates: Requirements 12.5.

    With no planner wired (the default), ``handle`` uses the parser path and the
    trace carries no planner entry — preserving the original behaviour.
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_Q"))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)

    convo = ConversationalAgent(
        graph, action, parser=_active_query_parser(), now=clock.iso
    )  # planner defaults to None

    reply = convo.handle(USER, "who is blocked on me?")

    assert reply.kind is ReplyKind.QUERY_RESULT
    assert TRACE_PLANNER not in _tools(reply)
    assert _tools(reply) == [TRACE_GRAPH]


# =========================================================================== #
# (c) build_llm_planner parses model JSON robustly (property, offline)
# =========================================================================== #
_TOOL_NAMES = st.sampled_from(sorted(KNOWN_TOOLS))
_ARG_VALUES = st.one_of(
    st.text(alphabet="ABCDE_ ", min_size=0, max_size=6),
    st.integers(min_value=0, max_value=72),
)
_ARGS = st.dictionaries(
    keys=st.sampled_from(["selector", "person", "teammate", "duration_hours", "duration_days"]),
    values=_ARG_VALUES,
    max_size=3,
)
_STEP = st.builds(
    lambda tool, args, rationale: {"tool": tool, "args": args, "rationale": rationale},
    tool=_TOOL_NAMES,
    args=_ARGS,
    rationale=st.text(alphabet="abcdefg ", min_size=0, max_size=12),
)
_PLAN_JSON = st.builds(
    lambda summary, steps: {"summary": summary, "steps": steps},
    summary=st.text(alphabet="abcdefg ", min_size=0, max_size=12),
    steps=st.lists(_STEP, min_size=1, max_size=5),
)


# Feature: loop-obligation-agent — dynamic orchestration planner JSON round-trips.
@settings(max_examples=150)
@given(plan_json=_PLAN_JSON, fences=st.booleans())
def test_property_llm_planner_parses_model_json(plan_json: dict, fences: bool) -> None:
    """Validates: Requirements 12.5.

    For any well-formed plan JSON the model could emit (optionally wrapped in a
    ```json code fence), :func:`build_llm_planner` parses it back into a
    :class:`Plan` whose ordered tools and step args match the source — so the visible
    plan faithfully reflects the model's reasoning.
    """
    body = json.dumps(plan_json)
    raw = f"```json\n{body}\n```" if fences else body

    planner = build_llm_planner(chat_fn=lambda *a, **k: raw)
    plan = planner("any request", ())

    expected_tools = tuple(step["tool"] for step in plan_json["steps"])
    assert plan.tools == expected_tools
    assert all(tool in KNOWN_TOOLS for tool in plan.tools)
    # Args survive the round-trip (JSON coerces, so compare via re-serialization).
    for step, src in zip(plan.steps, plan_json["steps"]):
        assert step.args == {str(k): v for k, v in src["args"].items()}


def test_parse_plan_rejects_garbage() -> None:
    """Validates: Requirements 12.5.

    Empty/garbage responses raise so the agent can fall back gracefully.
    """
    from loop.conversational.planner import PlannerError

    for bad in ["", "   ", "no json here", "{not json}", '{"steps": []}', '{"steps": "x"}']:
        with pytest.raises(PlannerError):
            parse_plan(bad)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
