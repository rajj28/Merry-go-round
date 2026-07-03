"""Regenerate the committed Block Kit preview JSONs for the two Slack surfaces.

This is offline, network-free tooling: it renders the **pure** App Home and
Assistant-pane builders against the deterministic seeded workspace (plus a small
*fake* avatar map so the premium avatar accessories show in the preview) and writes
the three preview files used for visual review:

  * loop/spikes/app_home_preview.json       — the App Home dashboard
  * loop/spikes/app_home_cards_preview.json  — the App Home dashboard, card style
  * loop/spikes/assistant_query_preview.json — an Assistant QUERY_RESULT reply
  * loop/spikes/assistant_query_cards_preview.json — QUERY_RESULT, card + chart style
  * loop/spikes/assistant_confirm_preview.json — an Assistant CONFIRM_REQUIRED card

Run from the repo root:
    python -m loop.spikes.generate_previews
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from loop.action.app_home import build_app_home_view
from loop.action.app_home import build_app_home_compact_view
from loop.action.app_home import slack_date
from loop.conversational.assistant_view import build_assistant_blocks
from loop.conversational.conversational_agent import (
    AssistantReply,
    ReplyKind,
    ToolUseTrace,
)
from loop.seed.fixtures import SEED_USER_ID, seed_avatars, seed_names
from loop.seed.loader import SEED_NOW, SeededWorkspace

_HERE = Path(__file__).resolve().parent

# The real seeded org avatar / name maps (single source of truth in fixtures.py),
# so the previews render the full Northwind org exactly as the live dashboard does.
FAKE_AVATARS = seed_avatars()
FAKE_NAMES = seed_names()


def _trace(*steps: tuple[str, str | None]) -> ToolUseTrace:
    trace = ToolUseTrace()
    for tool, detail in steps:
        trace.add(tool, detail)
    return trace


def _write(name: str, payload) -> None:
    path = _HERE / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {os.path.relpath(path)}")


def _write_blocks(name: str, blocks) -> None:
    """Write an assistant block list wrapped as a Block-Kit-Builder-ready object.

    ``build_assistant_blocks`` returns a bare list (what ``chat_postMessage(blocks=…)``
    wants), but Block Kit Builder requires a top-level object with a ``blocks`` key, so
    the preview files are wrapped in ``{"blocks": [...]}`` to paste cleanly.
    """
    _write(name, {"blocks": blocks})


def main() -> None:
    workspace = SeededWorkspace()
    graph = workspace.graph

    # --- App Home (seeded workspace + fake avatars) -------------------------
    view = build_app_home_view(
        graph,
        now=SEED_NOW,
        user_id=SEED_USER_ID,
        avatars=FAKE_AVATARS,
    )
    _write("app_home_preview.json", view)

    # --- App Home, CARD style (newer Block Kit `card` rows) -----------------
    view_cards = build_app_home_view(
        graph,
        now=SEED_NOW,
        user_id=SEED_USER_ID,
        avatars=FAKE_AVATARS,
        names=FAKE_NAMES,
        style="cards",
    )
    _write("app_home_cards_preview.json", view_cards)

    # --- App Home, COMPACT command-center layout (Best-UX scroll reduction) --
    view_compact = build_app_home_compact_view(
        graph,
        now=SEED_NOW,
        user_id=SEED_USER_ID,
        avatars=FAKE_AVATARS,
        names=FAKE_NAMES,
    )
    _write("app_home_compact_preview.json", view_compact)

    # --- App Home, COMPACT zero-state (the celebratory "all caught up" frame) -
    from loop.graph.models import LoopState
    from loop.graph.store import ObligationFilter

    zero_graph = SeededWorkspace().graph
    # Dismiss the three blocked-on-you loops to reach the inbox-zero payoff state.
    for ob in zero_graph.query(
        ObligationFilter(loop_states=frozenset({LoopState.BLOCKED_ON_YOU}), surfaced_only=True, now=SEED_NOW)
    ):
        zero_graph.upsert(ob.model_copy(update={"dismissed": True}))
    view_zero = build_app_home_compact_view(
        zero_graph, now=SEED_NOW, user_id=SEED_USER_ID, avatars=FAKE_AVATARS, names=FAKE_NAMES
    )
    _write("app_home_compact_zero_preview.json", view_zero)

    # --- Nudge composer modal (editable AI draft, "send as you") ------------
    from loop.action.app_home import build_nudge_modal

    b1 = graph.get("OBL_B1")
    nudge_modal = build_nudge_modal(
        b1,
        "Hi Alice — gentle nudge on the onboarding design doc review. I know you're "
        "blocked on my sign-off; I'll get you feedback by end of day. Thanks for your "
        "patience! 🙏",
        names=FAKE_NAMES,
    )
    _write("nudge_modal_preview.json", nudge_modal)

    # --- Confirmation DM cards (the professional Loop DM "history") ---------
    from loop.app import _confirmation_blocks

    sent_card = _confirmation_blocks(
        "✅ *Nudge sent*",
        quote=(
            "Hi Alice — gentle nudge on the onboarding design doc review. I'll get you "
            "feedback by end of day. Thanks for your patience! 🙏"
        ),
        context="Sent as you to <@U_ALICE> · in <#C_DESIGN> · "
        + slack_date(SEED_NOW),
    )
    _write_blocks("confirm_nudge_sent_preview.json", sent_card)

    snoozed_card = _confirmation_blocks(
        "🕓 *Snoozed*",
        context="Carol needs your sign-off on the Q3 launch checklist · <@U_CAROL> · "
        "hidden until " + slack_date(SEED_NOW),
        undo_action="loop_undo_snooze",
        oid="OBL_B3",
    )
    _write_blocks("confirm_snoozed_preview.json", snoozed_card)

    dismissed_card = _confirmation_blocks(
        "🗑️ *Dismissed*",
        context="Bob is blocked on you merging the widgets PR · <@U_BOB>",
        undo_action="loop_undo_dismiss",
        oid="OBL_B2",
    )
    _write_blocks("confirm_dismissed_preview.json", dismissed_card)

    # --- Assistant QUERY_RESULT (the three blocked-on-you loops) ------------
    blocked = [graph.get("OBL_B1"), graph.get("OBL_B2"), graph.get("OBL_B3")]
    query_reply = AssistantReply(
        kind=ReplyKind.QUERY_RESULT,
        text="Here are the loops blocked on you.",
        trace=_trace(("Obligation Graph", "query")),
        obligations=tuple(blocked),
    )
    query_blocks = build_assistant_blocks(
        query_reply, now=SEED_NOW, user_id=SEED_USER_ID, avatars=FAKE_AVATARS
    )
    _write_blocks("assistant_query_preview.json", query_blocks)

    # --- Assistant QUERY_RESULT, CARD style + aging bar chart ---------------
    query_cards = build_assistant_blocks(
        query_reply,
        now=SEED_NOW,
        user_id=SEED_USER_ID,
        avatars=FAKE_AVATARS,
        style="cards",
        chart=True,
    )
    _write_blocks("assistant_query_cards_preview.json", query_cards)

    # --- Assistant CONFIRM_REQUIRED (a send-as-you draft card) --------------
    confirm_reply = AssistantReply(
        kind=ReplyKind.CONFIRM_REQUIRED,
        text=(
            "Hi Bob — gentle nudge on the widgets PR, it has been a couple of days. "
            "Could you take a look when you get a sec? Thanks!"
        ),
        trace=_trace(("Obligation Graph", "query"), ("Action Agent", "draft_polite_nudge")),
        obligation=graph.get("OBL_B2"),
        requires_confirmation=True,
    )
    confirm_blocks = build_assistant_blocks(
        confirm_reply, now=SEED_NOW, user_id=SEED_USER_ID, avatars=FAKE_AVATARS
    )
    _write_blocks("assistant_confirm_preview.json", confirm_blocks)


if __name__ == "__main__":
    main()
