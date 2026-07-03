"""Property-based test for the Auto-Healed feed ordering (task 11.3 — Property 24).

This is the single numbered correctness property for the Auto-Healed Loops feed's
*ordering* rule (Req 9.3), complementing the membership property
(``test_app_home_feed_membership_properties.py``) and the example-based unit tests
in ``test_app_home.py``.

It exercises the REAL feed builder — :func:`loop.action.app_home.auto_healed_rows`
— over randomized graph states persisted in the real in-memory
:class:`~loop.graph.sqlite_store.SqliteObligationGraph`. No mocking: ordering is
produced by the actual store query + the actual feed selection/sort.

The property asserts the exact ordering contract of Req 9.3:

  * entries are ordered by **closure timestamp, most-recent → least-recent**; and
  * entries that **share** a closure timestamp are ordered by **resolved person
    identifier in ascending alphabetical order**.

Generated obligations are all autonomously-healed and non-dismissed (so every one
is a feed member), and their closure timestamps are drawn from a small pool so
duplicate timestamps — the tie-break case — occur frequently.

Runs >=100 Hypothesis examples (enforced by the workspace conftest profile).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from loop.action.app_home import (
    _closure_sort_dt,
    auto_healed_rows,
    resolved_person_id,
)
from loop.graph.models import ClosureKind, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import is_ok

USER_ID = "U_USER"

# --------------------------------------------------------------------------- #
# Smart generators — exercise both the primary key (closure ts) and the tie-break
# (resolved person id) of the ordering.
# --------------------------------------------------------------------------- #
_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=10,
)

# A small pool of closure timestamps so distinct obligations frequently SHARE a
# timestamp, forcing the resolved-person-id tie-break to be exercised. The values
# are deliberately not pre-sorted so the builder must do the ordering work.
_CLOSURE_TS_POOL = [
    "2025-01-08T09:00:00+00:00",
    "2025-01-08T12:30:00+00:00",
    "2025-01-07T23:15:00+00:00",
    "2025-01-09T01:00:00+00:00",
    "2025-01-08T12:30:00+00:00",  # intentional duplicate value reference
    "2025-01-05T18:45:00+00:00",
]

# A small pool of "other party" ids so resolved-person ties (and orderings) recur.
_PERSON_POOL = ["alice", "bob", "carol", "dave", "erin"]


@st.composite
def healed_feed_obligations(draw: st.DrawFn) -> list[Obligation]:
    """Generate a list of feed-eligible obligations with UNIQUE ids.

    Every obligation is ``healed`` via an ``autonomous`` closure and not dismissed,
    so all are feed members; only their closure timestamps and resolved-person ids
    vary. Closure timestamps come from a small pool (frequent duplicates → tie
    cases) and the resolved person is the (non-user) ``owed_person_id`` drawn from a
    small pool so the tie-break ordering recurs.
    """
    count = draw(st.integers(min_value=0, max_value=14))
    ids = draw(st.lists(_IDENT, min_size=count, max_size=count, unique=True))
    obligations: list[Obligation] = []
    for oid in ids:
        closure_ts = draw(st.sampled_from(_CLOSURE_TS_POOL))
        owed = draw(st.sampled_from(_PERSON_POOL))
        obligations.append(
            Obligation(
                obligation_id=oid,
                # Keep neither endpoint equal to USER_ID so resolved_person_id
                # deterministically resolves to owed_person_id (see module docs).
                owes_person_id=draw(st.sampled_from(_PERSON_POOL)),
                owed_person_id=owed,
                owner_person_id=USER_ID,
                loop_state=LoopState.HEALED,
                confidence_score=draw(st.floats(min_value=0.0, max_value=1.0)),
                last_touch_timestamp="2025-01-08T12:00:00+00:00",
                source_msg_channel=draw(_IDENT),
                source_msg_ts="1700000000.000100",
                subject_summary=draw(st.text(max_size=40)),
                dismissed=False,
                closure_kind=ClosureKind.AUTONOMOUS,
                closure_timestamp=closure_ts,
                closure_reason="PR merged",
            )
        )
    return obligations


# --------------------------------------------------------------------------- #
# Property 24: Auto-Healed feed ordering
# --------------------------------------------------------------------------- #
# Feature: loop-obligation-agent, Property 24: Auto-Healed feed ordering
# For any set of auto-healed obligations, feed entries are ordered by closure
# timestamp from most recent to least recent, and entries sharing a closure
# timestamp are ordered by resolved person identifier in ascending alphabetical
# order.
# Validates: Requirements 9.3
@given(obligations=healed_feed_obligations())
def test_auto_healed_feed_is_ordered_newest_first_ties_by_resolved_person(
    obligations: list[Obligation],
) -> None:
    """Feed order: closure ts newest→oldest, ties broken by resolved id ascending."""
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    for o in obligations:
        assert is_ok(graph.upsert(o))

    feed = auto_healed_rows(graph, user_id=USER_ID)

    # Pairwise ordering invariant — robust to entries that tie on BOTH fields.
    for earlier, later in zip(feed, feed[1:]):
        ts_earlier = _closure_sort_dt(earlier)
        ts_later = _closure_sort_dt(later)
        # Primary key: closure timestamp must be non-increasing (newest first).
        assert ts_earlier >= ts_later, (
            f"closure timestamps out of order: {ts_earlier} then {ts_later}"
        )
        # Tie-break: when timestamps are equal, resolved person id must ascend.
        if ts_earlier == ts_later:
            person_earlier = resolved_person_id(earlier, USER_ID) or ""
            person_later = resolved_person_id(later, USER_ID) or ""
            assert person_earlier <= person_later, (
                "tie-break violated: equal closure timestamp but resolved person "
                f"ids not ascending: {person_earlier!r} then {person_later!r}"
            )
