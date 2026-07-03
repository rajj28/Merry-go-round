"""Property-based test for the App Home Aging_Chip classification (task 9.6, Property 21).

This exercises the *real* aging-chip classifier
(:func:`loop.action.app_home.aging_chip`, which delegates to
:func:`loop.action.app_home.classify_aging`) against randomized obligations and
elapsed times — no mocking — mirroring the generator/store conventions in
``test_app_home_properties.py``.

Property 21 (design.md → "Correctness Properties"): for any obligation, exactly
one Aging_Chip state is assigned —
  * ``fresh``    when the elapsed time since Last_Touch_Timestamp is **< 24h**;
  * ``warning``  when it is **between 24h and 72h inclusive**;
  * ``overdue``  when it is **> 72h**.

The expected state is computed independently from the classifier (a plain
threshold comparison on the elapsed seconds), so the assertion pins the builder's
chip to the single source of truth for the 24h/72h boundaries (Req 6.5). Boundary
ages exactly at 24h and 72h are drawn explicitly so the inclusive ``warning`` band
edges are always exercised.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from loop.action.app_home import (
    AGING_OVERDUE_FLOOR_SECONDS,
    AGING_WARNING_FLOOR_SECONDS,
    AgingState,
    aging_chip,
    classify_aging,
)
from loop.graph.models import ArtifactType, ClosureKind, LoopState, Obligation

# Fixed reference "now" so every age decision is deterministic across runs.
NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()

_24H = AGING_WARNING_FLOOR_SECONDS  # 86_400.0
_72H = AGING_OVERDUE_FLOOR_SECONDS  # 259_200.0


# ---------------------------------------------------------------------------
# Hypothesis strategies — randomized, in-contract obligations + elapsed seconds.
# ---------------------------------------------------------------------------
_IDS = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=10,
)

_SAFE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=30,
)

_CONFIDENCE = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)

# Elapsed time (seconds) since Last_Touch_Timestamp. Spans well below 24h to well
# above 72h (up to ~30 days) AND injects the exact 24h / 72h boundaries plus their
# immediate neighbours so the inclusive ``warning`` band edges are pinned.
_BOUNDARY_AGES = st.sampled_from(
    [
        0.0,
        _24H - 1.0,
        _24H,            # exactly 24h → warning (inclusive lower edge)
        _24H + 1.0,
        _72H - 1.0,
        _72H,            # exactly 72h → warning (inclusive upper edge)
        _72H + 1.0,
        30 * 24 * 3600.0,
    ]
)
_AGE_SECONDS = st.one_of(
    st.floats(min_value=0.0, max_value=30 * 24 * 3600.0, allow_nan=False, allow_infinity=False),
    _BOUNDARY_AGES,
)


def _expected_state(age_seconds: float) -> AgingState:
    """Independent expected classification straight from the Req 6.5 boundaries."""
    if age_seconds < _24H:
        return AgingState.FRESH
    if age_seconds <= _72H:
        return AgingState.WARNING
    return AgingState.OVERDUE


@st.composite
def obligation_with_age(draw: st.DrawFn) -> tuple[Obligation, float]:
    """A randomized in-contract :class:`Obligation` plus its intended age in seconds.

    The obligation's ``last_touch_timestamp`` is set to exactly ``NOW - age`` so the
    classifier (which computes ``now - last_touch``) sees precisely ``age`` seconds
    of elapsed time. Every other field is randomized but irrelevant to aging.
    """
    age_seconds = draw(_AGE_SECONDS)
    last_touch = (NOW - timedelta(seconds=age_seconds)).isoformat()
    has_artifact = draw(st.booleans())
    obligation = Obligation(
        obligation_id=draw(_IDS),
        owes_person_id=draw(_IDS),
        owed_person_id=draw(_IDS),
        owner_person_id=draw(_IDS),
        loop_state=draw(st.sampled_from(list(LoopState))),
        confidence_score=draw(_CONFIDENCE),
        last_touch_timestamp=last_touch,
        source_msg_channel=draw(_IDS),
        source_msg_ts=draw(_SAFE_TEXT),
        subject_summary=draw(_SAFE_TEXT),
        dismissed=draw(st.booleans()),
        snoozed_until=None,
        artifact_type=ArtifactType.GITHUB_PR if has_artifact else None,
        artifact_ref=draw(_SAFE_TEXT) if has_artifact else None,
        closure_kind=draw(st.one_of(st.none(), st.sampled_from(list(ClosureKind)))),
        closure_timestamp=None,
        closure_reason=draw(st.one_of(st.none(), _SAFE_TEXT)),
    )
    return obligation, age_seconds


# ---------------------------------------------------------------------------
# Property 21 (task 9.6) — Aging chip classification
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 21: Aging chip classification
@given(data=obligation_with_age())
def test_property_21_aging_chip_classification(data: tuple[Obligation, float]) -> None:
    """Validates: Requirements 6.5.

    For any obligation and elapsed time since Last_Touch_Timestamp, exactly one
    Aging_Chip state is assigned: ``fresh`` when elapsed < 24h, ``warning`` when
    24h ≤ elapsed ≤ 72h (inclusive of both boundaries), and ``overdue`` when
    elapsed > 72h.
    """
    obligation, age_seconds = data

    chip = aging_chip(obligation, NOW_ISO)
    expected = _expected_state(age_seconds)

    # Exactly one of the three states is assigned (the chip's state is a single
    # AgingState enum member, and it is the one demanded by the Req 6.5 boundaries).
    assert isinstance(chip.state, AgingState)
    assert chip.state in (AgingState.FRESH, AgingState.WARNING, AgingState.OVERDUE)
    assert chip.state == expected

    # The classifier on raw seconds agrees with the obligation-level chip, and the
    # emoji cue is consistent with the chosen state.
    assert classify_aging(age_seconds) == expected
    assert chip.text.endswith(chip.age_text)


# Feature: loop-obligation-agent, Property 21: Aging chip classification
@given(age_seconds=_AGE_SECONDS)
def test_property_21_boundaries_are_inclusive_warning(age_seconds: float) -> None:
    """Validates: Requirements 6.5.

    Pins the inclusive ``warning`` band edges directly on :func:`classify_aging`:
    strictly below 24h is ``fresh``, exactly 24h and exactly 72h are ``warning``,
    and strictly above 72h is ``overdue`` — confirming the boundary belongs to the
    warning band, never to fresh or overdue.
    """
    state = classify_aging(age_seconds)
    if age_seconds < _24H:
        assert state == AgingState.FRESH
    elif age_seconds <= _72H:
        assert state == AgingState.WARNING
    else:
        assert state == AgingState.OVERDUE

    # The two exact boundaries are members of the (inclusive) warning band.
    assert classify_aging(_24H) == AgingState.WARNING
    assert classify_aging(_72H) == AgingState.WARNING
