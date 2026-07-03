"""Property-based test for the Action Agent's snooze hide-then-resume (task 13.3 — Property 26).

This is the single numbered correctness property for snooze, complementing the
example-based unit tests in ``test_action_snooze_delegate.py``.

It exercises ``ActionAgent.snooze`` end to end against the REAL collaborators — the
real in-memory :class:`~loop.graph.sqlite_store.SqliteObligationGraph` and the real
:class:`~loop.action.action_agent.ActionAgent` — and the REAL shared surfacing gate
:func:`loop.graph.surfacing.is_surfaced`. Nothing is mocked: snooze performs no I/O
beyond the graph write, and surfacing is a pure predicate.

The generators span the whole snooze input space:

  * requested durations: ``None`` (default), below the 1h floor (incl. zero/negative),
    inside ``[1h, 30d]``, and above the 30d ceiling;
  * evaluation instants: strictly before the snooze expiry, and at/after expiry.

The property asserts the three facets of Req 13.3 / 13.4:

  1. the realized snooze duration is clamped to the inclusive ``[1h, 30d]`` window and
     defaults to ``24h`` when no duration is requested;
  2. while ``now < snoozed_until`` the obligation is not surfaced (it WAS surfaced
     before the snooze, so the snooze is what hid it);
  3. once ``now >= snoozed_until`` the obligation is surfaced again — the resume is
     automatic, falling straight out of ``is_surfaced`` with no second write.

Runs >=100 Hypothesis examples (enforced by the workspace conftest profile).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from hypothesis import given
from hypothesis import strategies as st

from loop.action.action_agent import (
    SNOOZE_DEFAULT,
    SNOOZE_MAX,
    SNOOZE_MIN,
    ActionAgent,
)
from loop.graph.models import LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import is_ok
from loop.graph.surfacing import is_surfaced

# The instant at which every snooze in this test is applied (the agent's clock).
BASE_NOW = datetime(2025, 2, 1, 9, 0, 0, tzinfo=timezone.utc)
BASE_NOW_ISO = BASE_NOW.isoformat()

# Threshold passed to ``is_surfaced``. Held at the inclusive floor so the confidence
# gate always passes and the test isolates the *snooze* clause of the predicate.
SURFACING_THRESHOLD = 0.0

# The two active loop states — the only states that can surface on the active surface
# (``healed`` never surfaces there, so it could not demonstrate hide-then-resume).
_ACTIVE_STATES = st.sampled_from(
    [LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER]
)
_IDENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=1,
    max_size=12,
)


@st.composite
def surfaceable_obligations(draw: st.DrawFn) -> Obligation:
    """Generate a non-dismissed, active-loop Obligation that surfaces absent a snooze.

    Confidence is anywhere in ``[0.0, 1.0]`` — with ``SURFACING_THRESHOLD = 0.0`` the
    inclusive confidence gate always passes — so the only thing that can hide one of
    these obligations is the snooze clause under test.
    """
    return Obligation(
        obligation_id=draw(_IDENT),
        owes_person_id=draw(_IDENT),
        owed_person_id=draw(_IDENT),
        owner_person_id=draw(_IDENT),
        loop_state=draw(_ACTIVE_STATES),
        confidence_score=draw(st.floats(min_value=0.0, max_value=1.0)),
        last_touch_timestamp="2025-01-08T12:00:00+00:00",
        source_msg_channel=draw(_IDENT),
        source_msg_ts="1700000000.000100",
        subject_summary=draw(st.text(max_size=80)),
        dismissed=False,
        snoozed_until=None,
    )


# Requested snooze durations spanning every band of the clamp:
#   * None                          -> defaults to 24h
#   * <= 0 and below the 1h floor   -> clamped up to 1h
#   * inside [1h, 30d]              -> kept as-is
#   * above the 30d ceiling         -> clamped down to 30d
requested_durations = st.one_of(
    st.none(),
    st.timedeltas(min_value=timedelta(days=-1), max_value=timedelta(0)),
    st.timedeltas(
        min_value=timedelta(seconds=1), max_value=SNOOZE_MIN - timedelta(seconds=1)
    ),
    st.timedeltas(min_value=SNOOZE_MIN, max_value=SNOOZE_MAX),
    st.timedeltas(
        min_value=SNOOZE_MAX + timedelta(seconds=1), max_value=timedelta(days=120)
    ),
)


def _expected_clamp(duration: Optional[timedelta]) -> timedelta:
    """Independent reimplementation of the clamp, to check the agent without tautology."""
    if duration is None:
        return SNOOZE_DEFAULT
    if duration < SNOOZE_MIN:
        return SNOOZE_MIN
    if duration > SNOOZE_MAX:
        return SNOOZE_MAX
    return duration


def _agent(graph: SqliteObligationGraph) -> ActionAgent:
    # A verifier is required by the constructor but never touched by ``snooze``; a bare
    # object suffices so no external boundary is involved.
    return ActionAgent(
        graph,
        verifier=object(),  # type: ignore[arg-type]
        now=lambda: BASE_NOW_ISO,
    )


# --------------------------------------------------------------------------- #
# Property 26: Snooze hides then resumes
# --------------------------------------------------------------------------- #
# Feature: loop-obligation-agent, Property 26: Snooze hides then resumes
# For any obligation and snooze request, the snooze duration is clamped to the
# inclusive range 1 hour to 30 days (defaulting to 24 hours when none is selected); the
# obligation is not surfaced while the current time is before the snooze expiry and is
# surfaced again once the expiry has elapsed.
# Validates: Requirements 13.3, 13.4
@given(
    obligation=surfaceable_obligations(),
    requested=requested_durations,
    before_fraction=st.floats(
        min_value=0.0, max_value=1.0, exclude_max=True, allow_nan=False
    ),
    after_offset=st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=10)),
)
def test_snooze_hides_then_resumes(
    obligation: Obligation,
    requested: Optional[timedelta],
    before_fraction: float,
    after_offset: timedelta,
) -> None:
    """Duration clamped to [1h, 30d] (24h default); hidden before expiry, shown after."""
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    assert is_ok(graph.upsert(obligation))

    # Sanity: before any snooze the obligation surfaces, so the snooze is the only
    # thing that can hide it below.
    assert is_surfaced(obligation, SURFACING_THRESHOLD, BASE_NOW_ISO) is True

    result = _agent(graph).snooze(obligation, requested)
    assert result.snoozed is True

    # ----- facet 1: duration clamped to [1h, 30d], 24h default for None ----------
    expiry = datetime.fromisoformat(result.snoozed_until)
    realized = expiry - BASE_NOW
    assert SNOOZE_MIN <= realized <= SNOOZE_MAX
    assert realized == _expected_clamp(requested)
    if requested is None:
        assert realized == SNOOZE_DEFAULT

    snoozed = result.obligation

    # ----- facet 2: not surfaced while now < snoozed_until (Req 13.3) ------------
    # ``before`` is strictly inside [BASE_NOW, expiry). It is built with integer
    # microsecond arithmetic (clamped to realized - 1µs) rather than a float multiply,
    # so floating-point rounding can never push it onto the expiry boundary. The
    # realized duration is always positive (>= 1h), so this range is non-empty.
    total_micros = realized // timedelta(microseconds=1)
    offset_micros = min(int(before_fraction * total_micros), total_micros - 1)
    before = BASE_NOW + timedelta(microseconds=offset_micros)
    assert before < expiry
    assert is_surfaced(snoozed, SURFACING_THRESHOLD, before.isoformat()) is False

    # ----- facet 3: surfaced again once now >= snoozed_until (Req 13.4) ----------
    # ``after`` is at-or-past expiry (offset >= 0); the boundary now == snoozed_until
    # counts as elapsed, so surfacing resumes automatically with no second write.
    after = expiry + after_offset
    assert after >= expiry
    assert is_surfaced(snoozed, SURFACING_THRESHOLD, after.isoformat()) is True
