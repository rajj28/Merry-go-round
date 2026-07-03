"""Property-based + fault-injection tests for the SQLite Obligation Graph (tasks 3.5–3.10).

These exercise the *real* :class:`loop.graph.sqlite_store.SqliteObligationGraph`
(no mocking — an isolated ``:memory:`` database per example, matching the pattern in
``test_sqlite_store.py``) against the universal correctness properties from
design.md → "Correctness Properties":

  * 3.5 → Property 1 — Loop_State is always a valid enum value (Req 1.2, 1.3)
  * 3.6 → Property 2 — Confidence_Score stays in [0.0, 1.0] (Req 1.4, 1.5)
  * 3.7 → Property 3 — store/read round-trip preserves obligations (Req 1.6, 1.7, 3.3)
  * 3.8 → Property 4 — last-write-wins by timestamp (safety) (Req 1.9, 1.10)
  * 3.9 → Property 5 — data never leaves the workspace boundary (safety) (Req 1.11, 14.2, 14.6)
  * 3.10 → fault-injection unit test — graph persist failure retains last good value (Req 1.8)

Each property test runs ≥100 generated examples (enforced by the root ``conftest.py``)
and carries the required traceability tag.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from loop.graph.models import (
    LOOP_STATE_VALUES,
    ArtifactType,
    ClosureKind,
    LoopState,
    Obligation,
)
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import Err, GraphErrorCode, Ok, is_ok


# ---------------------------------------------------------------------------
# Helpers and custom Hypothesis strategies
# ---------------------------------------------------------------------------
def _graph() -> SqliteObligationGraph:
    """A fresh, isolated in-memory store (matches test_sqlite_store.py)."""
    return SqliteObligationGraph(database_path=IN_MEMORY)


# Printable-ASCII text keeps generated ids/summaries free of NULs and surrogate
# code points that SQLite's text storage rejects, without weakening coverage of
# the logic under test.
_SAFE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=40,
)

# Slack-style identifiers: short, non-empty, simple.
_IDS = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=12,
)

# Confidence values across the full inclusive range, boundaries included.
_VALID_CONFIDENCE = st.floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)

# Out-of-range confidence: strictly below 0.0 or strictly above 1.0 (finite).
# Built from two explicit half-open ranges so generation never relies on a filter.
_INVALID_CONFIDENCE = st.one_of(
    st.floats(
        min_value=-1e9, max_value=0.0, exclude_max=True,
        allow_nan=False, allow_infinity=False,
    ),
    st.floats(
        min_value=1.0, max_value=1e9, exclude_min=True,
        allow_nan=False, allow_infinity=False,
    ),
)

# Arbitrary strings that are NOT one of the three valid Loop_State values.
_INVALID_LOOP_STATE_STR = st.text(max_size=20).filter(
    lambda s: s not in LOOP_STATE_VALUES
)


@st.composite
def iso_timestamps(draw: st.DrawFn) -> str:
    """ISO 8601 UTC timestamp strings drawn from a bounded window.

    The window is deliberately narrow so equal timestamps recur across examples,
    exercising the identical-timestamp tie-break path (Req 1.10).
    """
    dt = draw(
        st.datetimes(
            min_value=datetime(2025, 1, 1, 0, 0, 0),
            max_value=datetime(2025, 1, 3, 0, 0, 0),
            timezones=st.just(timezone.utc),
        )
    )
    return dt.isoformat()


@st.composite
def obligations(draw: st.DrawFn, *, obligation_id: str | None = None) -> Obligation:
    """A valid :class:`Obligation` with randomized but in-contract fields.

    Covers: random loop_state, in-range confidence (incl. boundaries), random
    last_touch timestamps (incl. recurring/equal values), dismissed/snoozed flags,
    and optional GitHub artifact references and closure metadata.
    """
    oid = obligation_id if obligation_id is not None else draw(_IDS)
    state = draw(st.sampled_from(list(LoopState)))

    snoozed = draw(st.one_of(st.none(), iso_timestamps()))
    has_artifact = draw(st.booleans())
    closure = draw(st.one_of(st.none(), st.sampled_from(list(ClosureKind))))

    return Obligation(
        obligation_id=oid,
        owes_person_id=draw(_IDS),
        owed_person_id=draw(_IDS),
        owner_person_id=draw(_IDS),
        loop_state=state,
        confidence_score=draw(_VALID_CONFIDENCE),
        last_touch_timestamp=draw(iso_timestamps()),
        source_msg_channel=draw(_IDS),
        source_msg_ts=draw(_SAFE_TEXT),
        subject_summary=draw(_SAFE_TEXT),
        dismissed=draw(st.booleans()),
        snoozed_until=snoozed,
        artifact_type=ArtifactType.GITHUB_PR if has_artifact else None,
        artifact_ref=draw(_SAFE_TEXT) if has_artifact else None,
        closure_kind=closure,
        closure_timestamp=draw(st.one_of(st.none(), iso_timestamps())),
        closure_reason=draw(st.one_of(st.none(), _SAFE_TEXT)),
    )


def _with_invalid_loop_state(obligation: Obligation, bad: str) -> Obligation:
    """Copy ``obligation`` and inject a raw invalid Loop_State string.

    Bypasses the enum type to simulate an agent attempting to write a value
    outside the frozen vocabulary (mirrors test_sqlite_store.py).
    """
    clone = Obligation(**obligation.model_dump())
    object.__setattr__(clone, "loop_state", bad)
    return clone


def _with_confidence(obligation: Obligation, score: float) -> Obligation:
    clone = Obligation(**obligation.model_dump())
    object.__setattr__(clone, "confidence_score", score)
    return clone


# ---------------------------------------------------------------------------
# Property 1 (task 3.5) — Loop_State is always a valid enum value
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 1: Loop_State is always a valid enum value
@given(obligation=obligations(obligation_id="OBL"), bad_state=_INVALID_LOOP_STATE_STR)
def test_property_1_loop_state_always_valid_enum(
    obligation: Obligation, bad_state: str
) -> None:
    """Validates: Requirements 1.2, 1.3.

    Valid writes store a Loop_State within the frozen enum; an out-of-vocabulary
    write is rejected, the prior Loop_State is retained, and an error is returned.
    """
    graph = _graph()

    # Valid obligation stores, and the stored Loop_State is a valid enum value.
    result = graph.upsert(obligation)
    assert is_ok(result)
    stored = graph.get("OBL")
    assert stored is not None
    assert stored.loop_state in LOOP_STATE_VALUES or isinstance(
        stored.loop_state, LoopState
    )
    prior_state = stored.loop_state

    # An invalid Loop_State write is rejected; the prior value is retained.
    bad = _with_invalid_loop_state(obligation, bad_state)
    rejected = graph.upsert(bad)
    assert isinstance(rejected, Err)
    assert rejected.error.code is GraphErrorCode.INVALID_LOOP_STATE
    assert rejected.error.obligation_id == "OBL"

    retained = graph.get("OBL")
    assert retained is not None
    assert retained.loop_state == prior_state


# ---------------------------------------------------------------------------
# Property 2 (task 3.6) — Confidence_Score stays in [0.0, 1.0]
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 2: Confidence_Score stays in [0.0, 1.0]
@given(
    obligation=obligations(obligation_id="OBL"),
    bad_score=_INVALID_CONFIDENCE,
)
def test_property_2_confidence_score_in_range(
    obligation: Obligation, bad_score: float
) -> None:
    """Validates: Requirements 1.4, 1.5.

    Valid writes store a Confidence_Score in [0.0, 1.0]; an out-of-range write is
    rejected, the prior score is retained, and an error is returned.
    """
    graph = _graph()

    result = graph.upsert(obligation)
    assert is_ok(result)
    stored = graph.get("OBL")
    assert stored is not None
    assert 0.0 <= stored.confidence_score <= 1.0
    prior_score = stored.confidence_score

    bad = _with_confidence(obligation, bad_score)
    rejected = graph.upsert(bad)
    assert isinstance(rejected, Err)
    assert rejected.error.code is GraphErrorCode.INVALID_CONFIDENCE
    assert rejected.error.obligation_id == "OBL"

    retained = graph.get("OBL")
    assert retained is not None
    assert retained.confidence_score == prior_score


# ---------------------------------------------------------------------------
# Property 3 (task 3.7) — Store/read round-trip preserves obligations
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 3: Store/read round-trip preserves obligations
@given(obligation=obligations())
def test_property_3_store_read_round_trip(obligation: Obligation) -> None:
    """Validates: Requirements 1.6, 1.7, 3.3.

    Writing a valid obligation and reading it back yields an equal obligation,
    including last_touch_timestamp and the source Slack message reference.
    """
    graph = _graph()

    result = graph.upsert(obligation)
    assert is_ok(result)
    assert isinstance(result, Ok)

    fetched = graph.get(obligation.obligation_id)
    assert fetched is not None

    # Every field round-trips unchanged.
    assert fetched.model_dump() == obligation.model_dump()

    # Explicitly assert the called-out provenance fields (Req 1.6, 3.3).
    assert fetched.last_touch_timestamp == obligation.last_touch_timestamp
    assert fetched.source_msg_channel == obligation.source_msg_channel
    assert fetched.source_msg_ts == obligation.source_msg_ts


# ---------------------------------------------------------------------------
# Property 4 (task 3.8, SAFETY) — Last-write-wins by timestamp
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 4: Last-write-wins by timestamp
@given(
    base=obligations(obligation_id="OBL"),
    ts_a=iso_timestamps(),
    ts_b=iso_timestamps(),
    apply_b_first=st.booleans(),
)
def test_property_4_last_write_wins_by_timestamp(
    base: Obligation,
    ts_a: str,
    ts_b: str,
    apply_b_first: bool,
) -> None:
    """Validates: Requirements 1.9, 1.10.

    For two updates to the same obligation, the later last_touch_timestamp is
    retained regardless of apply order; on identical timestamps, the later-received
    write is retained.
    """
    graph = _graph()

    # Two updates to the same obligation, distinguished by owed_person_id so we can
    # tell which one the store retained.
    write_a = Obligation(**base.model_dump())
    object.__setattr__(write_a, "last_touch_timestamp", ts_a)
    object.__setattr__(write_a, "owed_person_id", "WRITE_A")

    write_b = Obligation(**base.model_dump())
    object.__setattr__(write_b, "last_touch_timestamp", ts_b)
    object.__setattr__(write_b, "owed_person_id", "WRITE_B")

    # Apply in the chosen order; both calls succeed (older write is silently kept,
    # not errored).
    first, second = (write_b, write_a) if apply_b_first else (write_a, write_b)
    assert is_ok(graph.upsert(first))
    assert is_ok(graph.upsert(second))

    stored = graph.get("OBL")
    assert stored is not None

    a_dt = datetime.fromisoformat(ts_a)
    b_dt = datetime.fromisoformat(ts_b)

    if a_dt > b_dt:
        expected = "WRITE_A"
    elif b_dt > a_dt:
        expected = "WRITE_B"
    else:
        # Identical timestamps -> the later-received (second-applied) write wins.
        expected = second.owed_person_id

    assert stored.owed_person_id == expected
    # The retained timestamp is the later of the two (or the common value on a tie).
    assert datetime.fromisoformat(stored.last_touch_timestamp) == max(a_dt, b_dt)


# ---------------------------------------------------------------------------
# Property 5 (task 3.9, SAFETY) — Data never leaves the workspace boundary
# ---------------------------------------------------------------------------
# Destinations that are guaranteed to be OUTSIDE the in-memory store's boundary
# (its only in-boundary destinations are None and the ":memory:" path).
_OUT_OF_BOUNDARY_DEST = st.one_of(
    st.text(min_size=1, max_size=60).filter(lambda s: s.strip() != IN_MEMORY),
    st.builds(object),
    st.integers(),
    st.lists(st.integers(), max_size=3),
)


# Feature: loop-obligation-agent, Property 5: Data never leaves the workspace boundary
@given(obligation=obligations(obligation_id="OBL"), dest=_OUT_OF_BOUNDARY_DEST)
def test_property_5_data_never_leaves_boundary(
    obligation: Obligation, dest: object
) -> None:
    """Validates: Requirements 1.11, 14.2, 14.6.

    Any attempt to write/copy graph data to an out-of-boundary destination is
    rejected, the data is retained inside the boundary, and an error is recorded.
    """
    graph = _graph()
    assert is_ok(graph.upsert(obligation))

    before = len(graph.boundary_violations)

    # The out-of-boundary write is refused (raised) — nothing is exported.
    with pytest.raises(PermissionError):
        graph.reject_external_write(dest)

    # An error indication was recorded for the refusal (Req 14.6).
    after = graph.boundary_violations
    assert len(after) == before + 1
    assert after[-1].code is GraphErrorCode.BOUNDARY_VIOLATION

    # The data is retained inside the boundary, intact and unchanged.
    retained = graph.get("OBL")
    assert retained is not None
    assert retained.model_dump() == obligation.model_dump()


# ---------------------------------------------------------------------------
# Task 3.10 — Fault-injection unit test: persist failure retains last good value
# ---------------------------------------------------------------------------
def _failing_commit(_session) -> None:
    raise RuntimeError("simulated commit failure")


def test_persist_failure_retains_last_good_value_and_returns_err() -> None:
    """Validates: Requirement 1.8.

    On a graph persist failure, the last successfully stored value is retained and
    the caller receives ``Err(PERSIST_FAILURE)`` (example-based fault injection,
    matching the established commit-override pattern).
    """
    graph = _graph()

    good = Obligation(
        obligation_id="OBL1",
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.7,
        last_touch_timestamp=datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary="needs a review",
    )
    assert is_ok(graph.upsert(good))

    # Inject a failing commit and attempt an update to the stored obligation.
    graph._commit = _failing_commit  # type: ignore[assignment]
    result = graph.upsert(_with_confidence(good, 0.2))

    assert isinstance(result, Err)
    assert result.error.code is GraphErrorCode.PERSIST_FAILURE
    assert result.error.obligation_id == "OBL1"

    # Restore commit and confirm the prior good value was retained.
    del graph._commit  # type: ignore[attr-defined]
    retained = graph.get("OBL1")
    assert retained is not None
    assert retained.confidence_score == 0.7


def test_persist_failure_on_new_obligation_creates_nothing() -> None:
    """Validates: Requirement 1.8.

    A persist failure on a brand-new obligation leaves the graph with no row for it.
    """
    graph = _graph()
    graph._commit = _failing_commit  # type: ignore[assignment]

    new = Obligation(
        obligation_id="NEW",
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=LoopState.WAITING_ON_OTHER,
        confidence_score=0.5,
        last_touch_timestamp=datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        source_msg_channel="C1",
        source_msg_ts="1700000000.000200",
        subject_summary="waiting on a reply",
    )
    result = graph.upsert(new)
    assert isinstance(result, Err)
    assert result.error.code is GraphErrorCode.PERSIST_FAILURE

    del graph._commit  # type: ignore[attr-defined]
    assert graph.get("NEW") is None
