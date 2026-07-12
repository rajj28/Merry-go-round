"""Example-based unit tests for the App Home view builder (tasks 9.1–9.3, Req 6, 9).

These exercise the deterministic, network-free Block Kit builder in
:mod:`loop.action.app_home`:

  * hero count matches the surfaced ``blocked-on-you`` obligations, including the
    zero-state copy (Req 6.1, 6.7);
  * the two active sections contain exactly the surfaced obligations of each
    state, sorted by ``last_touch_timestamp`` oldest→newest (Req 6.3, 6.4);
  * Aging_Chip boundaries — just under / exactly at 24h, exactly at / just over
    72h (Req 6.5, warning inclusive of both boundaries);
  * footer per-section totals (Req 6.6);
  * explicit empty-state messages for every section on an empty graph (Req 6.9).

Property-based coverage (Properties 19–22) is tasks 9.4–9.8; this file is the
example-based companion. Tests run against the real in-memory SQLite store so the
surfacing gate (threshold/dismissed/snooze) is exercised end to end without mocks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loop.action.app_home import (
    AgingState,
    HERO_ZERO_TEXT,
    HERO_SUBLINE_TEXT,
    HERO_REVIEW_BUTTON_TEXT,
    ACTION_REVIEW_BLOCKED,
    BLOCKED_SECTION_TITLE,
    WAITING_SECTION_TITLE,
    HEALED_SECTION_TITLE,
    HERO_TAGLINE,
    BLOCKED_SECTION_DESCRIPTOR,
    WAITING_SECTION_DESCRIPTOR,
    HEALED_SECTION_DESCRIPTOR,
    OBLIGATION_ZERO_BODY,
    BLOCKED_EMPTY_TEXT,
    WAITING_EMPTY_TEXT,
    HEALED_EMPTY_TEXT,
    HEALED_PERSON_PLACEHOLDER,
    HEALED_REASON_PLACEHOLDER,
    AUTO_HEALED_FEED_CAP,
    aging_chip,
    auto_healed_rows,
    blocked_on_you_rows,
    build_app_home_view,
    classify_aging,
    footer_text,
    hero_count,
    hero_text,
    hero_card_text,
    hero_stats_text,
    resolved_person_id,
    slack_date,
    waiting_on_other_rows,
)
from loop.graph.models import ArtifactType, ClosureKind, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph

# Fixed reference "now" so every age/aging assertion is deterministic.
NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()
THRESHOLD = 0.5
H = 3600.0


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ago(hours: float) -> str:
    """ISO timestamp ``hours`` before NOW."""
    return _iso(NOW - timedelta(hours=hours))


def _obligation(oid: str, **overrides) -> Obligation:
    """A baseline surfaced blocked-on-you obligation; override per test."""
    fields = dict(
        obligation_id=oid,
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.9,
        last_touch_timestamp=_ago(1),
        source_msg_channel="C1",
        source_msg_ts="100.1",
        subject_summary=f"Subject {oid}",
    )
    fields.update(overrides)
    return Obligation(**fields)


def _store() -> SqliteObligationGraph:
    graph = SqliteObligationGraph(IN_MEMORY)
    graph.set_threshold(THRESHOLD)
    return graph


# ---------------------------------------------------------------------------
# Aging chip boundaries (task 9.2, Req 6.5)
# ---------------------------------------------------------------------------
def test_classify_aging_just_under_24h_is_fresh() -> None:
    assert classify_aging(24 * H - 1) is AgingState.FRESH


def test_classify_aging_exactly_24h_is_warning() -> None:
    # Warning band is inclusive of the 24h lower boundary.
    assert classify_aging(24 * H) is AgingState.WARNING


def test_classify_aging_exactly_72h_is_warning() -> None:
    # Warning band is inclusive of the 72h upper boundary.
    assert classify_aging(72 * H) is AgingState.WARNING


def test_classify_aging_just_over_72h_is_overdue() -> None:
    assert classify_aging(72 * H + 1) is AgingState.OVERDUE


def test_aging_chip_uses_last_touch_relative_to_now() -> None:
    # 48h old → squarely in the warning band.
    chip = aging_chip(_obligation("OB", last_touch_timestamp=_ago(48)), NOW)
    assert chip.state is AgingState.WARNING
    assert chip.age_text == "2d"
    assert chip.text == "2d"  # plain age, no emoji cue


def test_aging_chip_fresh_and_overdue_states() -> None:
    fresh = aging_chip(_obligation("F", last_touch_timestamp=_ago(1)), NOW)
    overdue = aging_chip(_obligation("O", last_touch_timestamp=_ago(100)), NOW)
    assert fresh.state is AgingState.FRESH
    assert overdue.state is AgingState.OVERDUE
    # No emoji anywhere in the rendered chip text.
    assert fresh.text == fresh.age_text and overdue.text == overdue.age_text


# ---------------------------------------------------------------------------
# Hero banner count + zero-state (task 9.1, Req 6.1, 6.7)
# ---------------------------------------------------------------------------
def test_hero_count_matches_surfaced_blocked_on_you() -> None:
    graph = _store()
    graph.upsert(_obligation("B1"))
    graph.upsert(_obligation("B2"))
    graph.upsert(_obligation("B3"))
    # A waiting-on-other does NOT count toward the hero number.
    graph.upsert(_obligation("W1", loop_state=LoopState.WAITING_ON_OTHER))
    # A below-threshold blocked-on-you is unsurfaced → excluded.
    graph.upsert(_obligation("B_LOW", confidence_score=0.1))
    # A dismissed blocked-on-you is excluded.
    graph.upsert(_obligation("B_DIS", dismissed=True))

    assert hero_count(graph, NOW_ISO) == 3
    view = build_app_home_view(graph, NOW_ISO)
    assert view["type"] == "home"
    assert view["blocks"][0]["text"]["text"] == "3 people are blocked on you"


def test_hero_zero_state_message() -> None:
    graph = _store()
    # Only a waiting obligation exists — zero blocked-on-you.
    graph.upsert(_obligation("W1", loop_state=LoopState.WAITING_ON_OTHER))
    assert hero_count(graph, NOW_ISO) == 0
    view = build_app_home_view(graph, NOW_ISO)
    assert view["blocks"][0]["text"]["text"] == HERO_ZERO_TEXT


def test_hero_text_singular_pluralization() -> None:
    assert hero_text(1) == "1 person is blocked on you"
    assert hero_text(2) == "2 people are blocked on you"
    assert hero_text(0) == HERO_ZERO_TEXT


# ---------------------------------------------------------------------------
# Section contents + ordering (task 9.1, Req 6.3, 6.4)
# ---------------------------------------------------------------------------
def test_blocked_section_contains_exactly_surfaced_sorted_oldest_first() -> None:
    graph = _store()
    # Insert out of order; expect oldest→newest by last_touch_timestamp.
    graph.upsert(_obligation("NEW", last_touch_timestamp=_ago(1)))
    graph.upsert(_obligation("OLD", last_touch_timestamp=_ago(100)))
    graph.upsert(_obligation("MID", last_touch_timestamp=_ago(48)))
    # Noise that must be excluded from the blocked section:
    graph.upsert(_obligation("W", loop_state=LoopState.WAITING_ON_OTHER))
    graph.upsert(_obligation("LOW", confidence_score=0.0))
    graph.upsert(_obligation("DIS", dismissed=True))

    rows = blocked_on_you_rows(graph, NOW_ISO)
    assert [o.obligation_id for o in rows] == ["OLD", "MID", "NEW"]


def test_waiting_section_contains_exactly_surfaced_sorted_oldest_first() -> None:
    graph = _store()
    graph.upsert(
        _obligation("W_NEW", loop_state=LoopState.WAITING_ON_OTHER, last_touch_timestamp=_ago(2))
    )
    graph.upsert(
        _obligation("W_OLD", loop_state=LoopState.WAITING_ON_OTHER, last_touch_timestamp=_ago(80))
    )
    # A blocked-on-you must not leak into the waiting section.
    graph.upsert(_obligation("B", loop_state=LoopState.BLOCKED_ON_YOU))

    rows = waiting_on_other_rows(graph, NOW_ISO)
    assert [o.obligation_id for o in rows] == ["W_OLD", "W_NEW"]


def test_snoozed_obligation_is_excluded_from_section() -> None:
    graph = _store()
    future = _iso(NOW + timedelta(hours=5))
    graph.upsert(_obligation("SNOOZED", snoozed_until=future))
    graph.upsert(_obligation("VISIBLE"))
    rows = blocked_on_you_rows(graph, NOW_ISO)
    assert [o.obligation_id for o in rows] == ["VISIBLE"]


# ---------------------------------------------------------------------------
# Auto-Healed feed section (task 9.1 basic rows, Req 6.2, 6.8, 9.1)
# ---------------------------------------------------------------------------
def test_auto_healed_rows_membership_and_newest_first() -> None:
    graph = _store()
    graph.upsert(
        _obligation(
            "H_OLD",
            loop_state=LoopState.HEALED,
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_reason="PR merged",
            closure_timestamp=_ago(50),
            last_touch_timestamp=_ago(50),
        )
    )
    graph.upsert(
        _obligation(
            "H_NEW",
            loop_state=LoopState.HEALED,
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_reason="PR merged",
            closure_timestamp=_ago(2),
            last_touch_timestamp=_ago(2),
        )
    )
    # Manual closure is excluded from the feed.
    graph.upsert(
        _obligation(
            "H_MANUAL",
            loop_state=LoopState.HEALED,
            closure_kind=ClosureKind.MANUAL,
            last_touch_timestamp=_ago(1),
        )
    )

    rows = auto_healed_rows(graph, NOW_ISO)
    assert [o.obligation_id for o in rows] == ["H_NEW", "H_OLD"]


# ---------------------------------------------------------------------------
# Footer totals (task 9.3, Req 6.6)
# ---------------------------------------------------------------------------
def test_footer_counts_match_rendered_sections() -> None:
    graph = _store()
    graph.upsert(_obligation("B1"))
    graph.upsert(_obligation("B2"))
    graph.upsert(_obligation("W1", loop_state=LoopState.WAITING_ON_OTHER))
    graph.upsert(
        _obligation(
            "H1",
            loop_state=LoopState.HEALED,
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_reason="PR merged",
            closure_timestamp=_ago(3),
            last_touch_timestamp=_ago(3),
        )
    )

    view = build_app_home_view(graph, NOW_ISO)
    expected = footer_text(2, 1, 1)
    footer = view["blocks"][-1]
    assert footer["type"] == "context"
    assert footer["elements"][0]["text"] == expected
    assert "Blocked on you: *2*" in expected
    assert "Waiting on others: *1*" in expected
    assert "Auto-closed: *1*" in expected


# ---------------------------------------------------------------------------
# Empty states on an empty graph (task 9.3, Req 6.9)
# ---------------------------------------------------------------------------
def test_empty_graph_renders_all_section_empty_states_and_zero_hero() -> None:
    graph = _store()
    view = build_app_home_view(graph, NOW_ISO)

    texts = [
        b["text"]["text"]
        for b in view["blocks"]
        if b["type"] == "section" and b.get("text", {}).get("type") == "mrkdwn"
    ]
    assert BLOCKED_EMPTY_TEXT in texts
    assert WAITING_EMPTY_TEXT in texts
    assert HEALED_EMPTY_TEXT in texts

    # Hero shows the zero-state, and the footer reports all-zero counts.
    assert view["blocks"][0]["text"]["text"] == HERO_ZERO_TEXT
    assert view["blocks"][-1]["elements"][0]["text"] == footer_text(0, 0, 0)


def _section_empty_text(view: dict, section_title: str) -> str:
    """The first mrkdwn section text rendered under ``section_title``'s header.

    Locates the header block whose text equals ``section_title``, then returns the
    next mrkdwn section's text — i.e. the content the section renders directly under
    its header (the empty-state message when the section is empty).
    """
    blocks = view["blocks"]
    start = next(
        i
        for i, b in enumerate(blocks)
        if b["type"] == "header" and b["text"]["text"] == section_title
    )
    for b in blocks[start + 1 :]:
        if b["type"] == "header":  # reached the next section without content
            break
        if b["type"] == "section" and b.get("text", {}).get("type") == "mrkdwn":
            return b["text"]["text"]
    raise AssertionError(f"no mrkdwn section found under header {section_title!r}")


def test_each_section_renders_its_empty_state_on_empty_graph() -> None:
    # Req 6.9 / 9.4: every section with zero qualifying obligations renders its own
    # empty-state message under its own header.
    graph = _store()
    view = build_app_home_view(graph, NOW_ISO)

    assert _section_empty_text(view, BLOCKED_SECTION_TITLE) == BLOCKED_EMPTY_TEXT
    assert _section_empty_text(view, WAITING_SECTION_TITLE) == WAITING_EMPTY_TEXT
    assert _section_empty_text(view, HEALED_SECTION_TITLE) == HEALED_EMPTY_TEXT


def test_rows_carry_action_buttons_with_obligation_id() -> None:
    graph = _store()
    graph.upsert(_obligation("B1"))
    view = build_app_home_view(graph, NOW_ISO)
    action_blocks = [
        b for b in view["blocks"]
        if b["type"] == "actions" and str(b.get("block_id", "")).startswith("row_actions::")
    ]
    assert len(action_blocks) == 1
    elements = action_blocks[0]["elements"]

    # One visible primary Nudge button carrying the obligation id.
    nudge = next(e for e in elements if e.get("action_id") == "app_home_nudge")
    assert nudge["type"] == "button"
    assert nudge["style"] == "primary"
    assert nudge["value"] == "B1"

    # Secondary actions collapse into a single ⋮ overflow whose option values
    # encode "{verb}::{obligation_id}".
    overflow = next(e for e in elements if e.get("action_id") == "app_home_row_overflow")
    assert overflow["type"] == "overflow"
    option_values = [o["value"] for o in overflow["options"]]
    assert option_values == ["snooze::B1", "delegate::B1", "dismiss::B1"]


# ---------------------------------------------------------------------------
# Auto-Healed feed rows: resolved person + reason + timestamp, placeholders,
# and the 100-entry cap (task 11.1, Req 9.2, 9.5, 9.6)
# ---------------------------------------------------------------------------
def _healed(oid: str, **overrides) -> Obligation:
    """A baseline autonomously-healed obligation; override per test."""
    fields = dict(
        loop_state=LoopState.HEALED,
        closure_kind=ClosureKind.AUTONOMOUS,
        closure_reason="PR merged",
        closure_timestamp=_ago(2),
        last_touch_timestamp=_ago(2),
    )
    fields.update(overrides)
    return _obligation(oid, **fields)


def _healed_section_texts(view: dict) -> list[str]:
    """The Auto-Healed feed rows in a built view, each as headline + timeline subline.

    Each feed row renders a mrkdwn ``section`` headline (``Resolved with {person} — {reason}``)
    immediately followed by a ``context`` timeline subline (the native closure time
    plus the "Loop closed this automatically" attribution). This helper walks the
    Auto-Healed span — bounded so the trailing footer divider + footer context are
    excluded — and merges each headline with its subline so callers can assert on the
    whole row. An empty feed yields the single empty-state headline.
    """
    blocks = view["blocks"]
    start = next(
        i
        for i, b in enumerate(blocks)
        if b["type"] == "header" and b["text"]["text"] == HEALED_SECTION_TITLE
    )
    # Exclude the trailing footer divider + footer context block from the span.
    segment = blocks[start + 1 : len(blocks) - 2]
    texts: list[str] = []
    for b in segment:
        if b["type"] == "section" and b.get("text", {}).get("type") == "mrkdwn":
            texts.append(b["text"]["text"])
        elif b["type"] == "context" and texts:
            texts[-1] = texts[-1] + "\n" + b["elements"][0]["text"]
    return texts


def test_healed_row_shows_resolved_person_reason_and_timestamp() -> None:
    # Req 9.2: an entry carries the resolved person, the closure timestamp, and reason.
    graph = _store()
    closed_at = _ago(2)
    graph.upsert(
        _healed("H1", owed_person_id="U_BOB", closure_reason="PR merged", closure_timestamp=closed_at)
    )
    view = build_app_home_view(graph, NOW_ISO, user_id="U_USER")
    texts = _healed_section_texts(view)
    assert len(texts) == 1
    row = texts[0]
    assert "<@U_BOB>" in row          # resolved person (the non-user endpoint)
    assert "PR merged" in row          # closure reason
    assert slack_date(closed_at) in row  # closure timestamp, rendered natively


def test_healed_row_missing_reason_uses_placeholder_but_keeps_timestamp() -> None:
    # Req 9.5: missing reason → placeholder, timestamp still rendered.
    graph = _store()
    closed_at = _ago(3)
    graph.upsert(_healed("H1", closure_reason=None, closure_timestamp=closed_at))
    row = _healed_section_texts(build_app_home_view(graph, NOW_ISO, user_id="U_USER"))[0]
    assert HEALED_REASON_PLACEHOLDER in row
    assert slack_date(closed_at) in row


def test_healed_row_missing_person_uses_placeholder_but_keeps_timestamp() -> None:
    # Req 9.5: missing resolved person → placeholder, timestamp still rendered.
    graph = _store()
    closed_at = _ago(4)
    # Neither endpoint yields a usable identifier → no resolvable other party.
    graph.upsert(
        _healed("H1", owes_person_id="", owed_person_id="", closure_timestamp=closed_at)
    )
    row = _healed_section_texts(build_app_home_view(graph, NOW_ISO, user_id="U_USER"))[0]
    assert HEALED_PERSON_PLACEHOLDER in row
    assert slack_date(closed_at) in row


def test_resolved_person_picks_non_user_endpoint_for_either_direction() -> None:
    # Healed blocked-on-you: user owes, other is owed → resolved is the owed person.
    blocked = _healed("HB", owes_person_id="U_USER", owed_person_id="U_BOB")
    assert resolved_person_id(blocked, "U_USER") == "U_BOB"
    # Healed waiting-on-other: other owes, user is owed → resolved is the owing person.
    waiting = _healed("HW", owes_person_id="U_CAROL", owed_person_id="U_USER")
    assert resolved_person_id(waiting, "U_USER") == "U_CAROL"


def test_auto_healed_feed_caps_at_100_keeping_most_recent() -> None:
    # Req 9.6: more than 100 → keep the 100 most-recently-healed.
    graph = _store()
    # 120 healed obligations, closure timestamps 1h..120h ago (lower index = newer).
    for i in range(120):
        graph.upsert(
            _healed(
                f"H{i:03d}",
                owed_person_id=f"U_{i:03d}",
                closure_timestamp=_ago(i + 1),
                last_touch_timestamp=_ago(i + 1),
            )
        )
    rows = auto_healed_rows(graph, NOW_ISO, user_id="U_USER")
    assert len(rows) == AUTO_HEALED_FEED_CAP == 100
    # The 100 most-recent are H000..H099 (oldest 20 dropped), newest first.
    assert rows[0].obligation_id == "H000"
    assert rows[-1].obligation_id == "H099"
    assert all(o.obligation_id < "H100" for o in rows)


def test_auto_healed_feed_tie_break_by_resolved_person_ascending() -> None:
    # Req 9.3: equal closure timestamp → order by resolved person id ascending.
    graph = _store()
    same_ts = _ago(5)
    graph.upsert(_healed("HC", owed_person_id="U_CAROL", closure_timestamp=same_ts, last_touch_timestamp=same_ts))
    graph.upsert(_healed("HA", owed_person_id="U_ANNA", closure_timestamp=same_ts, last_touch_timestamp=same_ts))
    graph.upsert(_healed("HB", owed_person_id="U_BOB", closure_timestamp=same_ts, last_touch_timestamp=same_ts))
    rows = auto_healed_rows(graph, NOW_ISO, user_id="U_USER")
    assert [resolved_person_id(o, "U_USER") for o in rows] == ["U_ANNA", "U_BOB", "U_CAROL"]


def test_empty_auto_healed_feed_renders_empty_state() -> None:
    # Req 9.4: zero auto-closed obligations → empty-state message.
    graph = _store()
    graph.upsert(_obligation("B1"))  # an active loop, not a healed one
    texts = _healed_section_texts(build_app_home_view(graph, NOW_ISO, user_id="U_USER"))
    assert texts == [HEALED_EMPTY_TEXT]


# ---------------------------------------------------------------------------
# Best-UX hero card + scan stats (count > 0 only; zero-state stays minimal)
# ---------------------------------------------------------------------------
def _find_button(view: dict, action_id: str) -> dict | None:
    """The first element (button) anywhere in the view carrying ``action_id``."""
    for b in view["blocks"]:
        # Accessory buttons (e.g. the hero card) live under "accessory".
        accessory = b.get("accessory")
        if isinstance(accessory, dict) and accessory.get("action_id") == action_id:
            return accessory
        # Actions blocks carry a list of elements.
        for el in b.get("elements", []):
            if isinstance(el, dict) and el.get("action_id") == action_id:
                return el
    return None


def _context_texts(view: dict) -> list[str]:
    """All context-block mrkdwn texts in render order."""
    return [
        b["elements"][0]["text"]
        for b in view["blocks"]
        if b["type"] == "context" and b.get("elements")
    ]


def test_hero_card_and_review_button_present_when_blocked() -> None:
    graph = _store()
    graph.upsert(_obligation("B1"))
    graph.upsert(_obligation("B2"))
    view = build_app_home_view(graph, NOW_ISO)

    # The hero header is still block[0] (invariant preserved).
    assert view["blocks"][0]["type"] == "header"
    assert view["blocks"][0]["text"]["text"] == hero_text(2)

    # The primary Review button now lives in its own full-width actions block.
    button = _find_button(view, ACTION_REVIEW_BLOCKED)
    assert button is not None
    assert button["style"] == "primary"
    assert button["text"]["text"] == HERO_REVIEW_BUTTON_TEXT
    action_block = next(
        b for b in view["blocks"]
        if b["type"] == "actions" and b.get("block_id") == "hero_actions"
    )
    assert action_block["elements"][0]["action_id"] == ACTION_REVIEW_BLOCKED

    # The hero no longer renders the redundant bold restatement section (the header
    # already states the reveal) — the top stays uncrowded.
    assert not any(
        b["type"] == "section"
        and b.get("accessory", {}).get("action_id") == ACTION_REVIEW_BLOCKED
        for b in view["blocks"]
    )


def test_hero_card_and_review_button_absent_in_zero_state() -> None:
    graph = _store()
    graph.upsert(_obligation("W1", loop_state=LoopState.WAITING_ON_OTHER))
    view = build_app_home_view(graph, NOW_ISO)

    # Zero-state stays minimal: hero header, then straight into sections.
    assert view["blocks"][0]["text"]["text"] == HERO_ZERO_TEXT
    assert _find_button(view, ACTION_REVIEW_BLOCKED) is None
    assert HERO_SUBLINE_TEXT not in _context_texts(view)


def test_hero_stats_line_reports_total_loops_and_native_updated_time() -> None:
    graph = _store()
    graph.upsert(_obligation("B1"))
    graph.upsert(_obligation("W1", loop_state=LoopState.WAITING_ON_OTHER))
    graph.upsert(
        _obligation(
            "H1",
            loop_state=LoopState.HEALED,
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_reason="PR merged",
            closure_timestamp=_ago(3),
            last_touch_timestamp=_ago(3),
        )
    )
    view = build_app_home_view(graph, NOW_ISO)

    total = 1 + 1 + 1  # blocked + waiting + healed
    expected = hero_stats_text(total, slack_date(NOW_ISO))
    assert expected in _context_texts(view)
    # The "updated" time is rendered as a native Slack date token, not a raw ISO.
    assert slack_date(NOW_ISO) in expected
    assert "loops tracked" in expected


# ---------------------------------------------------------------------------
# Active rows carry the source channel as a native channel mention
# ---------------------------------------------------------------------------
def _section_segment(view: dict, section_title: str) -> list[dict]:
    """The blocks rendered under ``section_title`` up to the next header."""
    blocks = view["blocks"]
    start = next(
        i
        for i, b in enumerate(blocks)
        if b["type"] == "header" and b["text"]["text"] == section_title
    )
    seg: list[dict] = []
    for b in blocks[start + 1 :]:
        if b["type"] == "header":
            break
        seg.append(b)
    return seg


def _row_section_texts(view: dict, section_title: str) -> list[str]:
    """Each row's full text: the subject ``section`` plus its meta ``context`` line.

    A row now renders as a subject section followed by a compact context line
    (small avatar + counterparty · age · channel), so the "row text" a test cares
    about is the two joined.
    """
    seg = _section_segment(view, section_title)
    out: list[str] = []
    for i, b in enumerate(seg):
        if b["type"] == "section" and b.get("text", {}).get("type") == "mrkdwn":
            row = b["text"]["text"]
            if i + 1 < len(seg) and seg[i + 1]["type"] == "context":
                ctx = " ".join(
                    e.get("text", "")
                    for e in seg[i + 1]["elements"]
                    if e.get("type") == "mrkdwn"
                )
                row = f"{row}\n{ctx}"
            out.append(row)
    return out


def _row_avatar_url(view: dict, section_title: str, index: int = 0) -> str | None:
    """The small avatar's image URL from the context line under the ``index``-th row."""
    seg = _section_segment(view, section_title)
    sections = [
        i
        for i, b in enumerate(seg)
        if b["type"] == "section" and b.get("text", {}).get("type") == "mrkdwn"
    ]
    si = sections[index]
    if si + 1 < len(seg) and seg[si + 1]["type"] == "context":
        for e in seg[si + 1]["elements"]:
            if e.get("type") == "image":
                return e.get("image_url")
    return None


