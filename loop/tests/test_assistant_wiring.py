"""Wiring tests for the Assistant pane (the second Slack surface — Req 12).

These exercise :class:`loop.app.LoopApp`'s Assistant-pane wiring directly — no live
Slack — through a fake Bolt app and the real Conversational + Action agents:

  * ``handle_assistant_message`` returns a :class:`HandlerResult` whose ``.blocks``
    is a non-empty Block Kit rendering and whose ``.text`` fallback still carries the
    answer plus the ``Tools:`` Tool_Use_Trace summary (Req 12.5);
  * the in-pane confirm / decline action handlers route to the Conversational Agent's
    own 60s gate, render the reply into blocks, and post them.

Validates: Requirements 12.5, 12.6, 12.7.
"""

from __future__ import annotations

from loop.action.action_agent import ActionAgent
from loop.adjudicator.adjudicator import Adjudicator, Direction, OpusAdjudication
from loop.app import LoopApp
from loop.conversational.assistant_view import (
    ACTION_ASSISTANT_CONFIRM,
    ACTION_ASSISTANT_DECLINE,
)
from loop.conversational.conversational_agent import (
    CommandAction,
    ConversationalAgent,
    ParsedCommand,
    ParsedQuery,
)
from loop.graph.models import LoopState, Obligation, utc_now_iso
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import ObligationFilter
from loop.learn.feedback import LearnEngine
from loop.pipeline import AdjudicationQueue
from loop.verifier.types import VerificationResult
from loop.watcher.watcher import Watcher

USER = "U_USER"
OTHER = "U_OTHER"
_ACTIVE = frozenset({LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER})


class FakeBoltApp:
    def __init__(self):
        self.events: dict[str, object] = {}
        self.actions: dict[str, object] = {}
        self.views: dict[str, object] = {}

    def event(self, name):
        def deco(fn):
            self.events[name] = fn
            return fn

        return deco

    def action(self, action_id):
        def deco(fn):
            self.actions[action_id] = fn
            return fn

        return deco

    def view(self, callback_id):
        def deco(fn):
            self.views[callback_id] = fn
            return fn

        return deco


class FakeClient:
    def __init__(self):
        self.published: list[dict] = []
        self.messages: list[dict] = []
        self.opened_views: list[dict] = []

    def views_publish(self, **kwargs):
        self.published.append(kwargs)

    def views_open(self, **kwargs):
        self.opened_views.append(kwargs)

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)


class FakeVerifier:
    def __init__(self, result=VerificationResult.UNRESOLVED):
        self.result = result

    def verify_pr(self, obligation, *, purpose):  # noqa: ANN001
        return self.result


def _opus_user_owes(_c, *, user_id):  # noqa: ANN001
    return OpusAdjudication(
        is_loop=True,
        involves_user=True,
        direction=Direction.USER_OWES,
        confidence=0.95,
        subject_summary="please review the deck",
    )


def _obligation(oid="o1", state=LoopState.BLOCKED_ON_YOU, **kw) -> Obligation:
    base = dict(
        obligation_id=oid,
        owes_person_id=USER,
        owed_person_id=OTHER,
        owner_person_id=USER,
        loop_state=state,
        confidence_score=0.95,
        last_touch_timestamp="2024-01-01T00:00:00+00:00",
        source_msg_channel="C1",
        source_msg_ts="1700000000.0001",
        subject_summary="please review the deck",
    )
    base.update(kw)
    return Obligation(**base)


def _build_app(*, parser=None, sends=None) -> LoopApp:
    graph = SqliteObligationGraph(IN_MEMORY)
    verifier = FakeVerifier()
    sent = sends if sends is not None else []

    def send_as_user(channel, text):  # noqa: ANN001
        sent.append((channel, text))

    def claude_draft(obligation):  # noqa: ANN001
        return "Hi — nudging you about: " + obligation.subject_summary

    action = ActionAgent(
        graph,
        verifier,  # type: ignore[arg-type]
        slack_send_as_user=send_as_user,
        claude_draft=claude_draft,
    )
    adjudicator = Adjudicator(_opus_user_owes, graph)
    queue = AdjudicationQueue(adjudicator, USER)
    watcher = Watcher(
        graph,
        rts_client=lambda: {"ok": False},
        forward=queue.enqueue,
        interval_seconds=30,
    )
    kwargs = {"parser": parser} if parser is not None else {}
    conversational = ConversationalAgent(graph, action, **kwargs)
    learn = LearnEngine(graph)
    return LoopApp(
        graph=graph,
        watcher=watcher,
        adjudicator=adjudicator,
        verifier=verifier,  # type: ignore[arg-type]
        action=action,
        conversational=conversational,
        learn=learn,
        queue=queue,
        user_id=USER,
    )


