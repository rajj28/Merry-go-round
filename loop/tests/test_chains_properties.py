"""Property-based tests for chain & cycle analysis (:mod:`loop.graph.chains`).

Universal correctness properties, each run over ≥100 generated graphs (enforced
by the root ``conftest.py``):

  * Property A — downstream sets always contain the owed person, never the
    debtor, and only people that actually appear in the graph.
  * Property B — a graph whose every edge points from a smaller to a larger
    person id is a DAG: no deadlock is ever reported.
  * Property C — a constructed ring of k distinct people is found exactly once,
    canonically rotated to start at its smallest person id.
  * Property D — every reported cycle is well-formed: edges chain head-to-tail,
    the ring closes, people are distinct, and length respects the cap.
  * Property E — the analysis is deterministic under input permutation.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from loop.graph.chains import analyze, downstream_people, find_cycles
from loop.graph.models import LoopState, Obligation

# A small person universe keeps graphs dense enough to be interesting while the
# 100-example floor stays fast.
_PERSONS = tuple("ABCDEFGH")


def _edge(oid: str, owes: str, owed: str) -> Obligation:
    return Obligation(
        obligation_id=oid,
        owes_person_id=owes,
        owed_person_id=owed,
        owner_person_id=owes,
        loop_state=LoopState.WAITING_ON_OTHER,
        confidence_score=0.9,
        last_touch_timestamp="2025-01-08T12:00:00+00:00",
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary=f"{owes} owes {owed}",
    )


# Random directed pairs (self-loops allowed — the analysis must tolerate them).
_PAIRS = st.lists(
    st.tuples(st.sampled_from(_PERSONS), st.sampled_from(_PERSONS)),
    max_size=16,
)


def _graph_from_pairs(pairs: list[tuple[str, str]]) -> list[Obligation]:
    return [_edge(f"o{i}", owes, owed) for i, (owes, owed) in enumerate(pairs)]


# ---------------------------------------------------------------------------
# Property A — downstream set membership invariants
# ---------------------------------------------------------------------------
@given(pairs=_PAIRS.filter(lambda ps: len(ps) >= 1))
def test_downstream_contains_owed_never_the_debtor(
    pairs: list[tuple[str, str]],
) -> None:
    edges = _graph_from_pairs(pairs)
    everyone = {e.owes_person_id for e in edges} | {e.owed_person_id for e in edges}
    for edge in edges:
        if edge.owes_person_id == edge.owed_person_id:
            continue  # self-loop: no human is waiting behind it
        behind = downstream_people(edges, edge)
        assert edge.owed_person_id in behind
        assert edge.owes_person_id not in behind
        assert behind <= everyone


# ---------------------------------------------------------------------------
# Property B — a topologically-ordered graph has no deadlocks
# ---------------------------------------------------------------------------
@given(pairs=_PAIRS)
def test_dag_never_reports_a_cycle(pairs: list[tuple[str, str]]) -> None:
    # Force every edge to point "uphill" (smaller id owes larger id): the
    # person ordering is then a topological order, so no directed ring exists.
    dag_pairs = [(min(a, b), max(a, b)) for a, b in pairs if a != b]
    assert find_cycles(_graph_from_pairs(dag_pairs)) == ()


# ---------------------------------------------------------------------------
# Property C — a constructed ring is found exactly once, canonically
# ---------------------------------------------------------------------------
@given(
    ring=st.lists(st.sampled_from(_PERSONS), min_size=2, max_size=8, unique=True),
)
def test_constructed_ring_found_once_and_canonical(ring: list[str]) -> None:
    pairs = [(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))]
    cycles = find_cycles(_graph_from_pairs(pairs))
    assert len(cycles) == 1
    people = cycles[0].people
    # Same ring, rotated so the smallest person id comes first.
    assert set(people) == set(ring)
    assert people[0] == min(ring)
    start = ring.index(people[0])
    assert list(people) == ring[start:] + ring[:start]


# ---------------------------------------------------------------------------
# Property D — every reported cycle is well-formed
# ---------------------------------------------------------------------------
@given(pairs=_PAIRS)
def test_reported_cycles_are_well_formed(pairs: list[tuple[str, str]]) -> None:
    edges = _graph_from_pairs(pairs)
    for cycle in find_cycles(edges):
        n = len(cycle.edges)
        assert 2 <= n <= 8
        people = cycle.people
        assert len(set(people)) == n  # each person exactly once
        assert people[0] == min(people)  # canonical rotation
        for i in range(n):
            assert (
                cycle.edges[i].owed_person_id
                == cycle.edges[(i + 1) % n].owes_person_id
            )
        for edge in cycle.edges:
            assert edge in edges  # only real edges are reported


# ---------------------------------------------------------------------------
# Property E — deterministic under input permutation
# ---------------------------------------------------------------------------
@given(pairs=_PAIRS, seed=st.randoms(use_true_random=False))
def test_analysis_is_order_independent(pairs, seed) -> None:
    edges = _graph_from_pairs(pairs)
    shuffled = list(edges)
    seed.shuffle(shuffled)
    a, b = analyze(edges), analyze(shuffled)
    assert a.downstream == b.downstream
    assert [c.people for c in a.cycles] == [c.people for c in b.cycles]
    assert a.heaviest() == b.heaviest()
