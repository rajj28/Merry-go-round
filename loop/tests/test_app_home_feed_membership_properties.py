"""Property-based test for the Auto-Healed feed membership (task 11.2 — Property 23).

This is the single numbered correctness property for the Auto-Healed Loops feed's
*membership* rule (Req 9.1), complementing the example-based unit tests in
``test_app_home.py``.

It exercises the REAL feed builder — :func:`loop.action.app_home.auto_healed_rows`
— over randomized graph states persisted in the real in-memory
:class:`~loop.graph.sqlite_store.SqliteObligationGraph`. No mocking: the feed
selection runs against the actual store query + the actual
:func:`loop.graph.surfacing.is_in_auto_healed_feed` membership gate.

The property asserts the exact-membership biconditional: the feed contains an
obligation **if and only if** its Loop_State is ``healed`` via an *autonomous*
closure and it is **not dismissed**. Every manually-closed obligation, every
active (blocked-on-you / waiting-on-other) obligation, and every dismissed
obligation is excluded.

Runs >=100 Hypothesis examples (enforced by the workspace conftest profile).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from loop.action.app_home import auto_healed_rows
from loop.graph.models import ClosureKind, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import is_ok

# --------------------------------------------------------------------------- #
# Smart generators — span the full membership decision space
# --------------------------------------------------------------------------- #
_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=10,
)

# Cover every Loop_State and every closure_kind possibility (including None) so the
# generated population mixes feed-eligible and feed-ineligible obligations.
_LOOP_STATES = st.sampled_from(list(LoopState))
_CLOSURE_KINDS = st.sampled_from([None, ClosureKind.AUTONOMOUS, ClosureKind.MANUAL])


@st.composite
def feed_candidate_obligations(draw: st.DrawFn) -> list[Obligation]:
    """Generate a list of obligations with UNIQUE ids spanning the membership space.

    Each obligation independently varies the three fields that decide feed
    membership — ``loop_state`` (any of the three), ``closure_kind`` (none /
    autonomous / manual), and ``dismissed`` (bool) — so a single graph state mixes
    feed-eligible and feed-ineligible rows.
    """
    count = draw(st.integers(min_value=0, max_value=12))
    # Unique ids keep upserts from colliding under last-write-wins.
    ids = draw(
        st.lists(_IDENT, min_size=count, max_size=count, unique=True)
    )
    obligations: list[Obligation] = []
    for oid in ids:
        loop_state = draw(_LOOP_STATES)
        closure_kind = draw(_CLOSURE_KINDS)
        dismissed = draw(st.booleans())
        obligations.append(
            Obligation(
                obligation_id=oid,
                owes_person_id=draw(_IDENT),
                owed_person_id=draw(_IDENT),
                owner_person_id=draw(_IDENT),
                loop_state=loop_state,
                confidence_score=draw(st.floats(min_value=0.0, max_value=1.0)),
                last_touch_timestamp="2025-01-08T12:00:00+00:00",
                source_msg_channel=draw(_IDENT),
                source_msg_ts="1700000000.000100",
                subject_summary=draw(st.text(max_size=40)),
                dismissed=dismissed,
                closure_kind=closure_kind,
                closure_timestamp="2025-01-08T12:30:00+00:00" if closure_kind else None,
                closure_reason="PR merged" if closure_kind else None,
            )
        )
    return obligations


def _is_feed_member(o: Obligation) -> bool:
    """The membership rule per Req 9.1: non-dismissed AND healed-via-autonomous-closure."""
    return (
        o.loop_state is LoopState.HEALED
        and o.closure_kind is ClosureKind.AUTONOMOUS
        and not o.dismissed
    )


# --------------------------------------------------------------------------- #
# Property 23: Auto-Healed feed membership
# --------------------------------------------------------------------------- #
# Feature: loop-obligation-agent, Property 23: Auto-Healed feed membership
# For any graph state, the Auto-Healed feed contains exactly the obligations whose
# Loop_State is `healed` through an autonomous closure and that are not dismissed,
# and excludes every obligation healed through a manual action.
# Validates: Requirements 9.1
@given(obligations=feed_candidate_obligations())
def test_auto_healed_feed_membership_is_exactly_nondismissed_autonomous_heals(
    obligations: list[Obligation],
) -> None:
    """Feed membership == exactly the non-dismissed autonomously-healed obligations."""
    # REAL store; persist the whole randomized graph state.
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    for o in obligations:
        assert is_ok(graph.upsert(o))

    feed_ids = {o.obligation_id for o in auto_healed_rows(graph, user_id="U_USER")}

    expected_ids = {o.obligation_id for o in obligations if _is_feed_member(o)}
    manual_ids = {
        o.obligation_id
        for o in obligations
        if o.loop_state is LoopState.HEALED and o.closure_kind is ClosureKind.MANUAL
    }
    dismissed_ids = {o.obligation_id for o in obligations if o.dismissed}

    # Exact membership: nothing missing, nothing extra.
    assert feed_ids == expected_ids
    # Manual closures never appear in the feed.
    assert feed_ids.isdisjoint(manual_ids)
    # Dismissed obligations never appear, even if autonomously healed.
    assert feed_ids.isdisjoint(dismissed_ids)
