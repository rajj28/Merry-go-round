"""Example-based tests for chain & cycle analysis (:mod:`loop.graph.chains`).

Covers the structural intelligence layer on hand-built graphs where the right
answer is obvious by inspection:

  * downstream impact on a straight chain (A→B→C→D)
  * a fork counted once (no double-counting shared downstream people)
  * deadlock rings (2-cycle and 3-cycle), canonical rotation, and person-level
    dedup across parallel edges
  * healed / dismissed edges excluded; snoozed edges included
  * ``ChainReport`` helpers (impact, heaviest, cycles_involving)
"""

from __future__ import annotations

from loop.graph.chains import (
    BlockingCycle,
    active_edges,
    analyze,
    downstream_people,
    find_cycles,
)
from loop.graph.models import LoopState, Obligation, utc_now_iso


def _edge(
    oid: str,
    owes: str,
    owed: str,
    *,
    state: LoopState = LoopState.WAITING_ON_OTHER,
    dismissed: bool = False,
    snoozed_until: str | None = None,
) -> Obligation:
    return Obligation(
        obligation_id=oid,
        owes_person_id=owes,
        owed_person_id=owed,
        owner_person_id=owes,
        loop_state=state,
        confidence_score=0.9,
        last_touch_timestamp=utc_now_iso(),
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary=f"{owes} owes {owed} ({oid})",
        dismissed=dismissed,
        snoozed_until=snoozed_until,
    )


# ---------------------------------------------------------------------------
# Downstream impact
# ---------------------------------------------------------------------------
class TestDownstreamImpact:
    def test_straight_chain_counts_everyone_behind_the_head(self) -> None:
        # A owes B, B owes C, C owes D: behind A→B stand B, C and D.
        edges = [_edge("o1", "A", "B"), _edge("o2", "B", "C"), _edge("o3", "C", "D")]
        assert downstream_people(edges, edges[0]) == frozenset({"B", "C", "D"})
        assert downstream_people(edges, edges[1]) == frozenset({"C", "D"})
        assert downstream_people(edges, edges[2]) == frozenset({"D"})

    def test_fork_counts_each_person_once(self) -> None:
        # B owes both C and D; C and D both owe E. E is counted once.
        edges = [
            _edge("o1", "A", "B"),
            _edge("o2", "B", "C"),
            _edge("o3", "B", "D"),
            _edge("o4", "C", "E"),
            _edge("o5", "D", "E"),
        ]
        assert downstream_people(edges, edges[0]) == frozenset({"B", "C", "D", "E"})

    def test_blocker_is_excluded_even_in_a_cycle(self) -> None:
        # A owes B, B owes A (mutual deadlock): behind A→B stands only B.
        edges = [_edge("o1", "A", "B"), _edge("o2", "B", "A")]
        assert downstream_people(edges, edges[0]) == frozenset({"B"})

    def test_walk_does_not_continue_through_the_blocker(self) -> None:
        # B owes A closes back onto the blocker A; A separately owes X. X waits
        # on A directly and must not be attributed to the A→B edge.
        edges = [
            _edge("o1", "A", "B"),
            _edge("o2", "B", "A"),
            _edge("o3", "A", "X"),
        ]
        assert downstream_people(edges, edges[0]) == frozenset({"B"})


