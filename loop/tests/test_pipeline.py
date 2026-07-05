"""Tests for the detection work queue (loop/pipeline.py, task 17.1).

Verify the Watcher → work queue → Adjudicator hand-off: a forwarded candidate is
adjudicated against the tracked user and written to the graph, the synchronous
``drain`` returns one result per processed candidate, and a crashing adjudicator
can never kill the drain.
"""

from __future__ import annotations

from loop.adjudicator.adjudicator import (
    Adjudicator,
    Direction,
    SmartAdjudication,
)
from loop.graph.models import LoopState
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.pipeline import AdjudicationQueue
from loop.watcher.rts_contract import CandidateMessage


USER = "U_USER"


def _candidate(ts: str = "1700000000.0001", author: str = "U_OTHER") -> CandidateMessage:
    return CandidateMessage(
        channel_id="C1",
        message_ts=ts,
        author_id=author,
        text="can you review this?",
        permalink="https://x",
    )


def _smart_user_owes(_candidate, *, user_id):  # noqa: ANN001
    return SmartAdjudication(
        is_loop=True,
        involves_user=True,
        direction=Direction.USER_OWES,
        confidence=0.9,
        subject_summary="review request",
    )


def test_enqueue_then_drain_adjudicates_and_writes_graph():
    graph = SqliteObligationGraph(IN_MEMORY)
    adjudicator = Adjudicator(_smart_user_owes, graph)
    queue = AdjudicationQueue(adjudicator, USER)

    # The Watcher's forward seam is queue.enqueue.
    queue.enqueue(_candidate())
    results = queue.drain()

    assert len(results) == 1
    from loop.graph.store import ObligationFilter

    written = graph.query(ObligationFilter())
    assert len(written) == 1
    assert written[0].loop_state is LoopState.BLOCKED_ON_YOU
    assert written[0].owes_person_id == USER


def test_drain_returns_result_per_candidate_and_is_empty_when_drained():
    graph = SqliteObligationGraph(IN_MEMORY)
    queue = AdjudicationQueue(Adjudicator(_smart_user_owes, graph), USER)

    queue.enqueue(_candidate(ts="1700000000.0001"))
    queue.enqueue(_candidate(ts="1700000000.0002"))
    first = queue.drain()
    assert len(first) == 2
    # Nothing left to drain.
    assert queue.drain() == []


def test_drain_survives_a_crashing_adjudicator():
    graph = SqliteObligationGraph(IN_MEMORY)

    class Boom(Adjudicator):
        def adjudicate(self, candidate, *, user_id):  # noqa: ANN001
            raise RuntimeError("boom")

    queue = AdjudicationQueue(Boom(_smart_user_owes, graph), USER)
    queue.enqueue(_candidate())
    # The crash is contained: drain completes and yields no result for the bad one.
    assert queue.drain() == []


def test_on_result_callback_fires_after_each_adjudication():
    graph = SqliteObligationGraph(IN_MEMORY)
    seen = []
    queue = AdjudicationQueue(
        Adjudicator(_smart_user_owes, graph), USER, on_result=seen.append
    )
    queue.enqueue(_candidate())
    queue.drain()
    assert len(seen) == 1
