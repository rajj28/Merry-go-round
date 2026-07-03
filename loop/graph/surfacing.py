"""The single source of truth for *"is this obligation shown?"* (task 2.3).

This module freezes the **one** pure surfacing predicate that every Loop surface
shares — App Home (task 9), the Daily Digest (task 14), and the Assistant pane
(task 15) all import :func:`is_surfaced` rather than reimplementing the gate.
Enforcing the quiet-by-default and privacy guarantees in exactly one place is the
whole point of this freeze (design.md → "Surfacing predicate (single source of
truth for 'is this shown?')").

It is the authoritative realization of:
  * Req 3.5 — below-threshold obligations stay unsurfaced (quiet by default).
  * Req 3.6 — at/above-threshold obligations are eligible to surface.
  * Req 14.1 — surface only ``confidence_score >= threshold`` everywhere; keep
    strictly-below unsurfaced across App Home, Daily Digest, and Assistant pane.

It also folds in the surfacing-control invariants that ride on the same predicate
so no surface can leak a hidden obligation:
  * Req 5.3 / 14.3 / 14.5 — a ``dismissed`` obligation is never surfaced anywhere.
  * Req 13.3 / 13.4 — a snoozed obligation is hidden while ``now < snoozed_until``
    and resumes automatically once that instant passes.
  * Only the two *active* loop states (``blocked-on-you``, ``waiting-on-other``)
    surface on the active dashboard; ``healed`` never does.

The Auto-Healed feed is the **one documented exception** to this predicate. It is
deliberately a *separate* function (:func:`is_in_auto_healed_feed`) so the active
surface and the healed feed can never be confused: see Req 9.1.

Purity discipline (read before editing): every function here is a **pure**
function of its arguments — no DB, no I/O, no clock reads, no globals. Callers
pass ``now`` and ``threshold`` in. This keeps the predicate trivially testable
(tasks 6.3/6.4 add the property tests) and identical across all surfaces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Union

from loop.graph.models import ClosureKind, LoopState, Obligation

# The two *active* loop states that may appear on the active surface. ``healed``
# is intentionally excluded — healed obligations live only in the Auto-Healed
# feed (see :func:`is_in_auto_healed_feed`).
ACTIVE_SURFACE_STATES: frozenset[LoopState] = frozenset(
    {LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER}
)

# Accepted shapes for the ``now`` / timestamp arguments: either a real
# ``datetime`` or an ISO 8601 string (the format every ``*_timestamp`` field on
# the model uses, e.g. ``"2025-01-08T12:00:00+00:00"``).
TimeLike = Union[datetime, str]


def _to_utc_datetime(value: TimeLike) -> datetime:
    """Coerce an ISO 8601 string or ``datetime`` into a timezone-aware UTC
    ``datetime`` so timestamps compare correctly regardless of input form.

    * A ``datetime`` that is naive (no tzinfo) is assumed to already be UTC — the
      model documents every timestamp as ISO 8601 *UTC*.
    * An ISO 8601 string is parsed with :meth:`datetime.fromisoformat`; a bare
      trailing ``Z`` (Zulu/UTC) is normalized to ``+00:00`` first, since
      ``fromisoformat`` on older interpreters does not accept ``Z`` directly.
    * The result is always converted to UTC so two instants from different source
      offsets order correctly.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)

    if dt.tzinfo is None:
        # Naive timestamp: treat as UTC per the model's documented convention.
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_snooze_elapsed(snoozed_until: Union[str, None], now: TimeLike) -> bool:
    """Snooze clause: ``snoozed_until is None OR now >= snoozed_until``.

    Returns ``True`` when the obligation is *not* (or no longer) snoozed and so is
    free to surface. The boundary ``now == snoozed_until`` counts as elapsed, so
    surfacing resumes exactly at the snooze instant (Req 13.3, 13.4).
    """
    if snoozed_until is None:
        return True
    return _to_utc_datetime(now) >= _to_utc_datetime(snoozed_until)


def is_surfaced(obligation: Obligation, threshold: float, now: TimeLike) -> bool:
    """Return whether ``obligation`` is shown on the **active** surface right now.

    This is the single shared gate behind App Home, the Daily Digest, and the
    Assistant pane. It is the logical conjunction of four clauses, each tied to a
    specific requirement::

        is_surfaced(o, threshold, now) :=
              not o.dismissed                                    # Req 5.3 / 14.3 / 14.5
          AND (o.snoozed_until is None OR now >= o.snoozed_until) # Req 13.3 / 13.4
          AND o.confidence_score >= threshold                    # Req 3.5 / 3.6 / 14.1
          AND o.loop_state in {blocked-on-you, waiting-on-other}  # active states only

    Boundary semantics:
      * ``confidence_score == threshold`` surfaces (the gate is *inclusive*,
        ``>=``) — Req 3.6 / 14.1.
      * ``now == snoozed_until`` surfaces (snooze has elapsed) — Req 13.3 / 13.4.
      * ``healed`` never surfaces here; it appears only in the Auto-Healed feed.

    Args:
        obligation: the obligation to test (pure value object; not mutated).
        threshold: the current Confidence_Threshold, inclusive gate.
        now: the reference instant, as a ``datetime`` or ISO 8601 UTC string.

    Returns:
        ``True`` iff every clause holds.
    """
    # Clause 1 — never surface a dismissed obligation (Req 5.3, 14.3, 14.5).
    if obligation.dismissed:
        return False

    # Clause 2 — hidden while snoozed; resumes at/after snoozed_until (Req 13.3/13.4).
    if not _is_snooze_elapsed(obligation.snoozed_until, now):
        return False

    # Clause 3 — quiet by default: inclusive confidence gate (Req 3.5, 3.6, 14.1).
    if obligation.confidence_score < threshold:
        return False

    # Clause 4 — only the two active loop states surface here; healed never does.
    if obligation.loop_state not in ACTIVE_SURFACE_STATES:
        return False

    return True


def is_in_auto_healed_feed(obligation: Obligation) -> bool:
    """Return whether ``obligation`` belongs in the **Auto-Healed feed**.

    This is the *one documented exception* to :func:`is_surfaced`: the Auto-Healed
    feed shows resolved loops, not active ones. Per Req 9.1 it includes exactly the
    obligations that are::

        loop_state == healed AND closure_kind == autonomous AND not dismissed

    Manual closures are excluded (only Verifier-confirmed autonomous merges appear),
    and a dismissed obligation is never surfaced anywhere — including this feed
    (Req 5.3, 14.5). This predicate is independent of confidence, threshold, and
    snooze, which govern only the active surface.

    Args:
        obligation: the obligation to test (pure value object; not mutated).

    Returns:
        ``True`` iff the obligation is a non-dismissed, autonomously healed loop.
    """
    return (
        obligation.loop_state == LoopState.HEALED
        and obligation.closure_kind == ClosureKind.AUTONOMOUS
        and not obligation.dismissed
    )


__all__ = [
    "ACTIVE_SURFACE_STATES",
    "TimeLike",
    "is_surfaced",
    "is_in_auto_healed_feed",
]
