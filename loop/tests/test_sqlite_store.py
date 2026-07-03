"""Example-based unit tests for the SQLite-backed Obligation Graph (task 3.1).

These cover the task 3.1 deliverables only — ``upsert`` validation + ``get`` +
``query`` — using an isolated in-memory database per test:

  * valid upsert round-trips via ``get`` (Req 1.2, 1.4, 1.6, 1.7);
  * an invalid Loop_State is rejected with the prior value retained (Req 1.3);
  * an out-of-range Confidence_Score is rejected with the prior value retained (Req 1.5);
  * ``query`` honours its filter dimensions (state, dismissed, participants,
    artifact, closure kind, age window, and the ``surfaced_only`` gate).

Property-based coverage (Properties 1, 2, 3) lives in tasks 3.5–3.7; this file is
deliberately example-based.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from loop.graph.models import (
    ArtifactType,
    ClosureKind,
    LoopState,
    Obligation,
)
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import Err, GraphErrorCode, ObligationFilter, Ok, is_ok


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_obligation(
    obligation_id: str = "OBL1",
    *,
    loop_state: LoopState = LoopState.BLOCKED_ON_YOU,
    confidence: float = 0.9,
    owner: str = "U_USER",
    owes: str = "U_USER",
    owed: str = "U_OTHER",
    last_touch: str | None = None,
    dismissed: bool = False,
    snoozed_until: str | None = None,
    artifact_type: ArtifactType | None = None,
    artifact_ref: str | None = None,
    closure_kind: ClosureKind | None = None,
) -> Obligation:
    if last_touch is None:
        last_touch = _iso(datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc))
    return Obligation(
        obligation_id=obligation_id,
        owes_person_id=owes,
        owed_person_id=owed,
        owner_person_id=owner,
        loop_state=loop_state,
        confidence_score=confidence,
        last_touch_timestamp=last_touch,
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary="needs a review",
        dismissed=dismissed,
        snoozed_until=snoozed_until,
        artifact_type=artifact_type,
        artifact_ref=artifact_ref,
        closure_kind=closure_kind,
    )


# ---------------------------------------------------------------------------
# Valid upsert round-trips
# ---------------------------------------------------------------------------
def test_upsert_then_get_round_trip() -> None:
    graph = _graph()
    obligation = _make_obligation()

    result = graph.upsert(obligation)
    assert is_ok(result)
    assert isinstance(result, Ok)
    assert result.value.obligation_id == "OBL1"
    assert result.value.loop_state == LoopState.BLOCKED_ON_YOU
    assert result.value.confidence_score == 0.9

    fetched = graph.get("OBL1")
    assert fetched is not None
    assert fetched.obligation_id == "OBL1"
    assert fetched.owes_person_id == "U_USER"
    assert fetched.subject_summary == "needs a review"
    assert fetched.source_msg_channel == "C1"
    assert fetched.source_msg_ts == "1700000000.000100"


def test_get_missing_returns_none() -> None:
    graph = _graph()
    assert graph.get("does-not-exist") is None


def test_upsert_updates_existing_row() -> None:
    graph = _graph()
    graph.upsert(_make_obligation(confidence=0.6))

    updated = _make_obligation(confidence=0.8, loop_state=LoopState.WAITING_ON_OTHER)
    result = graph.upsert(updated)
    assert is_ok(result)

    fetched = graph.get("OBL1")
    assert fetched is not None
    assert fetched.confidence_score == 0.8
    assert fetched.loop_state == LoopState.WAITING_ON_OTHER


def test_upsert_accepts_boundary_confidence_values() -> None:
    graph = _graph()
    assert is_ok(graph.upsert(_make_obligation("LO", confidence=0.0)))
    assert is_ok(graph.upsert(_make_obligation("HI", confidence=1.0)))
    assert graph.get("LO").confidence_score == 0.0
    assert graph.get("HI").confidence_score == 1.0


# ---------------------------------------------------------------------------
# Invalid Loop_State rejected, prior retained (Req 1.3)
# ---------------------------------------------------------------------------
def test_invalid_loop_state_rejected_and_prior_retained() -> None:
    graph = _graph()
    graph.upsert(_make_obligation(loop_state=LoopState.BLOCKED_ON_YOU))

    bad = _make_obligation(loop_state=LoopState.BLOCKED_ON_YOU, confidence=0.9)
    # Bypass the enum type to simulate an agent writing a raw invalid string.
    object.__setattr__(bad, "loop_state", "not-a-real-state")

    result = graph.upsert(bad)
    assert isinstance(result, Err)
    assert result.error.code is GraphErrorCode.INVALID_LOOP_STATE
    assert result.error.obligation_id == "OBL1"

    # Prior value retained.
    retained = graph.get("OBL1")
    assert retained is not None
    assert retained.loop_state == LoopState.BLOCKED_ON_YOU


def test_invalid_loop_state_on_new_obligation_does_not_create_row() -> None:
    graph = _graph()
    bad = _make_obligation("NEW")
    object.__setattr__(bad, "loop_state", "garbage")

    result = graph.upsert(bad)
    assert isinstance(result, Err)
    assert result.error.code is GraphErrorCode.INVALID_LOOP_STATE
    assert graph.get("NEW") is None


# ---------------------------------------------------------------------------
# Out-of-range Confidence_Score rejected, prior retained (Req 1.5)
# ---------------------------------------------------------------------------
def test_out_of_range_confidence_rejected_and_prior_retained() -> None:
    graph = _graph()
    graph.upsert(_make_obligation(confidence=0.7))

    too_high = _make_obligation(confidence=1.5)
    result = graph.upsert(too_high)
    assert isinstance(result, Err)
    assert result.error.code is GraphErrorCode.INVALID_CONFIDENCE
    assert result.error.obligation_id == "OBL1"
    assert graph.get("OBL1").confidence_score == 0.7

    too_low = _make_obligation(confidence=-0.1)
    result2 = graph.upsert(too_low)
    assert isinstance(result2, Err)
    assert result2.error.code is GraphErrorCode.INVALID_CONFIDENCE
    # Still the original good value.
    assert graph.get("OBL1").confidence_score == 0.7


def test_out_of_range_confidence_on_new_obligation_does_not_create_row() -> None:
    graph = _graph()
    result = graph.upsert(_make_obligation("NEW", confidence=2.0))
    assert isinstance(result, Err)
    assert result.error.code is GraphErrorCode.INVALID_CONFIDENCE
    assert graph.get("NEW") is None


# ---------------------------------------------------------------------------
# Query filters
# ---------------------------------------------------------------------------
def test_query_empty_filter_matches_all_non_dismissed() -> None:
    graph = _graph()
    graph.upsert(_make_obligation("A"))
    graph.upsert(_make_obligation("B", loop_state=LoopState.WAITING_ON_OTHER))

    results = graph.query(ObligationFilter())
    assert {o.obligation_id for o in results} == {"A", "B"}


def test_query_filters_by_loop_state() -> None:
    graph = _graph()
    graph.upsert(_make_obligation("A", loop_state=LoopState.BLOCKED_ON_YOU))
    graph.upsert(_make_obligation("B", loop_state=LoopState.WAITING_ON_OTHER))
    graph.upsert(_make_obligation("C", loop_state=LoopState.HEALED, closure_kind=ClosureKind.AUTONOMOUS))

    blocked = graph.query(
        ObligationFilter(loop_states=frozenset({LoopState.BLOCKED_ON_YOU}))
    )
    assert {o.obligation_id for o in blocked} == {"A"}


def test_query_excludes_dismissed_by_default_and_includes_when_asked() -> None:
    graph = _graph()
    graph.upsert(_make_obligation("A"))
    graph.upsert(_make_obligation("B", dismissed=True))

    default = graph.query(ObligationFilter())
    assert {o.obligation_id for o in default} == {"A"}

    with_dismissed = graph.query(ObligationFilter(include_dismissed=True))
    assert {o.obligation_id for o in with_dismissed} == {"A", "B"}


def test_query_filters_by_participants() -> None:
    graph = _graph()
    graph.upsert(_make_obligation("A", owner="U1", owes="U1", owed="U2"))
    graph.upsert(_make_obligation("B", owner="U3", owes="U3", owed="U1"))

    by_owner = graph.query(ObligationFilter(owner_person_id="U1"))
    assert {o.obligation_id for o in by_owner} == {"A"}

    by_owes = graph.query(ObligationFilter(owes_person_id="U3"))
    assert {o.obligation_id for o in by_owes} == {"B"}

    by_owed = graph.query(ObligationFilter(owed_person_id="U1"))
    assert {o.obligation_id for o in by_owed} == {"B"}


def test_query_filters_by_artifact_and_closure_kind() -> None:
    graph = _graph()
    graph.upsert(
        _make_obligation(
            "A",
            artifact_type=ArtifactType.GITHUB_PR,
            artifact_ref="owner/repo#1",
        )
    )
    graph.upsert(_make_obligation("B"))
    graph.upsert(
        _make_obligation(
            "C",
            loop_state=LoopState.HEALED,
            closure_kind=ClosureKind.AUTONOMOUS,
        )
    )

    by_artifact_type = graph.query(ObligationFilter(artifact_type=ArtifactType.GITHUB_PR))
    assert {o.obligation_id for o in by_artifact_type} == {"A"}

    by_artifact_ref = graph.query(ObligationFilter(artifact_ref="owner/repo#1"))
    assert {o.obligation_id for o in by_artifact_ref} == {"A"}

    by_closure = graph.query(
        ObligationFilter(
            loop_states=frozenset({LoopState.HEALED}),
            closure_kind=ClosureKind.AUTONOMOUS,
        )
    )
    assert {o.obligation_id for o in by_closure} == {"C"}


def test_query_surfaced_only_applies_threshold_and_predicate() -> None:
    graph = _graph()
    now = _iso(datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc))
    graph.set_threshold(0.5)

    graph.upsert(_make_obligation("HIGH", confidence=0.8, last_touch=now))
    graph.upsert(_make_obligation("LOW", confidence=0.2, last_touch=now))
    graph.upsert(_make_obligation("DISMISSED", confidence=0.9, dismissed=True, last_touch=now))
    graph.upsert(
        _make_obligation(
            "HEALED",
            loop_state=LoopState.HEALED,
            confidence=0.9,
            closure_kind=ClosureKind.AUTONOMOUS,
            last_touch=now,
        )
    )

    surfaced = graph.query(ObligationFilter(surfaced_only=True, now=now))
    # Only the high-confidence, non-dismissed, active obligation surfaces.
    assert {o.obligation_id for o in surfaced} == {"HIGH"}


def test_query_age_window_filters_by_last_touch() -> None:
    graph = _graph()
    now = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc)
    fresh = _iso(now - timedelta(hours=1))
    old = _iso(now - timedelta(hours=100))

    graph.upsert(_make_obligation("FRESH", last_touch=fresh))
    graph.upsert(_make_obligation("OLD", last_touch=old))

    # Older than 72h.
    overdue = graph.query(
        ObligationFilter(min_age_seconds=72 * 3600, now=_iso(now))
    )
    assert {o.obligation_id for o in overdue} == {"OLD"}

    # Younger than 24h.
    recent = graph.query(
        ObligationFilter(max_age_seconds=24 * 3600, now=_iso(now))
    )
    assert {o.obligation_id for o in recent} == {"FRESH"}


# ---------------------------------------------------------------------------
# Last-write-wins conflict resolution (task 3.2 — Req 1.9, 1.10)
# ---------------------------------------------------------------------------
def test_later_timestamp_wins() -> None:
    graph = _graph()
    older = _iso(datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc))
    newer = _iso(datetime(2025, 1, 8, 13, 0, 0, tzinfo=timezone.utc))

    graph.upsert(_make_obligation(last_touch=older, confidence=0.4))
    graph.upsert(_make_obligation(last_touch=newer, confidence=0.9))

    fetched = graph.get("OBL1")
    assert fetched is not None
    assert fetched.last_touch_timestamp == newer
    assert fetched.confidence_score == 0.9


def test_earlier_timestamp_is_discarded() -> None:
    graph = _graph()
    older = _iso(datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc))
    newer = _iso(datetime(2025, 1, 8, 13, 0, 0, tzinfo=timezone.utc))

    # Store the newer write first, then attempt an out-of-order older write.
    graph.upsert(_make_obligation(last_touch=newer, confidence=0.9))
    result = graph.upsert(_make_obligation(last_touch=older, confidence=0.1))

    # The call still succeeds, but the stored (newer) value is retained.
    assert is_ok(result)
    fetched = graph.get("OBL1")
    assert fetched is not None
    assert fetched.last_touch_timestamp == newer
    assert fetched.confidence_score == 0.9


def test_identical_timestamp_later_received_wins() -> None:
    graph = _graph()
    same = _iso(datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc))

    graph.upsert(_make_obligation(last_touch=same, confidence=0.3, owed="U_FIRST"))
    graph.upsert(_make_obligation(last_touch=same, confidence=0.7, owed="U_SECOND"))

    fetched = graph.get("OBL1")
    assert fetched is not None
    # On a timestamp tie the later-received write replaces the earlier one.
    assert fetched.confidence_score == 0.7
    assert fetched.owed_person_id == "U_SECOND"


def test_last_write_wins_handles_z_suffix_timestamps() -> None:
    graph = _graph()
    graph.upsert(_make_obligation(last_touch="2025-01-08T12:00:00Z", confidence=0.4))
    # Same instant expressed with an explicit offset, but a strictly later time.
    graph.upsert(
        _make_obligation(last_touch="2025-01-08T12:00:01+00:00", confidence=0.8)
    )

    fetched = graph.get("OBL1")
    assert fetched is not None
    assert fetched.confidence_score == 0.8


# ---------------------------------------------------------------------------
# Persist-failure handling (task 3.3 — Req 1.8)
# ---------------------------------------------------------------------------
def _failing_commit(_session) -> None:
    raise RuntimeError("simulated commit failure")


def test_upsert_persist_failure_returns_err_and_retains_prior() -> None:
    graph = _graph()
    # Establish a known-good stored value first.
    assert is_ok(graph.upsert(_make_obligation(confidence=0.7)))

    # Inject a failing commit and attempt an update.
    graph._commit = _failing_commit  # type: ignore[assignment]
    result = graph.upsert(_make_obligation(confidence=0.2))

    assert isinstance(result, Err)
    assert result.error.code is GraphErrorCode.PERSIST_FAILURE
    assert result.error.obligation_id == "OBL1"

    # Restore commit and confirm the prior good value was retained.
    del graph._commit  # type: ignore[attr-defined]
    retained = graph.get("OBL1")
    assert retained is not None
    assert retained.confidence_score == 0.7


def test_upsert_persist_failure_on_new_obligation_creates_nothing() -> None:
    graph = _graph()
    graph._commit = _failing_commit  # type: ignore[assignment]

    result = graph.upsert(_make_obligation("NEW", confidence=0.5))
    assert isinstance(result, Err)
    assert result.error.code is GraphErrorCode.PERSIST_FAILURE

    del graph._commit  # type: ignore[attr-defined]
    assert graph.get("NEW") is None


# ---------------------------------------------------------------------------
# Read-after-write visibility within 1s (task 3.3 — Req 1.7)
# ---------------------------------------------------------------------------
def test_read_after_write_visibility() -> None:
    graph = _graph()
    result = graph.upsert(_make_obligation(confidence=0.55))
    assert is_ok(result)
    # Immediately readable after the upsert returns Ok.
    fetched = graph.get("OBL1")
    assert fetched is not None
    assert fetched.confidence_score == 0.55


# ---------------------------------------------------------------------------
# Workspace boundary guard (task 3.3 — Req 1.11, 14.2, 14.6)
# ---------------------------------------------------------------------------
def test_reject_external_write_allows_in_boundary() -> None:
    graph = _graph()
    # No external sink, and the store's own database path, are both in-boundary.
    graph.reject_external_write(None)
    graph.reject_external_write(IN_MEMORY)
    assert graph.boundary_violations == []


def test_reject_external_write_rejects_out_of_boundary() -> None:
    graph = _graph()

    for dest in ("https://example.com/exfil", "/tmp/exfil.db", object()):
        with pytest.raises(PermissionError):
            graph.reject_external_write(dest)

    # Each refusal recorded an error indication (Req 14.6).
    assert len(graph.boundary_violations) == 3
    assert all(
        e.code is GraphErrorCode.BOUNDARY_VIOLATION for e in graph.boundary_violations
    )


def test_reject_external_write_allows_configured_file_path(tmp_path) -> None:
    db_file = str(tmp_path / "loop.db")
    graph = SqliteObligationGraph(database_path=db_file)
    # The store's own configured database file is in-boundary.
    graph.reject_external_write(db_file)
    # A different file path is out-of-boundary.
    with pytest.raises(PermissionError):
        graph.reject_external_write(str(tmp_path / "other.db"))


# ---------------------------------------------------------------------------
# Threshold accessors with clamping + persistence (task 3.4 — Req 5.6)
# ---------------------------------------------------------------------------
def test_threshold_default_before_set() -> None:
    from loop.graph.models import DEFAULT_CONFIDENCE_THRESHOLD

    graph = _graph()
    assert graph.get_threshold() == DEFAULT_CONFIDENCE_THRESHOLD


def test_set_threshold_round_trip() -> None:
    graph = _graph()
    result = graph.set_threshold(0.42)
    assert isinstance(result, Ok)
    assert result.value == 0.42
    assert graph.get_threshold() == 0.42


def test_set_threshold_clamps_to_inclusive_range() -> None:
    graph = _graph()
    assert graph.set_threshold(1.5).value == 1.0
    assert graph.get_threshold() == 1.0

    assert graph.set_threshold(-0.3).value == 0.0
    assert graph.get_threshold() == 0.0

    # Inclusive bounds are accepted exactly.
    assert graph.set_threshold(0.0).value == 0.0
    assert graph.set_threshold(1.0).value == 1.0


def test_threshold_persists_across_store_instances(tmp_path) -> None:
    db_file = str(tmp_path / "loop.db")
    first = SqliteObligationGraph(database_path=db_file)
    first.set_threshold(0.73)

    # A fresh store over the same DB file observes the persisted threshold.
    second = SqliteObligationGraph(database_path=db_file)
    assert second.get_threshold() == 0.73


def test_set_threshold_persist_failure_returns_err_and_retains_prior() -> None:
    graph = _graph()
    graph.set_threshold(0.6)

    graph._commit = _failing_commit  # type: ignore[assignment]
    result = graph.set_threshold(0.9)
    assert isinstance(result, Err)
    assert result.error.code is GraphErrorCode.PERSIST_FAILURE

    del graph._commit  # type: ignore[attr-defined]
    # Prior threshold retained.
    assert graph.get_threshold() == 0.6