# ---------------------------------------------------------------------------
# Deadlock rings
# ---------------------------------------------------------------------------
class TestFindCycles:
    def test_three_person_ring_found_once_and_canonically_rotated(self) -> None:
        edges = [_edge("o1", "B", "C"), _edge("o2", "C", "A"), _edge("o3", "A", "B")]
        cycles = find_cycles(edges)
        assert len(cycles) == 1
        assert cycles[0].people == ("A", "B", "C")  # rotated to smallest-first
        assert [e.obligation_id for e in cycles[0].edges] == ["o3", "o1", "o2"]

    def test_two_person_mutual_block_is_a_deadlock(self) -> None:
        edges = [_edge("o1", "A", "B"), _edge("o2", "B", "A")]
        cycles = find_cycles(edges)
        assert len(cycles) == 1
        assert cycles[0].people == ("A", "B")

    def test_parallel_edges_do_not_duplicate_the_human_deadlock(self) -> None:
        # Two open obligations A→B plus one B→A: still one deadlock ring.
        edges = [
            _edge("o1", "A", "B"),
            _edge("o2", "A", "B"),
            _edge("o3", "B", "A"),
        ]
        assert len(find_cycles(edges)) == 1

    def test_dag_has_no_cycles(self) -> None:
        edges = [_edge("o1", "A", "B"), _edge("o2", "A", "C"), _edge("o3", "B", "C")]
        assert find_cycles(edges) == ()

    def test_self_loop_is_not_a_deadlock(self) -> None:
        assert find_cycles([_edge("o1", "A", "A")]) == ()

    def test_two_disjoint_rings_both_found_shortest_first(self) -> None:
        edges = [
            # 3-ring X, Y, Z
            _edge("o1", "X", "Y"),
            _edge("o2", "Y", "Z"),
            _edge("o3", "Z", "X"),
            # 2-ring A, B
            _edge("o4", "A", "B"),
            _edge("o5", "B", "A"),
        ]
        cycles = find_cycles(edges)
        assert [c.people for c in cycles] == [("A", "B"), ("X", "Y", "Z")]


# ---------------------------------------------------------------------------
# Edge filtering
# ---------------------------------------------------------------------------
class TestActiveEdges:
    def test_healed_and_dismissed_are_excluded_snoozed_kept(self) -> None:
        healed = _edge("o1", "A", "B", state=LoopState.HEALED)
        dismissed = _edge("o2", "B", "C", dismissed=True)
        snoozed = _edge("o3", "C", "D", snoozed_until="2999-01-01T00:00:00+00:00")
        open_edge = _edge("o4", "D", "E")
        kept = active_edges([healed, dismissed, snoozed, open_edge])
        assert [e.obligation_id for e in kept] == ["o3", "o4"]

    def test_broken_ring_via_healed_edge_is_not_a_deadlock(self) -> None:
        edges = [
            _edge("o1", "A", "B"),
            _edge("o2", "B", "C"),
            _edge("o3", "C", "A", state=LoopState.HEALED),
        ]
        assert analyze(edges).cycles == ()


# ---------------------------------------------------------------------------
# ChainReport helpers
# ---------------------------------------------------------------------------
class TestChainReport:
    def test_impact_and_heaviest(self) -> None:
        edges = [_edge("o1", "A", "B"), _edge("o2", "B", "C"), _edge("o3", "C", "D")]
        report = analyze(edges)
        assert report.impact("o1") == 3
        assert report.impact("o3") == 1
        assert report.impact("missing") == 0
        assert report.heaviest() == "o1"

    def test_heaviest_is_none_on_empty_graph(self) -> None:
        assert analyze([]).heaviest() is None

    def test_cycles_involving_filters_by_person(self) -> None:
        edges = [
            _edge("o1", "A", "B"),
            _edge("o2", "B", "A"),
            _edge("o3", "X", "Y"),
            _edge("o4", "Y", "X"),
        ]
        report = analyze(edges)
        assert len(report.cycles) == 2
        mine = report.cycles_involving("A")
        assert len(mine) == 1
        assert mine[0].people == ("A", "B")

    def test_report_is_deterministic_across_input_order(self) -> None:
        edges = [
            _edge("o1", "B", "C"),
            _edge("o2", "C", "A"),
            _edge("o3", "A", "B"),
            _edge("o4", "C", "D"),
        ]
        a, b = analyze(edges), analyze(list(reversed(edges)))
        assert a.downstream == b.downstream
        assert [c.people for c in a.cycles] == [c.people for c in b.cycles]


class TestBlockingCycleShape:
    def test_edges_chain_head_to_tail(self) -> None:
        edges = [_edge("o1", "B", "C"), _edge("o2", "C", "A"), _edge("o3", "A", "B")]
        (cycle,) = find_cycles(edges)
        n = len(cycle)
        assert n == 3
        for i in range(n):
            assert (
                cycle.edges[i].owed_person_id
                == cycle.edges[(i + 1) % n].owes_person_id
            )

    def test_len_matches_people(self) -> None:
        (cycle,) = find_cycles([_edge("o1", "A", "B"), _edge("o2", "B", "A")])
        assert len(cycle) == len(cycle.people) == 2
        assert isinstance(cycle, BlockingCycle)
