"""Property-based tests for the Adjudicator (tasks 5.3 + 5.4 — Properties 8 & 9).

These are the two numbered correctness properties for the Adjudicator's reasoning
stage, complementing the example-based unit tests in ``test_adjudicator.py``.

Both properties drive the *real* :class:`loop.adjudicator.adjudicator.Adjudicator`
against:
  * a **mocked Opus reasoning port** — a thin injectable callable that returns a
    generated :class:`OpusAdjudication` (or raises, to model an Opus
    error/unreachable, Req 3.7); the live Anthropic call is never touched.
  * a fresh **in-memory Obligation Graph** (``:memory:`` SqliteObligationGraph) per
    example, so each property observes the graph effect of exactly one adjudication.

Property 8 (task 5.3) — direction matches who owes (Req 3.2): for any real,
user-involving loop with a known direction, the assigned Loop_State and the edge
endpoints (owes/owed) follow the direction exactly.

Property 9 (task 5.4) — non-loops produce no graph change (Req 3.4, 3.7): for any
adjudication that is not-a-loop / does-not-involve-user / unknown-direction, or any
Opus error (the port raises), the graph is left completely unchanged.

Runs >=100 Hypothesis examples each (enforced by the workspace conftest profile).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from loop.adjudicator.adjudicator import (
    AdjudicationOutcome,
    Adjudicator,
    Direction,
    OpusAdjudication,
)
from loop.graph.models import LoopState
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import ObligationFilter
from loop.watcher.rts_contract import CandidateMessage


# --------------------------------------------------------------------------- #
# Test doubles — the injectable Opus port
# --------------------------------------------------------------------------- #
class StubReasoningClient:
    """A mock Opus port that returns a fixed generated judgement."""

    def __init__(self, judgement: OpusAdjudication) -> None:
        self._judgement = judgement

    def __call__(self, candidate: CandidateMessage, *, user_id: str) -> OpusAdjudication:
        return self._judgement


class RaisingReasoningClient:
    """A mock Opus port that raises, modelling an Opus error/unreachable (Req 3.7)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self, candidate: CandidateMessage, *, user_id: str) -> OpusAdjudication:
        raise self._exc


def _graph() -> SqliteObligationGraph:
    """A fresh, isolated in-memory graph per example."""
    return SqliteObligationGraph(database_path=IN_MEMORY)


# --------------------------------------------------------------------------- #
# Smart generators constrained to the Adjudicator's real input space
# --------------------------------------------------------------------------- #
# Slack user ids: "U" + uppercase-alnum suffix. We draw the tracked user and the
# message author from disjoint pools so the edge endpoints are always two distinct
# parties (a self-loop is not a meaningful obligation).
_ID_SUFFIX = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=4, max_size=8
)
user_ids = _ID_SUFFIX.map(lambda s: f"U_USER_{s}")
other_ids = _ID_SUFFIX.map(lambda s: f"U_OTHER_{s}")

# Slack message ts: "<epoch seconds>.<6-digit sequence>" — a plausible, parseable ts.
message_ts = st.builds(
    lambda secs, seq: f"{secs}.{seq:06d}",
    st.integers(min_value=1_000_000_000, max_value=2_000_000_000),
    st.integers(min_value=0, max_value=999_999),
)
channel_ids = _ID_SUFFIX.map(lambda s: f"C{s}")

# Confidence: any real number, including out-of-range values (clamped on use,
# Req 3.3). Direction is independent of confidence, so we exercise the full range.
confidences = st.floats(
    min_value=-1.0, max_value=2.0, allow_nan=False, allow_infinity=False
)


@st.composite
def candidates(draw: st.DrawFn, *, author_id: str) -> CandidateMessage:
    """Generate a Watcher candidate authored by ``author_id`` (the other party)."""
    channel = draw(channel_ids)
    ts = draw(message_ts)
    return CandidateMessage(
        channel_id=channel,
        message_ts=ts,
        author_id=author_id,
        text=draw(st.text(max_size=200)),
        permalink=f"https://example.slack.com/archives/{channel}/p{ts.replace('.', '')}",
    )