def test_blocked_row_shows_counterparty_and_source_channel_mention() -> None:
    graph = _store()
    graph.upsert(_obligation("B1", owed_person_id="U_BOB", source_msg_channel="C_DECK"))
    view = build_app_home_view(graph, NOW_ISO)
    row = _row_section_texts(view, BLOCKED_SECTION_TITLE)[0]
    assert "*Subject B1*" in row
    assert "<@U_BOB>" in row
    assert "<#C_DECK>" in row


def test_waiting_row_shows_counterparty_and_source_channel_mention() -> None:
    graph = _store()
    graph.upsert(
        _obligation(
            "W1",
            loop_state=LoopState.WAITING_ON_OTHER,
            owes_person_id="U_CAROL",
            source_msg_channel="C_BUDGET",
        )
    )
    view = build_app_home_view(graph, NOW_ISO)
    row = _row_section_texts(view, WAITING_SECTION_TITLE)[0]
    assert "<@U_CAROL>" in row
    assert "<#C_BUDGET>" in row


def test_overdue_row_bolds_the_age_chip() -> None:
    graph = _store()
    # 100h old → overdue (> 72h); the age renders bold with "overdue" on the row.
    graph.upsert(_obligation("B1", last_touch_timestamp=_ago(100)))
    view = build_app_home_view(graph, NOW_ISO)
    row = _row_section_texts(view, BLOCKED_SECTION_TITLE)[0]
    assert "*4d overdue*" in row


