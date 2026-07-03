"""App Home dashboard view builder (tasks 9.1–9.3 — Dev D, Req 6, 9).

This module is the **pure Block Kit view builder** for the Loop App Home — the
signature "N people are blocked on you right now" reveal (design.md → "Slack
Surfaces (Block Kit)" → "App Home"). It realizes:

  * task 9.1 — the hero banner (count of surfaced ``blocked-on-you`` obligations,
    with zero-state messaging) plus the two active sections (Blocked-On-You,
    Waiting-On-Others) containing exactly the surfaced obligations of each state,
    sorted by ``last_touch_timestamp`` oldest→newest (Req 6.1, 6.2, 6.3, 6.4, 6.7).
  * task 9.2 — the per-row Aging_Chip: fresh ``<24h``, warning ``24h–72h``
    inclusive, overdue ``>72h`` (Req 6.5).
  * task 9.3 — the footer context line with per-section totals and an explicit
    empty-state message for any section with zero qualifying items (Req 6.6, 6.9).

The Auto-Healed feed section (Req 9) is included here with its header, empty
state, and basic rows so the three-section layout (Req 6.2) is complete; the full
feed membership/ordering/cap/placeholder logic is task 11.1. This section uses the
:func:`loop.graph.surfacing.is_in_auto_healed_feed` predicate as its membership
gate.

Purity discipline (read before editing): :func:`build_app_home_view` is a **pure,
deterministic** function of the graph's current contents plus ``now``. It performs
no Slack network call — the ``views_publish`` wiring lives in task 17 and the
existing :mod:`loop.slack_app` ``app_home_opened`` handler will call this builder
then. Surfacing decisions are delegated to the store's
:class:`~loop.graph.store.ObligationFilter` (``surfaced_only=True``), which applies
the single :func:`loop.graph.surfacing.is_surfaced` gate against the store's own
threshold — so quiet-by-default and privacy are enforced in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Union

from loop.graph.models import ClosureKind, LoopState, Obligation, PersonId
from loop.graph.store import ObligationFilter, ObligationGraph
from loop.graph.surfacing import TimeLike, is_in_auto_healed_feed

# ---------------------------------------------------------------------------
# Aging chip thresholds (Req 6.5) — seconds since Last_Touch_Timestamp.
# fresh    : age <  24h
# warning  : 24h <= age <= 72h   (inclusive of BOTH boundaries)
# overdue  : age >  72h
# ---------------------------------------------------------------------------
AGING_WARNING_FLOOR_SECONDS: float = 24 * 3600.0     # 24h, inclusive lower bound of warning
AGING_OVERDUE_FLOOR_SECONDS: float = 72 * 3600.0     # 72h, inclusive upper bound of warning

# ---------------------------------------------------------------------------
# Copy constants — kept here so the hero/empty-state/footer text has one home and
# is trivially assertable in tests. The aesthetic is restrained and premium:
# urgency comes from typography (bold), button styles, structure, and real avatar
# images — never decorative emoji glyphs in the rendered surface.
# ---------------------------------------------------------------------------
HERO_ZERO_TEXT = "You're all caught up — nobody's blocked on you."

# Hero card (rendered beneath the hero header only when the blocked count > 0).
# A bold restatement of the reveal, a reassuring "we found these for you" subline,
# and a one-line scan summary that uses Slack-native date formatting for "updated".
HERO_SUBLINE_TEXT = "_Loop detected these automatically — you haven't replied yet._"
HERO_REVIEW_BUTTON_TEXT = "Review blocked loops"

BLOCKED_SECTION_TITLE = "Blocked on you"
WAITING_SECTION_TITLE = "Waiting on others"
HEALED_SECTION_TITLE = "Recently auto-closed"

BLOCKED_EMPTY_TEXT = "_Nobody's blocked on you right now. Nice._"
WAITING_EMPTY_TEXT = "_You're not waiting on anyone right now._"
HEALED_EMPTY_TEXT = "_No loops have been auto-closed yet._"

# Auto-Healed feed (Req 9) placeholders + cap.
# Req 9.5 — a missing resolved person / closure reason renders a placeholder while
# the closure timestamp is still shown.
HEALED_PERSON_PLACEHOLDER = "_someone (unknown)_"
HEALED_REASON_PLACEHOLDER = "_reason unavailable_"
# Req 9.6 — the feed shows at most the 100 most-recently-healed entries.
AUTO_HEALED_FEED_CAP = 100

# Per-row action button action_ids (the interaction handlers are wired in task 17;
# here these are just the Block Kit definitions).
ACTION_NUDGE = "app_home_nudge"
ACTION_SNOOZE = "app_home_snooze"
ACTION_DELEGATE = "app_home_delegate"
ACTION_DISMISS = "app_home_dismiss"
# The per-row overflow (⋮) menu that collapses the secondary actions
# (Snooze / Delegate / Dismiss) so a row shows only one visible button + a small
# menu — far less crowded than a four-button bar. Each option value encodes
# ``{verb}::{obligation_id}`` so the single handler can recover both.
ACTION_ROW_OVERFLOW = "app_home_row_overflow"
# The hero card's primary call-to-action — opens a DM summary of the blocked loops.
ACTION_REVIEW_BLOCKED = "app_home_review_blocked"
# The hero quick-action: a dropdown of everyone blocked on you. Picking a person
# opens the Nudge composer modal for that loop — a fast "nudge anyone" entry point
# shown only when more than one person is waiting.
ACTION_QUICK_NUDGE = "app_home_quick_nudge"


class AgingState(str, Enum):
    """The three visually-distinct Aging_Chip states (Req 6.5)."""

    FRESH = "fresh"        # age < 24h
    WARNING = "warning"    # 24h <= age <= 72h (inclusive)
    OVERDUE = "overdue"    # age > 72h


# Emoji cue per aging state (green→yellow→red urgency ramp).
_AGING_EMOJI: dict[AgingState, str] = {
    AgingState.FRESH: "🟢",
    AgingState.WARNING: "🟡",
    AgingState.OVERDUE: "🔴",
}


@dataclass(frozen=True)
class AgingChip:
    """A rendered Aging_Chip: its classification plus the human-readable cue.

    Attributes:
        state: the :class:`AgingState` bucket the obligation falls in.
        emoji: the urgency emoji cue for ``state``.
        age_text: a short human age such as ``"3d"`` / ``"5h"`` / ``"just now"``.
        text: the combined chip text (``"🟡 2d"``) rendered onto the row.
    """

    state: AgingState
    emoji: str
    age_text: str
    text: str


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a tz-aware UTC ``datetime``.

    Mirrors the coercion used across the store and surfacing predicate: a bare
    trailing ``Z`` becomes ``+00:00`` and a naive value is assumed to be UTC, so
    age/ordering comparisons here agree with the rest of the system.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def slack_date(iso_or_ts: str) -> str:
    """Render a Slack-native ``<!date…>`` token from an ISO 8601 timestamp.

    Slack clients localize a ``<!date^{epoch}^{tokens}|{fallback}>`` string to the
    viewer's own timezone and locale — so a single stored UTC instant reads
    correctly for everyone. We emit the ``date_short_pretty`` + ``time`` tokens
    (e.g. "May 30 at 10:00 AM") and supply the date part as the literal fallback
    Slack shows when it cannot resolve the token.

    The ``epoch`` is computed from the parsed UTC ``datetime``. If parsing fails
    (a malformed or non-ISO value), the original string is returned verbatim as a
    plain, non-native fallback so a row still shows *something* legible.
    """
    try:
        dt = _parse_iso_utc(iso_or_ts)
    except (ValueError, TypeError, AttributeError):
        return iso_or_ts
    epoch = int(dt.timestamp())
    fallback = dt.date().isoformat()
    return f"<!date^{epoch}^{{date_short_pretty}} at {{time}}|{fallback}>"


def _coerce_now(now: Optional[TimeLike]) -> datetime:
    """Coerce the ``now`` argument (``datetime`` | ISO string | None) to UTC datetime."""
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)
    return _parse_iso_utc(now)


def _now_iso(now: Optional[TimeLike]) -> str:
    """Render ``now`` as the ISO 8601 UTC string the store's filter expects."""
    return _coerce_now(now).isoformat()


