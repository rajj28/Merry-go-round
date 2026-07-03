"""Fault-injection unit test for delegation send failure (task 13.4 — Req 10.5).

Isolates the failure edge of ``ActionAgent.delegate``: a confirmed delegation whose
Slack "send as user" port always raises. The Action Agent must, per Req 10.5,

  * retry the send up to :data:`DELEGATE_MAX_ATTEMPTS` (3) times before giving up,
  * leave the obligation's original owner and Last_Touch_Timestamp untouched (no graph
    write occurs on the failure path), and
  * surface an error indication while reporting the delegation as not completed
    (``delegated=False``, ``sent=False``).

The happy-path and cancel cases live in ``test_action_snooze_delegate.py``; this file
deliberately exercises only the send-failure fault so the retain-on-failure contract is
covered in isolation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from loop.action.action_agent import DELEGATE_MAX_ATTEMPTS, ActionAgent
from loop.graph.models import LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import is_ok


FIXED_NOW = "2025-02-01T09:00:00+00:00"
ORIGINAL_OWNER = "U_USER"
ORIGINAL_TOUCH = datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat()


class _FailingSender:
    """A send-as-user port that always raises — injects a persistent send fault.

    Every call increments :attr:`attempts` so the test can assert the bounded-retry
    count, then raises to simulate an unreachable Slack / timeout / transport error.
    """

    def __init__(self) -> None:
        self.attempts = 0

    def __call__(self, recipient_id: str, text: str) -> None:
        self.attempts += 1
        raise ConnectionError("Slack unreachable")


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


def _obligation() -> Obligation:
    return Obligation(
        obligation_id="OBL_1",
        owes_person_id=ORIGINAL_OWNER,
        owed_person_id="U_OTHER",
        owner_person_id=ORIGINAL_OWNER,
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=0.9,
        last_touch_timestamp=ORIGINAL_TOUCH,
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary="Ship the release notes",
    )


def _agent(graph, sender) -> ActionAgent:
    # The Verifier is required by the constructor but unused by delegate; a bare
    # object suffices because delegate never calls it.
    return ActionAgent(
        graph,
        verifier=object(),  # type: ignore[arg-type]
        now=lambda: FIXED_NOW,
        slack_send_as_user=sender,
    )


def test_delegate_send_failure_retries_thrice_retains_owner_and_reports_error() -> None:
    """A confirmed delegation whose send always fails retains everything + errors (Req 10.5)."""
    graph = _graph()
    obligation = _obligation()
    assert is_ok(graph.upsert(obligation))
    sender = _FailingSender()

    result = _agent(graph, sender).delegate(obligation, "U_TEAMMATE", confirmed=True)

    # The send is retried up to the bounded maximum (3) before giving up (Req 10.5).
    assert sender.attempts == DELEGATE_MAX_ATTEMPTS

    # The delegation is reported as not completed, with an error indication (Req 10.5).
    assert result.delegated is False
    assert result.sent is False
    assert result.error is not None
    assert "U_TEAMMATE" in result.error

    # The returned obligation is the unchanged input — original owner + timestamp.
    assert result.obligation.owner_person_id == ORIGINAL_OWNER
    assert result.obligation.last_touch_timestamp == ORIGINAL_TOUCH

    # No graph write occurred on the failure path: the stored value is unchanged.
    stored = graph.get(obligation.obligation_id)
    assert stored.owner_person_id == ORIGINAL_OWNER
    assert stored.last_touch_timestamp == ORIGINAL_TOUCH
