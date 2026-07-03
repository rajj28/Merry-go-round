"""End-to-end wiring tests for the Loop composition root (loop/app.py, task 17.1).

These exercise the wiring logic of :class:`LoopApp` directly — no live Slack,
APScheduler, Anthropic, or GitHub MCP — through a fake Bolt app and injected agent
ports. They assert the four wiring guarantees of task 17.1:

  * handler registration covers App Home, the four row actions, confirm/decline,
    Assistant messages, and live message events;
  * the detection pipeline (sweep → queue → Adjudicator → graph) writes obligations;
  * autonomy boundaries hold — sensing/adjudication/snooze/dismiss/digest are
    autonomous, while nudge/delegate are withheld behind a 24h one-tap confirm;
  * the 24h confirmation timeout retains the action unsent (Req 13.5) and a decline
    cancels it (Req 13.6).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from loop.action.action_agent import ActionAgent
from loop.adjudicator.adjudicator import Adjudicator, Direction, OpusAdjudication
from loop.app import (
    ACTION_CONFIRM_SEND,
    ACTION_DECLINE_SEND,
    ACTION_UNDO_DISMISS,
    ACTION_UNDO_SNOOZE,
    LoopApp,
    PendingSend,
)
from loop.action.app_home import (
    ACTION_DELEGATE,
    ACTION_DISMISS,
    ACTION_NUDGE,
    ACTION_QUICK_NUDGE,
    ACTION_REVIEW_BLOCKED,
    ACTION_SNOOZE,
    NUDGE_MODAL_CALLBACK,
    NUDGE_MODAL_INPUT_ACTION,
    NUDGE_MODAL_INPUT_BLOCK,
)
from loop.conversational.conversational_agent import ConversationalAgent
from loop.config import Settings
from loop.graph.models import (
    ArtifactType,
    LoopState,
    Obligation,
    utc_now_iso,
)
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import ObligationFilter
from loop.learn.feedback import LearnEngine
from loop.pipeline import AdjudicationQueue
from loop.verifier.types import VerificationResult, VerifyPurpose
from loop.verifier.verifier import Verifier
from loop.watcher.rts_contract import CandidateMessage
from loop.watcher.watcher import Watcher


USER = "U_USER"
OTHER = "U_OTHER"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeBoltApp:
    """Records handlers registered via ``app.event(...)`` / ``app.action(...)``."""

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
    """Captures views_publish / views_open / chat_postMessage calls."""

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
    """A Verifier double returning a fixed result."""

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


def _build_app(
    *,
    sends: list | None = None,
    draft_ok: bool = True,
    verifier_result=VerificationResult.UNRESOLVED,
    now=utc_now_iso,
    settings=None,
) -> LoopApp:
    graph = SqliteObligationGraph(IN_MEMORY)
    verifier = FakeVerifier(verifier_result)

    sent = sends if sends is not None else []

    def send_as_user(channel, text):  # noqa: ANN001
        sent.append((channel, text))

    def claude_draft(obligation):  # noqa: ANN001
        if not draft_ok:
            raise RuntimeError("draft timeout")
        return "Hi — just nudging you about: " + obligation.subject_summary

    action = ActionAgent(
        graph,
        verifier,  # type: ignore[arg-type]
        now=now,
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
    conversational = ConversationalAgent(graph, action)
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
        now=now,
        settings=settings,
    )


# --------------------------------------------------------------------------- #
# Handler registration
# --------------------------------------------------------------------------- #
def test_register_handlers_binds_all_surfaces():
    app = _build_app()
    bolt = FakeBoltApp()
    app.register_handlers(bolt)

    assert "app_home_opened" in bolt.events
    assert "message" in bolt.events
    for action_id in (
        ACTION_NUDGE,
        ACTION_SNOOZE,
        ACTION_DELEGATE,
        ACTION_DISMISS,
        ACTION_REVIEW_BLOCKED,
        ACTION_CONFIRM_SEND,
        ACTION_DECLINE_SEND,
    ):
        assert action_id in bolt.actions


def test_app_home_opened_publishes_real_view():
    app = _build_app()
    bolt = FakeBoltApp()
    app.register_handlers(bolt)
    client = FakeClient()

    import logging

    bolt.events["app_home_opened"]({"user": USER}, client, logging.getLogger("t"))
    assert len(client.published) == 1
    view = client.published[0]["view"]
    assert view["type"] == "home"
    # The hero banner header is present (task 9 builder, not the 1.1 placeholder).
    assert any(b.get("type") == "header" for b in view["blocks"])


def test_app_home_compact_layout_publishes_carousel():
    """LOOP_HOME_LAYOUT=compact publishes the command-center view with a
    horizontal carousel of blocked-on-you cards instead of stacked sections."""
    app = _build_app(settings=Settings(home_layout="compact"))
    bolt = FakeBoltApp()
    app.register_handlers(bolt)
    client = FakeClient()

    app.graph.upsert(_obligation("o1", subject_summary="review the deck"))
    app.graph.upsert(_obligation("o2", subject_summary="merge the PR"))

    import logging

    bolt.events["app_home_opened"]({"user": USER}, client, logging.getLogger("t"))
    view = client.published[0]["view"]
    assert view["type"] == "home"
    # The blocked-on-you section renders as a single carousel block holding the
    # cards side-by-side (one per surfaced blocked obligation).
    carousels = [b for b in view["blocks"] if b.get("type") == "carousel"]
    assert len(carousels) == 1
    assert len(carousels[0]["elements"]) == 2
    assert all(card["type"] == "card" for card in carousels[0]["elements"])


# --------------------------------------------------------------------------- #
# Detection pipeline (Req 2.1)
# --------------------------------------------------------------------------- #
def test_run_sweep_pipeline_writes_obligation(monkeypatch):
    app = _build_app()

    # Feed the watcher one candidate via a stubbed RTS response.
    candidate_response = {
        "ok": True,
        "results": {
            "messages": [
                {
                    "channel_id": "C1",
                    "message_ts": "1700000000.0001",
                    "author_user_id": OTHER,
                    "content": "can you review?",
                    "permalink": "https://x",
                }
            ]
        },
    }
    app.watcher._rts_client = lambda: candidate_response  # type: ignore[attr-defined]

    client = FakeClient()
    app.run_sweep(client)

    written = app.graph.query(ObligationFilter())
    assert len(written) == 1
    assert written[0].owes_person_id == USER
    # A graph change refreshed the App Home.
    assert len(client.published) == 1


# --------------------------------------------------------------------------- #
# Autonomy boundaries — autonomous paths (Req 13.1)
# --------------------------------------------------------------------------- #
def test_snooze_is_autonomous_no_confirmation():
    app = _build_app()
    app.graph.upsert(_obligation())
    result = app.handle_snooze_click("o1", timedelta(hours=2))
    assert result.obligation is not None
    assert not result.requires_confirmation
    assert app.graph.get("o1").snoozed_until is not None


def test_dismiss_is_autonomous_and_unsurfaces():
    app = _build_app()
    app.graph.upsert(_obligation())
    result = app.handle_dismiss_click("o1")
    assert not result.requires_confirmation
    assert app.graph.get("o1").dismissed is True


def test_dismiss_offers_undo_that_restores_the_loop():
    """Dismiss confirmation carries a one-tap Undo; Undo clears the dismissal."""
    app = _build_app()
    app.graph.upsert(_obligation())

    dismissed = app.handle_dismiss_click("o1")
    assert app.graph.get("o1").dismissed is True
    # The confirmation DM carries an Undo button wired to the obligation id.
    assert dismissed.blocks is not None
    actions = [b for b in dismissed.blocks if b.get("type") == "actions"]
    assert actions and actions[0]["elements"][0]["action_id"] == ACTION_UNDO_DISMISS
    assert actions[0]["elements"][0]["value"] == "o1"

    undo = app.handle_undo_dismiss("o1")
    assert app.graph.get("o1").dismissed is False
    assert "restored" in undo.text.lower()


def test_snooze_offers_undo_that_unsnoozes_the_loop():
    """Snooze confirmation carries Undo; Undo clears snoozed_until immediately."""
    app = _build_app()
    app.graph.upsert(_obligation())

    snoozed = app.handle_snooze_click("o1", timedelta(hours=2))
    assert app.graph.get("o1").snoozed_until is not None
    assert snoozed.blocks is not None
    actions = [b for b in snoozed.blocks if b.get("type") == "actions"]
    assert actions and actions[0]["elements"][0]["action_id"] == ACTION_UNDO_SNOOZE

    undo = app.handle_undo_snooze("o1")
    assert app.graph.get("o1").snoozed_until is None
    assert "un-snoozed" in undo.text.lower()


# --------------------------------------------------------------------------- #
# Review-blocked hero button — read-only DM summary (Req 6.1)
# --------------------------------------------------------------------------- #
def test_review_blocked_summarizes_blocked_loops():
    app = _build_app()
    app.graph.upsert(_obligation("o1", subject_summary="review the deck", owed_person_id=OTHER))
    app.graph.upsert(
        _obligation("o2", subject_summary="sign the budget", owed_person_id="U_CAROL")
    )
    # A waiting-on-other loop must not appear in the blocked-on-you summary.
    app.graph.upsert(_obligation("w1", state=LoopState.WAITING_ON_OTHER))

    result = app.handle_review_blocked(USER)
    assert not result.requires_confirmation
    assert "2" in result.text
    assert "review the deck" in result.text
    assert "sign the budget" in result.text
    assert "<@U_OTHER>" in result.text
    assert "<@U_CAROL>" in result.text


def test_review_blocked_reports_all_clear_when_none_blocked():
    app = _build_app()
    app.graph.upsert(_obligation("w1", state=LoopState.WAITING_ON_OTHER))
    result = app.handle_review_blocked(USER)
    assert "caught up" in result.text.lower()


def test_review_blocked_action_handler_acks_and_dms():
    app = _build_app()
    app.graph.upsert(_obligation("o1", subject_summary="review the deck"))
    bolt = FakeBoltApp()
    app.register_handlers(bolt)
    client = FakeClient()

    acked = {"v": False}

    def ack():
        acked["v"] = True

    body = {"user": {"id": USER}, "actions": [{"value": "review_blocked"}]}
    bolt.actions[ACTION_REVIEW_BLOCKED](ack, body, client)

    assert acked["v"] is True
    assert len(client.messages) == 1
    assert client.messages[0]["channel"] == USER
    assert "review the deck" in client.messages[0]["text"]


# --------------------------------------------------------------------------- #
# Nudge composer modal (Best-UX flourish — editable AI draft, send as you)
# --------------------------------------------------------------------------- #
def test_nudge_click_opens_editable_modal_with_ai_draft():
    """Tapping Nudge opens a modal pre-filled with the AI-drafted, editable message."""
    app = _build_app()
    app.graph.upsert(_obligation("o1", subject_summary="review the deck"))
    bolt = FakeBoltApp()
    app.register_handlers(bolt)
    client = FakeClient()

    body = {"user": {"id": USER}, "trigger_id": "T1", "actions": [{"value": "o1"}]}
    bolt.actions[ACTION_NUDGE](lambda *a, **k: None, body, client)

    # A modal was opened (not a blind DM), carrying the obligation id + the draft.
    assert len(client.opened_views) == 1
    view = client.opened_views[0]["view"]
    assert view["type"] == "modal"
    assert view["callback_id"] == NUDGE_MODAL_CALLBACK
    assert view["private_metadata"] == "o1"
    input_block = next(b for b in view["blocks"] if b.get("type") == "input")
    assert "review the deck" in input_block["element"]["initial_value"]
    # Nothing is sent yet — the draft is awaiting the modal submit.
    assert app.confirmations.get(USER) is not None


def test_nudge_modal_submit_sends_edited_text_as_user():
    """Submitting the modal sends the (edited) message as the user."""
    sends: list = []
    app = _build_app(sends=sends)
    app.graph.upsert(_obligation("o1", subject_summary="review the deck"))
    bolt = FakeBoltApp()
    app.register_handlers(bolt)
    client = FakeClient()

    # Open the composer (registers the pending draft).
    open_body = {"user": {"id": USER}, "trigger_id": "T1", "actions": [{"value": "o1"}]}
    bolt.actions[ACTION_NUDGE](lambda *a, **k: None, open_body, client)

    # Submit with an edited message.
    submit_body = {
        "user": {"id": USER},
        "view": {
            "private_metadata": "o1",
            "state": {
                "values": {
                    NUDGE_MODAL_INPUT_BLOCK: {
                        NUDGE_MODAL_INPUT_ACTION: {"value": "Hey, gentle nudge on the deck 🙏"}
                    }
                }
            },
        },
    }
    bolt.views[NUDGE_MODAL_CALLBACK](lambda *a, **k: None, submit_body, client)

    # The edited text was posted as the user, and the pending send was consumed.
    assert sends and sends[-1][1] == "Hey, gentle nudge on the deck 🙏"
    assert app.confirmations.get(USER) is None


def test_nudge_modal_submit_falls_back_to_draft_when_unedited():
    """Leaving the box untouched sends the original AI draft."""
    sends: list = []
    app = _build_app(sends=sends)
    app.graph.upsert(_obligation("o1", subject_summary="review the deck"))

    view, message = app.prepare_nudge_modal("o1")
    assert view is not None and message == ""

    result = app.handle_nudge_modal_submit(USER, "o1", "")
    assert result.sent is True
    assert sends and "review the deck" in sends[-1][1]


def test_quick_nudge_dropdown_opens_modal_for_selected_person():
    """Picking someone in the hero 'nudge anyone' dropdown opens the composer for
    that exact loop."""
    app = _build_app()
    app.graph.upsert(_obligation("o1", subject_summary="review the deck"))
    app.graph.upsert(_obligation("o2", subject_summary="merge the PR"))
    bolt = FakeBoltApp()
    app.register_handlers(bolt)
    client = FakeClient()

    body = {
        "user": {"id": USER},
        "trigger_id": "T1",
        "actions": [{"type": "static_select", "selected_option": {"value": "o2"}}],
    }
    bolt.actions[ACTION_QUICK_NUDGE](lambda *a, **k: None, body, client)

    assert len(client.opened_views) == 1
    assert client.opened_views[0]["view"]["private_metadata"] == "o2"


# --------------------------------------------------------------------------- #
# Autonomy boundaries — send-as-user behind the 24h one-tap confirm (Req 13.2)
# --------------------------------------------------------------------------- #
def test_nudge_click_drafts_and_requires_confirmation_without_sending():
    sends: list = []
    app = _build_app(sends=sends)
    app.graph.upsert(_obligation())

    result = app.handle_nudge_click("o1")
    assert result.requires_confirmation is True
    assert app.confirmations.has_pending(USER)
    # Nothing sent yet (Req 7.2 / 13.2).
    assert sends == []


def test_confirm_sends_the_nudge_as_user():
    sends: list = []
    app = _build_app(sends=sends)
    app.graph.upsert(_obligation())

    app.handle_nudge_click("o1")
    result = app.handle_confirm_send(USER)

    assert result.sent is True
    assert len(sends) == 1
    assert sends[0][0] == "C1"  # posted in the source channel
    assert not app.confirmations.has_pending(USER)


def test_decline_cancels_without_sending():
    sends: list = []
    app = _build_app(sends=sends)
    app.graph.upsert(_obligation())

    app.handle_nudge_click("o1")
    result = app.handle_decline_send(USER)

    assert result.cancelled is True
    assert sends == []
    assert not app.confirmations.has_pending(USER)


def test_delegate_click_requires_confirmation_then_confirm_sends():
    sends: list = []
    app = _build_app(sends=sends)
    app.graph.upsert(_obligation())

    pre = app.handle_delegate_click("o1", "U_TEAMMATE")
    assert pre.requires_confirmation is True
    assert sends == []

    post = app.handle_confirm_send(USER)
    assert post.sent is True
    assert len(sends) == 1
    assert sends[0][0] == "U_TEAMMATE"
    assert app.graph.get("o1").owner_person_id == "U_TEAMMATE"


# --------------------------------------------------------------------------- #
# 24h confirmation timeout (Req 13.5)
# --------------------------------------------------------------------------- #
def test_confirmation_expires_after_24h_and_is_retained_unsent():
    sends: list = []
    clock = {"t": datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)}

    def now():
        return clock["t"].isoformat()

    app = _build_app(sends=sends, now=now)
    app.graph.upsert(_obligation())
    app.handle_nudge_click("o1")
    assert app.confirmations.has_pending(USER)

    # Advance past the 24h timeout, then sweep.
    clock["t"] = clock["t"] + timedelta(hours=24, minutes=1)
    expired = app.sweep_confirmation_timeouts()

    assert expired == [USER]
    assert not app.confirmations.has_pending(USER)
    # Retained unsent: the message was never sent (Req 13.5).
    assert sends == []


def test_confirm_after_24h_is_treated_as_expired():
    sends: list = []
    clock = {"t": datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)}

    def now():
        return clock["t"].isoformat()

    app = _build_app(sends=sends, now=now)
    app.graph.upsert(_obligation())
    app.handle_nudge_click("o1")

    clock["t"] = clock["t"] + timedelta(hours=25)
    result = app.handle_confirm_send(USER)

    assert result.cancelled is True
    assert sends == []


# --------------------------------------------------------------------------- #
# Draft failure (Req 7.8)
# --------------------------------------------------------------------------- #
def test_nudge_draft_failure_sends_nothing_and_does_not_gate():
    sends: list = []
    app = _build_app(sends=sends, draft_ok=False)
    app.graph.upsert(_obligation())

    result = app.handle_nudge_click("o1")
    assert result.requires_confirmation is False
    assert not app.confirmations.has_pending(USER)
    assert sends == []


# --------------------------------------------------------------------------- #
# Conversational routing (Req 12)
# --------------------------------------------------------------------------- #
def test_assistant_message_routes_to_conversational_with_trace():
    app = _build_app()
    app.graph.upsert(_obligation())
    result = app.handle_assistant_message(USER, "who is blocked on me?")
    assert "please review the deck" in result.text
    assert "Tools:" in result.text  # Tool_Use_Trace surfaced (Req 12.5)


# --------------------------------------------------------------------------- #
# Scheduler wiring (Req 2.1, 11.1, 13.5)
# --------------------------------------------------------------------------- #
def test_build_scheduler_registers_sweep_digest_and_timeout_jobs():
    pytest.importorskip("apscheduler")
    app = _build_app()
    scheduler = app.build_scheduler(client=FakeClient())
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {"watcher_sweep", "daily_digest", "confirmation_timeout_sweep"}

    # The Watcher sweep fires at the configured interval and must be ≤ 60s (Req 2.1).
    sweep = scheduler.get_job("watcher_sweep")
    assert sweep.trigger.interval.total_seconds() == 30
    assert sweep.trigger.interval.total_seconds() <= 60
