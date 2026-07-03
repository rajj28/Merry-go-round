"""Property-based test for App Home section contents and ordering (task 9.5, Property 20).

This exercises the *real* App Home section selectors
(:func:`loop.action.app_home.blocked_on_you_rows` and
:func:`loop.action.app_home.waiting_on_other_rows`) against the *real* in-memory
SQLite Obligation Graph store — no mocking — over randomized graph states,
matching the store-isolation pattern in ``test_app_home_properties.py``.

Property 20 (design.md → "Correctness Properties"): for any graph state and either
active Loop_State, the corresponding App Home section contains *exactly* the
surfaced obligations of that state, ordered by ``last_touch_timestamp`` from oldest
to newest (Req 6.3, 6.4).

The expected per-section set is computed independently from the builder using the
shared :func:`loop.graph.surfacing.is_surfaced` predicate, and the expected order
is computed by an independent oldest→newest sort, so the test pins each section's
membership and ordering to the single source of truth for "is this shown?".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from loop.action.app_home import (
    blocked_on_you_rows,
    waiting_on_other_rows,
)
from loop.graph.models import ArtifactType, ClosureKind, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.surfacing import is_surfaced

# Fixed reference "now" so every snooze/age decision is deterministic across runs.
NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# ---------------------------------------------------------------------------
# Hypothesis strategies — randomized but in-contract obligations + threshold.
# (Mirrors test_app_home_properties.py so both property tests share generators.)
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
_THRESHOLD = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# Timestamps within +/- 10 days of NOW: a mix of fresh/aged loops and, for
# ``snoozed_until``, both already-elapsed (past) and still-snoozed (future) values.
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
    full surfacing gate is exercised against both active sections.
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


def _parse(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp to UTC for independent ordering checks."""
    text = ts.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _expected_section(
    obs: list[Obligation], state: LoopState, threshold: float
) -> list[Obligation]:
    """Independent expected section: surfaced obligations of ``state``, oldest→newest.

    Uses the shared :func:`is_surfaced` gate so the assertion pins the builder to
    the single source of truth, then sorts by ``last_touch_timestamp`` ascending.
    """
    matching = [
        o
        for o in obs
        if o.loop_state == state and is_surfaced(o, threshold, NOW_ISO)
    ]
    return sorted(matching, key=lambda o: _parse(o.last_touch_timestamp))


# ---------------------------------------------------------------------------
# Property 20 (task 9.5) — Section contents and ordering
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 20: Section contents and ordering
@given(threshold=_THRESHOLD, obs=_GRAPH_STATES)
def test_property_20_section_contents_and_ordering(
    threshold: float, obs: list[Obligation]
) -> None:
    """Validates: Requirements 6.3, 6.4.

    For any graph state and threshold, each active App Home section contains
    *exactly* the surfaced obligations of its Loop_State (membership matches the
    shared ``is_surfaced`` gate — no extras, none missing) and renders them ordered
    by ``last_touch_timestamp`` from oldest to newest.
    """
    graph = _graph(threshold, obs)

    for state, section_rows in (
        (LoopState.BLOCKED_ON_YOU, blocked_on_you_rows),
        (LoopState.WAITING_ON_OTHER, waiting_on_other_rows),
    ):
        actual = section_rows(graph, NOW_ISO)
        expected = _expected_section(obs, state, threshold)

        actual_ids = [o.obligation_id for o in actual]
        expected_ids = [o.obligation_id for o in expected]

        # Contents: the section contains exactly the surfaced obligations of the
        # state — same set, no extras and none missing.
        assert set(actual_ids) == set(expected_ids)

        # Every row genuinely belongs to this state and is genuinely surfaced.
        for o in actual:
            assert o.loop_state == state
            assert is_surfaced(o, threshold, NOW_ISO)

        # Ordering: oldest→newest by last_touch_timestamp (matches the independent
        # sort, and the timestamps are non-decreasing along the section).
        assert actual_ids == expected_ids
        timestamps = [_parse(o.last_touch_timestamp) for o in actual]
        assert timestamps == sorted(timestamps)
