"""Unit tests for the card-style App Home rendering (Best-UX `card` block upgrade).

These lock the opt-in ``style="cards"`` path of :func:`build_app_home_view`:

  * obligation rows render as Block Kit ``card`` blocks (not section + actions);
  * each active-row card carries the avatar ``icon``, a ``title``/``subtitle``, and at
    most three action buttons (Nudge primary / Snooze / Dismiss danger);
  * healed-feed cards are action-less and carry the closure timestamp as ``subtext``;
  * the default (``style="sections"``) render is unchanged (no ``card`` blocks);
  * card text fields stay within Slack's documented length caps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loop.action.app_home import (
    ACTION_DISMISS,
    ACTION_NUDGE,
    ACTION_SNOOZE,
    CARD_SUBTITLE_MAX,
    CARD_TITLE_MAX,
    HERO_ZERO_TEXT,
    build_app_home_compact_view,
    build_app_home_view,
)
from loop.graph.models import ClosureKind, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph

NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()
THRESHOLD = 0.5
USER = "U_USER"

AVATARS = {
    "U_OTHER": "https://example.com/other_72.png",
    "U_OWES": "https://example.com/owes_72.png",
}


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def _store() -> SqliteObligationGraph:
    graph = SqliteObligationGraph(IN_MEMORY)
    graph.set_threshold(THRESHOLD)
    return graph


def _blocked(oid: str, **overrides) -> Obligation:
    fields = dict(
        obligation_id=oid,
        owes_person_id=USER,
        owed_person_id="U_OTHER",
        owner_person_id=USER,
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.9,
        last_touch_timestamp=_ago(80),  # overdue
        source_msg_channel="C1",
        source_msg_ts="100.1",
        subject_summary=f"Subject {oid}",
    )
    fields.update(overrides)
    return Obligation(**fields)


def _healed(oid: str, **overrides) -> Obligation:
    fields = dict(
        obligation_id=oid,
        owes_person_id="U_OTHER",
        owed_person_id=USER,
        owner_person_id=USER,
        loop_state=LoopState.HEALED,
        confidence_score=0.9,
        last_touch_timestamp=_ago(10),
        closure_kind=ClosureKind.AUTONOMOUS,
        closure_timestamp=_ago(5),
        closure_reason="PR merged",
        source_msg_channel="C1",
        source_msg_ts="100.2",
        subject_summary=f"Healed {oid}",
    )
    fields.update(overrides)
    return Obligation(**fields)


def _blocks_of_type(view: dict, block_type: str) -> list[dict]:
    return [b for b in view["blocks"] if b.get("type") == block_type]


def test_cards_style_renders_card_blocks() -> None:
    graph = _store()
    graph.upsert(_blocked("B1"))
    view = build_app_home_view(
        graph, now=NOW_ISO, user_id=USER, avatars=AVATARS, style="cards"
    )
    cards = _blocks_of_type(view, "card")
    assert cards, "expected at least one card block in cards style"
    # No legacy per-row actions blocks when rendering cards (the hero's own
    # actions block is allowed; row actions live inside the cards).
    row_action_blocks = [
        b for b in view["blocks"]
        if b.get("type") == "actions" and str(b.get("block_id", "")).startswith("row_actions::")
    ]
    assert not row_action_blocks


def test_sections_style_has_no_card_blocks() -> None:
    graph = _store()
    graph.upsert(_blocked("B1"))
    view = build_app_home_view(graph, now=NOW_ISO, user_id=USER, avatars=AVATARS)
    assert not _blocks_of_type(view, "card")
    # The proven section layout keeps its per-row actions blocks.
    assert _blocks_of_type(view, "actions")


def test_active_card_has_avatar_icon_title_subtitle() -> None:
    graph = _store()
    graph.upsert(_blocked("B1"))
    view = build_app_home_view(
        graph, now=NOW_ISO, user_id=USER, avatars=AVATARS, style="cards"
    )
    card = _blocks_of_type(view, "card")[0]
    assert card["icon"]["type"] == "image"
    assert card["icon"]["image_url"] == AVATARS["U_OTHER"]
    assert card["title"]["text"] == "Subject B1"
    assert "<@U_OTHER>" in card["subtitle"]["text"]


def test_active_card_has_at_most_three_buttons() -> None:
    graph = _store()
    graph.upsert(_blocked("B1"))
    view = build_app_home_view(
        graph, now=NOW_ISO, user_id=USER, avatars=AVATARS, style="cards"
    )
    card = _blocks_of_type(view, "card")[0]
    actions = card["actions"]
    assert 1 <= len(actions) <= 3
    action_ids = [a["action_id"] for a in actions]
    assert action_ids == [ACTION_NUDGE, ACTION_SNOOZE, ACTION_DISMISS]
    # Buttons still carry the obligation id so existing handlers resolve the row.
    assert all(a["value"] == "B1" for a in actions)


def test_healed_card_is_actionless_with_subtext_timestamp() -> None:
    graph = _store()
    graph.upsert(_healed("H1"))
    view = build_app_home_view(
        graph, now=NOW_ISO, user_id=USER, avatars=AVATARS, style="cards"
    )
    cards = _blocks_of_type(view, "card")
    healed_card = cards[-1]
    assert "actions" not in healed_card
    assert "subtext" in healed_card
    assert "closed automatically" in healed_card["subtext"]["text"]
    assert "PR merged" in healed_card["subtitle"]["text"]


def test_card_text_fields_within_length_caps() -> None:
    graph = _store()
    graph.upsert(_blocked("B1", subject_summary="x" * 400))
    view = build_app_home_view(
        graph, now=NOW_ISO, user_id=USER, avatars=AVATARS, style="cards"
    )
    card = _blocks_of_type(view, "card")[0]
    assert len(card["title"]["text"]) <= CARD_TITLE_MAX
    assert len(card["subtitle"]["text"]) <= CARD_SUBTITLE_MAX


NAMES = {"U_OTHER": "Alice"}


def test_named_card_leads_with_name_and_puts_ask_in_body() -> None:
    graph = _store()
    graph.upsert(_blocked("B1", subject_summary="Alice is waiting on your review."))
    view = build_app_home_view(
        graph, now=NOW_ISO, user_id=USER, avatars=AVATARS, names=NAMES, style="cards"
    )
    card = _blocks_of_type(view, "card")[0]
    # Title is the person's display name (no long, truncating subject).
    assert card["title"]["text"] == "Alice"
    # The full ask moves to the wrapping body (no truncation).
    assert card["body"]["text"] == "Alice is waiting on your review."
    # The subtitle is the clean meta line — no redundant @mention when named.
    assert "<@U_OTHER>" not in card["subtitle"]["text"]
    assert "overdue" in card["subtitle"]["text"]


# ---------------------------------------------------------------------------
# Compact "command-center" layout (carousel + celebratory zero-state)
# ---------------------------------------------------------------------------
def _waiting(oid: str, **overrides) -> Obligation:
    fields = dict(
        obligation_id=oid,
        owes_person_id="U_OWES",
        owed_person_id=USER,
        owner_person_id="U_OWES",
        loop_state=LoopState.WAITING_ON_OTHER,
        confidence_score=0.9,
        last_touch_timestamp=_ago(30),
        source_msg_channel="C2",
        source_msg_ts="100.3",
        subject_summary=f"Waiting {oid}",
    )
    fields.update(overrides)
    return Obligation(**fields)


def test_compact_layout_renders_blocked_as_carousel() -> None:
    graph = _store()
    graph.upsert(_blocked("B1"))
    graph.upsert(_blocked("B2"))
    view = build_app_home_compact_view(graph, now=NOW_ISO, user_id=USER, avatars=AVATARS)

    carousels = _blocks_of_type(view, "carousel")
    assert len(carousels) == 1
    assert len(carousels[0]["elements"]) == 2
    assert all(c["type"] == "card" for c in carousels[0]["elements"])
    # Waiting/auto-closed are compact, not cards, so no standalone card blocks remain.
    assert not _blocks_of_type(view, "card")


def test_compact_quick_nudge_dropdown_shown_only_for_multiple_blocked() -> None:
    """With 2+ blocked loops a 'nudge anyone' static_select appears, listing each
    loop by obligation id; with a single blocked loop it does not."""
    graph = _store()
    graph.upsert(_blocked("B1"))
    graph.upsert(_blocked("B2"))
    view = build_app_home_compact_view(graph, now=NOW_ISO, user_id=USER, avatars=AVATARS)

    selects = [
        el
        for b in _blocks_of_type(view, "actions")
        for el in b.get("elements", [])
        if el.get("type") == "static_select"
    ]
    assert len(selects) == 1
    values = {opt["value"] for opt in selects[0]["options"]}
    assert values == {"B1", "B2"}

    # Single blocked loop → no dropdown (the per-card Nudge button suffices).
    graph_one = _store()
    graph_one.upsert(_blocked("B1"))
    view_one = build_app_home_compact_view(graph_one, now=NOW_ISO, user_id=USER, avatars=AVATARS)
    selects_one = [
        el
        for b in _blocks_of_type(view_one, "actions")
        for el in b.get("elements", [])
        if el.get("type") == "static_select"
    ]
    assert selects_one == []


def test_compact_zero_state_is_celebratory() -> None:
    """With nobody blocked, the hero shows the all-caught-up line and a celebratory
    stats line (loops auto-closed for you), and renders no carousel."""
    graph = _store()
    graph.upsert(_waiting("W1"))
    graph.upsert(_healed("H1"))
    view = build_app_home_compact_view(graph, now=NOW_ISO, user_id=USER, avatars=AVATARS)

    assert view["blocks"][0]["text"]["text"] == HERO_ZERO_TEXT
    assert not _blocks_of_type(view, "carousel")
    # The celebratory stats line rewards the empty court with the work Loop did.
    contexts = _blocks_of_type(view, "context")
    celebratory = contexts[0]["elements"][0]["text"]
    assert "auto-closed" in celebratory and "1" in celebratory