def _format_age(seconds: float) -> str:
    """Format an age in seconds as a compact human string (``3d`` / ``5h`` / ``12m``)."""
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def classify_aging(age_seconds: float) -> AgingState:
    """Bucket an age (seconds) into a :class:`AgingState` (Req 6.5).

    Boundary semantics — the warning band is **inclusive of both 24h and 72h**:
      * ``age < 24h``           → FRESH
      * ``24h <= age <= 72h``   → WARNING
      * ``age > 72h``           → OVERDUE
    """
    if age_seconds < AGING_WARNING_FLOOR_SECONDS:
        return AgingState.FRESH
    if age_seconds <= AGING_OVERDUE_FLOOR_SECONDS:
        return AgingState.WARNING
    return AgingState.OVERDUE


def aging_chip(obligation: Obligation, now: Optional[TimeLike]) -> AgingChip:
    """Build the :class:`AgingChip` for ``obligation`` relative to ``now`` (Req 6.5).

    Age is ``now - last_touch_timestamp``; the classification uses the inclusive
    24h/72h boundaries documented on :func:`classify_aging`.
    """
    age_seconds = (_coerce_now(now) - _parse_iso_utc(obligation.last_touch_timestamp)).total_seconds()
    state = classify_aging(age_seconds)
    emoji = _AGING_EMOJI[state]
    age_text = _format_age(age_seconds)
    return AgingChip(state=state, emoji=emoji, age_text=age_text, text=f"{emoji} {age_text}")


# ---------------------------------------------------------------------------
# Section content selectors — pure, deterministic ordered obligation lists.
# These are the testable heart of tasks 9.1/9.3: the view builder composes them.
# ---------------------------------------------------------------------------
def _sorted_oldest_first(obligations: list[Obligation]) -> list[Obligation]:
    """Sort active-section obligations by ``last_touch_timestamp`` oldest→newest (Req 6.3/6.4)."""
    return sorted(obligations, key=lambda o: _parse_iso_utc(o.last_touch_timestamp))


def _sorted_newest_first(obligations: list[Obligation]) -> list[Obligation]:
    """Sort feed obligations by ``last_touch_timestamp`` newest→oldest (Req 6.8)."""
    return sorted(
        obligations, key=lambda o: _parse_iso_utc(o.last_touch_timestamp), reverse=True
    )


def blocked_on_you_rows(graph: ObligationGraph, now: Optional[TimeLike]) -> list[Obligation]:
    """Surfaced ``blocked-on-you`` obligations, oldest→newest (Req 6.3).

    ``surfaced_only=True`` defers to the store's single surfacing gate (not
    dismissed, not snoozed, confidence ≥ threshold, active state).
    """
    rows = graph.query(
        ObligationFilter(
            loop_states=frozenset({LoopState.BLOCKED_ON_YOU}),
            surfaced_only=True,
            now=_now_iso(now),
        )
    )
    return _sorted_oldest_first(rows)


def waiting_on_other_rows(graph: ObligationGraph, now: Optional[TimeLike]) -> list[Obligation]:
    """Surfaced ``waiting-on-other`` obligations, oldest→newest (Req 6.4)."""
    rows = graph.query(
        ObligationFilter(
            loop_states=frozenset({LoopState.WAITING_ON_OTHER}),
            surfaced_only=True,
            now=_now_iso(now),
        )
    )
    return _sorted_oldest_first(rows)


def auto_healed_rows(
    graph: ObligationGraph,
    now: Optional[TimeLike] = None,
    user_id: Optional[PersonId] = None,
) -> list[Obligation]:
    """Auto-Healed feed membership + ordering + cap, as ordered obligations (Req 9).

    Membership (Req 9.1) is gated by
    :func:`loop.graph.surfacing.is_in_auto_healed_feed`: exactly the non-dismissed
    obligations that reached ``healed`` through an *autonomous* closure — manual
    closures are excluded.

    Ordering (Req 9.3): newest→oldest by closure timestamp; ties (equal closure
    timestamp) broken by resolved person id ascending alphabetical.

    Cap (Req 9.6): at most :data:`AUTO_HEALED_FEED_CAP` (100) entries, retaining the
    most-recently-healed when more exist.

    ``user_id`` identifies the tracked user so the *resolved person* (the other
    party — Req 9.2) can be picked as the non-user endpoint of the edge; see
    :func:`resolved_person_id`.
    """
    rows = graph.query(
        ObligationFilter(
            loop_states=frozenset({LoopState.HEALED}),
            closure_kind=ClosureKind.AUTONOMOUS,
            include_dismissed=False,
        )
    )
    rows = [o for o in rows if is_in_auto_healed_feed(o)]
    return _ordered_capped_healed(rows, user_id)


def _ordered_capped_healed(
    rows: list[Obligation], user_id: Optional[PersonId]
) -> list[Obligation]:
    """Order healed obligations newest→oldest by closure ts (ties → resolved person
    id ascending) and keep at most the 100 most-recent (Req 9.3, 9.6).

    Implemented as a two-pass stable sort: first ascending by resolved person id,
    then a stable descending sort by closure timestamp. Stability preserves the
    ascending person order *within* a shared closure timestamp, realizing the
    documented tie-break exactly.
    """
    by_person = sorted(rows, key=lambda o: _resolved_sort_key(o, user_id))
    newest_first = sorted(by_person, key=_closure_sort_dt, reverse=True)
    return newest_first[:AUTO_HEALED_FEED_CAP]


def _closure_sort_dt(obligation: Obligation) -> datetime:
    """The instant an obligation healed, for ordering (Req 9.3).

    Uses ``closure_timestamp`` when present; falls back to ``last_touch_timestamp``
    so an entry still orders sensibly even if the closure timestamp was never
    stamped (the auto-close path always stamps it — Req 8.5 — so this is defensive).
    """
    ts = obligation.closure_timestamp or obligation.last_touch_timestamp
    return _parse_iso_utc(ts)


