"""Third-party edge creation via explicit party extraction (live realtime graph).

The judgement's ``owes_id``/``owed_id`` fields let the Adjudicator write loops
between two people *other than* the tracked user — the edges that grow the
workspace map, chains, and deadlock rings from real conversation. Covers:

  * a third-party judgement is CREATED (not discarded), edge exactly as named,
    state ``WAITING_ON_OTHER`` (nobody is "blocked on you" but the user);
  * explicit endpoints naming the user map to the user-centric states;
  * self-loop endpoints are discarded;
  * one-sided extraction falls back to the legacy direction mapping (and its
    "does not involve user" discard), so old clients keep old behaviour;
  * mention-syntax ids (``<@U…>``) are normalized before writing.
"""

from __future__ import annotations

from loop.adjudicator.adjudicator import (
    AdjudicationOutcome,
    Adjudicator,
    Direction,
    SmartAdjudication,
    _clean_person_id,
)
from loop.graph.models import LoopState
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import ObligationFilter
from loop.watcher.rts_contract import CandidateMessage

USER_ID = "U_USER"
PRIYA = "U_PRIYA"
MARCO = "U_MARCO"


class StubReasoningClient:
    def __init__(self, judgement: SmartAdjudication) -> None:
        self._judgement = judgement

    def __call__(self, candidate: CandidateMessage, *, user_id: str) -> SmartAdjudication:
        return self._judgement


def _candidate(author_id: str = PRIYA, text: str = "can you review my doc?") -> CandidateMessage:
    return CandidateMessage(
        channel_id="C123",
        message_ts="1700000000.000100",
        author_id=author_id,
        text=text,
        permalink="https://example.slack.com/archives/C123/p1700000000000100",
    )


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


def _judgement(**overrides) -> SmartAdjudication:
    base = dict(
        is_loop=True,
        involves_user=False,
        direction=Direction.UNKNOWN,
        confidence=0.8,
        subject_summary="Review the doc",
    )
    base.update(overrides)
    return SmartAdjudication(**base)


# --------------------------------------------------------------------------- #
# Third-party creation
# --------------------------------------------------------------------------- #
def test_third_party_loop_is_created_not_discarded() -> None:
    graph = _graph()
    adjudicator = Adjudicator(
        StubReasoningClient(_judgement(owes_id=MARCO, owed_id=PRIYA)), graph
    )

    result = adjudicator.adjudicate(_candidate(author_id=PRIYA), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.CREATED
    stored = result.obligation
    assert stored is not None
    assert stored.owes_person_id == MARCO
    assert stored.owed_person_id == PRIYA
    assert stored.loop_state is LoopState.WAITING_ON_OTHER


def test_explicit_endpoints_where_user_owes_map_to_blocked_on_you() -> None:
    graph = _graph()
    adjudicator = Adjudicator(
        StubReasoningClient(_judgement(owes_id=USER_ID, owed_id=PRIYA)), graph
    )

    result = adjudicator.adjudicate(_candidate(), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.CREATED
    assert result.obligation.owes_person_id == USER_ID
    assert result.obligation.loop_state is LoopState.BLOCKED_ON_YOU


def test_explicit_endpoints_where_user_is_owed_map_to_waiting() -> None:
    graph = _graph()
    adjudicator = Adjudicator(
        StubReasoningClient(_judgement(owes_id=MARCO, owed_id=USER_ID)), graph
    )

    result = adjudicator.adjudicate(_candidate(author_id=MARCO), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.CREATED
    assert result.obligation.owes_person_id == MARCO
    assert result.obligation.owed_person_id == USER_ID
    assert result.obligation.loop_state is LoopState.WAITING_ON_OTHER


def test_self_loop_endpoints_are_discarded() -> None:
    graph = _graph()
    adjudicator = Adjudicator(
        StubReasoningClient(_judgement(owes_id=PRIYA, owed_id=PRIYA)), graph
    )

    result = adjudicator.adjudicate(_candidate(), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.DISCARDED
    assert result.discard_reason == "self-loop endpoints"
    assert graph.query(ObligationFilter(include_dismissed=True)) == []


def test_one_sided_extraction_falls_back_to_the_legacy_discard() -> None:
    # Only owes_id extracted → no usable edge → legacy path, which discards a
    # judgement that does not involve the user (Req 3.4, preserved).
    graph = _graph()
    adjudicator = Adjudicator(
        StubReasoningClient(_judgement(owes_id=MARCO, owed_id=None)), graph
    )

    result = adjudicator.adjudicate(_candidate(), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.DISCARDED
    assert result.discard_reason == "does not involve user"


def test_mention_syntax_ids_are_normalized() -> None:
    graph = _graph()
    adjudicator = Adjudicator(
        StubReasoningClient(
            _judgement(owes_id="<@U_MARCO>", owed_id="<@U_PRIYA|priya>")
        ),
        graph,
    )

    result = adjudicator.adjudicate(_candidate(), user_id=USER_ID)

    assert result.outcome is AdjudicationOutcome.CREATED
    assert result.obligation.owes_person_id == MARCO
    assert result.obligation.owed_person_id == PRIYA


# --------------------------------------------------------------------------- #
# _clean_person_id normalization
# --------------------------------------------------------------------------- #
def test_clean_person_id_accepts_bare_and_mention_forms() -> None:
    assert _clean_person_id("U0AB12CD3") == "U0AB12CD3"
    assert _clean_person_id("<@U0AB12CD3>") == "U0AB12CD3"
    assert _clean_person_id("<@U0AB12CD3|dave>") == "U0AB12CD3"
    assert _clean_person_id("@U0AB12CD3") == "U0AB12CD3"
    assert _clean_person_id(" U0AB12CD3 ") == "U0AB12CD3"


def test_clean_person_id_rejects_junk() -> None:
    assert _clean_person_id(None) is None
    assert _clean_person_id("") is None
    assert _clean_person_id("   ") is None
    assert _clean_person_id("???") is None
    assert _clean_person_id(42) is None
