"""Unit tests for the Obligation Graph store **interface contract** (task 2.2).

These tests do not exercise persistence (that is task 3). They verify the day-1
interface freeze: the abstract base class :class:`ObligationGraph`, the
:class:`ObligationFilter` query shape, and the ``Result``/``GraphError`` rejection
contract are importable, instantiable via a trivial in-memory fake, and that every
method signature is satisfiable. They also pin the abstract-ness of the base class
and the stable :class:`GraphErrorCode` values.
"""

from __future__ import annotations

from typing import Optional

import pytest

from loop.graph.models import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    LoopState,
    Obligation,
    ObligationId,
    clamp_confidence,
    is_valid_confidence,
    is_valid_loop_state,
    utc_now_iso,
)
from loop.graph.store import (
    Err,
    GraphError,
    GraphErrorCode,
    ObligationFilter,
    ObligationGraph,
    Ok,
    Result,
    err,
    is_ok,
    ok,
)


# ---------------------------------------------------------------------------
# A trivial in-memory fake that satisfies the abstract interface.
# It implements just enough to honour the contract for signature exercising;
# the authoritative SQLite store is task 3.
# ---------------------------------------------------------------------------
class InMemoryFakeGraph(ObligationGraph):
    def __init__(self) -> None:
        self._store: dict[ObligationId, Obligation] = {}
        self._threshold: float = DEFAULT_CONFIDENCE_THRESHOLD

    def upsert(self, obligation: Obligation) -> Result[Obligation]:
        if not is_valid_loop_state(obligation.loop_state):
            return err(GraphErrorCode.INVALID_LOOP_STATE, obligation_id=obligation.obligation_id)
        if not is_valid_confidence(obligation.confidence_score):
            return err(GraphErrorCode.INVALID_CONFIDENCE, obligation_id=obligation.obligation_id)
        self._store[obligation.obligation_id] = obligation
        return ok(obligation)

    def get(self, obligation_id: ObligationId) -> Optional[Obligation]:
        return self._store.get(obligation_id)

    def query(self, filter: ObligationFilter) -> list[Obligation]:
        results = list(self._store.values())
        if filter.loop_states:
            results = [o for o in results if o.loop_state in filter.loop_states]
        if not filter.include_dismissed:
            results = [o for o in results if not o.dismissed]
        if filter.owner_person_id is not None:
            results = [o for o in results if o.owner_person_id == filter.owner_person_id]
        return results

    def set_threshold(self, value: float) -> Result[float]:
        self._threshold = clamp_confidence(value)
        return ok(self._threshold)

    def get_threshold(self) -> float:
        return self._threshold

    def reject_external_write(self, dest: object) -> None:
        # Trivial fake treats every destination as out-of-boundary.
        raise PermissionError(f"external write rejected: {dest!r}")


def _make_obligation(
    obligation_id: str = "OBL1",
    loop_state: LoopState = LoopState.BLOCKED_ON_YOU,
    confidence: float = 0.9,
) -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=loop_state,
        confidence_score=confidence,
        last_touch_timestamp=utc_now_iso(),
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary="needs a review",
    )


def test_base_class_is_abstract() -> None:
    # The interface cannot be instantiated directly — it is a contract.
    with pytest.raises(TypeError):
        ObligationGraph()  # type: ignore[abstract]


def test_fake_subclass_instantiates_and_satisfies_interface() -> None:
    graph = InMemoryFakeGraph()
    assert isinstance(graph, ObligationGraph)


def test_upsert_get_round_trip_via_fake() -> None:
    graph = InMemoryFakeGraph()
    obligation = _make_obligation()

    result = graph.upsert(obligation)
    assert is_ok(result)
    assert isinstance(result, Ok)
    assert result.value is obligation

    fetched = graph.get("OBL1")
    assert fetched is obligation
    assert graph.get("missing") is None


def test_upsert_rejects_out_of_range_confidence_returns_err() -> None:
    graph = InMemoryFakeGraph()
    bad = _make_obligation(confidence=1.5)

    result = graph.upsert(bad)
    assert isinstance(result, Err)
    assert result.error.code is GraphErrorCode.INVALID_CONFIDENCE
    assert result.error.obligation_id == "OBL1"
    # Rejected write did not take effect.
    assert graph.get("OBL1") is None


def test_query_filters_by_state_and_dismissed() -> None:
    graph = InMemoryFakeGraph()
    graph.upsert(_make_obligation("A", LoopState.BLOCKED_ON_YOU))
    graph.upsert(_make_obligation("B", LoopState.WAITING_ON_OTHER))

    blocked = graph.query(ObligationFilter(loop_states=frozenset({LoopState.BLOCKED_ON_YOU})))
    assert {o.obligation_id for o in blocked} == {"A"}

    everything = graph.query(ObligationFilter())
    assert {o.obligation_id for o in everything} == {"A", "B"}


def test_threshold_accessors_clamp() -> None:
    graph = InMemoryFakeGraph()
    assert graph.get_threshold() == DEFAULT_CONFIDENCE_THRESHOLD

    assert isinstance(graph.set_threshold(2.0), Ok)
    assert graph.get_threshold() == 1.0
    graph.set_threshold(-0.5)
    assert graph.get_threshold() == 0.0


def test_reject_external_write_guards_boundary() -> None:
    graph = InMemoryFakeGraph()
    with pytest.raises(PermissionError):
        graph.reject_external_write("https://example.com/exfil")


def test_result_helpers_and_error_shape() -> None:
    good: Result[int] = ok(7)
    assert isinstance(good, Ok) and good.value == 7 and is_ok(good)

    bad = err(GraphErrorCode.PERSIST_FAILURE, "disk full", obligation_id="X")
    assert isinstance(bad, Err) and not is_ok(bad)
    assert bad.error == GraphError(
        code=GraphErrorCode.PERSIST_FAILURE, message="disk full", obligation_id="X"
    )


def test_graph_error_code_values_are_stable() -> None:
    assert {c.value for c in GraphErrorCode} == {
        "invalid_loop_state",
        "invalid_confidence",
        "persist_failure",
        "boundary_violation",
    }


def test_obligation_filter_defaults_match_everything() -> None:
    f = ObligationFilter()
    assert f.loop_states == frozenset()
    assert f.surfaced_only is False
    assert f.include_dismissed is False
    assert f.owner_person_id is None
    assert f.min_age_seconds is None and f.max_age_seconds is None