def resolved_person_id(
    obligation: Obligation, user_id: Optional[PersonId] = None
) -> Optional[PersonId]:
    """The *resolved person* shown on a feed entry — the other party (Req 9.2).

    A healed obligation has lost its active direction, so the counterparty is found
    structurally: when ``user_id`` is known, the resolved person is whichever edge
    endpoint is *not* the user (for a healed blocked-on-you that is the owed person;
    for a healed waiting-on-other that is the owing person). When ``user_id`` is
    unknown (or the user is neither endpoint), the owed person is used as the other
    party per the feed's blocked-on-you convention.

    Returns ``None`` when no usable identifier exists, so the caller can render the
    missing-person placeholder while still showing the timestamp (Req 9.5).
    """
    owes = obligation.owes_person_id or None
    owed = obligation.owed_person_id or None
    if user_id:
        if owes == user_id and owed != user_id:
            return owed
        if owed == user_id and owes != user_id:
            return owes
    return owed or owes


def _resolved_sort_key(obligation: Obligation, user_id: Optional[PersonId]) -> str:
    """Tie-break key (Req 9.3): the resolved person id, with a missing id sorting
    first via the empty string so ordering stays total and deterministic."""
    return resolved_person_id(obligation, user_id) or ""


def hero_count(graph: ObligationGraph, now: Optional[TimeLike]) -> int:
    """The hero-banner count: surfaced ``blocked-on-you`` obligations (Req 6.1, 6.7)."""
    return len(blocked_on_you_rows(graph, now))


def hero_text(count: int) -> str:
    """The hero-banner copy for a given blocked-on-you ``count`` (Req 6.1, 6.7).

    Zero-state surfaces the reassuring "all caught up" line (Req 6.7); otherwise
    the signature reveal, pluralized.
    """
    if count == 0:
        return HERO_ZERO_TEXT
    noun = "person" if count == 1 else "people"
    verb = "is" if count == 1 else "are"
    return f"{count} {noun} {verb} blocked on you"


def hero_card_text(count: int) -> str:
    """The bold restatement shown on the hero card beneath the header (count > 0).

    Reinforces the reveal in mrkdwn bold so it reads as the screen's focal point;
    only rendered when at least one person is blocked on the user (the zero-state
    stays a single minimal header).
    """
    noun = "person" if count == 1 else "people"
    verb = "is" if count == 1 else "are"
    return f"*{count} {noun} {verb} still waiting on a reply from you.*"


def hero_stats_text(total: int, when: str) -> str:
    """The single scan-summary line shown beneath the hero header (count > 0).

    Merges the "Loop found these for you" reassurance with the scan stats into one
    clean context line, so the hero is just: header → this line → Review button.
    ``when`` is a Slack-native :func:`slack_date` token so "scanned" localizes to the
    viewer; ``total`` is the tracked-loop count across all three sections.
    """
    return f"Loop found these automatically · scanned {when} · {total} loops tracked"


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------
def _header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _mrkdwn_section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _avatar_accessory(
    person_id: Optional[PersonId], avatars: Optional[Mapping[str, str]]
) -> Optional[dict[str, Any]]:
    """An ``image`` accessory for a person's avatar, or ``None`` when unavailable.

    The premium lever: when an https avatar URL is supplied for ``person_id`` the row
    section carries a real person image instead of a glyph. Builders stay **pure** —
    the URL is looked up from the caller-supplied ``avatars`` map, never fetched here,
    so a missing entry (or no map at all) simply renders the section with no accessory.
    """
    if not avatars or not person_id:
        return None
    url = avatars.get(person_id)
    if not url:
        return None
    return {"type": "image", "image_url": url, "alt_text": person_id}


def _logo_block(logo_url: str) -> dict[str, Any]:
    """A small leading Loop logo ``image`` block (rendered only when configured)."""
    return {"type": "image", "image_url": logo_url, "alt_text": "Loop"}


def _hero_review_actions() -> dict[str, Any]:
    """The hero's primary call-to-action as a full-width ``actions`` block.

    Rendered as its own actions block (not a section accessory) so the Review button
    sits cleanly on its own line and never floats/wraps awkwardly in the narrow App
    Home side panel. Carries :data:`ACTION_REVIEW_BLOCKED`; its handler DMs the user a
    concise summary of the loops blocked on them.
    """
    return {
        "type": "actions",
        "block_id": "hero_actions",
        "elements": [
            {
                "type": "button",
                "action_id": ACTION_REVIEW_BLOCKED,
                "text": {"type": "plain_text", "text": HERO_REVIEW_BUTTON_TEXT, "emoji": True},
                "style": "primary",
                "value": "review_blocked",
            }
        ],
    }


def _hero_blocks(count: int, total: int, now: Optional[TimeLike]) -> list[dict[str, Any]]:
    """The hero region beneath the header (Req 6.1 dashboard polish).

    Zero-state stays a single minimal header — no stats, no button. When at least one
    person is blocked on the user, render exactly two clean blocks: a single merged
    scan-summary context line and the full-width Review button. The bold restatement is
    intentionally dropped — the header already states the reveal — so the top no longer
    feels crowded.
    """
    if count == 0:
        return []
    when = slack_date(_now_iso(now))
    return [
        _context(hero_stats_text(total, when)),
        _hero_review_actions(),
    ]


def _row_action_buttons(obligation: Obligation) -> dict[str, Any]:
    """The per-row actions block: one visible **Nudge** button + a ⋮ overflow menu.

    To keep the dashboard uncrowded, only the primary action (Nudge) is shown as a
    button; the secondary actions (Snooze / Delegate / Dismiss) collapse into a single
    overflow (⋮) menu. The Nudge button carries the obligation id as its ``value`` (so
    the existing :data:`ACTION_NUDGE` handler is unchanged); each overflow option value
    encodes ``{verb}::{obligation_id}`` for the single :data:`ACTION_ROW_OVERFLOW`
    handler. These are Block Kit definitions only; handlers live in :mod:`loop.app`.
    """
    oid = obligation.obligation_id
    return {
        "type": "actions",
        "block_id": f"row_actions::{oid}",
        "elements": [
            {
                "type": "button",
                "action_id": ACTION_NUDGE,
                "text": {"type": "plain_text", "text": "Nudge", "emoji": True},
                "style": "primary",
                "value": oid,
            },
            {
                "type": "overflow",
                "action_id": ACTION_ROW_OVERFLOW,
                "options": [
                    {
                        "text": {"type": "plain_text", "text": "Snooze", "emoji": True},
                        "value": f"snooze::{oid}",
                    },
                    {
                        "text": {"type": "plain_text", "text": "Delegate", "emoji": True},
                        "value": f"delegate::{oid}",
                    },
                    {
                        "text": {"type": "plain_text", "text": "Dismiss", "emoji": True},
                        "value": f"dismiss::{oid}",
                    },
                ],
            },
        ],
    }