# --------------------------------------------------------------------------- #
# Property 8: Adjudicated direction matches who owes (task 5.3)
# --------------------------------------------------------------------------- #
# Feature: loop-obligation-agent, Property 8: Adjudicated direction matches who owes.
# For any candidate the Adjudicator judges a real open loop involving the user, the
# assigned Loop_State is blocked-on-you exactly when the user owes
# (Direction.USER_OWES) and waiting-on-other exactly when the other owes
# (Direction.OTHER_OWES); edge endpoints (owes/owed) match.
# Validates: Requirements 3.2
@given(
    user_id=user_ids,
    other_id=other_ids,
    direction=st.sampled_from([Direction.USER_OWES, Direction.OTHER_OWES]),
    confidence=confidences,
    summary=st.text(max_size=120),
    data=st.data(),
)
def test_adjudicated_direction_matches_who_owes(
    user_id: str,
    other_id: str,
    direction: Direction,
    confidence: float,
    summary: str,
    data: st.DataObject,
) -> None:
    """Loop_State and edge endpoints follow the judged direction exactly (Req 3.2)."""
    candidate = data.draw(candidates(author_id=other_id))
    judgement = OpusAdjudication(
        is_loop=True,
        involves_user=True,
        direction=direction,
        confidence=confidence,
        subject_summary=summary,
    )
    graph = _graph()
    adjudicator = Adjudicator(StubReasoningClient(judgement), graph)

    result = adjudicator.adjudicate(candidate, user_id=user_id)

    # A real, user-involving loop with a known direction is always written.
    assert result.outcome is AdjudicationOutcome.CREATED
    ob = result.obligation
    assert ob is not None

    if direction is Direction.USER_OWES:
        # The user owes the reply -> blocked-on-you, edge user -> other.
        assert ob.loop_state is LoopState.BLOCKED_ON_YOU
        assert ob.owes_person_id == user_id
        assert ob.owed_person_id == other_id
    else:
        # The other party owes the reply -> waiting-on-other, edge other -> user.
        assert ob.loop_state is LoopState.WAITING_ON_OTHER
        assert ob.owes_person_id == other_id
        assert ob.owed_person_id == user_id

    # The owner of an open loop is always the party who owes the response.
    assert ob.owner_person_id == ob.owes_person_id

    # The same direction/endpoint relationship is durable in the graph.
    stored = graph.get(ob.obligation_id)
    assert stored is not None
    assert stored.loop_state is ob.loop_state
    assert stored.owes_person_id == ob.owes_person_id
    assert stored.owed_person_id == ob.owed_person_id


# --------------------------------------------------------------------------- #
# Property 9: Non-loops produce no graph change (task 5.4)
# --------------------------------------------------------------------------- #
# Negative-case judgements: each is a *valid* OpusAdjudication that the Adjudicator
# must discard with no write (Req 3.4) — not-a-loop, does-not-involve-user, and
# unknown-direction. is_loop/involves_user are otherwise free to vary so the
# generator covers the full discard surface, not just one canonical shape.
@st.composite
def non_loop_judgements(draw: st.DrawFn) -> OpusAdjudication:
    """Generate a judgement that must be DISCARDED (one of the three Req 3.4 cases)."""
    case = draw(st.sampled_from(["not_a_loop", "no_user", "unknown_direction"]))
    confidence = draw(confidences)
    summary = draw(st.text(max_size=120))
    if case == "not_a_loop":
        return OpusAdjudication(
            is_loop=False,
            involves_user=draw(st.booleans()),
            direction=draw(st.sampled_from(list(Direction))),
            confidence=confidence,
            subject_summary=summary,
        )
    if case == "no_user":
        # A real loop, but it does not involve the tracked user.
        return OpusAdjudication(
            is_loop=True,
            involves_user=False,
            direction=draw(st.sampled_from(list(Direction))),
            confidence=confidence,
            subject_summary=summary,
        )
    # unknown_direction: a real, user-involving loop whose direction is unclear.
    return OpusAdjudication(
        is_loop=True,
        involves_user=True,
        direction=Direction.UNKNOWN,
        confidence=confidence,
        subject_summary=summary,
    )


# Feature: loop-obligation-agent, Property 9: Non-loops produce no graph change.
# For any adjudication outcome that is not-a-loop, does-not-involve-user,
# unknown-direction, or an Opus error (port raises), the graph is left unchanged
# (no obligation created or modified).
# Validates: Requirements 3.4, 3.7
@given(
    user_id=user_ids,
    other_id=other_ids,
    judgement=non_loop_judgements(),
    raises=st.booleans(),
    data=st.data(),
)
def test_non_loops_produce_no_graph_change(
    user_id: str,
    other_id: str,
    judgement: OpusAdjudication,
    raises: bool,
    data: st.DataObject,
) -> None:
    """Negative judgements and Opus errors never touch the graph (Req 3.4, 3.7)."""
    candidate = data.draw(candidates(author_id=other_id))
    graph = _graph()

    # The graph starts empty; assert it as a precondition for the invariant.
    assert graph.query(ObligationFilter()) == []

    if raises:
        # Opus error/unreachable: the port raises (Req 3.7).
        adjudicator = Adjudicator(
            RaisingReasoningClient(RuntimeError("opus unreachable")), graph
        )
        result = adjudicator.adjudicate(candidate, user_id=user_id)
        assert result.outcome is AdjudicationOutcome.ERROR
        # An error indication is recorded; no obligation is produced.
        assert result.error_message is not None
        assert result.obligation is None
    else:
        # A negative (Req 3.4) judgement: discarded with a reason.
        adjudicator = Adjudicator(StubReasoningClient(judgement), graph)
        result = adjudicator.adjudicate(candidate, user_id=user_id)
        assert result.outcome is AdjudicationOutcome.DISCARDED
        assert result.discard_reason is not None
        assert result.obligation is None
        # Discard never reports surfacing eligibility.
        assert result.surfacing_eligible is None

    # The core invariant: the graph is left completely unchanged — no obligation
    # was created or modified by this adjudication.
    assert graph.query(ObligationFilter()) == []