# ---------------------------------------------------------------------------
# slack_date native token formatting
# ---------------------------------------------------------------------------
def test_slack_date_renders_native_token_with_epoch_and_fallback() -> None:
    iso = "2025-05-30T10:00:00+00:00"
    token = slack_date(iso)
    epoch = int(datetime(2025, 5, 30, 10, 0, 0, tzinfo=timezone.utc).timestamp())
    assert token == f"<!date^{epoch}^{{date_short_pretty}} at {{time}}|2025-05-30>"
    assert token.startswith("<!date^")
    assert token.endswith("|2025-05-30>")


def test_slack_date_accepts_trailing_z() -> None:
    assert slack_date("2025-05-30T10:00:00Z") == slack_date("2025-05-30T10:00:00+00:00")


def test_slack_date_falls_back_to_plain_string_on_parse_failure() -> None:
    assert slack_date("not-a-timestamp") == "not-a-timestamp"


# ---------------------------------------------------------------------------
# Avatar image accessories (the premium lever) + optional logo block
# ---------------------------------------------------------------------------
def _row_sections(view: dict, section_title: str) -> list[dict]:
    """The mrkdwn row section blocks rendered under ``section_title`` before next header."""
    blocks = view["blocks"]
    start = next(
        i
        for i, b in enumerate(blocks)
        if b["type"] == "header" and b["text"]["text"] == section_title
    )
    out: list[dict] = []
    for b in blocks[start + 1 :]:
        if b["type"] == "header":
            break
        if b["type"] == "section" and b.get("text", {}).get("type") == "mrkdwn":
            out.append(b)
    return out