def _row_age_label(chip: AgingChip) -> str:
    """The age as plain row text — bolded, with "overdue" appended, only when overdue.

    Urgency is conveyed by typography, not a colored-dot glyph: a fresh/warning row
    shows a quiet plain age (``"5h"`` / ``"2d"``); an overdue row (> 72h) shows a bold
    ``"*4d overdue*"`` so the most-urgent loops jump out. The :class:`AgingChip` and
    its emoji are left untouched for other callers — they are simply not used in row
    rendering anymore.
    """
    if chip.state is AgingState.OVERDUE:
        return f"*{chip.age_text} overdue*"
    return chip.age_text


def _channel_suffix(obligation: Obligation) -> str:
    """The ``· in <#C…>`` source-channel mention appended to an active row's line 2.

    Renders the source channel as a Slack-native channel mention so it links
    straight to where the loop started; omitted when no source channel is known.
    """
    channel = obligation.source_msg_channel
    return f" · in <#{channel}>" if channel else ""


def _blocked_row_text(obligation: Obligation, chip: AgingChip) -> str:
    """Row copy for a blocked-on-you obligation (the other party waits on the user)."""
    return (
        f"*{obligation.subject_summary}*\n"
        f"<@{obligation.owed_person_id}> · {_row_age_label(chip)}{_channel_suffix(obligation)}"
    )


def _waiting_row_text(obligation: Obligation, chip: AgingChip) -> str:
    """Row copy for a waiting-on-other obligation (the user waits on the other party)."""
    return (
        f"*{obligation.subject_summary}*\n"
        f"<@{obligation.owes_person_id}> · {_row_age_label(chip)}{_channel_suffix(obligation)}"
    )


def _healed_row_text(
    obligation: Obligation, user_id: Optional[PersonId] = None
) -> str:
    """Auto-Healed feed row headline (task 11.1 — Req 9.2, 9.5).

    The headline carries the two human-facing fields of a closed loop: the
    **resolved person** and the **closure reason** — ``Resolved with {person} —
    {reason}``. The closure *timestamp* rides on the row's context subline (:func:`_healed_subline`)
    rendered natively via :func:`slack_date`, so a row always shows *who*, *why*, and
    *when*.

    Req 9.5 placeholder rule — if the resolved person identifier or the closure
    reason is unavailable a placeholder is shown for *that* field (the timestamp on
    the subline is rendered regardless):
      * a missing resolved person → :data:`HEALED_PERSON_PLACEHOLDER`
        (``resolved_person_id`` returns ``None`` when neither edge endpoint yields a
        usable id);
      * a missing/empty closure reason → :data:`HEALED_REASON_PLACEHOLDER`.
    """
    person = resolved_person_id(obligation, user_id)
    person_text = f"<@{person}>" if person else HEALED_PERSON_PLACEHOLDER
    reason = obligation.closure_reason or HEALED_REASON_PLACEHOLDER
    return f"Resolved with {person_text} — {reason}"


def _healed_subline(obligation: Obligation) -> str:
    """The timeline subline beneath an Auto-Healed row: when it closed + who closed it.

    The closure timestamp is rendered natively via :func:`slack_date` so it localizes
    per viewer, followed by a quiet attribution that the loop closed itself. The
    timestamp falls back to ``last_touch_timestamp`` only if the closure timestamp was
    never stamped (defensive — the auto-close path always stamps it per Req 8.5), so a
    row always shows *when* the loop healed.
    """
    when = obligation.closure_timestamp or obligation.last_touch_timestamp
    return f"{slack_date(when)} · closed automatically"


