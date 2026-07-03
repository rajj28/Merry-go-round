"""Example-based unit tests for the Action Agent's Daily_Digest (task 14.1 / 14.2).

Covers ``ActionAgent.send_daily_digest`` against an isolated in-memory Obligation
Graph, a fixed clock, and a mockable Slack send port:

content (Req 11.1, 11.2, 11.3):
  * the digest counts only surfaced obligations, grouped by Loop_State — dismissed,
    below-threshold, snoozed, and healed obligations are excluded from the counts.

empty digest (Req 11.4):
  * an empty graph produces a "no open loops require attention" digest that is still
    delivered.

one digest per 24h (Req 11.5):
  * a second call within the rolling 24h window is suppressed (nothing sent); a call
    after the window elapses sends again.

send failure (Req 11.6):
  * a failing send is retried 3 times, then records an error, leaves obligation state
    unchanged, and does not consume the 24h window (so the next call retries).
"""

from __future__ import annotations

from datetime import datetime, timezone

from loop.action.action_agent import DIGEST_MAX_ATTEMPTS, ActionAgent
from loop.graph.models import LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import is_ok


# --------------------------------------------------------------------------- #
# Test doubles / fixtures
# --------------------------------------------------------------------------- #
class _RecordingSender:
    """A mock send port that records every (recipient, text) call and succeeds."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, recipient_id: str, text: str) -> None:
        self.calls.append((recipient_id, text))


class _FailingSender:
    """A mock send port that always raises — simulates a send failure."""

    def __init__(self) -> None:
        self.attempts = 0

    def __call__(self, recipient_id: str, text: str) -> None:
        self.attempts += 1
        raise ConnectionError("Slack unreachable")


FIXED_NOW = "2025-02-01T09:00:00+00:00"
USER_ID = "U_USER"


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


def _agent(graph, sender=None, *, now: str = FIXED_NOW) -> ActionAgent:
    # A verifier is required by the constructor but unused by the digest path.
    return ActionAgent(
        graph,
        verifier=object(),  # type: ignore[arg-type]
        now=lambda: now,
        slack_send_as_user=sender,
    )


def _obligation(
    obligation_id: str,
    loop_state: LoopState,
    *,
    confidence: float = 0.9,
    dismissed: bool = False,
) -> Obligation:
    return Obligation(
        obligation_id=obligation_id,
        owes_person_id="U_USER",
        owed_person_id="U_OTHER",
        owner_person_id="U_USER",
        loop_state=loop_state,
        confidence_score=confidence,
        last_touch_timestamp=datetime(2025, 1, 8, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary=f"Loop {obligation_id}",
        dismissed=dismissed,
    )


# --------------------------------------------------------------------------- #
# content — only surfaced obligations counted, grouped by state (Req 11.2, 11.3)
# --------------------------------------------------------------------------- #
def test_digest_counts_only_surfaced_obligations_per_state() -> None:
    graph = _graph()
    threshold = graph.get_threshold()  # default 0.5
    # Two blocked-on-you and one waiting-on-other qualify; three are excluded.
    for o in (
        _obligation("BOY_1", LoopState.BLOCKED_ON_YOU),
        _obligation("BOY_2", LoopState.BLOCKED_ON_YOU),
        _obligation("WOO_1", LoopState.WAITING_ON_OTHER),
        _obligation("DISMISSED", LoopState.BLOCKED_ON_YOU, dismissed=True),
        _obligation("BELOW", LoopState.BLOCKED_ON_YOU, confidence=threshold - 0.1),
        _obligation("HEALED", LoopState.HEALED),
    ):
        assert is_ok(graph.upsert(o))

    sender = _RecordingSender()
    result = _agent(graph, sender).send_daily_digest(USER_ID)

    assert result.sent is True
    assert result.suppressed is False
    assert result.empty is False
    assert result.blocked_on_you_count == 2          # excludes dismissed + below-threshold
    assert result.waiting_on_other_count == 1
    # Delivered once, to the user, with the counts reflected in the body (Req 11.3).
    assert len(sender.calls) == 1
    assert sender.calls[0][0] == USER_ID
    assert "2" in sender.calls[0][1]
    assert "1" in sender.calls[0][1]


# --------------------------------------------------------------------------- #
# empty digest — no qualifying obligations (Req 11.4)
# --------------------------------------------------------------------------- #
def test_empty_graph_sends_no_open_loops_digest() -> None:
    graph = _graph()
    sender = _RecordingSender()

    result = _agent(graph, sender).send_daily_digest(USER_ID)

    assert result.sent is True
    assert result.empty is True
    assert result.blocked_on_you_count == 0
    assert result.waiting_on_other_count == 0
    assert len(sender.calls) == 1
    assert "no open loops" in sender.calls[0][1].lower()


# --------------------------------------------------------------------------- #
# one digest per 24h (Req 11.5)
# --------------------------------------------------------------------------- #
def test_second_digest_within_24h_is_suppressed() -> None:
    graph = _graph()
    sender = _RecordingSender()
    agent = _agent(graph, sender, now=FIXED_NOW)

    first = agent.send_daily_digest(USER_ID)
    second = agent.send_daily_digest(USER_ID)  # same clock => within the window

    assert first.sent is True
    assert second.sent is False
    assert second.suppressed is True
    assert len(sender.calls) == 1  # only the first call actually sent


def test_digest_sends_again_after_24h_window_elapses() -> None:
    graph = _graph()
    sender = _RecordingSender()

    # Drive one agent across two scheduled instants 24h+1s apart so the single
    # in-process 24h-window record is observed at both times.
    later = "2025-02-02T09:00:01+00:00"  # 24h + 1s after FIXED_NOW
    agent = _MovingClockAgent(graph, sender, [FIXED_NOW, later])

    first = agent.send_daily_digest(USER_ID)   # FIXED_NOW: first send
    second = agent.send_daily_digest(USER_ID)  # later: window elapsed

    assert first.sent is True
    assert second.sent is True
    assert second.suppressed is False
    assert len(sender.calls) == 2


# --------------------------------------------------------------------------- #
# send failure — retry 3x, record error, leave state unchanged (Req 11.6)
# --------------------------------------------------------------------------- #
def test_digest_send_failure_retries_then_records_error_and_leaves_state() -> None:
    graph = _graph()
    obligation = _obligation("BOY_1", LoopState.BLOCKED_ON_YOU)
    assert is_ok(graph.upsert(obligation))
    sender = _FailingSender()
    agent = _agent(graph, sender)

    result = agent.send_daily_digest(USER_ID)

    assert result.sent is False
    assert result.suppressed is False
    assert result.error is not None
    assert sender.attempts == DIGEST_MAX_ATTEMPTS  # retried up to 3 times (Req 11.6)

    # Obligation state is unchanged (the digest is read-only over the graph).
    stored = graph.get(obligation.obligation_id)
    assert stored.loop_state is LoopState.BLOCKED_ON_YOU
    assert stored.dismissed is False

    # The failed send did not consume the 24h window: a subsequent send is not
    # suppressed and retries delivery (Req 11.6).
    good = _RecordingSender()
    agent._send_as_user = good  # type: ignore[attr-defined]
    retry = agent.send_daily_digest(USER_ID)
    assert retry.suppressed is False
    assert retry.sent is True
    assert len(good.calls) == 1


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class _MovingClockAgent(ActionAgent):
    """An ActionAgent whose injected clock advances through a fixed list of instants
    on each call, so one agent instance (and thus one 24h-window record) can be
    observed across multiple scheduled digest times."""

    def __init__(self, graph, sender, instants: list[str]) -> None:
        self._instants = list(instants)
        super().__init__(
            graph,
            verifier=object(),  # type: ignore[arg-type]
            now=self._tick,
            slack_send_as_user=sender,
        )

    def _tick(self) -> str:
        return self._instants.pop(0)
