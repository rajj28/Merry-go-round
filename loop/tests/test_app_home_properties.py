"""Property-based test for the App Home hero banner count (task 9.4, Property 19).

This exercises the *real* App Home view builder
(:func:`loop.action.app_home.hero_count` / :func:`build_app_home_view`) against
the *real* in-memory SQLite Obligation Graph store — no mocking — over randomized
graph states, matching the store-isolation pattern in ``test_graph_properties.py``
and the real-store usage in ``test_app_home.py``.

Property 19 (design.md → "Correctness Properties"): the hero banner count equals
the number of surfaced ``blocked-on-you`` obligations — those whose Loop_State is
``blocked-on-you`` that are at/above the Confidence_Threshold and not dismissed
(and not currently snoozed) — and when that count is zero the banner reports the
zero-state "all caught up" copy rather than a number (Req 6.1, 6.7, 15.2).

The expected count is computed independently from the builder using the shared
:func:`loop.graph.surfacing.is_surfaced` predicate, so the test pins the builder's
hero number to the single source of truth for "is this shown?".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from loop.action.app_home import (
    HERO_ZERO_TEXT,
    build_app_home_view,
    hero_count,
    hero_text,
)
from loop.graph.models import ArtifactType, ClosureKind, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.surfacing import is_surfaced

# Fixed reference "now" so every snooze/age decision is deterministic across runs.
NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# ---------------------------------------------------------------------------
# Hypothesis strategies — randomized but in-contract obligations + threshold.
# ---------------------------------------------------------------------------
# Slack-style identifiers: short, non-empty, simple (keeps SQLite text storage happy).
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

# Confidence + threshold span the full inclusive range, boundaries included, so the
# inclusive ``>=`` gate (Req 3.6, 14.1) is exercised at the boundary.
_CONFIDENCE = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_THRESHOLD = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# Timestamps within +/- 10 days of NOW: produces a mix of fresh/aged loops and,
# for ``snoozed_until``, both already-elapsed (past) and still-snoozed (future)
# values relative to NOW.
_TIMESTAMPS = st.datetimes(
    min_value=(NOW - timedelta(days=10)).replace(tzinfo=None),
    max_value=(NOW + timedelta(days=10)).replace(tzinfo=None),
    timezones=st.just(timezone.utc),
).map(_iso)


@st.composite
def obligations(draw: st.DrawFn) -> Obligation:
    """A randomized, in-contract :class:`Obligation`.

    Loop_State ranges over all three values; confidence spans the full inclusive
    range; ``dismissed`` and ``snoozed_until`` (None / past / future) vary so the
    full surfacing gate is exercised. Healed obligations carry closure metadata so
    they are realistic feed members, but they never count toward the hero number.
    """
    state = draw(st.sampled_from(list(LoopState)))
    has_artifact = draw(st.booleans())
    closure = draw(st.one_of(st.none(), st.sampled_from(list(ClosureKind))))
    return Obligation(
        obligation_id=draw(_IDS),
        owes_person_id=draw(_IDS),
        owed_person_id=draw(_IDS),
        owner_person_id=draw(_IDS),
        loop_state=state,
        confidence_score=draw(_CONFIDENCE),
        last_touch_timestamp=draw(_TIMESTAMPS),
        source_msg_channel=draw(_IDS),
        source_msg_ts=draw(_SAFE_TEXT),
        subject_summary=draw(_SAFE_TEXT),
        dismissed=draw(st.booleans()),
        snoozed_until=draw(st.one_of(st.none(), _TIMESTAMPS)),
        artifact_type=ArtifactType.GITHUB_PR if has_artifact else None,
        artifact_ref=draw(_SAFE_TEXT) if has_artifact else None,
        closure_kind=closure,
        closure_timestamp=draw(st.one_of(st.none(), _TIMESTAMPS)),
        closure_reason=draw(st.one_of(st.none(), _SAFE_TEXT)),
    )


# Lists of obligations with unique ids — distinct ids avoid the store's
# last-write-wins collapse so each generated obligation is an independent row.
_GRAPH_STATES = st.lists(obligations(), max_size=25, unique_by=lambda o: o.obligation_id)


def _graph(threshold: float, obs: list[Obligation]) -> SqliteObligationGraph:
    """A fresh in-memory store seeded with ``obs`` at the given surfacing threshold."""
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    graph.set_threshold(threshold)
    for o in obs:
        graph.upsert(o)
    return graph


def _expected_surfaced_blocked(obs: list[Obligation], threshold: float) -> int:
    """Independent expected hero count: surfaced ``blocked-on-you`` obligations.

    Uses the shared :func:`is_surfaced` gate so the assertion pins the builder to
    the single source of truth (not dismissed, not snoozed, confidence ≥ threshold,
    active state) restricted to the ``blocked-on-you`` state.
    """
    return sum(
        1
        for o in obs
        if o.loop_state == LoopState.BLOCKED_ON_YOU and is_surfaced(o, threshold, NOW_ISO)
    )


# ---------------------------------------------------------------------------
# Property 19 (task 9.4) — Hero banner count equals surfaced blocked-on-you obligations
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 19: Hero banner count equals surfaced blocked-on-you obligations
@given(threshold=_THRESHOLD, obs=_GRAPH_STATES)
def test_property_19_hero_count_equals_surfaced_blocked_on_you(
    threshold: float, obs: list[Obligation]
) -> None:
    """Validates: Requirements 6.1, 6.7, 15.2.

    For any graph state and threshold, the App Home hero banner count equals the
    number of surfaced ``blocked-on-you`` obligations (≥ threshold, not dismissed,
    not snoozed); and when that count is zero the hero shows the zero-state copy
    rather than a number.
    """
    graph = _graph(threshold, obs)
    expected = _expected_surfaced_blocked(obs, threshold)

    # The builder's hero count matches the independently-computed surfaced count.
    count = hero_count(graph, NOW_ISO)
    assert count == expected

    # The rendered hero block reflects that same count.
    view = build_app_home_view(graph, NOW_ISO)
    hero_block_text = view["blocks"][0]["text"]["text"]
    assert hero_block_text == hero_text(count)

    if expected == 0:
        # Zero-state: the banner reports nobody blocked, with no count rendered.
        assert hero_block_text == HERO_ZERO_TEXT
    else:
        # Non-zero: the banner surfaces the exact count of people blocked on you.
        assert HERO_ZERO_TEXT != hero_block_text
        assert str(expected) in hero_block_text
