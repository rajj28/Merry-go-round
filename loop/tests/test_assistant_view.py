"""Pure renderer tests for the Assistant-pane Block Kit builder (Req 12).

These exercise :func:`loop.conversational.assistant_view.build_assistant_blocks`
directly — it is a pure, network-free function of an
:class:`~loop.conversational.conversational_agent.AssistantReply` — asserting the
four visual guarantees of the Best-UX surface:

  * the stepped Tool_Use_Trace plan line is rendered from the reply's actual trace
    entries, in invocation order (Req 12.5);
  * a QUERY_RESULT renders the trace context plus one App-Home-styled section per
    obligation (Aging_Chip + bold subject + counterparty mention + source channel);
  * a CONFIRM_REQUIRED renders the quoted draft card with Send / Cancel buttons
    carrying the right action_ids and the user id as their value (Req 12.6);
  * an UNSUPPORTED reply lists the supported actions.

Validates: Requirements 12.1, 12.5, 12.6, 12.9.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loop.conversational.assistant_view import (
    ACTION_ASSISTANT_CONFIRM,
    ACTION_ASSISTANT_DECLINE,
    QUERY_RESULT_HEADER,
    TRACE_PREFIX,
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
# ~2 days before NOW, so the Aging_Chip lands in the warning band.
OLD_TS = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def _obligation(
    oid: str = "o1",
    *,
    state: LoopState = LoopState.BLOCKED_ON_YOU,
    summary: str = "ship the release notes",
    owed: str = OTHER,
    owes: str = USER,
    channel: str = "C_PANE",
) -> Obligation:
    return Obligation(
        obligation_id=oid,
        owes_person_id=owes,
        owed_person_id=owed,
        owner_person_id=USER,
        loop_state=state,
        confidence_score=0.95,
        last_touch_timestamp=OLD_TS,
        source_msg_channel=channel,
        source_msg_ts="1700000000.000100",
        subject_summary=summary,
    )


def _trace(*steps: tuple[str, str | None]) -> ToolUseTrace:
    trace = ToolUseTrace()
    for tool, detail in steps:
        trace.add(tool, detail)
    return trace


def _block_types(blocks: list[dict]) -> list[str]:
    return [b["type"] for b in blocks]


def _all_text(blocks: list[dict]) -> str:
    """Flatten every mrkdwn/plain_text string across the blocks for substring asserts."""
    out: list[str] = []
    for b in blocks:
        text = b.get("text")
        if isinstance(text, dict):
            out.append(text.get("text", ""))
        for el in b.get("elements", []) or []:
            if isinstance(el, dict):
                t = el.get("text")
                if isinstance(t, str):
                    out.append(t)
                elif isinstance(t, dict):
                    out.append(t.get("text", ""))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# QUERY_RESULT — trace context + one section per obligation
# --------------------------------------------------------------------------- #
def test_query_result_renders_trace_and_obligation_rows():
    obligations = (
        _obligation("o1", summary="review the deck", owed=OTHER),
        _obligation("o2", summary="sign the budget", owed="U_CAROL"),
    )
    reply = AssistantReply(
        kind=ReplyKind.QUERY_RESULT,
        text="Found 2 loops.",
        trace=_trace(("Obligation Graph", "query")),
        obligations=obligations,
    )

    blocks = build_assistant_blocks(reply, now=NOW, user_id=USER)

    # The trace reasoning line leads the reply (Req 12.5).
    assert blocks[0]["type"] == "context"
    assert TRACE_PREFIX in blocks[0]["elements"][0]["text"]
    assert "Searched workspace" in blocks[0]["elements"][0]["text"]

    text = _all_text(blocks)
    assert QUERY_RESULT_HEADER in text
    # One App-Home-styled section per obligation: bold subject + counterparty mention.
    assert "*review the deck*" in text
    assert "*sign the budget*" in text
    assert "<@U_OTHER>" in text
    assert "<@U_CAROL>" in text
    # Source channel rendered as a native channel mention.
    assert "<#C_PANE>" in text
    # The plain-text age label is present on the rows (no emoji chip).
    assert "2d" in text


def test_query_result_caps_rows_and_shows_overflow():
    obligations = tuple(
        _obligation(f"o{i}", summary=f"loop {i}") for i in range(13)
    )
    reply = AssistantReply(
        kind=ReplyKind.QUERY_RESULT,
        text="Found 13 loops.",
        trace=_trace(("Obligation Graph", "query")),
        obligations=obligations,
    )
    blocks = build_assistant_blocks(reply, now=NOW, user_id=USER)
    text = _all_text(blocks)
    # 13 - 10 cap = 3 overflow.
    assert "+3 more" in text
    # Only the first 10 obligation sections are rendered.
    section_count = sum(1 for b in blocks if b["type"] == "section")
    # 1 header section + 10 obligation sections.
    assert section_count == 11


# --------------------------------------------------------------------------- #
# CONFIRM_REQUIRED — quoted draft card with Send / Cancel buttons (Req 12.6)
# --------------------------------------------------------------------------- #
def test_confirm_required_renders_draft_card_with_buttons():
    reply = AssistantReply(
        kind=ReplyKind.CONFIRM_REQUIRED,
        text="Hi! Just circling back on the release notes — any update?",
        trace=_trace(("Action Agent", "draft_polite_nudge")),
        obligation=_obligation("o1", channel="C_PANE"),
        requires_confirmation=True,
    )

    blocks = build_assistant_blocks(reply, now=NOW, user_id=USER)

    # Find the actions block.
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    elements = actions[0]["elements"]
    action_ids = [e["action_id"] for e in elements]
    assert action_ids == [ACTION_ASSISTANT_CONFIRM, ACTION_ASSISTANT_DECLINE]
    # Each button carries the user id as its value (so the 60s gate resolves the user).
    assert all(e["value"] == USER for e in elements)
    # The Send button is primary.
    send = next(e for e in elements if e["action_id"] == ACTION_ASSISTANT_CONFIRM)
    assert send.get("style") == "primary"

    text = _all_text(blocks)
    # The draft is quoted (blockquote) and the "send as you" context names the channel.
    assert ">Hi! Just circling back" in text
    assert "<#C_PANE>" in text


# --------------------------------------------------------------------------- #
# UNSUPPORTED — lists the supported actions
# --------------------------------------------------------------------------- #
def test_unsupported_lists_supported_actions():
    reply = AssistantReply(
        kind=ReplyKind.UNSUPPORTED,
        text="I'm not sure how to do that.",
        trace=ToolUseTrace(),
        supported_actions=("nudge", "delegate", "snooze", "close"),
    )
    blocks = build_assistant_blocks(reply)
    text = _all_text(blocks)
    assert "I'm not sure how to do that." in text
    for action in ("nudge", "delegate", "snooze", "close"):
        assert action in text


# --------------------------------------------------------------------------- #
# Trace ordering — the plan line reflects exact invocation order (Req 12.5)
# --------------------------------------------------------------------------- #
def test_trace_context_appears_in_invocation_order():
    reply = AssistantReply(
        kind=ReplyKind.ACTION_DONE,
        text="Done.",
        trace=_trace(
            ("Planner", "plan 3 steps"),
            ("Obligation Graph", "query"),
            ("Verifier (GitHub MCP)", "verify_pr"),
            ("Action Agent", "send_polite_nudge"),
        ),
    )
    blocks = build_assistant_blocks(reply, now=NOW, user_id=USER)
    plan_line = blocks[0]["elements"][0]["text"]
    # The tools appear in exactly the order they were invoked, mapped to human labels.
    assert (
        plan_line.index("Planned")
        < plan_line.index("Searched workspace")
        < plan_line.index("Verified GitHub")
        < plan_line.index("Sent reply")
    )


def test_empty_trace_renders_no_plan_line():
    reply = AssistantReply(
        kind=ReplyKind.NO_MATCH,
        text="No loops match that.",
        trace=ToolUseTrace(),
    )
    blocks = build_assistant_blocks(reply)
    # No leading context block when nothing ran; just the clean section.
    assert _block_types(blocks) == ["section"]
    assert "No loops match that." in _all_text(blocks)


# --------------------------------------------------------------------------- #
# Plain-text trace label mapping (no emoji) — Req 12.5
# --------------------------------------------------------------------------- #
def test_trace_maps_internal_tool_names_to_human_labels():
    reply = AssistantReply(
        kind=ReplyKind.ACTION_DONE,
        text="Done.",
        trace=_trace(
            ("Planner", "plan 2 steps"),
            ("Obligation Graph", "query"),
            ("Verifier (GitHub MCP)", "verify_pr"),
            ("Action Agent", "draft_polite_nudge"),
        ),
    )
    line = build_assistant_blocks(reply)[0]["elements"][0]["text"]
    assert line == "Loop's reasoning: Planned → Searched workspace → Verified GitHub → Drafted reply"
    # No decorative emoji leaked into the trace.
    for glyph in ("🧠", "🔍", "🔗", "⚡"):
        assert glyph not in line


def test_trace_action_agent_label_is_action_appropriate():
    def line_for(detail: str) -> str:
        reply = AssistantReply(
            kind=ReplyKind.ACTION_DONE,
            text="Done.",
            trace=_trace(("Action Agent", detail)),
        )
        return build_assistant_blocks(reply)[0]["elements"][0]["text"]

    assert "Sent reply" in line_for("send_polite_nudge")
    assert "Delegated" in line_for("delegate")
    assert "Snoozed" in line_for("snooze")
    assert "Closed loop" in line_for("auto_close")


# --------------------------------------------------------------------------- #
# Avatar image accessories on obligation rows (the premium lever)
# --------------------------------------------------------------------------- #
def test_query_result_row_has_avatar_accessory_when_supplied():
    reply = AssistantReply(
        kind=ReplyKind.QUERY_RESULT,
        text="Found 1 loop.",
        trace=_trace(("Obligation Graph", "query")),
        obligations=(_obligation("o1", owed=OTHER),),
    )
    avatars = {OTHER: "https://avatars.example.com/other_72.png"}
    blocks = build_assistant_blocks(reply, now=NOW, user_id=USER, avatars=avatars)
    row = next(b for b in blocks if b["type"] == "section" and "*ship" in b["text"]["text"])
    assert row.get("accessory", {}).get("type") == "image"
    assert row["accessory"]["image_url"] == "https://avatars.example.com/other_72.png"
    assert row["accessory"]["alt_text"] == OTHER


def test_query_result_row_has_no_accessory_when_avatar_absent():
    reply = AssistantReply(
        kind=ReplyKind.QUERY_RESULT,
        text="Found 1 loop.",
        trace=_trace(("Obligation Graph", "query")),
        obligations=(_obligation("o1", owed=OTHER),),
    )
    blocks = build_assistant_blocks(reply, now=NOW, user_id=USER)  # no avatars
    row = next(b for b in blocks if b["type"] == "section" and "*ship" in b["text"]["text"])
    assert "accessory" not in row
