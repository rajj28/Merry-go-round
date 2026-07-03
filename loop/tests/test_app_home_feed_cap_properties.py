"""Property-based test for the Auto-Healed feed cap (task 11.4 — Property 25).

This is the single numbered correctness property for the Auto-Healed Loops feed's
*cap* rule (Req 9.6), complementing the membership property
(``test_app_home_feed_membership_properties.py``), the ordering property
(``test_app_home_feed_ordering_properties.py``), and the example-based unit test
in ``test_app_home.py``.

It exercises the REAL feed builder — :func:`loop.action.app_home.auto_healed_rows`
— over randomized graph states persisted in the real in-memory
:class:`~loop.graph.sqlite_store.SqliteObligationGraph`. No mocking: the cap is
produced by the actual store query + the actual feed selection/sort/truncation.

The property asserts the exact cap contract of Req 9.6:

  * for any set of **more than 100** auto-closed obligations, the feed contains
    **exactly 100** entries; and
  * those entries are **exactly the 100 most-recently-healed** (by closure
    timestamp) — the older surplus is dropped.

To make "the 100 most recent" unambiguous, every generated obligation gets a
**distinct** closure timestamp (a distinct minute offset from a fixed base), and
the obligations are upserted in randomized order so the builder must do the
ordering + truncation work rather than relying on insertion order. Every
obligation is autonomously-healed and non-dismissed, so all are feed-eligible and
only the cap decides which survive.

Runs >=100 Hypothesis examples (enforced by the workspace conftest profile).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from loop.action.app_home import (
    AUTO_HEALED_FEED_CAP,
    _closure_sort_dt,
    auto_healed_rows,
)
from loop.graph.models import ClosureKind, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import is_ok

USER_ID = "U_USER"

# Fixed base instant; each obligation heals at base - (offset minutes), so a SMALLER
# offset == a MORE-RECENT closure. Distinct offsets => distinct closure timestamps,
# making "the 100 most recent" a well-defined, tie-free set.
_BASE = datetime(2025, 1, 9, 0, 0, 0, tzinfo=timezone.utc)

_PERSON_POOL = ["alice", "bob", "carol", "dave", "erin"]


def _closure_ts(offset_minutes: int) -> str:
    """ISO 8601 UTC closure timestamp ``offset_minutes`` before the fixed base."""
    return (_BASE - timedelta(minutes=offset_minutes)).isoformat()


@st.composite
def over_cap_healed_obligations(draw: st.DrawFn) -> list[Obligation]:
    """Generate MORE THAN 100 feed-eligible obligations with distinct closure ts.

    Every obligation is ``healed`` via an ``autonomous`` closure and not dismissed,
    so all are feed members; the cap alone decides which survive. Each obligation
    gets a unique minute offset (=> unique closure timestamp), drawn so the recency
    ordering is total and the "100 most recent" set is unambiguous. The list is
    returned in randomized (shuffled) order so the builder cannot lean on insertion
    order.
    """
    # Strictly more than the cap, with headroom so the surplus that gets dropped
    # is itself varied in size.
    count = draw(st.integers(min_value=AUTO_HEALED_FEED_CAP + 1, max_value=AUTO_HEALED_FEED_CAP + 60))
    # Distinct minute offsets -> distinct closure timestamps (tie-free recency).
    offsets = draw(
        st.lists(
            st.integers(min_value=1, max_value=100_000),
            min_size=count,
            max_size=count,
            unique=True,
        )
    )
    obligations: list[Obligation] = []
    for i, offset in enumerate(offsets):
        obligations.append(
            Obligation(
                obligation_id=f"H{i:05d}",
                owes_person_id=draw(st.sampled_from(_PERSON_POOL)),
                owed_person_id=draw(st.sampled_from(_PERSON_POOL)),
                owner_person_id=USER_ID,
                loop_state=LoopState.HEALED,
                confidence_score=draw(st.floats(min_value=0.0, max_value=1.0)),
                last_touch_timestamp="2025-01-08T12:00:00+00:00",
                source_msg_channel="C_DEMO",
                source_msg_ts="1700000000.000100",
                subject_summary=draw(st.text(max_size=40)),
                dismissed=False,
                closure_kind=ClosureKind.AUTONOMOUS,
                closure_timestamp=_closure_ts(offset),
                closure_reason="PR merged",
            )
        )
    # Shuffle so persistence order is independent of recency order.
    draw(st.randoms(use_true_random=True)).shuffle(obligations)
    return obligations


# --------------------------------------------------------------------------- #
# Property 25: Auto-Healed feed cap
# --------------------------------------------------------------------------- #
# Feature: loop-obligation-agent, Property 25: Auto-Healed feed cap
# For any set of more than 100 auto-closed obligations, the feed retains exactly
# the 100 most recently healed entries.
# Validates: Requirements 9.6
@given(obligations=over_cap_healed_obligations())
def test_auto_healed_feed_caps_at_100_most_recently_healed(
    obligations: list[Obligation],
) -> None:
    """>100 auto-closed -> feed is exactly the 100 most-recently-healed entries."""
    assert len(obligations) > AUTO_HEALED_FEED_CAP  # generator guarantees over-cap

    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    for o in obligations:
        assert is_ok(graph.upsert(o))

    feed = auto_healed_rows(graph, user_id=USER_ID)

    # 1. The feed is capped at exactly 100 entries.
    assert len(feed) == AUTO_HEALED_FEED_CAP == 100

    # 2. The retained entries are EXACTLY the 100 most-recently-healed. Compute the
    #    expected set independently: sort all obligations by closure timestamp
    #    newest-first and take the first 100.
    expected_top_100 = sorted(
        obligations, key=_closure_sort_dt, reverse=True
    )[:AUTO_HEALED_FEED_CAP]
    expected_ids = {o.obligation_id for o in expected_top_100}
    feed_ids = {o.obligation_id for o in feed}
    assert feed_ids == expected_ids

    # 3. Every dropped entry is strictly older than every retained entry — i.e. the
    #    truncation kept the most-recent ones, not an arbitrary slice.
    retained_min_ts = min(_closure_sort_dt(o) for o in feed)
    dropped = [o for o in obligations if o.obligation_id not in feed_ids]
    for o in dropped:
        assert _closure_sort_dt(o) <= retained_min_ts

    # 4. The feed itself is still ordered newest-first by closure timestamp.
    for earlier, later in zip(feed, feed[1:]):
        assert _closure_sort_dt(earlier) >= _closure_sort_dt(later)
