"""Example-based unit tests for the Conversational Agent — task 15.5.

These tests exercise the natural-language *control* surface of the front door
(:class:`loop.conversational.conversational_agent.ConversationalAgent`):

  (a) a command routes to the Action Agent for a single identified target
      (Req 12.3);
  (b) a command that maps to more than one candidate yields a disambiguation
      prompt and does **not** route or mutate the graph (Req 12.4);
  (c) the Tool_Use_Trace lists each tool/agent in the exact invocation order for
      a scripted request (Req 12.5);
  (d) an unmappable request replies with the set of supported actions (Req 12.9).

The tests run against the *real* Conversational Agent, the *real* Action Agent,
and the *real* in-memory Obligation Graph; only the outward Slack "send as user"
port, the Verifier (GitHub MCP), and LLM drafting are mocked so behaviour is
deterministic. Natural-language understanding is driven through the injectable
parser port with explicit :class:`ParsedCommand` / :class:`ParsedQuery` intents,
so routing/disambiguation/trace logic is tested independently of NL coverage —
except test (d), which also asserts the shipped :func:`default_parser` returns
the supported-actions reply for genuinely unmappable text.

Validates: Requirements 12.3, 12.4, 12.5, 12.9.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from loop.action.action_agent import ActionAgent
from loop.conversational.conversational_agent import (
    SUPPORTED_ACTIONS,
    TRACE_ACTION_AGENT,
    TRACE_GRAPH,
    TRACE_VERIFIER,
    CommandAction,
    ConversationalAgent,
    ParsedCommand,
    ParsedQuery,
    ReplyKind,
    Selector,
)
from loop.graph.models import ArtifactType, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import ObligationFilter
from loop.verifier.types import VerificationResult, VerifyPurpose

USER = "U_USER"
_FIXED_TS = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat()
_ACTIVE_STATES = frozenset({LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER})


# --------------------------------------------------------------------------- #
# Test doubles
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
    """A surfaced, active, high-confidence obligation owned by the user."""
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
        draft=lambda obligation: "Friendly nudge text.",
    )


def _command_parser(
    action: CommandAction,
    *,
    selector: Selector | None = None,
    teammate_id: str | None = None,
    duration: timedelta | None = None,
    loop_states: frozenset[LoopState] = _ACTIVE_STATES,
):
    """A deterministic parser that maps any text to a fixed command intent."""

    def parser(text: str) -> ParsedCommand:
        return ParsedCommand(
            action=action,
            filter=ObligationFilter(loop_states=loop_states, surfaced_only=True),
            selector=selector,
            teammate_id=teammate_id,
            duration=duration,
        )

    return parser


# =========================================================================== #
# (a) Routing to the Action Agent for a single identified target (Req 12.3)
# =========================================================================== #
def test_snooze_command_routes_to_action_agent_for_single_target() -> None:
    """Validates: Requirements 12.3.

    A snooze command resolving to exactly one target routes straight to the Action
    Agent, which mutates that obligation in the shared graph (the snooze persists).
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_ONLY"))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)
    convo = ConversationalAgent(
        graph, action, parser=_command_parser(CommandAction.SNOOZE), now=clock.iso
    )

    reply = convo.handle_command(USER, "snooze this loop")

    # Routed and completed against the single identified target.
    assert reply.kind is ReplyKind.ACTION_DONE
    assert reply.obligation is not None
    assert reply.obligation.obligation_id == "OBL_ONLY"
    # The Action Agent actually mutated the shared graph (proves it routed there).
    stored = graph.get("OBL_ONLY")
    assert stored is not None
    assert stored.snoozed_until is not None
    # The trace records the Action Agent invocation.
    assert any(e.tool == TRACE_ACTION_AGENT for e in reply.trace.entries)