def test_row_has_small_avatar_in_the_context_line_when_supplied() -> None:
    graph = _store()
    graph.upsert(_obligation("B1", owed_person_id="U_BOB"))
    avatars = {"U_BOB": "https://avatars.example.com/bob_72.png"}
    view = build_app_home_view(graph, NOW_ISO, avatars=avatars)
    # Avatar now rides the row's context line (small ~20px), not a big accessory.
    assert _row_avatar_url(view, BLOCKED_SECTION_TITLE) == "https://avatars.example.com/bob_72.png"
    # ...and never as a large section accessory.
    assert "accessory" not in _row_sections(view, BLOCKED_SECTION_TITLE)[0]


def test_row_has_no_avatar_when_none_supplied() -> None:
    graph = _store()
    graph.upsert(_obligation("B1", owed_person_id="U_BOB"))
    # No avatars map at all → graceful, no avatar image anywhere.
    view = build_app_home_view(graph, NOW_ISO)
    assert _row_avatar_url(view, BLOCKED_SECTION_TITLE) is None
    assert "accessory" not in _row_sections(view, BLOCKED_SECTION_TITLE)[0]
    # An avatars map missing this person → still no avatar.
    view2 = build_app_home_view(graph, NOW_ISO, avatars={"U_SOMEONE_ELSE": "https://x/y.png"})
    assert _row_avatar_url(view2, BLOCKED_SECTION_TITLE) is None


