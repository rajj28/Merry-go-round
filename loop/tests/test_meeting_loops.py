"""Meeting loops — detection, rendering, and the schedule→confirm→heal beat.

A message proposing a meeting ("let's meet tomorrow at 4 PM") is an open loop of
``kind=meeting`` carrying the proposed time in ``due_at``. It rides the exact
same pipeline as every reply loop (adjudicate → graph → surface → learn); the
only meeting-specific behaviour is:

  * the App Home row shows a 📅 proposed-time chip and a *Schedule it* link
    button (the prefilled Google Calendar event — no OAuth);
  * *Schedule it* posts a one-tap confirmation proposal into the source thread;
  * a confirm heals the loop **autonomously**, landing it in the Auto-Healed feed.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from loop.action.action_agent import ActionAgent
from loop.action.app_home import (
    ACTION_SCHEDULE,
    SCHEDULE_BUTTON_TEXT,
    auto_healed_rows,
    build_app_home_view,
    is_meeting,
    meeting_calendar_url,
    meeting_time_suffix,
)
from loop.adjudicator.adjudicator import (
    AdjudicationOutcome,
    Adjudicator,
    Direction,
    SmartAdjudication,
)
from loop.app import ACTION_MEETING_CONFIRM, LoopApp
from loop.conversational.conversational_agent import ConversationalAgent
from loop.graph.models import (
    ClosureKind,
    LoopState,
    Obligation,
    ObligationKind,
    utc_now_iso,
)
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.learn.feedback import LearnEngine
from loop.pipeline import AdjudicationQueue
from loop.verifier.types import VerificationResult
from loop.watcher.rts_contract import CandidateMessage
from loop.watcher.watcher import Watcher

USER = "U_USER"
OTHER = "U_OTHER"
NOW = "2025-01-05T12:00:00+00:00"


def _obligation(oid="OBL_M1", **kw) -> Obligation:
    base = dict(
        obligation_id=oid,
        owes_person_id=USER,
        owed_person_id=OTHER,
        owner_person_id=USER,
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.9,
        last_touch_timestamp="2025-01-05T09:00:00+00:00",
        source_msg_channel="C1",
        source_msg_ts="1735808400.000100",
        subject_summary="Sync on the launch plan",
    )
    base.update(kw)
    return Obligation(**base)


def _meeting(oid="OBL_M1", **kw) -> Obligation:
    kw.setdefault("kind", ObligationKind.MEETING)
    kw.setdefault("due_at", "2025-01-06T16:00:00+00:00")
    return _obligation(oid, **kw)


def _candidate(text_="let's meet tomorrow at 4 PM?") -> CandidateMessage:
    return CandidateMessage(
        channel_id="C1",
        message_ts="1735808400.000100",
        author_id=OTHER,
        text=text_,
        permalink="",
    )


# --------------------------------------------------------------------------- #
# Model + store
# --------------------------------------------------------------------------- #
def test_kind_defaults_to_reply():
    assert _obligation().kind is ObligationKind.REPLY
    assert _obligation().due_at is None


def test_meeting_roundtrips_through_store():
    graph = SqliteObligationGraph(IN_MEMORY)
    graph.upsert(_meeting())
    stored = graph.get("OBL_M1")
    assert stored.kind is ObligationKind.MEETING
    assert stored.due_at == "2025-01-06T16:00:00+00:00"


def test_migration_adds_columns_to_pre_meeting_database(tmp_path):
    """A database created before kind/due_at shipped gains both columns on open."""
    db = str(tmp_path / "old.db")
    first = SqliteObligationGraph(db)
    first.upsert(_obligation())
    # Simulate the pre-meeting schema by dropping the new columns in place.
    with first._engine.connect() as conn:
        conn.execute(text("ALTER TABLE obligation DROP COLUMN kind"))
        conn.execute(text("ALTER TABLE obligation DROP COLUMN due_at"))
        conn.commit()
    first._engine.dispose()

    reopened = SqliteObligationGraph(db)
    stored = reopened.get("OBL_M1")
    assert stored.kind is ObligationKind.REPLY  # old row reads as a reply loop
    result = reopened.upsert(_meeting("OBL_M2"))
    assert result.value.kind is ObligationKind.MEETING


# --------------------------------------------------------------------------- #
# Adjudicator — the smart tier's kind / due_at extraction is written verbatim
# --------------------------------------------------------------------------- #
def _judgement(**kw) -> SmartAdjudication:
    base = dict(
        is_loop=True,
        involves_user=True,
        direction=Direction.OTHER_OWES,
        confidence=0.9,
        subject_summary="Sync on the launch plan",
    )
    base.update(kw)
    return SmartAdjudication(**base)


def _adjudicate(judgement) -> Obligation:
    graph = SqliteObligationGraph(IN_MEMORY)
    adjudicator = Adjudicator(lambda c, *, user_id: judgement, graph)
    result = adjudicator.adjudicate(_candidate(), user_id=USER)
    assert result.outcome is AdjudicationOutcome.CREATED
    return result.obligation


def test_adjudicator_writes_meeting_kind_and_due_at():
    stored = _adjudicate(
        _judgement(kind="meeting", due_at="2025-01-06T16:00:00+00:00")
    )
    assert stored.kind is ObligationKind.MEETING
    assert stored.due_at == "2025-01-06T16:00:00+00:00"


def test_adjudicator_normalizes_naive_and_z_suffixed_due_at():
    stored = _adjudicate(_judgement(kind="meeting", due_at="2025-01-06T16:00:00Z"))
    assert stored.due_at == "2025-01-06T16:00:00+00:00"


def test_adjudicator_defaults_junk_kind_and_unparseable_due_at():
    stored = _adjudicate(_judgement(kind="tea party", due_at="tomorrow-ish"))
    assert stored.kind is ObligationKind.REPLY
    assert stored.due_at is None


def test_adjudicator_default_judgement_stays_reply():
    stored = _adjudicate(_judgement())
    assert stored.kind is ObligationKind.REPLY
    assert stored.due_at is None


# --------------------------------------------------------------------------- #
# App Home rendering — 📅 chip + Schedule it button on meeting rows only
# --------------------------------------------------------------------------- #
def test_meeting_time_suffix_only_for_meetings_with_time():
    assert "proposed for" in meeting_time_suffix(_meeting())
    assert meeting_time_suffix(_obligation()) == ""
    assert meeting_time_suffix(_meeting(due_at=None)) == ""


def test_calendar_url_prefills_a_30_minute_event():
    url = meeting_calendar_url(_meeting())
    assert url is not None
    assert "action=TEMPLATE" in url
    assert "dates=20250106T160000Z/20250106T163000Z" in url
    assert meeting_calendar_url(_obligation()) is None


def test_app_home_meeting_row_carries_chip_and_schedule_button():
    graph = SqliteObligationGraph(IN_MEMORY)
    graph.upsert(_meeting())
    view = json.dumps(build_app_home_view(graph, NOW), ensure_ascii=False)
    assert "proposed for" in view
    assert ACTION_SCHEDULE in view
    assert SCHEDULE_BUTTON_TEXT in view
    assert "calendar.google.com" in view


def test_app_home_reply_row_has_no_schedule_button():
    graph = SqliteObligationGraph(IN_MEMORY)
    graph.upsert(_obligation())
    view = json.dumps(build_app_home_view(graph, NOW), ensure_ascii=False)
    assert ACTION_SCHEDULE not in view
    assert "proposed for" not in view


def test_cards_style_swaps_snooze_for_schedule_on_meetings():
    graph = SqliteObligationGraph(IN_MEMORY)
    graph.upsert(_meeting())
    view = json.dumps(build_app_home_view(graph, NOW, style="cards"))
    assert ACTION_SCHEDULE in view


# --------------------------------------------------------------------------- #
# The schedule → in-thread confirm → autonomous heal beat
# --------------------------------------------------------------------------- #
class FakeVerifier:
    def verify_pr(self, obligation, *, purpose):  # noqa: ANN001
        return VerificationResult.UNRESOLVED


class FakeClient:
    """Records chat_postMessage / chat_update calls."""

    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []

    def chat_postMessage(self, **kw):  # noqa: ANN003, N802
        self.posts.append(kw)
        return {"ok": True}

    def chat_update(self, **kw):  # noqa: ANN003, N802
        self.updates.append(kw)
        return {"ok": True}


def _build_app(graph: SqliteObligationGraph) -> LoopApp:
    verifier = FakeVerifier()
    action = ActionAgent(
        graph,
        verifier,  # type: ignore[arg-type]
        now=utc_now_iso,
        slack_send_as_user=lambda channel, text_: None,
        draft=lambda o: f"nudge about {o.subject_summary}",
    )
    adjudicator = Adjudicator(lambda c, *, user_id: _judgement(), graph)
    queue = AdjudicationQueue(adjudicator, USER)
    watcher = Watcher(
        graph,
        rts_client=lambda: {"ok": False},
        forward=queue.enqueue,
        interval_seconds=30,
    )
    return LoopApp(
        graph=graph,
        watcher=watcher,
        adjudicator=adjudicator,
        verifier=verifier,  # type: ignore[arg-type]
        action=action,
        conversational=ConversationalAgent(graph, action),
        learn=LearnEngine(graph),
        queue=queue,
        user_id=USER,
        now=utc_now_iso,
    )


def test_schedule_click_posts_confirm_proposal_into_source_thread():
    graph = SqliteObligationGraph(IN_MEMORY)
    graph.upsert(_meeting())
    app = _build_app(graph)
    client = FakeClient()

    result = app.handle_schedule_click("OBL_M1", client)

    assert len(client.posts) == 1
    post = client.posts[0]
    assert post["channel"] == "C1"
    assert post["thread_ts"] == "1735808400.000100"
    assert ACTION_MEETING_CONFIRM in json.dumps(post["blocks"])
    assert "calendar.google.com" in json.dumps(post["blocks"])
    assert "confirms" in result.text


def test_schedule_click_on_missing_or_timeless_loop_is_safe():
    graph = SqliteObligationGraph(IN_MEMORY)
    app = _build_app(graph)
    assert "no longer exists" in app.handle_schedule_click("nope", FakeClient()).text

    graph.upsert(_meeting(due_at=None))
    assert "No proposed time" in app.handle_schedule_click("OBL_M1", FakeClient()).text


def test_meeting_confirm_heals_autonomously_into_the_feed():
    graph = SqliteObligationGraph(IN_MEMORY)
    graph.upsert(_meeting())
    app = _build_app(graph)

    result = app.handle_meeting_confirm("OBL_M1")

    stored = graph.get("OBL_M1")
    assert stored.loop_state is LoopState.HEALED
    assert stored.closure_kind is ClosureKind.AUTONOMOUS
    assert "Meeting confirmed" in stored.closure_reason
    assert stored.closure_timestamp is not None
    assert "confirmed" in result.text
    # An autonomous closure is exactly what the Auto-Healed feed shows (Req 9.1).
    assert [o.obligation_id for o in auto_healed_rows(graph, user_id=USER)] == ["OBL_M1"]


def test_meeting_confirm_is_idempotent_and_safe_on_missing_loop():
    graph = SqliteObligationGraph(IN_MEMORY)
    graph.upsert(_meeting())
    app = _build_app(graph)

    app.handle_meeting_confirm("OBL_M1")
    again = app.handle_meeting_confirm("OBL_M1")
    assert "already confirmed" in again.text
    assert "no longer exists" in app.handle_meeting_confirm("nope").text


def test_is_meeting_predicate():
    assert is_meeting(_meeting())
    assert not is_meeting(_obligation())


def test_live_client_anchors_spoken_times_to_author_timezone(monkeypatch):
    """The live prompt carries the author's wall-clock time (Slack tz_offset)."""
    from loop import llm
    from loop.adjudicator.adjudicator import build_smart_reasoning_client

    prompts: list[str] = []

    def fake_chat(messages, **kw):  # noqa: ANN001, ANN003
        prompts.append(messages[0]["content"])
        return json.dumps(
            {
                "is_loop": True,
                "owes_id": OTHER,
                "owed_id": USER,
                "confidence": 0.9,
                "subject_summary": "Sync on the launch plan",
                "kind": "meeting",
                "due_at": "2025-01-02T14:00:00+00:00",
            }
        )

    monkeypatch.setattr(llm, "chat", fake_chat)

    class _Settings:
        llm_provider = "groq"
        groq_api_key = "gsk-test"

        def require(self, field):  # noqa: ANN001
            assert getattr(self, field, None)

    client = build_smart_reasoning_client(_Settings(), tz_lookup=lambda a: 7200)
    judgement = client(_candidate(), user_id=USER)
    assert judgement.kind == "meeting"
    assert "UTC offset +2.0h" in prompts[0]
    assert "author's local clock" in prompts[0]

    unknown_tz = build_smart_reasoning_client(_Settings(), tz_lookup=lambda a: None)
    unknown_tz(_candidate(), user_id=USER)
    assert "assume UTC" in prompts[1]


def test_loops_own_proposal_message_never_feeds_back_into_perception():
    """Loop's in-thread proposal is a bot message — it must never become a loop."""
    graph = SqliteObligationGraph(IN_MEMORY)
    forwarded: list[CandidateMessage] = []
    watcher = Watcher(
        graph,
        rts_client=lambda: {"ok": False},
        forward=forwarded.append,
        interval_seconds=30,
    )
    outcome = watcher.on_message_event(
        {
            "channel": "C1",
            "ts": "1735808500.000200",
            "bot_id": "B_LOOP",
            "text": "proposing *Sync on the launch plan* — one tap to lock it in.",
        }
    )
    assert outcome.value == "dropped"
    assert forwarded == []