def _active_section_blocks(
    title: str,
    rows: list[Obligation],
    empty_text: str,
    now: Optional[TimeLike],
    *,
    row_text,
    person_of,
    avatars: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """Build the blocks for one active section: header, then rows or an empty state.

    Each rendered row is a ``section`` (carrying a person **avatar image accessory**
    when one is supplied via ``avatars``) followed by its Nudge/Snooze/Delegate/Dismiss
    ``actions`` block. ``person_of`` resolves the row's counterparty so the avatar maps
    to the right person.
    """
    blocks: list[dict[str, Any]] = [_header(title), {"type": "divider"}]
    if not rows:
        blocks.append(_mrkdwn_section(empty_text))
        return blocks
    for o in rows:
        chip = aging_chip(o, now)
        section = _mrkdwn_section(row_text(o, chip))
        accessory = _avatar_accessory(person_of(o), avatars)
        if accessory is not None:
            section["accessory"] = accessory
        blocks.append(section)
        blocks.append(_row_action_buttons(o))
    return blocks


def _healed_section_blocks(
    rows: list[Obligation],
    user_id: Optional[PersonId] = None,
    *,
    avatars: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """Build the Recently-auto-closed feed section: header, then feed rows or empty state.

    The empty state (Req 9.4) renders :data:`HEALED_EMPTY_TEXT` when no loops have
    been auto-closed. Otherwise each membership-gated, ordered, capped row (computed
    upstream in :func:`auto_healed_rows`) is rendered via :func:`_healed_row_text`,
    which carries the resolved person / timestamp / reason with placeholders for any
    unavailable field (Req 9.2, 9.5), plus a person avatar accessory when supplied.
    """
    blocks: list[dict[str, Any]] = [_header(HEALED_SECTION_TITLE), {"type": "divider"}]
    if not rows:
        blocks.append(_mrkdwn_section(HEALED_EMPTY_TEXT))
        return blocks
    for o in rows:
        section = _mrkdwn_section(_healed_row_text(o, user_id))
        accessory = _avatar_accessory(resolved_person_id(o, user_id), avatars)
        if accessory is not None:
            section["accessory"] = accessory
        blocks.append(section)
        blocks.append(_context(_healed_subline(o)))
    return blocks


# ---------------------------------------------------------------------------
# Card-based rendering (newer Block Kit "card" block — Best-UX upgrade).
#
# The `card` block (surfaces: Home tabs + Messages) groups an avatar `icon`, a
# `title`, a `subtitle`, and up to **three** action buttons into one cohesive,
# bordered card — a far more premium "native Slack" row than a section + image
# accessory + separate actions block. It is opt-in via build_app_home_view(style=…)
# so the proven section layout stays the default/fallback until a live render check
# confirms cards render in the target workspace.
#
# Card field limits we respect: title 150, subtitle 150, body 200, subtext 200,
# max 3 action buttons (danger left-aligned, primary right-most).
# ---------------------------------------------------------------------------
CARD_TITLE_MAX = 150
CARD_SUBTITLE_MAX = 150
CARD_BODY_MAX = 200
CARD_SUBTEXT_MAX = 200


def _display_name(
    person_id: Optional[PersonId], names: Optional[Mapping[str, str]]
) -> Optional[str]:
    """A person's human display name from the caller-supplied ``names`` map, or ``None``.

    Pure: looked up only, never fetched. When present it becomes the card title (e.g.
    "Alice") so the card leads with *who*, leaving the full ask for the wrapping body.
    """
    if not names or not person_id:
        return None
    return names.get(person_id) or None


def _truncate(text: str, limit: int) -> str:
    """Trim ``text`` to ``limit`` chars with an ellipsis, respecting card field caps."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _card_button(action_id: str, label: str, oid: str, *, style: Optional[str] = None) -> dict[str, Any]:
    """One card action button element (carrying the obligation id as its ``value``)."""
    btn: dict[str, Any] = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": label, "emoji": True},
        "value": oid,
    }
    if style:
        btn["style"] = style
    return btn


def _card_action_buttons(obligation: Obligation) -> list[dict[str, Any]]:
    """The (max 3) card action buttons for an active row.

    A card caps actions at three, so the row's four section-style actions collapse to
    the three highest-value ones — **Nudge** (primary), **Snooze**, **Dismiss**
    (danger, left-aligned by Slack). Delegate is omitted on the card surface; it
    remains available on the section layout and via the Assistant pane.
    """
    oid = obligation.obligation_id
    return [
        _card_button(ACTION_NUDGE, "Nudge", oid, style="primary"),
        _card_button(ACTION_SNOOZE, "Snooze", oid),
        _card_button(ACTION_DISMISS, "Dismiss", oid, style="danger"),
    ]


def _obligation_card(
    title: str,
    subtitle: str,
    person_id: Optional[PersonId],
    avatars: Optional[Mapping[str, str]],
    actions: Optional[list[dict[str, Any]]],
    *,
    body: Optional[str] = None,
    subtext: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble one ``card`` block: avatar icon + title + subtitle (+ body/subtext/actions).

    The avatar (when supplied) becomes the card ``icon``; ``title`` is a short label
    (ideally the person's name), ``subtitle`` the meta line, and the wrapping ``body``
    carries the full ask sentence (so it never truncates the way a long title would).
    All text fields are clipped to their card limits so an over-long value can't break
    the payload.
    """
    card: dict[str, Any] = {"type": "card"}
    icon = _avatar_accessory(person_id, avatars)
    if icon is not None:
        card["icon"] = icon
    card["title"] = {"type": "mrkdwn", "text": _truncate(title, CARD_TITLE_MAX)}
    card["subtitle"] = {"type": "mrkdwn", "text": _truncate(subtitle, CARD_SUBTITLE_MAX)}
    if body:
        card["body"] = {"type": "mrkdwn", "text": _truncate(body, CARD_BODY_MAX)}
    if subtext:
        card["subtext"] = {"type": "mrkdwn", "text": _truncate(subtext, CARD_SUBTEXT_MAX)}
    if actions:
        card["actions"] = actions
    return card


def _active_section_cards(
    title: str,
    rows: list[Obligation],
    empty_text: str,
    now: Optional[TimeLike],
    *,
    person_of,
    avatars: Optional[Mapping[str, str]] = None,
    names: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """Card variant of :func:`_active_section_blocks`: header then one card per row.

    Each obligation renders as a single :func:`_obligation_card`. When a display name is
    known the card leads with the **person's name** (title), the **age · channel** meta
    (subtitle), and the full ask in the wrapping **body** — so nothing truncates and the
    card reads cleanly. Without a name it falls back to the subject as the title and the
    ``@mention`` on the subtitle. Empty sections keep the explicit empty-state (Req 6.9).
    """
    blocks: list[dict[str, Any]] = [_header(title), {"type": "divider"}]
    if not rows:
        blocks.append(_mrkdwn_section(empty_text))
        return blocks
    for o in rows:
        chip = aging_chip(o, now)
        person = person_of(o)
        name = _display_name(person, names)
        if name:
            card_title = name
            card_body: Optional[str] = o.subject_summary
            subtitle = f"{_row_age_label(chip)}{_channel_suffix(o)}"
        else:
            card_title = o.subject_summary
            card_body = None
            subtitle = f"<@{person}> · {_row_age_label(chip)}{_channel_suffix(o)}"
        blocks.append(
            _obligation_card(
                card_title,
                subtitle,
                person,
                avatars,
                _card_action_buttons(o),
                body=card_body,
            )
        )
    return blocks


def _healed_section_cards(
    rows: list[Obligation],
    user_id: Optional[PersonId] = None,
    *,
    avatars: Optional[Mapping[str, str]] = None,
    names: Optional[Mapping[str, str]] = None,
) -> list[dict[str, Any]]:
    """Card variant of :func:`_healed_section_blocks`: header then one card per healed loop.

    When the resolved person's display name is known the card leads with it (title) and
    carries the closure reason in the wrapping body; otherwise the subject is the title.
    The native closure timestamp rides on the card ``subtext`` (Req 9.2, 9.5).
    """
    blocks: list[dict[str, Any]] = [_header(HEALED_SECTION_TITLE), {"type": "divider"}]
    if not rows:
        blocks.append(_mrkdwn_section(HEALED_EMPTY_TEXT))
        return blocks
    for o in rows:
        person = resolved_person_id(o, user_id)
        name = _display_name(person, names)
        reason = o.closure_reason or HEALED_REASON_PLACEHOLDER
        if name:
            card_title = name
            card_body: Optional[str] = f"Resolved — {reason}"
        else:
            card_title = o.subject_summary or "Resolved loop"
            card_body = _healed_row_text(o, user_id)
        blocks.append(
            _obligation_card(
                card_title,
                f"Auto-closed · {reason}" if name else _healed_row_text(o, user_id),
                person,
                avatars,
                None,
                body=card_body if name else None,
                subtext=_healed_subline(o),
            )
        )
    return blocks


def footer_text(blocked_count: int, waiting_count: int, healed_count: int) -> str:
    """The footer context line stating per-section totals (Req 6.6)."""
    return (
        f"Blocked on you: *{blocked_count}*  ·  "
        f"Waiting on others: *{waiting_count}*  ·  "
        f"Auto-closed: *{healed_count}*"
    )


def build_app_home_view(
    graph: ObligationGraph,
    now: Optional[TimeLike] = None,
    user_id: Optional[PersonId] = None,
    *,
    avatars: Optional[Mapping[str, str]] = None,
    names: Optional[Mapping[str, str]] = None,
    logo_url: str = "",
    style: str = "sections",
) -> dict[str, Any]:
    """Build the Loop App Home Block Kit ``home`` view (tasks 9.1–9.3, 11.1, Req 6, 9).

    Pure and deterministic given ``graph``, ``now``, and ``user_id``; performs no
    Slack network call (``views_publish`` is wired in task 17). Layout:

      1. **Hero banner** — count of surfaced ``blocked-on-you`` obligations, with
         zero-state messaging (Req 6.1, 6.7).
      2. **Blocked on you** — surfaced ``blocked-on-you``, oldest→newest, each
         row carrying a plain-text age label, an optional avatar accessory, and
         Nudge/Snooze/Delegate/Dismiss actions (Req 6.3, 6.5).
      3. **Waiting on others** — surfaced ``waiting-on-other``, oldest→newest
         (Req 6.4, 6.5).
      4. **Recently auto-closed** — non-dismissed autonomously-healed loops,
         newest→oldest by closure timestamp (ties → resolved person id ascending),
         capped at the 100 most-recent, each row showing the resolved person, closure
         timestamp, and reason with placeholders for any unavailable field
         (Req 6.2, 9.1–9.6).
      5. **Footer** — per-section totals (Req 6.6).

    Any section with zero qualifying items renders an explicit empty-state message
    (Req 6.9, 9.4).

    Args:
        graph: the shared Obligation Graph store (read-only here).
        now: reference instant as a ``datetime`` or ISO 8601 UTC string; defaults
            to the current UTC time when omitted.
        user_id: the tracked User's Slack id, used by the Auto-Healed feed to pick
            the *resolved person* as the non-user endpoint of a healed edge (Req 9.2).
            When omitted, the feed falls back to the owed person as the other party.
        avatars: optional ``person_id → https avatar URL`` map. When a row's person
            has an entry, that row's section renders a real image accessory; absent
            entries render with no accessory. Looked up only — never fetched here, so
            the builder stays pure and network-free.
        logo_url: optional https URL for a small leading Loop logo image block,
            rendered above the hero header. When empty (the default) no logo block is
            added, so block[0] stays the hero header.
        style: row rendering style — ``"sections"`` (default; the proven section +
            image-accessory + actions layout) or ``"cards"`` (the newer Block Kit
            ``card`` block: avatar icon + title + subtitle + ≤3 buttons per row). Cards
            are opt-in so the default render is unchanged until a live check confirms
            the ``card`` block renders in the target workspace.

    Returns:
        A Slack Block Kit home-view dict ``{"type": "home", "blocks": [...]}``.
    """
    blocked = blocked_on_you_rows(graph, now)
    waiting = waiting_on_other_rows(graph, now)
    healed = auto_healed_rows(graph, now, user_id)

    use_cards = style == "cards"

    blocks: list[dict[str, Any]] = []

    # 0. Optional leading Loop logo (config-driven). When no logo is configured the
    #    hero header stays block[0] (the zero-state/invariant the tests rely on).
    if logo_url:
        blocks.append(_logo_block(logo_url))

    # 1. Hero banner — the signature reveal (Req 6.1, 6.7).
    blocks.append(_header(hero_text(len(blocked))))
    # Hero card + scan stats beneath the header (count > 0 only; zero-state stays minimal).
    blocks.extend(_hero_blocks(len(blocked), len(blocked) + len(waiting) + len(healed), now))
    blocks.append({"type": "divider"})

    # 2. Blocked-On-You (Req 6.3, 6.5, 6.9).
    if use_cards:
        blocks.extend(
            _active_section_cards(
                BLOCKED_SECTION_TITLE,
                blocked,
                BLOCKED_EMPTY_TEXT,
                now,
                person_of=lambda o: o.owed_person_id,
                avatars=avatars,
                names=names,
            )
        )
    else:
        blocks.extend(
            _active_section_blocks(
                BLOCKED_SECTION_TITLE,
                blocked,
                BLOCKED_EMPTY_TEXT,
                now,
                row_text=_blocked_row_text,
                person_of=lambda o: o.owed_person_id,
                avatars=avatars,
            )
        )

    # 3. Waiting-On-Others (Req 6.4, 6.5, 6.9).
    if use_cards:
        blocks.extend(
            _active_section_cards(
                WAITING_SECTION_TITLE,
                waiting,
                WAITING_EMPTY_TEXT,
                now,
                person_of=lambda o: o.owes_person_id,
                avatars=avatars,
                names=names,
            )
        )
    else:
        blocks.extend(
            _active_section_blocks(
                WAITING_SECTION_TITLE,
                waiting,
                WAITING_EMPTY_TEXT,
                now,
                row_text=_waiting_row_text,
                person_of=lambda o: o.owes_person_id,
                avatars=avatars,
            )
        )

    # 4. Recently auto-closed (Req 6.2, 9.1–9.6; full feed — task 11.1).
    if use_cards:
        blocks.extend(_healed_section_cards(healed, user_id, avatars=avatars, names=names))
    else:
        blocks.extend(_healed_section_blocks(healed, user_id, avatars=avatars))

    # 5. Footer totals (Req 6.6).
    blocks.append({"type": "divider"})
    blocks.append(_context(footer_text(len(blocked), len(waiting), len(healed))))

    return {"type": "home", "blocks": blocks}


__all__ = [
    "AgingState",
    "AgingChip",
    "classify_aging",
    "aging_chip",
    "blocked_on_you_rows",
    "waiting_on_other_rows",
    "auto_healed_rows",
    "resolved_person_id",
    "hero_count",
    "hero_text",
    "hero_card_text",
    "hero_stats_text",
    "slack_date",
    "footer_text",
    "build_app_home_view",
    "build_app_home_compact_view",
    "scan_stats_text",
    "hero_zero_stats_text",
    "COMPACT_HEALED_INLINE_CAP",
    "CAROUSEL_CARD_CAP",
    "build_nudge_modal",
    "NUDGE_MODAL_CALLBACK",
    "NUDGE_MODAL_INPUT_BLOCK",
    "NUDGE_MODAL_INPUT_ACTION",
    "HERO_ZERO_TEXT",
    "HERO_SUBLINE_TEXT",
    "HERO_REVIEW_BUTTON_TEXT",
    "BLOCKED_SECTION_TITLE",
    "WAITING_SECTION_TITLE",
    "HEALED_SECTION_TITLE",
    "BLOCKED_EMPTY_TEXT",
    "WAITING_EMPTY_TEXT",
    "HEALED_EMPTY_TEXT",
    "HEALED_PERSON_PLACEHOLDER",
    "HEALED_REASON_PLACEHOLDER",
    "AUTO_HEALED_FEED_CAP",
    "ACTION_NUDGE",
    "ACTION_SNOOZE",
    "ACTION_DELEGATE",
    "ACTION_DISMISS",
    "ACTION_ROW_OVERFLOW",
    "ACTION_REVIEW_BLOCKED",
    "ACTION_QUICK_NUDGE",
    "AGING_WARNING_FLOOR_SECONDS",
    "AGING_OVERDUE_FLOOR_SECONDS",
]


# ---------------------------------------------------------------------------
# Compact "command-center" layout (Best-UX upgrade — scroll reduction).
#
# Problem: rendering every section as a tall `card` makes the App Home grow
# linearly and forces a long scroll as the org scales. This layout keeps all
# three required sections (Req 6.2) and still lists every obligation (Req 6.3/6.4,
# Req 9), but encodes priority as *size* so the page stays short and scannable:
#
#   * Blocked on you   → rich cards (the only actionable-now section)
#   * Waiting on others → compact one-line section rows (small avatar, no buttons)
#   * Recently auto-closed → tiny context lines (reference only, smallest footprint)
#
# It is a separate builder so the proven section/card builders stay the default
# until a live render check confirms this layout in the target workspace.
# ---------------------------------------------------------------------------
COMPACT_HEALED_INLINE_CAP = 5
# A carousel block holds at most 10 cards (Slack Block Kit limit). Beyond that the
# overflow is summarized with a "+N more" line.
CAROUSEL_CARD_CAP = 10


def _carousel(cards: list[dict[str, Any]], *, block_id: str) -> dict[str, Any]:
    """Wrap up to 10 ``card`` blocks in a horizontally-scrolling ``carousel`` block.

    The ``carousel`` block (surfaces: Messages + Home tabs) lays its cards out
    side-by-side in a swipeable row instead of stacking them vertically — so a
    section of cards reads as one compact, scannable strip rather than a long scroll.
    """
    return {"type": "carousel", "block_id": block_id, "elements": cards}


def _blocked_card(
    obligation: Obligation,
    now: Optional[TimeLike],
    *,
    avatars: Optional[Mapping[str, str]] = None,
    names: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Build one blocked-on-you ``card`` (name-led when known) for the carousel/stack."""
    chip = aging_chip(obligation, now)
    person = obligation.owed_person_id
    name = _display_name(person, names)
    if name:
        title, body, subtitle = name, obligation.subject_summary, f"{_row_age_label(chip)}{_channel_suffix(obligation)}"
    else:
        title, body, subtitle = (
            obligation.subject_summary,
            None,
            f"<@{person}> · {_row_age_label(chip)}{_channel_suffix(obligation)}",
        )
    return _obligation_card(title, subtitle, person, avatars, _card_action_buttons(obligation), body=body)


def scan_stats_text(channels: int, tracked: int, when: str) -> str:
    """The command-center scan-summary line: channels swept, loops tracked, updated.

    Tells the production-scale story in one quiet line — "Loop watched the whole
    org and surfaced only what matters" — without adding any vertical card weight.
    """
    chan_word = "channel" if channels == 1 else "channels"
    loop_word = "loop" if tracked == 1 else "loops"
    return f"Scanned {channels} {chan_word} · {tracked} {loop_word} tracked · updated {when}"


def hero_zero_stats_text(healed: int, waiting: int, channels: int) -> str:
    """The celebratory zero-state line shown when nobody is blocked on the user.

    The "inbox-zero" payoff: it rewards the empty court by surfacing the work Loop
    did *for* the user (loops it auto-closed) and what's still in flight (loops the
    user is waiting on others for), plus the scan scale — so an empty dashboard still
    proves the agent is working, rather than reading as a blank screen.
    """
    parts: list[str] = []
    if healed:
        parts.append(f"Loop auto-closed *{healed}* loop{'s' if healed != 1 else ''} for you")
    if waiting:
        parts.append(f"you're waiting on *{waiting}* other{'s' if waiting != 1 else ''}")
    parts.append(f"scanned {channels} channel{'s' if channels != 1 else ''}")
    return "🎉 " + " · ".join(parts)


def _distinct_channels(*groups: list[Obligation]) -> int:
    """Count distinct source channels across the rendered obligation groups."""
    channels: set[str] = set()
    for group in groups:
        for o in group:
            if o.source_msg_channel:
                channels.add(o.source_msg_channel)
    return len(channels)


def _section_label(title: str, count: int) -> dict[str, Any]:
    """A compact mrkdwn section header carrying the section name + live count.

    Used for the secondary (compact) sections so the section title and its size
    read on one line — lighter than a full `header` block.
    """
    return _mrkdwn_section(f"*{title}*  ·  {count}")


def _compact_waiting_row(
    obligation: Obligation,
    now: Optional[TimeLike],
    *,
    avatars: Optional[Mapping[str, str]] = None,
    names: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """One waiting-on-other loop as a compact context line with a small avatar.

    Rendered as a ``context`` block (small inline avatar thumbnail + mrkdwn text)
    rather than a ``section`` with an image *accessory* — a section accessory image
    is rendered large by Slack and can't be sized down, whereas a context image is a
    small thumbnail. The bold name keeps the row scannable: "*Name* — ask · age ·
    #channel". No inline buttons (the secondary section stays uncluttered).
    """
    person = obligation.owes_person_id
    name = _display_name(person, names)
    chip = aging_chip(obligation, now)
    meta = f"{_row_age_label(chip)}{_channel_suffix(obligation)}"
    if name:
        text = f"*{name}* — {obligation.subject_summary} · {meta}"
    else:
        text = f"*{obligation.subject_summary}* · <@{person}> · {meta}"
    elements: list[dict[str, Any]] = []
    if avatars and person and avatars.get(person):
        elements.append({"type": "image", "image_url": avatars[person], "alt_text": person})
    elements.append({"type": "mrkdwn", "text": text})
    return {"type": "context", "elements": elements}


def _compact_healed_context(
    obligation: Obligation,
    user_id: Optional[PersonId] = None,
    *,
    avatars: Optional[Mapping[str, str]] = None,
    names: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """One auto-closed loop as a tiny context line (avatar thumbnail + text).

    The smallest-footprint row: a context block with the resolved person's avatar
    thumbnail followed by "*Name* — reason · when". Reference information, so it
    carries no buttons and the least vertical weight of any row.
    """
    person = resolved_person_id(obligation, user_id)
    name = _display_name(person, names) or (f"<@{person}>" if person else HEALED_PERSON_PLACEHOLDER)
    reason = obligation.closure_reason or HEALED_REASON_PLACEHOLDER
    when = obligation.closure_timestamp or obligation.last_touch_timestamp
    elements: list[dict[str, Any]] = []
    if avatars and person and avatars.get(person):
        elements.append({"type": "image", "image_url": avatars[person], "alt_text": person})
    elements.append(
        {"type": "mrkdwn", "text": f"*{name}* — {reason} · {slack_date(when)}"}
    )
    return {"type": "context", "elements": elements}


def _quick_nudge_block(
    blocked: list[Obligation], names: Optional[Mapping[str, str]] = None
) -> dict[str, Any]:
    """A hero dropdown to nudge any of the people blocked on you (shown when >1).

    Renders a ``static_select`` whose options are the blocked-on-you loops (label =
    person name + ask, value = obligation id). Selecting one fires
    :data:`ACTION_QUICK_NUDGE`, whose handler opens the Nudge composer modal for that
    loop — a fast "nudge anyone" entry point that complements the per-card buttons.
    """
    options: list[dict[str, Any]] = []
    for o in blocked:
        name = _display_name(o.owed_person_id, names) or o.subject_summary
        label = _truncate(f"{name} — {o.subject_summary}", 75)
        options.append(
            {"text": {"type": "plain_text", "text": label, "emoji": True}, "value": o.obligation_id}
        )
    return {
        "type": "actions",
        "block_id": "quick_nudge",
        "elements": [
            {
                "type": "static_select",
                "action_id": ACTION_QUICK_NUDGE,
                "placeholder": {
                    "type": "plain_text",
                    "text": "Nudge someone who's waiting…",
                    "emoji": True,
                },
                "options": options[:100],
            }
        ],
    }


def build_app_home_compact_view(
    graph: ObligationGraph,
    now: Optional[TimeLike] = None,
    user_id: Optional[PersonId] = None,
    *,
    avatars: Optional[Mapping[str, str]] = None,
    names: Optional[Mapping[str, str]] = None,
    logo_url: str = "",
    healed_inline_cap: int = COMPACT_HEALED_INLINE_CAP,
) -> dict[str, Any]:
    """Build the compact "command-center" App Home view (Best-UX scroll reduction).

    Same three required sections and same surfaced/feed sets as
    :func:`build_app_home_view`, but with a priority-encoded visual hierarchy so the
    page stays short as the org scales:

      1. **Hero** — the signature blocked-on-you count (Req 6.1, 6.7) + a one-line
         scan-summary that tells the production-scale story (channels swept, loops
         tracked) without adding card weight.
      2. **Blocked on you** — rich cards (the only actionable-now section), every
         qualifying loop, oldest→newest (Req 6.3).
      3. **Waiting on others** — compact one-line rows with a small avatar, every
         qualifying loop, oldest→newest (Req 6.4).
      4. **Recently auto-closed** — tiny context lines, newest→oldest; the inline
         list is capped at ``healed_inline_cap`` with a "+N more" line so the
         reference feed never dominates the page (the full feed remains available
         via the Assistant pane / a future "view all" modal).
      5. **Footer** — per-section totals (Req 6.6).

    Returns a Block Kit ``{"type": "home", "blocks": [...]}`` dict; pure and
    network-free like the default builder.
    """
    blocked = blocked_on_you_rows(graph, now)
    waiting = waiting_on_other_rows(graph, now)
    healed = auto_healed_rows(graph, now, user_id)

    blocks: list[dict[str, Any]] = []
    if logo_url:
        blocks.append(_logo_block(logo_url))

    # 1. Hero + scan-summary (the production-scale story, one quiet line).
    blocks.append(_header(hero_text(len(blocked))))
    when = slack_date(_now_iso(now))
    channels = _distinct_channels(blocked, waiting, healed)
    tracked = len(blocked) + len(waiting) + len(healed)
    if not blocked:
        # Zero-state: reward the empty court with a celebratory stats line instead
        # of the plain scan-summary, so an empty dashboard still proves Loop worked.
        blocks.append(_context(hero_zero_stats_text(len(healed), len(waiting), channels)))
    else:
        blocks.append(_context(scan_stats_text(channels, tracked, when)))
    blocks.append({"type": "divider"})

    # 2. Blocked on you — horizontal carousel of rich cards (the actionable focus).
    blocks.append(_header(BLOCKED_SECTION_TITLE))
    if not blocked:
        blocks.append(_mrkdwn_section(BLOCKED_EMPTY_TEXT))
    else:
        # When several people are waiting, offer a "nudge anyone" dropdown above the
        # carousel so the user can jump straight into the composer for any of them.
        if len(blocked) >= 2:
            blocks.append(_quick_nudge_block(blocked, names))
        cards = [
            _blocked_card(o, now, avatars=avatars, names=names)
            for o in blocked[:CAROUSEL_CARD_CAP]
        ]
        blocks.append(_carousel(cards, block_id="blocked_carousel"))
        extra = len(blocked) - len(cards)
        if extra > 0:
            blocks.append(_context(f"_+{extra} more blocked on you_"))

    # 3. Waiting on others — compact one-line rows.
    blocks.append({"type": "divider"})
    blocks.append(_section_label(WAITING_SECTION_TITLE, len(waiting)))
    if not waiting:
        blocks.append(_context(WAITING_EMPTY_TEXT))
    else:
        for o in waiting:
            blocks.append(_compact_waiting_row(o, now, avatars=avatars, names=names))

    # 4. Recently auto-closed — tiny context lines, capped inline.
    blocks.append({"type": "divider"})
    blocks.append(_section_label(HEALED_SECTION_TITLE, len(healed)))
    if not healed:
        blocks.append(_context(HEALED_EMPTY_TEXT))
    else:
        shown = healed[:healed_inline_cap]
        for o in shown:
            blocks.append(_compact_healed_context(o, user_id, avatars=avatars, names=names))
        remaining = len(healed) - len(shown)
        if remaining > 0:
            blocks.append(_context(f"_+{remaining} more auto-closed_"))

    # 5. Footer totals (Req 6.6).
    blocks.append({"type": "divider"})
    blocks.append(_context(footer_text(len(blocked), len(waiting), len(healed))))

    return {"type": "home", "blocks": blocks}


# ---------------------------------------------------------------------------
# Nudge composer modal (Best-UX flourish — the AI-drafted "send as you" loop).
#
# Tapping Nudge opens this modal: Loop shows the message it drafted in the user's
# voice, pre-filled and *editable*, with a "Send as you" submit. It turns a blind
# one-tap into a transparent, controllable agent interaction — the user sees and
# approves exactly what will be posted as them.
# ---------------------------------------------------------------------------
NUDGE_MODAL_CALLBACK = "loop_nudge_modal"
NUDGE_MODAL_INPUT_BLOCK = "nudge_message"
NUDGE_MODAL_INPUT_ACTION = "nudge_message_input"


def build_nudge_modal(
    obligation: Obligation,
    draft_text: str,
    *,
    names: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Build the Nudge composer ``modal`` view (the editable AI-drafted nudge).

    The modal leads with who's waiting and the loop subject, then an editable
    multiline input pre-filled with the AI draft, and a context line stating the
    message will be sent **as the user** in the source channel. The obligation id
    rides on ``private_metadata`` so the submission handler can recover it.
    """
    person = obligation.owed_person_id
    name = _display_name(person, names)
    who = name or (f"<@{person}>" if person else "Someone")

    blocks: list[dict[str, Any]] = [
        _mrkdwn_section(f"*{who}* is waiting on you:\n{obligation.subject_summary}"),
        {
            "type": "input",
            "block_id": NUDGE_MODAL_INPUT_BLOCK,
            "label": {"type": "plain_text", "text": "Your message", "emoji": True},
            "element": {
                "type": "plain_text_input",
                "action_id": NUDGE_MODAL_INPUT_ACTION,
                "multiline": True,
                "initial_value": draft_text,
            },
            "hint": {
                "type": "plain_text",
                "text": "Loop drafted this for you — edit it however you like.",
                "emoji": True,
            },
        },
    ]
    if obligation.source_msg_channel:
        sends = f"Loop will send this *as you* · in <#{obligation.source_msg_channel}>"
    else:
        sends = "Loop will send this *as you*."
    blocks.append(_context(sends))

    return {
        "type": "modal",
        "callback_id": NUDGE_MODAL_CALLBACK,
        "private_metadata": obligation.obligation_id,
        "title": {"type": "plain_text", "text": "Send a nudge", "emoji": True},
        "submit": {"type": "plain_text", "text": "Send as you", "emoji": True},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": True},
        "blocks": blocks,
    }
