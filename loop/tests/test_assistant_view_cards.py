"""Card + chart rendering tests for the Assistant pane (Best-UX `card`/`data_visualization`).

These lock the opt-in ``style="cards"`` and ``chart=True`` paths of
:func:`build_assistant_blocks`:

  * a QUERY_RESULT with ``style="cards"`` renders obligation ``card`` blocks (not
    section rows), each with at most three action buttons;
  * ``chart=True`` appends a single ``data_visualization`` bar chart whose data points
    match the x-axis categories and sum to the number of shown obligations;
  * the default render (no style/chart) emits no ``card`` or ``data_visualization``
    blocks, so the proven layout is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loop.conversational.assistant_view import (
    CHART_TITLE,
    build_assistant_blocks,
)
from loop.conversational.conversational_agent import (
    AssistantReply,
    ReplyKind,
    ToolUseTrace,
)
from loop.graph.models import LoopState, Obligation

USER = "U_USER"
OTHER = "U_OTHER"
NOW = datetime(2025, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
OLD_TS = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def _obligation(oid: str, summary: str) -> Obligation:
    return Obligation(
        obligation_id=oid,
        owes_person_id=USER,
        owed_person_id=OTHER,
        owner_person_id=USER,
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.95,
        last_touch_timestamp=OLD_TS,
        source_msg_channel="C_PANE",
        source_msg_ts="1700000000.000100",
        subject_summary=summary,
    )


def _query_reply(*obligations: Obligation) -> AssistantReply:
    trace = ToolUseTrace()
    trace.add("Obligation Graph", "query")
    return AssistantReply(
        kind=ReplyKind.QUERY_RESULT,
        text="Here are the loops blocked on you.",
        trace=trace,
        obligations=tuple(obligations),
    )


def _of_type(blocks: list[dict], t: str) -> list[dict]:
    return [b for b in blocks if b.get("type") == t]


def test_cards_style_renders_obligation_cards() -> None:
    reply = _query_reply(_obligation("o1", "review the deck"))
    blocks = build_assistant_blocks(reply, now=NOW, user_id=USER, style="cards")
    cards = _of_type(blocks, "card")
    assert len(cards) == 1
    card = cards[0]
    assert card["title"]["text"] == "review the deck"
    assert 1 <= len(card["actions"]) <= 3


def test_default_style_has_no_cards_or_chart() -> None:
    reply = _query_reply(_obligation("o1", "review the deck"))
    blocks = build_assistant_blocks(reply, now=NOW, user_id=USER)
    assert not _of_type(blocks, "card")
    assert not _of_type(blocks, "data_visualization")


def test_chart_appended_and_well_formed() -> None:
    obligations = [
        _obligation("o1", "review the deck"),
        _obligation("o2", "merge the PR"),
    ]
    reply = _query_reply(*obligations)
    blocks = build_assistant_blocks(
        reply, now=NOW, user_id=USER, style="cards", chart=True
    )
    charts = _of_type(blocks, "data_visualization")
    assert len(charts) == 1
    chart = charts[0]
    assert chart["title"] == CHART_TITLE
    spec = chart["chart"]
    assert spec["type"] == "bar"
    categories = spec["axis_config"]["categories"]
    series = spec["series"][0]["data"]
    # Every data point label matches a declared category (Slack validation rule).
    assert [p["label"] for p in series] == categories
    # Counts sum to the number of obligations shown.
    assert sum(p["value"] for p in series) == len(obligations)


def test_chart_omitted_when_no_obligations() -> None:
    trace = ToolUseTrace()
    trace.add("Obligation Graph", "query")
    reply = AssistantReply(
        kind=ReplyKind.QUERY_RESULT,
        text="No loops found.",
        trace=trace,
        obligations=(),
    )
    blocks = build_assistant_blocks(
        reply, now=NOW, user_id=USER, style="cards", chart=True
    )
    assert not _of_type(blocks, "data_visualization")