def test_healed_row_has_small_avatar_in_the_context_line_when_supplied() -> None:
    graph = _store()
    graph.upsert(_healed("H1", owed_person_id="U_BOB"))
    avatars = {"U_BOB": "https://avatars.example.com/bob_72.png"}
    view = build_app_home_view(graph, NOW_ISO, user_id="U_USER", avatars=avatars)
    assert _row_avatar_url(view, HEALED_SECTION_TITLE) == "https://avatars.example.com/bob_72.png"


def test_logo_block_prepended_when_logo_url_set_and_hero_follows() -> None:
    graph = _store()
    graph.upsert(_obligation("B1"))
    view = build_app_home_view(graph, NOW_ISO, logo_url="https://cdn.example.com/loop.png")
    # block[0] is the logo image; the hero header follows immediately.
    assert view["blocks"][0]["type"] == "image"
    assert view["blocks"][0]["image_url"] == "https://cdn.example.com/loop.png"
    assert view["blocks"][1]["type"] == "header"
    assert view["blocks"][1]["text"]["text"] == hero_text(1)


def test_no_logo_block_when_logo_url_absent_keeps_hero_as_block_zero() -> None:
    graph = _store()
    graph.upsert(_obligation("B1"))
    view = build_app_home_view(graph, NOW_ISO)  # default: no logo
    assert view["blocks"][0]["type"] == "header"
    assert all(b["type"] != "image" for b in view["blocks"])


