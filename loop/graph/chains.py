"""Chain & cycle analysis over the Obligation Graph — the workspace blocking graph.

The store already persists obligations as **directed edges between arbitrary
people** (``owes_person_id -> owed_person_id``, see :mod:`loop.graph.models`), so
the graph is not limited to edges that touch the tracked User. This module adds
the structural intelligence layered on top of those edges:

  * **Downstream impact** — for every open edge ``A owes B``, the set of people
    *transitively* stuck behind A's inaction: B directly, plus anyone waiting on
    something B owes, and so on. This is what turns a flat reminder ("Priya is
    waiting on you") into a prioritized one ("this one review is holding up 4
    people across 2 teams").
  * **Deadlock (cycle) detection** — ``A owes B, B owes C, C owes A``: every
    party is simultaneously blocked and blocking, so no amount of individual
    nudging can resolve it. Loop detects the cycle so it can be broken
    deliberately (see :mod:`loop.action.cycle_breaker` for the reasoning step
    that picks *where* to break it).

Purity discipline (same as :mod:`loop.graph.surfacing` — read before editing):
every function here is a **pure** function of its arguments — no DB, no I/O, no
clock reads, no globals. Callers fetch the edges (e.g. via
``graph.query(ObligationFilter(loop_states=ACTIVE_SURFACE_STATES))``) and pass
them in. All outputs are deterministically ordered so the seeded demo and the
property tests reproduce exactly.

Semantics notes:
  * Only the two *active* loop states participate; ``healed`` edges are ignored
    (a resolved obligation blocks nobody). Dismissed edges are excluded — the
    user has declared them not real.
  * Snoozed edges still participate: snoozing hides a card, it does not unblock
    the person waiting.
  * Cycles are reported at the *person* level (each person appears once) with
    one representative obligation per hop, chosen deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from loop.graph.models import LoopState, Obligation, ObligationId, PersonId

# The two active states — kept in sync with surfacing.ACTIVE_SURFACE_STATES but
# defined from the model enum so this module depends only on models.
_ACTIVE_STATES: frozenset[LoopState] = frozenset(
    {LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER}
)

# Guard rail for cycle enumeration: obligation graphs are people-sized, but a
# pathological workspace should degrade to "the shortest deadlocks" rather than
# an exponential search.
DEFAULT_MAX_CYCLE_LENGTH = 8


@dataclass(frozen=True)
class BlockingCycle:
    """One deadlock: a closed ring of open obligations.

    The edges chain head-to-tail — ``edges[i].owed_person_id ==
    edges[(i + 1) % len].owes_person_id`` — and the last edge closes back onto
    the first, so every party is simultaneously blocked and blocking.
    ``people`` is the ring of distinct debtors in traversal order, canonically
    rotated so the lexicographically smallest person id comes first (making the
    representation — and therefore dedup and tests — deterministic).
    """

    edges: tuple[Obligation, ...]

    @property
    def people(self) -> tuple[PersonId, ...]:
        """The ring of debtors in traversal order (each person exactly once)."""
        return tuple(edge.owes_person_id for edge in self.edges)

    def __len__(self) -> int:
        return len(self.edges)


@dataclass(frozen=True)
class ChainReport:
    """The full structural analysis of one set of active edges.

    Attributes:
        downstream: per open obligation, the set of people transitively waiting
            behind it (always includes the directly-owed person). ``len`` of a
            value is the edge's *downstream impact* — the prioritization weight.
        cycles: every distinct deadlock found (person-level dedup), sorted by
            (length, people ring) so shorter deadlocks — the actionable ones —
            come first.
    """

    downstream: Mapping[ObligationId, frozenset[PersonId]]
    cycles: tuple[BlockingCycle, ...]

    def impact(self, obligation_id: ObligationId) -> int:
        """Downstream impact of one edge (0 for unknown/inactive ids)."""
        return len(self.downstream.get(obligation_id, frozenset()))

    def heaviest(self) -> Optional[ObligationId]:
        """The obligation holding up the most people (ties: smallest id).

        Returns None when there are no active edges.
        """
        if not self.downstream:
            return None
        return min(
            self.downstream,
            key=lambda oid: (-len(self.downstream[oid]), oid),
        )

    def cycles_involving(self, person_id: PersonId) -> tuple[BlockingCycle, ...]:
        """The deadlocks this person is part of (for the personal surface)."""
        return tuple(c for c in self.cycles if person_id in c.people)


def active_edges(obligations: Iterable[Obligation]) -> list[Obligation]:
    """Filter to the edges that participate in chain/cycle analysis.

    Active loop state and not dismissed; snoozed edges are kept (see module
    docstring). Output is sorted by ``obligation_id`` so every downstream
    computation is deterministic.
    """
    kept = [
        o
        for o in obligations
        if o.loop_state in _ACTIVE_STATES and not o.dismissed
    ]
    return sorted(kept, key=lambda o: o.obligation_id)


def _adjacency(edges: Sequence[Obligation]) -> dict[PersonId, tuple[Obligation, ...]]:
    """Out-edges per debtor: ``adjacency[P]`` = obligations P currently owes.

    Each list is sorted by (owed person, obligation id) for determinism.
    """
    out: dict[PersonId, list[Obligation]] = {}
    for edge in edges:
        out.setdefault(edge.owes_person_id, []).append(edge)
    return {
        person: tuple(sorted(group, key=lambda o: (o.owed_person_id, o.obligation_id)))
        for person, group in out.items()
    }


def downstream_people(
    edges: Sequence[Obligation], obligation: Obligation
) -> frozenset[PersonId]:
    """People transitively waiting behind one edge (BFS from the owed party).

    ``B`` (the directly-owed person) is always included. From B we follow the
    obligations *B owes* (B cannot deliver while waiting on A — the structural
    assumption that makes a chain a chain), then what those people owe, and so
    on. The debtor A is excluded even when a cycle routes back to them — A is
    the blocker here, not someone waiting behind themself — and the walk never
    continues *through* A, so people waiting on A's unrelated obligations are
    not misattributed to this edge.
    """
    adjacency = _adjacency(edges)
    blocker = obligation.owes_person_id
    seen: set[PersonId] = {obligation.owed_person_id}
    frontier: list[PersonId] = [obligation.owed_person_id]
    while frontier:
        person = frontier.pop()
        if person == blocker:
            continue
        for edge in adjacency.get(person, ()):
            if edge.owed_person_id not in seen:
                seen.add(edge.owed_person_id)
                frontier.append(edge.owed_person_id)
    seen.discard(blocker)
    return frozenset(seen)


def find_cycles(
    edges: Sequence[Obligation],
    *,
    max_length: int = DEFAULT_MAX_CYCLE_LENGTH,
) -> tuple[BlockingCycle, ...]:
    """Enumerate every distinct simple deadlock ring up to ``max_length`` people.

    Person-level dedup: between any ordered pair of people only the first
    obligation (by owed person, then id — the :func:`_adjacency` order) is used
    as the representative hop, so two parallel edges A→B never produce two
    copies of the same human deadlock. Each cycle is found exactly once by the
    canonical-root rule: a ring is only emitted from the DFS rooted at its
    lexicographically smallest person id, and the DFS never descends to a person
    smaller than the root.
    """
    adjacency = _adjacency(edges)
    # One representative edge per ordered (owes, owed) pair — adjacency order.
    representative: dict[tuple[PersonId, PersonId], Obligation] = {}
    successors: dict[PersonId, list[PersonId]] = {}
    for person, out in adjacency.items():
        for edge in out:
            pair = (person, edge.owed_person_id)
            if pair not in representative:
                representative[pair] = edge
                successors.setdefault(person, []).append(edge.owed_person_id)

    cycles: list[BlockingCycle] = []
    for root in sorted(successors):
        # DFS over persons >= root; closing back to root emits a canonical ring.
        stack: list[tuple[PersonId, list[PersonId]]] = [(root, [root])]
        while stack:
            person, path = stack.pop()
            for nxt in successors.get(person, ()):
                if nxt == root and len(path) >= 2:
                    ring = tuple(path)
                    cycles.append(
                        BlockingCycle(
                            edges=tuple(
                                representative[(ring[i], ring[(i + 1) % len(ring)])]
                                for i in range(len(ring))
                            )
                        )
                    )
                elif nxt > root and nxt not in path and len(path) < max_length:
                    stack.append((nxt, path + [nxt]))

    # Self-loops (A owes A) are not human deadlocks; the len >= 2 guard above
    # excludes them. Sort short-first, then by the people ring, for determinism.
    cycles.sort(key=lambda c: (len(c), c.people))
    return tuple(cycles)


def analyze(
    obligations: Iterable[Obligation],
    *,
    max_cycle_length: int = DEFAULT_MAX_CYCLE_LENGTH,
) -> ChainReport:
    """Run the full structural analysis over a set of obligations.

    Filters to :func:`active_edges`, then computes every edge's downstream set
    and every deadlock ring. Pure and deterministic: equal inputs (in any
    iteration order) produce an identical report.
    """
    edges = active_edges(obligations)
    downstream = {
        edge.obligation_id: downstream_people(edges, edge) for edge in edges
    }
    return ChainReport(
        downstream=downstream,
        cycles=find_cycles(edges, max_length=max_cycle_length),
    )


__all__ = [
    "BlockingCycle",
    "ChainReport",
    "DEFAULT_MAX_CYCLE_LENGTH",
    "active_edges",
    "downstream_people",
    "find_cycles",
    "analyze",
]
