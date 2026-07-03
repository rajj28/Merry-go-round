"""Example-based unit tests for the single surfacing predicate (task 2.3).

These exercise the truth table of :func:`loop.graph.surfacing.is_surfaced` — each
of the four clauses is flipped in isolation to confirm it alone can change the
result — plus the documented boundary cases (confidence exactly at threshold,
``now`` exactly equal to ``snoozed_until``, healed never on the active surface but
present in the Auto-Healed feed when autonomous) and the ISO-string/``datetime``
robustness of the ``now`` argument.

Property-based coverage of the predicate comes later in tasks 6.3/6.4; this file
is deliberately example-based per task 2.3.

Requirements exercised: 3.5, 3.6, 14.1 (quiet-by-default gate), 5.3/14.5
(dismissed never surfaced), 13.3/13.4 (snooze hide/resume), and 9.1 (Auto-Healed
feed exception).
"""

from __future__ import annotations

from datetime import datetime, timezone

from loop.graph.models import ClosureKind, LoopState, Obligation
from loop.graph.surfacing import is_in_auto_healed_feed, is_surfaced

# A fixed reference "now" used across the table.
NOW_ISO = "2025-01-08T12:00:00+00:00"
NOW_DT = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc)
THRESHOLD = 0.5


def _obligation(**overrides) -> Obligation:
    """Build a baseline obligation that IS surfaced, then apply overrides.

    Baseline: not dismissed, not snoozed, confidence above threshold, an active
    loop state. Each test flips exactly one field to probe a single clause.
    """
    fields = dict(
        obligation_id="OB",
        owes_person_id="U0",
        owed_person_id="U1",
        owner_person_id="U0",
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.8,
        last_touch_timestamp=NOW_ISO,
        source_msg_channel="C1",
        source_msg_ts="111.222",
        subject_summary="Review the runbook",
    )
    fields.update(overrides)
    return Obligation(**fields)


# --- baseline: all clauses satisfied ----------------------------------------
def test_baseline_is_surfaced() -> None:
    assert is_surfaced(_obligation(), THRESHOLD, NOW_ISO) is True


# --- truth table: each clause flips the result independently -----------------
def test_dismissed_flips_to_hidden() -> None:
    # Clause 1 — dismissed (Req 5.3, 14.5).
    assert is_surfaced(_obligation(dismissed=True), THRESHOLD, NOW_ISO) is False


def test_active_snooze_flips_to_hidden() -> None:
    # Clause 2 — snoozed into the future (Req 13.3).
    future = "2025-01-08T13:00:00+00:00"
    assert is_surfaced(_obligation(snoozed_until=future), THRESHOLD, NOW_ISO) is False


def test_below_threshold_flips_to_hidden() -> None:
    # Clause 3 — quiet by default (Req 3.5, 14.1).
    assert is_surfaced(_obligation(confidence_score=0.49), THRESHOLD, NOW_ISO) is False


def test_healed_state_flips_to_hidden_on_active_surface() -> None:
    # Clause 4 — healed is never on the active surface.
    assert is_surfaced(_obligation(loop_state=LoopState.HEALED), THRESHOLD, NOW_ISO) is False


def test_waiting_on_other_is_an_active_state() -> None:
    # Clause 4 — the other active state still surfaces.
    assert is_surfaced(
        _obligation(loop_state=LoopState.WAITING_ON_OTHER), THRESHOLD, NOW_ISO
    ) is True


# --- boundary cases ----------------------------------------------------------
def test_confidence_exactly_at_threshold_is_surfaced() -> None:
    # Inclusive gate: confidence == threshold surfaces (Req 3.6, 14.1).
    assert is_surfaced(_obligation(confidence_score=0.5), THRESHOLD, NOW_ISO) is True


def test_confidence_just_below_threshold_is_hidden() -> None:
    # The strict-below side of the same boundary (Req 3.5).
    assert is_surfaced(_obligation(confidence_score=0.4999), THRESHOLD, NOW_ISO) is False


def test_snoozed_until_exactly_equal_to_now_is_surfaced() -> None:
    # now == snoozed_until counts as elapsed, so it surfaces (Req 13.3, 13.4).
    assert is_surfaced(_obligation(snoozed_until=NOW_ISO), THRESHOLD, NOW_ISO) is True


def test_snooze_one_second_in_the_future_is_hidden() -> None:
    just_after = "2025-01-08T12:00:01+00:00"
    assert is_surfaced(_obligation(snoozed_until=just_after), THRESHOLD, NOW_ISO) is False


def test_elapsed_snooze_in_the_past_is_surfaced() -> None:
    past = "2025-01-08T11:00:00+00:00"
    assert is_surfaced(_obligation(snoozed_until=past), THRESHOLD, NOW_ISO) is True


def test_no_snooze_is_surfaced() -> None:
    assert is_surfaced(_obligation(snoozed_until=None), THRESHOLD, NOW_ISO) is True


# --- now-argument robustness: datetime and ISO string agree ------------------
def test_now_accepts_datetime_and_iso_string_equivalently() -> None:
    snoozed = "2025-01-08T12:30:00+00:00"  # 30 min after NOW
    o = _obligation(snoozed_until=snoozed)
    # Both forms of "now" are before the snooze instant → hidden either way.
    assert is_surfaced(o, THRESHOLD, NOW_ISO) is False
    assert is_surfaced(o, THRESHOLD, NOW_DT) is False


def test_now_handles_zulu_suffix_and_differing_offsets() -> None:
    # snoozed_until written with a Zulu 'Z' suffix; now in a +05:00 offset that
    # corresponds to the same UTC instant → boundary equal → surfaces.
    o = _obligation(snoozed_until="2025-01-08T12:00:00Z")
    now_plus_five = "2025-01-08T17:00:00+05:00"  # == 12:00:00 UTC
    assert is_surfaced(o, THRESHOLD, now_plus_five) is True


def test_naive_datetime_now_is_treated_as_utc() -> None:
    naive_now = datetime(2025, 1, 8, 12, 0, 0)  # no tzinfo → assumed UTC
    assert is_surfaced(_obligation(snoozed_until=NOW_ISO), THRESHOLD, naive_now) is True


# --- Auto-Healed feed exception (Req 9.1) ------------------------------------
def test_autonomous_healed_is_in_auto_healed_feed_but_not_active() -> None:
    o = _obligation(loop_state=LoopState.HEALED, closure_kind=ClosureKind.AUTONOMOUS)
    assert is_surfaced(o, THRESHOLD, NOW_ISO) is False  # never on the active surface
    assert is_in_auto_healed_feed(o) is True


def test_manual_closure_excluded_from_auto_healed_feed() -> None:
    o = _obligation(loop_state=LoopState.HEALED, closure_kind=ClosureKind.MANUAL)
    assert is_in_auto_healed_feed(o) is False


def test_dismissed_autonomous_healed_excluded_from_auto_healed_feed() -> None:
    o = _obligation(
        loop_state=LoopState.HEALED,
        closure_kind=ClosureKind.AUTONOMOUS,
        dismissed=True,
    )
    assert is_in_auto_healed_feed(o) is False


def test_active_state_not_in_auto_healed_feed() -> None:
    # An active, non-healed obligation never appears in the healed feed.
    assert is_in_auto_healed_feed(_obligation()) is False