# ---------------------------------------------------------------------------
# Design adoption: tagline, section descriptors, View-message links, GitHub tag
# ---------------------------------------------------------------------------
def test_home_shows_section_descriptors_but_not_the_marketing_tagline() -> None:
    graph = _store()
    graph.upsert(_obligation("B1"))  # any tracked loop ends the first-run state
    text = str(build_app_home_view(graph, NOW_ISO, user_id="U_USER"))
    # The redesign dropped the marketing tagline from the daily view for focus.
    assert HERO_TAGLINE not in text
    assert BLOCKED_SECTION_DESCRIPTOR in text
    assert WAITING_SECTION_DESCRIPTOR in text
    assert HEALED_SECTION_DESCRIPTOR in text


def test_obligation_zero_payoff_shows_when_nothing_open_but_history_exists() -> None:
    graph = _store()
    graph.upsert(
        _obligation(
            "H",
            owes_person_id="U_OTHER",
            owed_person_id="U_USER",
            loop_state=LoopState.HEALED,
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_timestamp=_ago(2),
            closure_reason="PR merged",
        )
    )
    text = str(build_app_home_view(graph, NOW_ISO, user_id="U_USER", show_impact=True))
    assert OBLIGATION_ZERO_BODY in text  # the calm payoff line
    assert HEALED_SECTION_TITLE in text  # ...proof still browsable below


