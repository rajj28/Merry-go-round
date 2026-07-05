"""Tests for the Cycle Breaker Reason step (:mod:`loop.action.cycle_breaker`).

The contract under test: :func:`plan_cycle_break` always returns an actionable
plan — the injected smart-tier client's answer when it is valid, and the
deterministic stalest-edge heuristic on any raise, unknown edge id, or empty
text (failure containment, same posture as the AdjudicationQueue).
"""

from __future__ import annotations

from loop.action.cycle_breaker import (
    CYCLE_BREAK_DRAFT_MAX_CHARS,
    SOURCE_HEURISTIC,
    SOURCE_LLM,
    plan_cycle_break,
    stalest_edge,
)
from loop.graph.chains import BlockingCycle, find_cycles
from loop.graph.models import LoopState, Obligation


def _edge(oid: str, owes: str, owed: str, *, touched: str) -> Obligation:
    return Obligation(
        obligation_id=oid,
        owes_person_id=owes,
        owed_person_id=owed,
        owner_person_id=owes,
        loop_state=LoopState.WAITING_ON_OTHER,
        confidence_score=0.9,
        last_touch_timestamp=touched,
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary=f"{owes} owes {owed} ({oid})",
    )


def _ring() -> BlockingCycle:
    """A 3-person deadlock; o2 (B owes C) is the stalest edge."""
    edges = [
        _edge("o1", "A", "B", touched="2025-01-08T12:00:00+00:00"),
        _edge("o2", "B", "C", touched="2025-01-02T12:00:00+00:00"),
        _edge("o3", "C", "A", touched="2025-01-06T12:00:00+00:00"),
    ]
    (cycle,) = find_cycles(edges)
    return cycle


class TestStalestEdge:
    def test_picks_the_oldest_last_touch(self) -> None:
        assert stalest_edge(_ring()).obligation_id == "o2"

    def test_ties_break_on_obligation_id(self) -> None:
        same = "2025-01-02T12:00:00+00:00"
        edges = [
            _edge("o9", "A", "B", touched=same),
            _edge("o1", "B", "A", touched=same),
        ]
        (cycle,) = find_cycles(edges)
        assert stalest_edge(cycle).obligation_id == "o1"


class TestPlanCycleBreak:
    def test_valid_client_answer_is_used_verbatim(self) -> None:
        plan = plan_cycle_break(
            _ring(),
            client=lambda c: ("o3", "Smallest ask in the ring.", "Hi C — go first?"),
        )
        assert plan.source == SOURCE_LLM
        assert plan.break_obligation_id == "o3"
        assert plan.break_edge.owes_person_id == "C"
        assert plan.rationale == "Smallest ask in the ring."
        assert plan.draft_message == "Hi C — go first?"

    def test_no_client_falls_back_to_heuristic(self) -> None:
        plan = plan_cycle_break(_ring())
        assert plan.source == SOURCE_HEURISTIC
        assert plan.break_obligation_id == "o2"
        assert plan.rationale
        assert plan.draft_message

    def test_raising_client_is_contained(self) -> None:
        def boom(_cycle):  # noqa: ANN001
            raise RuntimeError("smart tier 503")

        plan = plan_cycle_break(_ring(), client=boom)
        assert plan.source == SOURCE_HEURISTIC
        assert plan.break_obligation_id == "o2"

    def test_unknown_edge_id_falls_back(self) -> None:
        plan = plan_cycle_break(
            _ring(), client=lambda c: ("nope", "reason", "draft")
        )
        assert plan.source == SOURCE_HEURISTIC

    def test_empty_rationale_or_draft_falls_back(self) -> None:
        for rationale, draft in [("", "draft"), ("reason", "   ")]:
            plan = plan_cycle_break(
                _ring(), client=lambda c, r=rationale, d=draft: ("o1", r, d)
            )
            assert plan.source == SOURCE_HEURISTIC

    def test_overlong_draft_is_clipped(self) -> None:
        plan = plan_cycle_break(
            _ring(), client=lambda c: ("o1", "why", "x" * 5000)
        )
        assert len(plan.draft_message) == CYCLE_BREAK_DRAFT_MAX_CHARS

    def test_heuristic_draft_names_the_ring_and_the_subject(self) -> None:
        plan = plan_cycle_break(_ring())
        assert "A → B → C → A" in plan.draft_message
        assert plan.break_edge.subject_summary in plan.draft_message
