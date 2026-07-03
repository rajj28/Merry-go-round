"""Confirm/Dismiss feedback recording — the Learn loop's write half (task 6.1).

This module implements the :class:`LearnEngine`, the single home for the
Confirm/Dismiss feedback path that closes Loop's autonomous cycle
(design.md → "Learn"). It is the authoritative realization of:

  * Req 5.1 — confirming a surfaced Obligation records **positive** feedback
    against it (within 2s; the operation is a single local DB append).
  * Req 5.2 / 14.3 — dismissing a surfaced Obligation sets the Obligation to the
    ``dismissed`` state **and** records **negative** feedback against it.
  * Req 5.3 / 14.5 — a dismissed Obligation is thereafter never surfaced. This is
    not re-enforced here; the shared :func:`loop.graph.surfacing.is_surfaced`
    predicate already gates ``dismissed`` out of every surface. :meth:`is_surfaced`
    on this engine is a thin convenience that re-uses that one predicate so the
    guarantee lives in exactly one place.
  * Req 14.4 — the user may dismiss **any** Obligation; :meth:`record_dismiss`
    accepts any stored obligation id with no surfaced/eligibility precondition.
  * Req 5.7 / 14.7 — if recording the feedback event **or** updating the dismissed
    state fails, the prior Obligation state and surfacing eligibility are retained,
    the Confidence_Threshold is left unchanged, and the returned
    :class:`FeedbackResult` reports ``saved == False`` so the caller can tell the
    user the feedback was not saved.

Scope discipline (read before editing): task 6.1 is **recording + dismiss +
failure handling only**. Bounded, clamped Confidence_Threshold tuning is task 6.2
(Req 5.4–5.6). The seam for it is :meth:`_tune_threshold`, a deliberate no-op that
6.2 fills in; nothing here reads or writes the threshold, so "leave the threshold
unchanged on failure" (Req 5.7) holds trivially today and keeps holding once 6.2
slots its tuning into the seam on the **success** paths only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from loop.graph.models import (
    FeedbackEvent,
    FeedbackPolarity,
    Obligation,
    ObligationId,
    utc_now_iso,
)
from loop.graph.sqlite_store import SqliteObligationGraph
from loop.graph.store import Err
from loop.graph.surfacing import TimeLike, is_surfaced


@dataclass(frozen=True)
class FeedbackResult:
    """Outcome of a confirm/dismiss feedback operation.

    ``saved`` is the single flag the caller checks: when ``False`` the feedback was
    **not** persisted, the Obligation's prior state and surfacing eligibility are
    intact, the threshold is unchanged, and ``message`` explains what to tell the
    user (Req 5.7, 14.7). On success ``event`` carries the persisted
    :class:`FeedbackEvent`.
    """

    saved: bool
    message: str = ""
    event: Optional[FeedbackEvent] = None


# User-facing copy for the "not saved" paths (Req 5.7, 14.7). Kept as constants so
# the surfaces and tests share one source of truth.
_NOT_FOUND_MSG = "That loop no longer exists, so nothing was changed."
_CONFIRM_NOT_SAVED_MSG = "Your confirmation could not be saved. Please try again."
_DISMISS_NOT_SAVED_MSG = "Your dismissal could not be saved. Please try again."

# The maximum amount the Confidence_Threshold moves per feedback event (Req 5.4,
# 5.5): positive feedback nudges it *down* by this much, negative *up* by this
# much. Kept as a single module constant so the bound lives in exactly one place.
THRESHOLD_STEP = 0.05


class LearnEngine:
    """Records Confirm/Dismiss feedback and applies dismissals (Learn, task 6.1).

    The engine owns the *write* side of the Learn loop. It is intentionally thin:
    it composes the already-frozen store primitives (``get``/``upsert`` for state,
    ``add_feedback`` for the feedback log) and the shared surfacing predicate,
    rather than re-implementing any of those semantics.

    Args:
        store: the shared :class:`SqliteObligationGraph`. The engine relies on the
            store's ``upsert`` retaining the prior value on failure (Req 1.8) to
            give the dismiss path its all-or-nothing guarantee.
    """

    def __init__(self, store: SqliteObligationGraph) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Confirm (Req 5.1)
    # ------------------------------------------------------------------
    def record_confirm(self, obligation_id: ObligationId) -> FeedbackResult:
        """Record **positive** feedback for ``obligation_id`` (Req 5.1).

        Confirming does not change Obligation state — it only appends a positive
        :class:`FeedbackEvent`. If the obligation is unknown, or the append fails,
        a ``not saved`` :class:`FeedbackResult` is returned and nothing is changed
        (Req 5.7). On success the Confidence_Threshold tuning seam (task 6.2) is
        invoked; today that is a no-op so the threshold is left unchanged.
        """
        if self._store.get(obligation_id) is None:
            return FeedbackResult(saved=False, message=_NOT_FOUND_MSG)

        result = self._store.add_feedback(
            self._make_event(obligation_id, FeedbackPolarity.POSITIVE)
        )
        if isinstance(result, Err):
            return FeedbackResult(saved=False, message=_CONFIRM_NOT_SAVED_MSG)

        # Seam for task 6.2: positive feedback nudges the threshold down (≤0.05).
        self._tune_threshold(FeedbackPolarity.POSITIVE)
        return FeedbackResult(saved=True, event=result.value)

    # ------------------------------------------------------------------
    # Dismiss (Req 5.2, 5.3, 14.3, 14.4, 14.5)
    # ------------------------------------------------------------------
    def record_dismiss(self, obligation_id: ObligationId) -> FeedbackResult:
        """Dismiss ``obligation_id`` and record **negative** feedback (Req 5.2).

        The user may dismiss **any** stored obligation (Req 14.4) — there is no
        surfaced/eligibility precondition. The operation is all-or-nothing so the
        failure contract (Req 5.7, 14.7) holds:

          1. Set ``dismissed = True`` via ``upsert``. If that write fails, the store
             retains the prior value (Req 1.8); we return ``not saved`` having
             changed nothing, so the obligation keeps surfacing (Req 14.7).
          2. Append the negative :class:`FeedbackEvent`. If that append fails, we
             **roll the dismissal back** to the obligation's prior ``dismissed``
             value so its state and surfacing eligibility are exactly as before, then
             return ``not saved`` (Req 5.7, 14.7).

        Once both succeed the obligation is dismissed and, via the shared
        :func:`is_surfaced` predicate, is never surfaced again (Req 5.3, 14.5).
        ``last_touch_timestamp`` is intentionally left unchanged — dismissing is not
        a "touch" that should affect aging; the store's last-write-wins accepts the
        same-timestamp update as the later-received write (Req 1.10).
        """
        obligation = self._store.get(obligation_id)
        if obligation is None:
            return FeedbackResult(saved=False, message=_NOT_FOUND_MSG)

        prior_dismissed = obligation.dismissed

        # Step 1 — set the dismissed state.
        dismiss_result = self._store.upsert(self._with_dismissed(obligation, True))
        if isinstance(dismiss_result, Err):
            return FeedbackResult(saved=False, message=_DISMISS_NOT_SAVED_MSG)

        # Step 2 — record the negative feedback; roll back the dismissal on failure.
        feedback_result = self._store.add_feedback(
            self._make_event(obligation_id, FeedbackPolarity.NEGATIVE)
        )
        if isinstance(feedback_result, Err):
            # Best-effort restore of the prior dismissed state so surfacing
            # eligibility is exactly as it was before this call (Req 5.7, 14.7).
            self._store.upsert(self._with_dismissed(obligation, prior_dismissed))
            return FeedbackResult(saved=False, message=_DISMISS_NOT_SAVED_MSG)

        # Seam for task 6.2: negative feedback nudges the threshold up (≤0.05).
        self._tune_threshold(FeedbackPolarity.NEGATIVE)
        return FeedbackResult(saved=True, event=feedback_result.value)

    # ------------------------------------------------------------------
    # Surfacing convenience (re-uses the single shared predicate — Req 5.3/14.5)
    # ------------------------------------------------------------------
    def is_surfaced(self, obligation_id: ObligationId, now: TimeLike) -> bool:
        """Whether ``obligation_id`` currently surfaces, per the shared predicate.

        A thin convenience over :func:`loop.graph.surfacing.is_surfaced` against the
        store's current threshold. It exists so callers (and tests) can verify the
        "dismissed obligations never surface" guarantee (Req 5.3, 14.5) without
        re-deriving the gate. An unknown obligation never surfaces.
        """
        obligation = self._store.get(obligation_id)
        if obligation is None:
            return False
        return is_surfaced(obligation, self._store.get_threshold(), now)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _tune_threshold(self, polarity: FeedbackPolarity) -> None:
        """Bounded, clamped Confidence_Threshold tuning (task 6.2, Req 5.4–5.6).

        Implements the Learn loop's threshold adjustment:

          * Positive feedback (a confirm) **decreases** the threshold by
            :data:`THRESHOLD_STEP` (≤0.05), so obligations similar to the confirmed
            one become more likely to surface (Req 5.4).
          * Negative feedback (a dismiss) **increases** the threshold by
            :data:`THRESHOLD_STEP` (≤0.05), so similar obligations become less
            likely to surface (Req 5.5).

        The result is always clamped to the inclusive ``[0.0, 1.0]`` range. The
        clamp is delegated to the store's :meth:`set_threshold`, which is the
        authoritative clamp (Req 5.6), so the persisted threshold can never leave
        the range no matter how many same-polarity events accumulate. Because the
        new threshold is persisted, it applies to every subsequent surfacing
        decision (Req 5.6).

        Invoked only on the **success** paths of :meth:`record_confirm` /
        :meth:`record_dismiss`, so the "leave the threshold unchanged on failure"
        guarantee (Req 5.7) is preserved — a failed feedback recording never reaches
        this method.
        """
        current = self._store.get_threshold()
        if polarity is FeedbackPolarity.POSITIVE:
            target = current - THRESHOLD_STEP
        else:
            target = current + THRESHOLD_STEP
        # The store clamps to [0.0, 1.0] (Req 5.6) and persists the new value so it
        # governs all subsequent surfacing decisions.
        self._store.set_threshold(target)

    @staticmethod
    def _make_event(
        obligation_id: ObligationId, polarity: FeedbackPolarity
    ) -> FeedbackEvent:
        """Build a fresh :class:`FeedbackEvent` with a unique id and UTC timestamp."""
        return FeedbackEvent(
            event_id=str(uuid.uuid4()),
            obligation_id=obligation_id,
            polarity=polarity,
            created_at=utc_now_iso(),
        )

    @staticmethod
    def _with_dismissed(obligation: Obligation, dismissed: bool) -> Obligation:
        """Return a copy of ``obligation`` with ``dismissed`` set as given.

        ``last_touch_timestamp`` is preserved so dismissing does not alter aging;
        the store's last-write-wins treats the same-timestamp write as the
        later-received one and applies it (Req 1.10).
        """
        data = obligation.model_dump()
        data["dismissed"] = dismissed
        return Obligation(**data)


__all__ = ["FeedbackResult", "LearnEngine", "THRESHOLD_STEP"]
