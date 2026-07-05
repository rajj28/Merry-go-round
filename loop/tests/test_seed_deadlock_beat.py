"""The seeded workspace-graph intelligence beat (Demo Beat 5).

Pins the deterministic contract of the seeded third-party edges: exactly one
deadlock ring (Frank → Hank → Ivy), exactly the designed chain pressure behind
the user's OBL_B3, and — critically — no disturbance to the personal surfacing
contract (hero == 3) that every other beat depends on.
"""

from __future__ import annotations

from loop.action.cycle_breaker import plan_cycle_break
from loop.graph.chains import analyze
from loop.graph.models import LoopState
from loop.seed.fixtures import seed_obligations


def _report():
    return analyze(seed_obligations())


def test_seed_has_exactly_one_deadlock_ring() -> None:
    report = _report()
    assert len(report.cycles) == 1
    assert report.cycles[0].people == ("U_FRANK", "U_HANK", "U_IVY")


def test_seed_ring_yields_a_deterministic_break_plan() -> None:
    (cycle,) = _report().cycles
    plan = plan_cycle_break(cycle)  # heuristic path: stalest edge
    assert plan.break_obligation_id == "OBL_RING1"  # oldest last_touch in the ring
    assert plan.break_edge.owes_person_id == "U_FRANK"


def test_seed_chain_pressure_behind_obl_b3() -> None:
    report = _report()
    # The user's design review for Carol transitively holds up Carol, Leo, Jack.
    assert report.downstream["OBL_B3"] == frozenset({"U_CAROL", "U_LEO", "U_JACK"})
    assert report.impact("OBL_B3") == 3
    # The other two surfaced blocked loops carry no chain weight.
    assert report.impact("OBL_B1") == 1
    assert report.impact("OBL_B2") == 1


def test_intelligence_edges_stay_below_the_surfacing_gate() -> None:
    ring_and_chain = {
        o.obligation_id: o
        for o in seed_obligations()
        if o.obligation_id.startswith(("OBL_RING", "OBL_CHAIN"))
    }
    assert len(ring_and_chain) == 5
    for o in ring_and_chain.values():
        assert o.confidence_score < 0.5  # below SEED_THRESHOLD → never surfaced
        assert o.loop_state is LoopState.WAITING_ON_OTHER
        assert not o.dismissed
