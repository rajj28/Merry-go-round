"""Example-based unit tests for the Action Agent's snooze and delegate (tasks 13.1 / 13.2).

Covers ``ActionAgent.snooze`` and ``ActionAgent.delegate`` against an isolated
in-memory Obligation Graph, a fixed clock, and a mockable Slack "send as user" port:

snooze (Req 13.3, 13.4):
  * default duration is 24h; ``snoozed_until = now + 24h``
  * shorter-than-1h request is clamped up to the 1h floor
  * longer-than-30d request is clamped down to the 30d ceiling
  * a snoozed obligation does not surface, then resumes once ``now`` passes expiry

delegate (Req 10.1-10.6):
  * not confirmed (cancel) → no change, nothing sent
  * confirmed + send success → owner reassigned + Last_Touch_Timestamp = confirm time
  * send failure after 3 attempts → original owner + timestamp retained + error

The property test (Property 26, task 13.3) and the delegation fault property
(task 13.4) are separate tasks and intentionally NOT included here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loop.action.action_agent import (
    DELEGATE_MAX_ATTEMPTS,
    SNOOZE_MAX,
    SNOOZE_MIN,
    ActionAgent,
)
from loop.graph.models import LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import is_ok
from loop.graph.surfacing import is_surfaced


# --------------------------------------------------------------------------- #
# Test doubles / fixtures
# --------------------------------------------------------------------------- #
class _RecordingSender:
    """A mock send-as-user port that records every call and always succeeds."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, recipient_id: str, text: str) -> None:
        self.calls.append((recipient_id, text))


class _FailingSender:
    """A mock send-as-user port that always raises — simulates send failure."""

    def __init__(self) -> None:
        self.attempts = 0

    def __call__(self, recipient_id: str, text: str) -> None:
        self.attempts += 1
        raise ConnectionError("Slack unreachable")


FIXED_NOW = "2025-02-01T09:00:00+00:00"


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


def _obligation(obligation_id: str = "OBL_1") -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.9,
        last_touch_timestamp=datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary="Ship the release notes",
    )


def _agent(graph, sender=None, *, now: str = FIXED_NOW) -> ActionAgent:
    # A verifier is required by the constructor but unused by snooze/delegate; a
    # bare object suffices since neither method calls it.
    return ActionAgent(
        graph,
        verifier=object(),  # type: ignore[arg-type]
        now=lambda: now,
        slack_send_as_user=sender,
    )


# --------------------------------------------------------------------------- #
# snooze — clamping (Req 13.3)
# --------------------------------------------------------------------------- #
def test_snooze_defaults_to_24h() -> None:
    graph = _graph()
    obligation = _obligation()
    assert is_ok(graph.upsert(obligation))

    result = _agent(graph).snooze(obligation)

    assert result.snoozed is True
    expected = datetime(2025, 2, 2, 9, 0, 0, tzinfo=timezone.utc)  # now + 24h
    assert datetime.fromisoformat(result.snoozed_until) == expected
    assert graph.get(obligation.obligation_id).snoozed_until == result.snoozed_until


def test_snooze_clamps_below_one_hour_up_to_floor() -> None:
    graph = _graph()
    obligation = _obligation()
    assert is_ok(graph.upsert(obligation))

    result = _agent(graph).snooze(obligation, timedelta(minutes=5))

    expected = _parse(FIXED_NOW) + SNOOZE_MIN  # raised to 1h
    assert datetime.fromisoformat(result.snoozed_until) == expected


def test_snooze_clamps_above_thirty_days_down_to_ceiling() -> None:
    graph = _graph()
    obligation = _obligation()
    assert is_ok(graph.upsert(obligation))

    result = _agent(graph).snooze(obligation, timedelta(days=365))

    expected = _parse(FIXED_NOW) + SNOOZE_MAX  # lowered to 30d
    assert datetime.fromisoformat(result.snoozed_until) == expected


# --------------------------------------------------------------------------- #
# snooze — hide then resume (Req 13.3, 13.4)
# --------------------------------------------------------------------------- #
def test_snoozed_obligation_hidden_then_resumes_after_expiry() -> None:
    graph = _graph()
    obligation = _obligation()
    assert is_ok(graph.upsert(obligation))

    result = _agent(graph).snooze(obligation, timedelta(hours=2))
    snoozed = result.obligation
    threshold = graph.get_threshold()

    # Before snooze: surfaced. While snoozed: hidden. After expiry: resumes (Req 13.4).
    during = _parse(FIXED_NOW) + timedelta(hours=1)
    after = _parse(FIXED_NOW) + timedelta(hours=3)

    assert is_surfaced(obligation, threshold, FIXED_NOW) is True
    assert is_surfaced(snoozed, threshold, during) is False
    assert is_surfaced(snoozed, threshold, after) is True


# --------------------------------------------------------------------------- #
# delegate — cancel / not confirmed (Req 10.1, 10.2)
# --------------------------------------------------------------------------- #
def test_delegate_not_confirmed_makes_no_change() -> None:
    graph = _graph()
    obligation = _obligation()
    assert is_ok(graph.upsert(obligation))
    sender = _RecordingSender()

    result = _agent(graph, sender).delegate(obligation, "U_TEAMMATE", confirmed=False)

    assert result.delegated is False
    assert result.sent is False
    assert sender.calls == []                       # nothing sent (Req 10.2)
    assert "U_TEAMMATE" in result.prompt            # prompt shows teammate (Req 10.1)
    assert obligation.subject_summary in result.prompt

    stored = graph.get(obligation.obligation_id)
    assert stored.owner_person_id == "U_USER"        # owner unchanged (Req 10.2)
    assert stored.last_touch_timestamp == obligation.last_touch_timestamp


# --------------------------------------------------------------------------- #
# delegate — confirmed + send success (Req 10.3, 10.4)
# --------------------------------------------------------------------------- #
def test_delegate_confirmed_send_success_reassigns_owner_and_timestamp() -> None:
    graph = _graph()
    obligation = _obligation()
    assert is_ok(graph.upsert(obligation))
    sender = _RecordingSender()
    confirm_time = "2025-03-01T10:15:00+00:00"

    result = _agent(graph, sender, now=confirm_time).delegate(
        obligation, "U_TEAMMATE", confirmed=True
    )

    assert result.delegated is True
    assert result.sent is True
    assert result.error is None
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == "U_TEAMMATE"        # sent to the one teammate (Req 10.6)

    stored = graph.get(obligation.obligation_id)
    assert stored.owner_person_id == "U_TEAMMATE"    # new owner (Req 10.4)
    assert stored.last_touch_timestamp == confirm_time  # confirm time (Req 10.4)


# --------------------------------------------------------------------------- #
# delegate — send failure after 3 attempts (Req 10.5)
# --------------------------------------------------------------------------- #
def test_delegate_send_failure_retains_original_and_reports_error() -> None:
    graph = _graph()
    obligation = _obligation()
    assert is_ok(graph.upsert(obligation))
    sender = _FailingSender()

    result = _agent(graph, sender).delegate(obligation, "U_TEAMMATE", confirmed=True)

    assert result.delegated is False
    assert result.sent is False
    assert result.error is not None                  # error indication (Req 10.5)
    assert sender.attempts == DELEGATE_MAX_ATTEMPTS   # retried up to 3 times (Req 10.5)

    stored = graph.get(obligation.obligation_id)
    assert stored.owner_person_id == "U_USER"        # original owner retained (Req 10.5)
    assert stored.last_touch_timestamp == obligation.last_touch_timestamp


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)