# --------------------------------------------------------------------------- #
# handle_assistant_message returns rich blocks AND a valid text fallback
# --------------------------------------------------------------------------- #
def test_assistant_message_returns_blocks_and_text_fallback():
    app = _build_app()
    app.graph.upsert(_obligation())
    result = app.handle_assistant_message(USER, "who is blocked on me?")

    # Rich Block Kit rendering present (the Assistant pane).
    assert result.blocks is not None
    assert len(result.blocks) > 0
    # The text fallback still carries the answer + the Tool_Use_Trace summary.
    assert "please review the deck" in result.text
    assert "Tools:" in result.text


def test_register_handlers_binds_assistant_confirm_decline():
    app = _build_app()
    bolt = FakeBoltApp()
    app.register_handlers(bolt)
    assert ACTION_ASSISTANT_CONFIRM in bolt.actions
    assert ACTION_ASSISTANT_DECLINE in bolt.actions


# --------------------------------------------------------------------------- #
# Confirm / decline action handlers route to the agent and post blocks
# --------------------------------------------------------------------------- #
def _nudge_parser(text: str):
    return ParsedCommand(
        action=CommandAction.NUDGE,
        filter=ObligationFilter(loop_states=_ACTIVE, surfaced_only=True),
    )


def test_assistant_confirm_handler_sends_and_posts_blocks():
    sends: list = []
    app = _build_app(parser=_nudge_parser, sends=sends)
    app.graph.upsert(_obligation())

    # Stage a pending in-pane confirmation via the conversational agent.
    pre = app.handle_assistant_message(USER, "nudge them")
    assert pre.requires_confirmation is True
    assert app.conversational.has_pending_confirmation(USER)
    assert sends == []  # nothing sent before confirm

    bolt = FakeBoltApp()
    app.register_handlers(bolt)
    client = FakeClient()
    acked = {"v": False}

    def ack():
        acked["v"] = True

    body = {"user": {"id": USER}, "actions": [{"value": USER}]}
    bolt.actions[ACTION_ASSISTANT_CONFIRM](ack, body, client)

    assert acked["v"] is True
    # The send went through the conversational agent's gate.
    assert len(sends) == 1
    assert not app.conversational.has_pending_confirmation(USER)
    # The reply was posted with rich blocks + a text fallback.
    assert len(client.messages) == 1
    posted = client.messages[0]
    assert posted["channel"] == USER
    assert posted.get("blocks")
    assert posted.get("text")


def test_assistant_decline_handler_cancels_and_posts_blocks():
    sends: list = []
    app = _build_app(parser=_nudge_parser, sends=sends)
    app.graph.upsert(_obligation())

    app.handle_assistant_message(USER, "nudge them")
    assert app.conversational.has_pending_confirmation(USER)

    bolt = FakeBoltApp()
    app.register_handlers(bolt)
    client = FakeClient()
    body = {"user": {"id": USER}, "actions": [{"value": USER}]}
    bolt.actions[ACTION_ASSISTANT_DECLINE](lambda: None, body, client)

    # Declined: nothing sent, pending cleared, blocks posted.
    assert sends == []
    assert not app.conversational.has_pending_confirmation(USER)
    assert len(client.messages) == 1
    assert client.messages[0].get("blocks")


def test_post_assistant_reply_posts_blocks_and_returns_result():
    app = _build_app()
    app.graph.upsert(_obligation())
    client = FakeClient()
    result = app.post_assistant_reply(USER, "who is blocked on me?", client)

    assert result.blocks is not None and len(result.blocks) > 0
    assert len(client.messages) == 1
    assert client.messages[0]["channel"] == USER
    assert client.messages[0].get("blocks")
    assert "Tools:" in client.messages[0]["text"]