def test_greeting_eyebrow_renders_above_the_hero_when_the_viewer_name_is_known() -> None:
    graph = _store()
    graph.upsert(_obligation("B1"))
    view = build_app_home_view(graph, NOW_ISO, user_id="U_USER", viewer_name="Ruturaj")
    first = view["blocks"][0]
    assert first["type"] == "context"
    assert "Ruturaj" in first["elements"][0]["text"]
    # Absent (header stays block[0]) when no name resolved — the test invariant.
    plain = build_app_home_view(graph, NOW_ISO, user_id="U_USER")
    assert plain["blocks"][0]["type"] == "header"


def test_active_rows_carry_a_view_message_permalink() -> None:
    graph = _store()
    graph.upsert(_obligation("B1", source_msg_channel="C9", source_msg_ts="1783.55"))
    text = str(build_app_home_view(graph, NOW_ISO, user_id="U_USER"))
    # Slack archive permalink: /archives/{channel}/p{ts without the dot}
    assert "https://slack.com/archives/C9/p178355" in text
    assert "View message" in text


def _healed_pr(oid: str, *, is_pr: bool) -> Obligation:
    return _obligation(
        oid,
        owes_person_id="U_OTHER",
        owed_person_id="U_USER",
        loop_state=LoopState.HEALED,
        closure_kind=ClosureKind.AUTONOMOUS,
        closure_timestamp=_ago(2),
        closure_reason="PR merged" if is_pr else "replied",
        artifact_type=ArtifactType.GITHUB_PR if is_pr else None,
    )


def test_healed_row_tags_github_verified_only_for_pr_closures() -> None:
    graph = _store()
    graph.upsert(_healed_pr("H_PR", is_pr=True))
    graph.upsert(_healed_pr("H_CHAT", is_pr=False))
    text = str(build_app_home_view(graph, NOW_ISO, user_id="U_USER"))
    assert "verified via GitHub" in text  # the PR-grounded closure carries the tag
    # ...but it is not slapped on every auto-closed row.
    assert text.count("verified via GitHub") == 1
