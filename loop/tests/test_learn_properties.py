"""Property-based tests for the Learn loop and the surfacing predicate (tasks 6.3–6.5).

These exercise the *real* surfacing predicate
(:func:`loop.graph.surfacing.is_surfaced`) and the *real* Learn write path
(:class:`loop.learn.feedback.LearnEngine` against an isolated ``:memory:`` store)
against the universal correctness properties from design.md → "Correctness
Properties":

  * 6.3 → Property 10 (SAFETY) — surfacing predicate, quiet by default
    (Req 3.5, 3.6, 11.2, 14.1)
  * 6.4 → Property 11 (SAFETY) — dismissed obligations are never surfaced
    (Req 5.3, 14.3, 14.5)
  * 6.5 → Property 12 — threshold tuning is bounded and clamped (Req 5.4, 5.5)

Each property test runs ≥100 generated examples (enforced by the root
``conftest.py``) and carries the required traceability tag.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from hypothesis import given
from hypothesis import strategies as st

from loop.graph.models import LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.surfacing import ACTIVE_SURFACE_STATES, is_surfaced
from loop.learn.feedback import THRESHOLD_STEP, LearnEngine
from loop.graph.models import FeedbackPolarity


# ---------------------------------------------------------------------------
# Helpers and custom Hypothesis strategies
# ---------------------------------------------------------------------------
def _graph() -> SqliteObligationGraph:
    """A fresh, isolated in-memory store (matches the existing test suites)."""
    return SqliteObligationGraph(database_path=IN_MEMORY)


# Slack-style identifiers: short, non-empty, simple.
_IDS = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=12,
)

# Printable-ASCII text keeps generated summaries free of NULs/surrogates that
# SQLite text storage rejects, without weakening logic coverage.
_SAFE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=40,
)

# Confidence + threshold values across the full inclusive range, boundaries
# included. Drawn so equal/near-equal values recur and exercise the inclusive
# ``>=`` boundary of the surfacing gate.
_UNIT_FLOAT = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)


def _bounded_datetimes() -> st.SearchStrategy[datetime]:
    """UTC datetimes drawn from a narrow window so ``now`` and ``snoozed_until``
    straddle each other often, exercising both sides of the snooze boundary."""
    return st.datetimes(
        min_value=datetime(2025, 1, 1, 0, 0, 0),
        max_value=datetime(2025, 1, 5, 0, 0, 0),
        timezones=st.just(timezone.utc),
    )


@st.composite
def obligations(draw: st.DrawFn, *, obligation_id: str | None = None) -> Obligation:
    """A valid :class:`Obligation` with randomized but in-contract fields.

    Covers: every loop_state (incl. ``healed``), in-range confidence (incl.
    boundaries), the dismissed flag, and an optional snooze instant — exactly the
    inputs the surfacing predicate gates on.
    """
    oid = obligation_id if obligation_id is not None else draw(_IDS)
    snoozed = draw(st.one_of(st.none(), _bounded_datetimes().map(lambda d: d.isoformat())))
    return Obligation(
        obligation_id=oid,
        owes_person_id=draw(_IDS),
        owed_person_id=draw(_IDS),
        owner_person_id=draw(_IDS),
        loop_state=draw(st.sampled_from(list(LoopState))),
        confidence_score=draw(_UNIT_FLOAT),
        last_touch_timestamp=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat(),
        source_msg_channel=draw(_IDS),
        source_msg_ts=draw(_SAFE_TEXT),
        subject_summary=draw(_SAFE_TEXT),
        dismissed=draw(st.booleans()),
        snoozed_until=snoozed,
    )


def _snooze_elapsed(snoozed_until: str | None, now_dt: datetime) -> bool:
    """Reference snooze clause: not snoozed, or now is at/after the snooze instant."""
    if snoozed_until is None:
        return True
    return now_dt >= datetime.fromisoformat(snoozed_until)


# ---------------------------------------------------------------------------
# Property 10 (task 6.3, SAFETY) — Surfacing predicate, quiet by default
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 10: Surfacing predicate — quiet by default
@given(
    obligation=obligations(),
    threshold=_UNIT_FLOAT,
    now=_bounded_datetimes(),
)
def test_property_10_surfacing_predicate_quiet_by_default(
    obligation: Obligation, threshold: float, now: datetime
) -> None:
    """Validates: Requirements 3.5, 3.6, 11.2, 14.1.

    For any obligation and threshold, ``is_surfaced`` is True **if and only if**
    the obligation is not dismissed, not currently snoozed, has an active
    loop_state, and has confidence at or above the threshold.
    """
    now_iso = now.isoformat()
    actual = is_surfaced(obligation, threshold, now_iso)

    expected = (
        not obligation.dismissed
        and _snooze_elapsed(obligation.snoozed_until, now)
        and obligation.confidence_score >= threshold
        and obligation.loop_state in ACTIVE_SURFACE_STATES
    )

    assert actual is expected


# ---------------------------------------------------------------------------
# Property 11 (task 6.4, SAFETY) — Dismissed obligations are never surfaced
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 11: Dismissed obligations are never surfaced
@given(
    obligation=obligations(),
    threshold=_UNIT_FLOAT,
    now=_bounded_datetimes(),
)
def test_property_11_dismissed_obligations_never_surface(
    obligation: Obligation, threshold: float, now: datetime
) -> None:
    """Validates: Requirements 5.3, 14.3, 14.5.

    For any obligation forced into the dismissed state — regardless of its
    confidence, loop_state, snooze, or the threshold — the shared surfacing
    predicate (the single gate behind App Home, the Daily Digest, and the
    Assistant pane) returns False.
    """
    dismissed = Obligation(**{**obligation.model_dump(), "dismissed": True})

    assert is_surfaced(dismissed, threshold, now.isoformat()) is False


# ---------------------------------------------------------------------------
# Property 12 (task 6.5) — Threshold tuning is bounded and clamped
# ---------------------------------------------------------------------------
def _seed_obligation() -> Obligation:
    """A single high-confidence obligation to attach feedback to."""
    return Obligation(
        obligation_id="OBL",
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.9,
        last_touch_timestamp=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc).isoformat(),
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary="needs a review",
    )


# Feature: loop-obligation-agent, Property 12: Threshold tuning is bounded and clamped
@given(
    start_threshold=_UNIT_FLOAT,
    polarity=st.sampled_from(list(FeedbackPolarity)),
)
def test_property_12_threshold_tuning_bounded_and_clamped(
    start_threshold: float, polarity: FeedbackPolarity
) -> None:
    """Validates: Requirements 5.4, 5.5.

    Driving a single feedback event through the Learn engine: positive feedback
    decreases the threshold by at most :data:`THRESHOLD_STEP` (0.05), negative
    increases it by at most that, and the result always stays within [0.0, 1.0].
    """
    graph = _graph()
    set_result = graph.set_threshold(start_threshold)
    # The store clamps; read back the actual starting threshold it persisted.
    before = graph.get_threshold()
    graph.upsert(_seed_obligation())
    engine = LearnEngine(graph)

    if polarity is FeedbackPolarity.POSITIVE:
        result = engine.record_confirm("OBL")
    else:
        result = engine.record_dismiss("OBL")

    assert result.saved is True
    after = graph.get_threshold()

    # Bound 1: result stays within the inclusive [0.0, 1.0] range.
    assert 0.0 <= after <= 1.0

    # Bound 2: the move is in the correct direction and at most one step.
    tol = 1e-9
    if polarity is FeedbackPolarity.POSITIVE:
        assert after <= before + tol                      # never increases
        assert before - after <= THRESHOLD_STEP + tol     # by at most one step
        expected = max(0.0, before - THRESHOLD_STEP)       # clamped target
    else:
        assert after >= before - tol                      # never decreases
        assert after - before <= THRESHOLD_STEP + tol     # by at most one step
        expected = min(1.0, before + THRESHOLD_STEP)       # clamped target

    assert math.isclose(after, expected, abs_tol=tol)