def test_close_command_routes_to_action_agent_and_verifies_then_heals() -> None:
    """Validates: Requirements 12.3.

    A close command on a single PR-referencing target routes through the Action
    Agent's verified auto-close; a verified merge heals exactly that obligation.
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_PR", pr=True))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.RESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)
    convo = ConversationalAgent(
        graph, action, parser=_command_parser(CommandAction.CLOSE), now=clock.iso
    )

    reply = convo.handle_command(USER, "close this loop")

    assert reply.kind is ReplyKind.ACTION_DONE
    assert reply.obligation is not None
    assert reply.obligation.obligation_id == "OBL_PR"
    assert verifier.calls == 1  # the Action Agent grounded closure in real state
    stored = graph.get("OBL_PR")
    assert stored is not None
    assert stored.loop_state is LoopState.HEALED


def test_single_candidate_via_selector_does_not_disambiguate() -> None:
    """Validates: Requirements 12.3, 12.4.

    With multiple matches but an explicit "oldest" selector, the command collapses
    to a single target and routes without a disambiguation prompt.
    """
    graph = _graph()
    older = _obligation("OBL_OLD", ts="2025-01-01T00:00:00+00:00")
    newer = _obligation("OBL_NEW", ts="2025-01-05T00:00:00+00:00")
    graph.upsert(older)
    graph.upsert(newer)
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)
    convo = ConversationalAgent(
        graph,
        action,
        parser=_command_parser(CommandAction.SNOOZE, selector=Selector.OLDEST),
        now=clock.iso,
    )

    reply = convo.handle_command(USER, "snooze the oldest loop")

    assert reply.kind is ReplyKind.ACTION_DONE
    assert reply.obligation is not None
    assert reply.obligation.obligation_id == "OBL_OLD"  # oldest by last_touch


# =========================================================================== #
# (b) Multiple candidates → disambiguation prompt (Req 12.4)
# =========================================================================== #
def test_multiple_candidates_yields_disambiguation_and_no_routing() -> None:
    """Validates: Requirements 12.4.

    When a command maps to more than one candidate, the agent replies with the
    candidate set and asks the user to pick one — it does NOT route to the Action
    Agent and leaves every candidate unchanged.
    """
    graph = _graph()
    a = _obligation("OBL_A", ts="2025-01-01T00:00:00+00:00")
    b = _obligation("OBL_B", ts="2025-01-05T00:00:00+00:00")
    graph.upsert(a)
    graph.upsert(b)
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)
    convo = ConversationalAgent(
        graph, action, parser=_command_parser(CommandAction.SNOOZE), now=clock.iso
    )

    reply = convo.handle_command(USER, "snooze a loop")

    assert reply.kind is ReplyKind.DISAMBIGUATION
    candidate_ids = {o.obligation_id for o in reply.candidates}
    assert candidate_ids == {"OBL_A", "OBL_B"}
    # No routing occurred: nothing sent, nothing snoozed.
    assert sender.count == 0
    assert verifier.calls == 0
    assert graph.get("OBL_A").snoozed_until is None
    assert graph.get("OBL_B").snoozed_until is None


# =========================================================================== #
# (c) Tool_Use_Trace lists tools/agents in invocation order (Req 12.5)
# =========================================================================== #
def _rendered(reply) -> list[tuple[str, str | None]]:
    return [(e.tool, e.detail) for e in reply.trace.entries]


def test_trace_order_for_query() -> None:
    """Validates: Requirements 12.5.

    A query invokes only the Obligation Graph, so its trace is exactly that one
    invocation.
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_Q"))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)

    def parser(text: str) -> ParsedQuery:
        return ParsedQuery(
            filter=ObligationFilter(loop_states=_ACTIVE_STATES, surfaced_only=True)
        )

    convo = ConversationalAgent(graph, action, parser=parser, now=clock.iso)

    reply = convo.handle_query(USER, "who's blocked on me?")

    assert _rendered(reply) == [(TRACE_GRAPH, "query")]
    assert reply.trace_text == "Obligation Graph (query)"


