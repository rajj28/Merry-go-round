"""Assistant-pane Block Kit view builder (Best-UX surface, Req 12).

This is the **pure, network-free Block Kit renderer** for Loop's second Slack
surface: the Assistant pane (the conversational front door). Where the App Home
(:mod:`loop.action.app_home`) is the always-on dashboard, this surface turns each
:class:`~loop.conversational.conversational_agent.AssistantReply` into a rich,
dense Block Kit message — the visible **Tool_Use_Trace** (Req 12.5) as a stepped
"Loop's plan" context line, App-Home-styled obligation rows, and a quoted
send-as-you "draft card" with one-tap Send / Cancel buttons (Req 12.6).

Design discipline (read before editing): :func:`build_assistant_blocks` is a
**pure function** of the reply (plus an optional ``now`` clock and ``user_id`` for
counterparty resolution). It performs no Slack network call — the ``chat_postMessage``
wiring lives in :mod:`loop.app`. Styling helpers (the Aging_Chip, native dates,
counterparty resolution) are reused from the App Home builder so both surfaces read
consistently.

The trace block is the credibility centerpiece: it is rendered from the reply's
*actual* :class:`~loop.conversational.conversational_agent.ToolUseTrace` entries, in
invocation order, never a canned string — so judges see the agent's real plan
(Obligation Graph → Verifier (GitHub MCP) → Action Agent).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from loop.action.app_home import (
    _card_action_buttons,
    _obligation_card,
    _row_age_label,
    aging_chip,
    resolved_person_id,
    slack_date,  # noqa: F401  (re-exported style helper; used by callers/tests)
)
from loop.action.app_home import AgingState
from loop.conversational.conversational_agent import AssistantReply, ReplyKind
from loop.graph.models import LoopState, Obligation, PersonId
from loop.graph.surfacing import TimeLike

# Block Kit action_ids for the in-pane one-tap confirm / decline of a send-as-user
# command (the Conversational Agent's own 60s gate — Req 12.6, 12.7). These are the
# Assistant-pane analogues of the App Home / 24h-gate ACTION_CONFIRM_SEND pair.
ACTION_ASSISTANT_CONFIRM = "assistant_confirm_send"
ACTION_ASSISTANT_DECLINE = "assistant_decline_send"

# How many obligation rows the (narrow) Assistant pane shows before collapsing the
# remainder into a "+N more" context line.
QUERY_ROW_CAP = 10

# Maps the internal Tool_Use_Trace tool names to short, human-readable labels for the
# restrained "Loop's reasoning" trace line (Req 12.5). Matched by a substring of the
# canonical trace tool name so detail-bearing entries still map. The Action Agent's
# label is refined per-operation in :func:`_trace_label`.
_TRACE_LABELS: tuple[tuple[str, str], ...] = (
    ("Planner", "Planned"),
    ("Obligation Graph", "Searched workspace"),
    ("Verifier", "Verified GitHub"),
)

# Copy constants — one home for the Assistant-pane strings so they're trivially
# assertable in tests.
QUERY_RESULT_HEADER = "Here's what I found"
TRACE_PREFIX = "Loop's reasoning:"
DRAFT_CARD_CONTEXT = "Loop will send this as you"
DRAFT_SEND_LABEL = "Send"
DRAFT_CANCEL_LABEL = "Cancel"
DISAMBIGUATION_PROMPT = "I found a few — which one did you mean?"
UNSUPPORTED_PREFIX = "I can help with:"


# ---------------------------------------------------------------------------
# Low-level Block Kit element builders (kept tiny + pure)
# ---------------------------------------------------------------------------
def _section(text: str, *, accessory: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "section", "text": {"type": "mrkdwn", "text": text}}
    if accessory is not None:
        block["accessory"] = accessory
    return block


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _divider() -> dict[str, Any]:
    return {"type": "divider"}


def _avatar_accessory(
    person_id: Optional[PersonId], avatars: Optional[Mapping[str, str]]
) -> Optional[dict[str, Any]]:
    """An ``image`` accessory for ``person_id``'s avatar, or ``None`` when unavailable.

    Pure: the URL is looked up from the caller-supplied ``avatars`` map, never fetched
    here. A missing entry (or no map) renders the section with no accessory.
    """
    if not avatars or not person_id:
        return None
    url = avatars.get(person_id)
    if not url:
        return None
    return {"type": "image", "image_url": url, "alt_text": person_id}


def _avatar_context(
    text: str,
    person_id: Optional[PersonId],
    avatars: Optional[Mapping[str, str]],
) -> Optional[dict[str, Any]]:
    """A ``context`` block: a *small* (~20px) avatar image beside ``text``.

    Context images render far smaller than a section image accessory (a large
    thumbnail with no size control), so a chat row reads as one compact line.
    Returns ``None`` when there is neither an avatar nor text.
    """
    elements: list[dict[str, Any]] = []
    url = avatars.get(person_id) if (avatars and person_id) else None
    if url:
        elements.append({"type": "image", "image_url": url, "alt_text": person_id or "avatar"})
    if text:
        elements.append({"type": "mrkdwn", "text": text})
    return {"type": "context", "elements": elements} if elements else None


# ---------------------------------------------------------------------------
# Tool_Use_Trace → clean "Loop's reasoning" context line (Req 12.5)
# ---------------------------------------------------------------------------
def _trace_label(tool: str, detail: Optional[str]) -> str:
    """Map an internal trace tool name to a short human label (no emoji).

    ``Obligation Graph`` → "Searched workspace", ``Verifier (GitHub MCP)`` →
    "Verified GitHub", ``Planner`` → "Planned", and the ``Action Agent`` to an
    action-appropriate verb keyed off the invocation detail.
    """
    if "Action Agent" in tool:
        d = (detail or "").lower()
        if "draft" in d:
            return "Drafted reply"
        if "send" in d:
            return "Sent reply"
        if "delegate" in d:
            return "Delegated"
        if "snooze" in d:
            return "Snoozed"
        if "close" in d:
            return "Closed loop"
        return "Took action"
    for needle, label in _TRACE_LABELS:
        if needle in tool:
            return label
    return tool


def _trace_block(reply: AssistantReply) -> Optional[dict[str, Any]]:
    """Render the Tool_Use_Trace as a single clean ``context`` line (Req 12.5).

    Built from ``reply.trace.entries`` **in invocation order** — the real record of
    what the agent did, mapped to short human labels (no emoji). Returns ``None`` when
    the trace is empty so a reply with no invocations stays clean.
    """
    entries = reply.trace.entries
    if not entries:
        return None
    steps = [_trace_label(e.tool, e.detail) for e in entries]
    return _context(f"{TRACE_PREFIX} {' → '.join(steps)}")


# ---------------------------------------------------------------------------
# Obligation row rendering (App-Home-consistent styling)
# ---------------------------------------------------------------------------
def _counterparty_person(
    obligation: Obligation, user_id: Optional[PersonId]
) -> Optional[PersonId]:
    """The person shown on an obligation row: the *other* party for this viewer.

    Endpoint-first: when the viewer sits on the edge, the counterparty is simply
    the opposite endpoint — stored state labels are relative to the tracked user,
    not the viewer, so label-based resolution would show a member their own face.
    The label mapping remains for edges the viewer is not on (``blocked-on-you``
    → the owed person; ``waiting-on-other`` → the owing person) and healed rows
    fall back to the structurally-resolved other party.
    """
    owes = obligation.owes_person_id or None
    owed = obligation.owed_person_id or None
    if user_id and user_id in (owes, owed) and owes != owed:
        return resolved_person_id(obligation, user_id)
    state = obligation.loop_state
    if state == LoopState.BLOCKED_ON_YOU and owed:
        return owed
    if state == LoopState.WAITING_ON_OTHER and owes:
        return owes
    return resolved_person_id(obligation, user_id)


def _counterparty_line(
    obligation: Obligation, now: Optional[TimeLike], user_id: Optional[PersonId]
) -> str:
    """The second line of an obligation row: counterparty · age · source channel.

    Mirrors the new App Home active-row phrasing so both surfaces read identically:
    ``<@{person}> · {age} · in <#{channel}>`` (age bolded with "overdue" when > 72h).
    """
    person = _counterparty_person(obligation, user_id)
    mention = f"<@{person}>" if person else "_unknown counterparty_"
    age_label = _row_age_label(aging_chip(obligation, now))
    line = f"{mention} · {age_label}"
    channel = obligation.source_msg_channel
    if channel:
        line += f" · in <#{channel}>"
    return line


def _obligation_section(
    obligation: Obligation,
    now: Optional[TimeLike],
    user_id: Optional[PersonId],
    avatars: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """One obligation as an App-Home-styled row: a subject ``section`` then a compact
    ``context`` line carrying a *small* avatar + counterparty · age · channel."""
    person = _counterparty_person(obligation, user_id)
    blocks: list[dict[str, Any]] = [_section(f"*{obligation.subject_summary}*")]
    meta = _avatar_context(_counterparty_line(obligation, now, user_id), person, avatars)
    if meta is not None:
        blocks.append(meta)
    return blocks


def _obligation_card_row(
    obligation: Obligation,
    now: Optional[TimeLike],
    user_id: Optional[PersonId],
    avatars: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """One obligation rendered as a Block Kit ``card`` (avatar icon + subject + who + actions).

    Reuses the App Home card builder so both surfaces read identically: the subject is
    the card title, the counterparty line the subtitle, and the same Nudge/Snooze/Dismiss
    buttons (carrying the obligation id) sit at the card foot.
    """
    person = _counterparty_person(obligation, user_id)
    subtitle = _counterparty_line(obligation, now, user_id)
    return _obligation_card(
        obligation.subject_summary,
        subtitle,
        person,
        avatars,
        _card_action_buttons(obligation),
    )


# ---------------------------------------------------------------------------
# Data visualization (Messages-only "data_visualization" block) — an aging
# snapshot bar chart that turns a list of loops into an at-a-glance picture.
# ---------------------------------------------------------------------------
CHART_TITLE = "Loops by age"
_AGING_CATEGORIES: tuple[tuple[AgingState, str], ...] = (
    (AgingState.FRESH, "Fresh (<24h)"),
    (AgingState.WARNING, "Aging (1–3d)"),
    (AgingState.OVERDUE, "Overdue (>3d)"),
)


def _aging_bar_chart(
    obligations: list[Obligation], now: Optional[TimeLike]
) -> Optional[dict[str, Any]]:
    """A ``data_visualization`` bar chart of the obligations bucketed by aging band.

    Returns ``None`` for an empty list so a no-result reply stays clean. Counts each
    obligation into Fresh / Aging / Overdue using the same 24h/72h boundaries as the
    Aging_Chip, then emits a single-series bar chart (Messages surface only).
    """
    if not obligations:
        return None
    counts: dict[AgingState, int] = {state: 0 for state, _ in _AGING_CATEGORIES}
    for o in obligations:
        chip = aging_chip(o, now)
        counts[chip.state] = counts.get(chip.state, 0) + 1
    categories = [label for _, label in _AGING_CATEGORIES]
    data = [
        {"label": label, "value": counts[state]} for state, label in _AGING_CATEGORIES
    ]
    return {
        "type": "data_visualization",
        "title": CHART_TITLE,
        "chart": {
            "type": "bar",
            "series": [{"name": "Loops", "data": data}],
            "axis_config": {
                "categories": categories,
                "x_label": "Age",
                "y_label": "Loops",
            },
        },
    }


def _query_summary_line(
    obligations: list[Obligation], user_id: Optional[PersonId]
) -> str:
    """A concise, human lead line explaining the result (redesign) — how many and
    which direction, then a prompt toward the actionable cards below."""
    n = len(obligations)
    people = "person" if n == 1 else "people"
    blocking = sum(1 for o in obligations if user_id and o.owes_person_id == user_id)
    waiting = sum(1 for o in obligations if user_id and o.owed_person_id == user_id)
    if n and blocking == n:
        verb = "is" if n == 1 else "are"
        return (
            f"*{n} {people} {verb} waiting on you.* "
            "Nudge, snooze, or close any of these in a tap:"
        )
    if n and waiting == n:
        return (
            f"*You're waiting on {n} {people}.* "
            "Here's what's still open — I can draft a nudge for any of them:"
        )
    return f"*{n} open loop{'s' if n != 1 else ''}.* Here's where things stand:"


def _query_result_blocks(
    reply: AssistantReply,
    now: Optional[TimeLike],
    user_id: Optional[PersonId],
    avatars: Optional[Mapping[str, str]],
    *,
    style: str = "sections",
    chart: bool = False,
) -> list[dict[str, Any]]:
    """Blocks for a QUERY_RESULT reply: a concise explanation, then capped rows.

    Leads with a one-line summary of *what* the result is (how many loops, waiting
    on you vs you waiting) so the chat explains before it lists. With ``style="cards"``
    each obligation renders as a Block Kit ``card`` (avatar icon + subject +
    counterparty + Nudge/Snooze/Dismiss); otherwise the section + context row.
    """
    obligations = list(reply.obligations)
    blocks: list[dict[str, Any]] = [_section(_query_summary_line(obligations, user_id))]
    shown = obligations[:QUERY_ROW_CAP]
    for o in shown:
        if style == "cards":
            blocks.append(_obligation_card_row(o, now, user_id, avatars))
        else:
            blocks.extend(_obligation_section(o, now, user_id, avatars))
    overflow = len(obligations) - len(shown)
    if overflow > 0:
        blocks.append(_context(f"_+{overflow} more not shown_"))
    if chart:
        chart_block = _aging_bar_chart(shown, now)
        if chart_block is not None:
            blocks.append(chart_block)
    return blocks


# ---------------------------------------------------------------------------
# CONFIRM_REQUIRED → quoted send-as-you "draft card" with Send / Cancel
# ---------------------------------------------------------------------------
def _blockquote(text: str) -> str:
    """Render ``text`` as a Slack mrkdwn blockquote (every line prefixed with ``>``)."""
    lines = text.splitlines() or [text]
    return "\n".join(f">{line}" if line else ">" for line in lines)


def _draft_card_blocks(
    reply: AssistantReply, user_id: Optional[PersonId]
) -> list[dict[str, Any]]:
    """Blocks for a CONFIRM_REQUIRED reply: a quoted draft + Send/Cancel buttons (Req 12.6).

    The drafted message (``reply.text``) is quoted so it reads as a card, a context
    line states Loop will send it *as the user* (with the source channel when known),
    and an ``actions`` block carries a primary **Send** (``ACTION_ASSISTANT_CONFIRM``)
    and a secondary **Cancel** (``ACTION_ASSISTANT_DECLINE``) — each ``value`` is the
    user id so the in-pane 60s gate handler can resolve whose confirmation it is.
    """
    blocks: list[dict[str, Any]] = [_section(_blockquote(reply.text))]

    context_text = DRAFT_CARD_CONTEXT
    obligation = reply.obligation
    if obligation is not None and obligation.source_msg_channel:
        context_text += f" · in <#{obligation.source_msg_channel}>"
    blocks.append(_context(context_text))

    value = user_id or ""
    blocks.append(
        {
            "type": "actions",
            "block_id": "assistant_confirm",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_ASSISTANT_CONFIRM,
                    "text": {"type": "plain_text", "text": DRAFT_SEND_LABEL, "emoji": True},
                    "style": "primary",
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": ACTION_ASSISTANT_DECLINE,
                    "text": {"type": "plain_text", "text": DRAFT_CANCEL_LABEL, "emoji": True},
                    "value": value,
                },
            ],
        }
    )
    return blocks


# ---------------------------------------------------------------------------
# DISAMBIGUATION → "pick one" + a section per candidate
# ---------------------------------------------------------------------------
def _disambiguation_blocks(
    reply: AssistantReply,
    now: Optional[TimeLike],
    user_id: Optional[PersonId],
    avatars: Optional[Mapping[str, str]],
    *,
    style: str = "sections",
) -> list[dict[str, Any]]:
    """Blocks for a DISAMBIGUATION reply: a pick-one prompt, then one row per candidate."""
    prompt = reply.text or DISAMBIGUATION_PROMPT
    blocks: list[dict[str, Any]] = [_section(prompt)]
    for o in reply.candidates:
        if style == "cards":
            blocks.append(_obligation_card_row(o, now, user_id, avatars))
        else:
            blocks.extend(_obligation_section(o, now, user_id, avatars))
    return blocks


# ---------------------------------------------------------------------------
# UNSUPPORTED → reply text + supported-actions context line
# ---------------------------------------------------------------------------
def _unsupported_blocks(reply: AssistantReply) -> list[dict[str, Any]]:
    """Blocks for an UNSUPPORTED reply: the message + a supported-actions context line."""
    blocks: list[dict[str, Any]] = [_section(reply.text)]
    if reply.supported_actions:
        actions = ", ".join(f"`{a}`" for a in reply.supported_actions)
        blocks.append(_context(f"{UNSUPPORTED_PREFIX} {actions}"))
    return blocks


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------
def people_in_reply(
    reply: AssistantReply, user_id: Optional[PersonId] = None
) -> set[str]:
    """The counterparty person ids an assistant reply will render (for avatar resolution).

    Returns exactly the people whose avatars a rendered reply could show — the
    counterparty of every result/candidate obligation (and the single target, if any).
    Used by the (network-bearing) wiring layer to fetch just the avatars it needs;
    the builder itself never fetches.
    """
    people: set[str] = set()
    candidates = list(reply.obligations) + list(reply.candidates)
    if reply.obligation is not None:
        candidates.append(reply.obligation)
    for o in candidates:
        person = _counterparty_person(o, user_id)
        if person:
            people.add(person)
    return people


def build_assistant_blocks(
    reply: AssistantReply,
    *,
    now: Optional[TimeLike] = None,
    user_id: Optional[PersonId] = None,
    avatars: Optional[Mapping[str, str]] = None,
    style: str = "sections",
    chart: bool = False,
) -> list[dict[str, Any]]:
    """Build the Assistant-pane Block Kit blocks for an :class:`AssistantReply` (Req 12).

    Pure and deterministic given the reply (plus ``now`` for aging labels and
    ``user_id`` for counterparty resolution); performs no Slack network call. The
    body varies by :class:`ReplyKind`:

      * **QUERY_RESULT** — a "Here's what I found" header, then one App-Home-styled
        section per matching obligation (bold subject + counterparty mention + plain
        age + source channel + optional avatar accessory), capped at
        :data:`QUERY_ROW_CAP` with a "+N more".
      * **CONFIRM_REQUIRED** — a quoted send-as-you draft card with Send / Cancel
        buttons (Req 12.6).
      * **DISAMBIGUATION** — a pick-one prompt then a section per candidate.
      * **UNSUPPORTED** — the message plus a supported-actions context line.
      * **NO_MATCH / CANCELLED / ACTION_DONE / ACTION_FAILED / CLARIFICATION** — a
        single clean section with the reply text.

    Every reply is **prefixed** with the clean "Loop's reasoning" Tool_Use_Trace line
    whenever the trace is non-empty (Req 12.5) — the credibility centerpiece.

    Args:
        avatars: optional ``person_id → https avatar URL`` map; rows whose person has
            an entry render a real image accessory. Looked up only, never fetched.
        style: ``"sections"`` (default) or ``"cards"`` — when cards, QUERY_RESULT and
            disambiguation obligation rows render as Block Kit ``card`` blocks.
        chart: when set, a QUERY_RESULT with at least one obligation appends an aging
            ``data_visualization`` bar chart (Messages-surface only).

    Returns:
        A list of Block Kit block dicts ready to pass as ``blocks=`` to
        ``chat_postMessage`` (with the reply text as the accessibility fallback).
    """
    blocks: list[dict[str, Any]] = []

    # The Tool_Use_Trace reasoning line leads every reply that ran any tool (Req 12.5).
    trace = _trace_block(reply)
    if trace is not None:
        blocks.append(trace)

    if reply.kind == ReplyKind.QUERY_RESULT:
        blocks.extend(
            _query_result_blocks(reply, now, user_id, avatars, style=style, chart=chart)
        )
    elif reply.kind == ReplyKind.CONFIRM_REQUIRED:
        blocks.extend(_draft_card_blocks(reply, user_id))
    elif reply.kind == ReplyKind.DISAMBIGUATION:
        blocks.extend(_disambiguation_blocks(reply, now, user_id, avatars, style=style))
    elif reply.kind == ReplyKind.UNSUPPORTED:
        blocks.extend(_unsupported_blocks(reply))
    else:
        # NO_MATCH / CANCELLED / ACTION_DONE / ACTION_FAILED / CLARIFICATION — a clean
        # single section carrying the reply text.
        blocks.append(_section(reply.text))

    return blocks


__all__ = [
    "build_assistant_blocks",
    "people_in_reply",
    "ACTION_ASSISTANT_CONFIRM",
    "ACTION_ASSISTANT_DECLINE",
    "QUERY_ROW_CAP",
    "QUERY_RESULT_HEADER",
    "TRACE_PREFIX",
    "DRAFT_CARD_CONTEXT",
    "DRAFT_SEND_LABEL",
    "DRAFT_CANCEL_LABEL",
    "UNSUPPORTED_PREFIX",
    "CHART_TITLE",
]
