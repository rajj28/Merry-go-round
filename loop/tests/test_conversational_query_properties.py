"""Property-based test for the conversational query result set (task 15.4, Property 27).

This exercises the *real* Conversational Agent
(:meth:`loop.conversational.conversational_agent.ConversationalAgent.handle_query`)
against the *real* in-memory SQLite Obligation Graph store — no mocking of the
graph — over randomized graph states and randomized structured filters, matching
the real-store usage pattern in ``test_app_home_properties.py`` and the
deterministic-parser injection pattern used across ``loop/tests``.

Property 27 (design.md → "Correctness Properties"): for any graph state and any
structured filter derived from a query, the Conversational Agent returns exactly
the obligations satisfying the filter; and when no obligation satisfies the filter
it returns a no-match indication (Req 12.1, 12.2).

The natural-language *understanding* is isolated behind the agent's injectable
parser port, so this test drives a parser stub that yields a known
:class:`ObligationFilter` directly — pinning the agent's query behaviour to the
graph's own ``query`` semantics independent of any LLM / NL coverage. The expected
matching set is computed independently by querying the same graph with the same
filter, so the assertion ties the agent's reply to the single source of truth for
"which obligations satisfy this filter?".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from loop.conversational.conversational_agent import (
    ConversationalAgent,
    ParsedQuery,
    ReplyKind,
)
from loop.graph.models import ArtifactType, ClosureKind, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import ObligationFilter

# Fixed reference "now" so every snooze/age decision is deterministic across runs.
NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()

USER = "U_TESTER"


# ---------------------------------------------------------------------------
# Hypothesis strategies — randomized but in-contract obligations + threshold.
# ---------------------------------------------------------------------------
# Slack-style identifiers: short, non-empty, simple (keeps SQLite text storage happy).
# A small alphabet/size keeps the id space small enough that randomly-generated
# person filters actually hit stored rows often, exercising the match path.
_IDS = st.text(alphabet="ABCDE_", min_size=1, max_size=3)

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
    """A randomized, in-contract :class:`Obligation` spanning the full surfacing gate."""
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


# Distinct ids avoid the store's last-write-wins collapse so each obligation is a row.
_GRAPH_STATES = st.lists(obligations(), max_size=25, unique_by=lambda o: o.obligation_id)

_LOOP_STATE_SETS = st.sets(st.sampled_from(list(LoopState))).map(frozenset)
_AGE_SECONDS = st.one_of(
    st.none(),
    st.floats(min_value=0.0, max_value=15 * 24 * 3600, allow_nan=False, allow_infinity=False),
)
_OPT_ID = st.one_of(st.none(), _IDS)


@st.composite
def filters(draw: st.DrawFn) -> ObligationFilter:
    """A randomized structured :class:`ObligationFilter`, as a query would produce.

    Every dimension the graph supports is exercised (loop states, surfacing controls,
    participant/ownership, artifact, closure provenance, age window). ``now`` is
    pinned to :data:`NOW_ISO` so snooze/age evaluation is deterministic and the
    agent's ``_apply_now`` is a no-op — the agent queries with exactly this filter.
    """
    return ObligationFilter(
        loop_states=draw(_LOOP_STATE_SETS),
        surfaced_only=draw(st.booleans()),
        include_dismissed=draw(st.booleans()),
        owner_person_id=draw(_OPT_ID),
        owes_person_id=draw(_OPT_ID),
        owed_person_id=draw(_OPT_ID),
        artifact_type=draw(st.one_of(st.none(), st.just(ArtifactType.GITHUB_PR))),
        artifact_ref=draw(st.one_of(st.none(), _SAFE_TEXT)),
        closure_kind=draw(st.one_of(st.none(), st.sampled_from(list(ClosureKind)))),
        min_age_seconds=draw(_AGE_SECONDS),
        max_age_seconds=draw(_AGE_SECONDS),
        now=NOW_ISO,
    )


def _graph(threshold: float, obs: list[Obligation]) -> SqliteObligationGraph:
    """A fresh in-memory store seeded with ``obs`` at the given surfacing threshold."""
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    graph.set_threshold(threshold)
    for o in obs:
        graph.upsert(o)
    return graph


def _agent_with_filter(graph: SqliteObligationGraph, filt: ObligationFilter) -> ConversationalAgent:
    """A Conversational Agent whose parser deterministically yields ``filt`` as a query.

    Isolating NL understanding behind the injected parser pins the test to the
    agent's query/result-set logic, independent of any LLM. The Action Agent is
    never invoked on the query path, so a plain placeholder suffices.
    """
    return ConversationalAgent(
        graph=graph,
        action=object(),  # unused on the query path (no command is routed)
        parser=lambda _text: ParsedQuery(filter=filt),
        now=lambda: NOW_ISO,
    )


# ---------------------------------------------------------------------------
# Property 27 (task 15.4) — Conversational query returns the matching set
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 27: Conversational query returns the matching set
@settings(max_examples=200)
@given(threshold=_THRESHOLD, obs=_GRAPH_STATES, filt=filters())
def test_property_27_conversational_query_returns_matching_set(
    threshold: float, obs: list[Obligation], filt: ObligationFilter
) -> None:
    """Validates: Requirements 12.1, 12.2.

    For any graph state and any structured filter derived from a query, the
    Conversational Agent returns exactly the obligations satisfying the filter
    (Req 12.1); and when none satisfy the filter it returns a no-match indication
    (Req 12.2).
    """
    graph = _graph(threshold, obs)
    agent = _agent_with_filter(graph, filt)

    # Independent expected matching set: query the same graph with the same filter.
    expected = graph.query(filt)
    expected_ids = sorted(o.obligation_id for o in expected)

    reply = agent.handle_query(USER, "any natural-language question about my loops")

    returned_ids = sorted(o.obligation_id for o in reply.obligations)

    if expected_ids:
        # Req 12.1: returns exactly the obligations satisfying the filter.
        assert reply.kind == ReplyKind.QUERY_RESULT
        assert returned_ids == expected_ids
    else:
        # Req 12.2: no obligation satisfies the filter → explicit no-match indication.
        assert reply.kind == ReplyKind.NO_MATCH
        assert reply.obligations == ()