def test_trace_order_for_snooze_command() -> None:
    """Validates: Requirements 12.5.

    A snooze routes graph→Action Agent, in that order.
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_S"))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)
    convo = ConversationalAgent(
        graph,
        action,
        parser=_command_parser(CommandAction.SNOOZE, selector=Selector.OLDEST),
        now=clock.iso,
    )

    reply = convo.handle_command(USER, "snooze the oldest loop")

    assert _rendered(reply) == [
        (TRACE_GRAPH, "query"),
        (TRACE_ACTION_AGENT, "snooze"),
    ]


def test_trace_order_for_close_command_includes_verifier_before_action() -> None:
    """Validates: Requirements 12.5.

    A close on a PR-referencing loop is a scripted, known invocation order:
    Obligation Graph (query) → Verifier (GitHub MCP) → Action Agent (auto_close).
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_CLOSE", pr=True))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.RESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)
    convo = ConversationalAgent(
        graph,
        action,
        parser=_command_parser(CommandAction.CLOSE, selector=Selector.OLDEST),
        now=clock.iso,
    )

    reply = convo.handle_command(USER, "close the oldest loop")

    assert _rendered(reply) == [
        (TRACE_GRAPH, "query"),
        (TRACE_VERIFIER, "verify_pr"),
        (TRACE_ACTION_AGENT, "auto_close"),
    ]
    assert reply.trace_text == (
        "Obligation Graph (query) → Verifier (GitHub MCP) (verify_pr) "
        "→ Action Agent (auto_close)"
    )


def test_trace_order_for_pr_nudge_prepare_then_confirm() -> None:
    """Validates: Requirements 12.5.

    A PR-referencing nudge has a known two-phase invocation order:
      prepare:  Obligation Graph (query) → Action Agent (draft_polite_nudge)
      confirm:  Verifier (GitHub MCP) (verify_pr) → Action Agent (send_polite_nudge)
    """
    graph = _graph()
    graph.upsert(_obligation("OBL_NUDGE", pr=True))
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)
    convo = ConversationalAgent(
        graph,
        action,
        parser=_command_parser(CommandAction.NUDGE, selector=Selector.OLDEST),
        now=clock.iso,
    )

    prepare = convo.handle_command(USER, "nudge the oldest loop")
    assert prepare.kind is ReplyKind.CONFIRM_REQUIRED
    assert _rendered(prepare) == [
        (TRACE_GRAPH, "query"),
        (TRACE_ACTION_AGENT, "draft_polite_nudge"),
    ]
    assert sender.count == 0  # nothing sent on preparation

    confirm = convo.confirm(USER)
    assert confirm.kind is ReplyKind.ACTION_DONE
    assert _rendered(confirm) == [
        (TRACE_VERIFIER, "verify_pr"),
        (TRACE_ACTION_AGENT, "send_polite_nudge"),
    ]
    assert sender.count == 1  # sent exactly once after confirm


# =========================================================================== #
# (d) Unmappable request → supported actions reply (Req 12.9)
# =========================================================================== #
def test_unmappable_request_replies_with_supported_actions() -> None:
    """Validates: Requirements 12.9.

    A request the parser cannot map to a supported action gets a reply enumerating
    the supported actions.
    """
    graph = _graph()
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)

    # A parser that maps everything to "unmappable".
    convo = ConversationalAgent(graph, action, parser=lambda text: None, now=clock.iso)

    reply = convo.handle_command(USER, "make me a sandwich")

    assert reply.kind is ReplyKind.UNSUPPORTED
    assert reply.supported_actions == SUPPORTED_ACTIONS
    for verb in ("nudge", "delegate", "snooze", "close"):
        assert verb in reply.text


def test_default_parser_unmappable_text_replies_with_supported_actions() -> None:
    """Validates: Requirements 12.9.

    The shipped default parser also funnels genuinely unmappable free text to the
    supported-actions reply (end-to-end through the real parser).
    """
    graph = _graph()
    sender = _RecordingSender()
    verifier = _FakeVerifier(VerificationResult.UNRESOLVED)
    clock = _Clock(datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc))
    action = _action_agent(graph, verifier=verifier, sender=sender, clock=clock)
    convo = ConversationalAgent(graph, action, now=clock.iso)  # real default_parser

    reply = convo.handle(USER, "tell me a joke about penguins")

    assert reply.kind is ReplyKind.UNSUPPORTED
    assert reply.supported_actions == SUPPORTED_ACTIONS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
