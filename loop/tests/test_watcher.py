"""Example-based unit tests for the Watcher's RTS periodic sweep (task 4.1).

Covers ``Watcher.run_sweep`` with a mocked RTS port (a thin injectable callable),
exercising the task-4.1 observable behaviour:

  * a successful RTS response yields the parsed candidates as NEW candidates;
  * a candidate whose ``(channel, ts)`` source ref already maps to an existing
    Obligation is skipped (dedup, Req 2.9);
  * RTS returning ``ok=false`` / empty completes the sweep with no candidates
    (Req 2.6);
  * RTS raising / unreachable is contained: no candidates, no crash, ready to retry
    next sweep (Req 2.7).

Also asserts the seams (classify pass-through, forward hand-off) and the ≤60s
interval validation (Req 2.1). The property tests (4.4/4.5) and the broader
fault-injection test (4.6) are separate tasks and intentionally NOT included here.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from loop.graph.models import LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.watcher.rts_contract import CandidateMessage
from loop.watcher.watcher import (
    MAX_SWEEP_INTERVAL_SECONDS,
    CandidateOutcome,
    SweepResult,
    Watcher,
    build_fast_classify_client,
    candidate_from_message_event,
)


# --------------------------------------------------------------------------- #
# Test doubles / fixtures
# --------------------------------------------------------------------------- #
class StaticRtsClient:
    """A mock RTS port returning a fixed raw response and recording call count."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        self._response = response
        self.calls = 0

    def __call__(self) -> Mapping[str, Any]:
        self.calls += 1
        return self._response


class RaisingRtsClient:
    """A mock RTS port that always raises, simulating unreachable/timeout/error."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc or ConnectionError("RTS unreachable")
        self.calls = 0

    def __call__(self) -> Mapping[str, Any]:
        self.calls += 1
        raise self._exc


def _rts_message(channel_id: str, message_ts: str, text: str = "any update?") -> dict:
    """One ``results.messages[i]`` entry in the RTS response shape."""
    return {
        "channel_id": channel_id,
        "message_ts": message_ts,
        "author_user_id": "U_AUTHOR",
        "author_name": "Author",
        "content": text,
        "permalink": f"https://example.slack.com/archives/{channel_id}/p{message_ts}",
        "is_author_bot": False,
    }


def _rts_ok(*messages: dict) -> dict:
    """A successful ``assistant.search.context`` response wrapping ``messages``."""
    return {
        "ok": True,
        "results": {"messages": list(messages)},
        "response_metadata": {"next_cursor": ""},
    }


def _graph() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


def _seed_obligation(
    graph: SqliteObligationGraph, channel_id: str, message_ts: str
) -> None:
    """Persist an Obligation whose source ref is ``(channel_id, message_ts)``."""
    graph.upsert(
        Obligation(
            obligation_id=f"ob-{channel_id}-{message_ts}",
            owes_person_id="U_DEV",
            owed_person_id="U_USER",
            owner_person_id="U_DEV",
            loop_state=LoopState.WAITING_ON_OTHER,
            confidence_score=0.9,
            last_touch_timestamp="2025-01-08T12:00:00+00:00",
            source_msg_channel=channel_id,
            source_msg_ts=message_ts,
            subject_summary="seeded",
        )
    )


# --------------------------------------------------------------------------- #
# Happy path: parsed candidates are returned as NEW candidates
# --------------------------------------------------------------------------- #
def test_sweep_returns_parsed_candidates() -> None:
    client = StaticRtsClient(
        _rts_ok(
            _rts_message("C1", "100.1"),
            _rts_message("C2", "200.2"),
        )
    )
    watcher = Watcher(_graph(), client, interval_seconds=30)

    result = watcher.run_sweep()

    assert isinstance(result, SweepResult)
    assert result.rts_ok is True
    assert client.calls == 1
    assert [c.dedup_key for c in result.new_candidates] == [
        ("C1", "100.1"),
        ("C2", "200.2"),
    ]
    assert result.duplicates_skipped == 0


def test_sweep_forwards_each_new_candidate_to_adjudicator_seam() -> None:
    forwarded: list[CandidateMessage] = []
    client = StaticRtsClient(_rts_ok(_rts_message("C1", "100.1")))
    watcher = Watcher(
        _graph(), client, forward=forwarded.append, interval_seconds=30
    )

    result = watcher.run_sweep()

    assert [c.dedup_key for c in forwarded] == [("C1", "100.1")]
    assert [c.dedup_key for c in result.new_candidates] == [("C1", "100.1")]


def test_classify_seam_can_drop_candidates() -> None:
    # The classify seam (task 4.3 plugs fast-tier here) can reject a candidate; with the
    # default pass-through every candidate is kept, so we inject a rejecting classifier.
    client = StaticRtsClient(
        _rts_ok(_rts_message("C1", "100.1"), _rts_message("C2", "200.2"))
    )
    watcher = Watcher(
        _graph(),
        client,
        classify=lambda c: c.channel_id == "C2",
        interval_seconds=30,
    )

    result = watcher.run_sweep()

    assert [c.dedup_key for c in result.new_candidates] == [("C2", "200.2")]


# --------------------------------------------------------------------------- #
# Dedup: existing source ref is skipped (Req 2.9)
# --------------------------------------------------------------------------- #
def test_sweep_skips_candidate_with_existing_source_ref() -> None:
    graph = _graph()
    _seed_obligation(graph, "C1", "100.1")  # this source message already mapped

    client = StaticRtsClient(
        _rts_ok(
            _rts_message("C1", "100.1"),  # duplicate -> skipped
            _rts_message("C2", "200.2"),  # new       -> kept
        )
    )
    watcher = Watcher(graph, client, interval_seconds=30)

    result = watcher.run_sweep()

    assert [c.dedup_key for c in result.new_candidates] == [("C2", "200.2")]
    assert result.duplicates_skipped == 1


def test_sweep_dedups_within_a_single_page() -> None:
    # Two messages with the same source ref in one response collapse to one candidate.
    client = StaticRtsClient(
        _rts_ok(_rts_message("C1", "100.1"), _rts_message("C1", "100.1"))
    )
    watcher = Watcher(_graph(), client, interval_seconds=30)

    result = watcher.run_sweep()

    assert [c.dedup_key for c in result.new_candidates] == [("C1", "100.1")]
    assert result.duplicates_skipped == 1


def test_dedup_skips_even_dismissed_obligations() -> None:
    # A dismissed obligation still occupies its source message; re-detecting it would
    # resurrect a loop the user dismissed, so dedup must still skip it (Req 2.9).
    graph = _graph()
    graph.upsert(
        Obligation(
            obligation_id="ob-dismissed",
            owes_person_id="U_DEV",
            owed_person_id="U_USER",
            owner_person_id="U_DEV",
            loop_state=LoopState.WAITING_ON_OTHER,
            confidence_score=0.9,
            last_touch_timestamp="2025-01-08T12:00:00+00:00",
            source_msg_channel="C1",
            source_msg_ts="100.1",
            subject_summary="seeded",
            dismissed=True,
        )
    )
    client = StaticRtsClient(_rts_ok(_rts_message("C1", "100.1")))
    watcher = Watcher(graph, client, interval_seconds=30)

    result = watcher.run_sweep()

    assert result.new_candidates == []
    assert result.duplicates_skipped == 1


# --------------------------------------------------------------------------- #
# Session memory: a candidate is LLM-judged at most once per session
# --------------------------------------------------------------------------- #
def test_dropped_candidate_is_not_reclassified_on_later_sweeps() -> None:
    # RTS returns recent history, so the same not-a-loop chatter comes back on
    # every ≤60s sweep; without session memory each sweep would re-spend
    # fast-tier tokens re-judging it.
    client = StaticRtsClient(_rts_ok(_rts_message("C1", "100.1")))
    classified: list[tuple[str, str]] = []

    def classify(candidate) -> bool:  # noqa: ANN001
        classified.append(candidate.dedup_key)
        return False

    watcher = Watcher(_graph(), client, classify=classify, interval_seconds=30)

    watcher.run_sweep()
    second = watcher.run_sweep()

    assert classified == [("C1", "100.1")]  # judged exactly once
    assert second.new_candidates == []
    assert second.duplicates_skipped == 1


def test_forwarded_candidate_is_not_reforwarded_after_an_adjudicator_discard() -> None:
    # A forwarded candidate the Adjudicator discards writes nothing to the
    # graph, so graph-based dedup alone would re-forward it into smart-tier
    # reasoning every sweep. The session memory must stop that re-spend.
    client = StaticRtsClient(_rts_ok(_rts_message("C1", "100.1")))
    forwarded: list = []
    watcher = Watcher(_graph(), client, forward=forwarded.append, interval_seconds=30)

    watcher.run_sweep()
    second = watcher.run_sweep()  # graph still has no obligation for the ref

    assert [c.dedup_key for c in forwarded] == [("C1", "100.1")]
    assert second.new_candidates == []
    assert second.duplicates_skipped == 1


def test_live_event_shares_the_session_memory_with_sweeps() -> None:
    # The live message path and the sweep must not double-judge the same source
    # message either — they share one per-candidate pipeline.
    client = StaticRtsClient(_rts_ok(_rts_message("C1", "100.1")))
    classified: list[tuple[str, str]] = []

    def classify(candidate) -> bool:  # noqa: ANN001
        classified.append(candidate.dedup_key)
        return False

    watcher = Watcher(_graph(), client, classify=classify, interval_seconds=30)
    event = {"channel": "C1", "ts": "100.1", "user": "U_AUTHOR", "text": "any update?"}

    assert watcher.on_message_event(event) is CandidateOutcome.DROPPED
    assert watcher.on_message_event(event) is CandidateOutcome.DUPLICATE
    sweep = watcher.run_sweep()

    assert classified == [("C1", "100.1")]
    assert sweep.duplicates_skipped == 1


# --------------------------------------------------------------------------- #
# RTS empty / ok=false -> no candidates (Req 2.6)
# --------------------------------------------------------------------------- #
def test_sweep_with_empty_results_creates_no_candidates() -> None:
    client = StaticRtsClient(_rts_ok())  # ok=true, zero messages
    watcher = Watcher(_graph(), client, interval_seconds=30)

    result = watcher.run_sweep()

    assert result.rts_ok is True
    assert result.new_candidates == []


def test_sweep_with_ok_false_creates_no_candidates() -> None:
    client = StaticRtsClient({"ok": False, "error": "not_authed"})
    watcher = Watcher(_graph(), client, interval_seconds=30)

    result = watcher.run_sweep()

    # parse_rts_response treats ok=false as empty; the sweep completes cleanly.
    assert result.new_candidates == []


# --------------------------------------------------------------------------- #
# RTS raising / unreachable -> contained, no crash, retry next sweep (Req 2.7)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "exc",
    [ConnectionError("unreachable"), TimeoutError("timed out"), RuntimeError("boom")],
)
def test_sweep_contains_rts_failure(exc: BaseException) -> None:
    client = RaisingRtsClient(exc)
    watcher = Watcher(_graph(), client, interval_seconds=30)

    result = watcher.run_sweep()  # must NOT raise

    assert result.rts_ok is False
    assert result.new_candidates == []
    assert result.error is not None
    assert client.calls == 1


def test_next_sweep_retries_after_a_failure() -> None:
    # Req 2.7: a failed sweep does not crash the loop; the next sweep can succeed.
    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> Mapping[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("transient")
            return _rts_ok(_rts_message("C1", "100.1"))

    client = FlakyClient()
    watcher = Watcher(_graph(), client, interval_seconds=30)

    first = watcher.run_sweep()
    second = watcher.run_sweep()

    assert first.rts_ok is False and first.new_candidates == []
    assert second.rts_ok is True
    assert [c.dedup_key for c in second.new_candidates] == [("C1", "100.1")]
    assert client.calls == 2


# --------------------------------------------------------------------------- #
# Interval validation (Req 2.1)
# --------------------------------------------------------------------------- #
def test_interval_must_not_exceed_sixty_seconds() -> None:
    client = StaticRtsClient(_rts_ok())
    with pytest.raises(ValueError):
        Watcher(_graph(), client, interval_seconds=MAX_SWEEP_INTERVAL_SECONDS + 1)


def test_interval_is_exposed() -> None:
    client = StaticRtsClient(_rts_ok())
    watcher = Watcher(_graph(), client, interval_seconds=45)
    assert watcher.interval_seconds == 45


def test_nonpositive_interval_is_rejected() -> None:
    client = StaticRtsClient(_rts_ok())
    with pytest.raises(ValueError):
        Watcher(_graph(), client, interval_seconds=0)


# --------------------------------------------------------------------------- #
# Live message-event evaluation — on_message_event (task 4.2, Req 2.2)
# --------------------------------------------------------------------------- #
def _message_event(
    channel: str, ts: str, text: str = "any update?", user: str = "U_AUTHOR", **extra
) -> dict:
    """A Slack Events-API ``message`` event payload (the shape Bolt delivers)."""
    return {"channel": channel, "ts": ts, "user": user, "text": text, **extra}


def test_candidate_from_message_event_maps_fields() -> None:
    candidate = candidate_from_message_event(
        _message_event("C9", "900.9", text="can you review?", user="U7")
    )
    assert candidate.dedup_key == ("C9", "900.9")
    assert candidate.author_id == "U7"
    assert candidate.text == "can you review?"
    assert candidate.is_author_bot is False


def test_candidate_from_message_event_flags_bot_author() -> None:
    # A bot-authored message is marked so high-recall filtering can drop bot chatter.
    candidate = candidate_from_message_event(
        _message_event("C9", "900.9", user="", bot_id="B123", subtype="bot_message")
    )
    assert candidate.is_author_bot is True


def test_on_message_event_forwards_a_new_candidate() -> None:
    forwarded: list[CandidateMessage] = []
    # RTS client is irrelevant to the live path; a static empty one suffices.
    watcher = Watcher(
        _graph(), StaticRtsClient(_rts_ok()), forward=forwarded.append, interval_seconds=30
    )

    outcome = watcher.on_message_event(_message_event("C1", "100.1"))

    assert outcome is CandidateOutcome.FORWARDED
    assert [c.dedup_key for c in forwarded] == [("C1", "100.1")]


def test_on_message_event_dedups_existing_source_ref() -> None:
    # Req 2.9: a message whose (channel, ts) already maps to an Obligation is skipped.
    graph = _graph()
    _seed_obligation(graph, "C1", "100.1")
    forwarded: list[CandidateMessage] = []
    watcher = Watcher(
        graph, StaticRtsClient(_rts_ok()), forward=forwarded.append, interval_seconds=30
    )

    outcome = watcher.on_message_event(_message_event("C1", "100.1"))

    assert outcome is CandidateOutcome.DUPLICATE
    assert forwarded == []


def test_on_message_event_shares_pipeline_classify_drop() -> None:
    # The live path runs the same classify seam as run_sweep: a negative drops it.
    forwarded: list[CandidateMessage] = []
    watcher = Watcher(
        _graph(),
        StaticRtsClient(_rts_ok()),
        classify=lambda c: False,
        forward=forwarded.append,
        interval_seconds=30,
    )

    outcome = watcher.on_message_event(_message_event("C1", "100.1"))

    assert outcome is CandidateOutcome.DROPPED
    assert forwarded == []


# --------------------------------------------------------------------------- #
# fast-tier high-recall classification + forwarding (task 4.3, Req 2.3/2.4/2.5)
# --------------------------------------------------------------------------- #
def test_fast_tier_positive_low_certainty_candidates_are_forwarded() -> None:
    # High recall (Req 2.5): EVERY positive, including a low-certainty YES, is kept.
    forwarded: list[CandidateMessage] = []

    def fast_classify(_c: CandidateMessage) -> bool:
        return True  # potential loop (even if uncertain)

    client = StaticRtsClient(
        _rts_ok(_rts_message("C1", "100.1"), _rts_message("C2", "200.2"))
    )
    watcher = Watcher(
        _graph(), client, classify=fast_classify, forward=forwarded.append, interval_seconds=30
    )

    result = watcher.run_sweep()

    assert [c.dedup_key for c in forwarded] == [("C1", "100.1"), ("C2", "200.2")]
    assert [c.dedup_key for c in result.new_candidates] == [
        ("C1", "100.1"),
        ("C2", "200.2"),
    ]


def test_fast_tier_negative_candidates_are_dropped() -> None:
    forwarded: list[CandidateMessage] = []

    def fast_classify(c: CandidateMessage) -> bool:
        # Only C2 is a potential loop; C1 is clearly not.
        return c.channel_id == "C2"

    client = StaticRtsClient(
        _rts_ok(_rts_message("C1", "100.1"), _rts_message("C2", "200.2"))
    )
    watcher = Watcher(
        _graph(), client, classify=fast_classify, forward=forwarded.append, interval_seconds=30
    )

    result = watcher.run_sweep()

    assert [c.dedup_key for c in forwarded] == [("C2", "200.2")]
    assert [c.dedup_key for c in result.new_candidates] == [("C2", "200.2")]


# --------------------------------------------------------------------------- #
# fast-tier failure → exclude + re-evaluate next sweep (task 4.3, Req 2.8)
# --------------------------------------------------------------------------- #
def test_fast_tier_failure_excludes_candidate_from_forwarding() -> None:
    # Req 2.8: when fast-tier raises, the candidate is excluded (not forwarded), and the
    # sweep does not crash.
    forwarded: list[CandidateMessage] = []

    def failing_classifier(_c: CandidateMessage) -> bool:
        raise RuntimeError("fast-tier unreachable")

    client = StaticRtsClient(_rts_ok(_rts_message("C1", "100.1")))
    watcher = Watcher(
        _graph(),
        client,
        classify=failing_classifier,
        forward=forwarded.append,
        interval_seconds=30,
    )

    result = watcher.run_sweep()

    assert forwarded == []
    assert result.new_candidates == []
    assert result.rts_ok is True  # RTS itself succeeded; only fast-tier failed


def test_fast_tier_failure_candidate_is_reevaluated_next_sweep() -> None:
    # Req 2.8: a candidate excluded by a fast-tier failure must NOT be marked processed,
    # so the next sweep that retrieves it re-evaluates it. Here fast-tier fails on the
    # first sweep and succeeds on the second; the candidate is forwarded the 2nd time.
    forwarded: list[CandidateMessage] = []
    calls = {"n": 0}

    def flaky_classifier(_c: CandidateMessage) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient fast-tier error")
        return True

    client = StaticRtsClient(_rts_ok(_rts_message("C1", "100.1")))
    watcher = Watcher(
        _graph(),
        client,
        classify=flaky_classifier,
        forward=forwarded.append,
        interval_seconds=30,
    )

    first = watcher.run_sweep()
    second = watcher.run_sweep()

    assert first.new_candidates == []  # excluded on fast-tier failure
    assert [c.dedup_key for c in second.new_candidates] == [("C1", "100.1")]
    assert [c.dedup_key for c in forwarded] == [("C1", "100.1")]


def test_on_message_event_fast_tier_failure_is_reevaluated_by_a_sweep() -> None:
    # The live path shares the same exclusion semantics: a fast-tier failure on a live
    # message excludes it without marking it processed, so a later sweep re-evaluates.
    forwarded: list[CandidateMessage] = []
    calls = {"n": 0}

    def flaky_classifier(_c: CandidateMessage) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient fast-tier error")
        return True

    client = StaticRtsClient(_rts_ok(_rts_message("C1", "100.1")))
    watcher = Watcher(
        _graph(),
        client,
        classify=flaky_classifier,
        forward=forwarded.append,
        interval_seconds=30,
    )

    live = watcher.on_message_event(_message_event("C1", "100.1"))
    assert live is CandidateOutcome.CLASSIFY_FAILED
    assert forwarded == []  # nothing forwarded yet — fast-tier failed

    # the next sweep that retrieves it re-evaluates and forwards it
    sweep = watcher.run_sweep()
    assert [c.dedup_key for c in sweep.new_candidates] == [("C1", "100.1")]
    assert [c.dedup_key for c in forwarded] == [("C1", "100.1")]


# --------------------------------------------------------------------------- #
# build_fast_classify_client — lazy, network-free import (task 4.3)
# --------------------------------------------------------------------------- #
def test_build_fast_classify_client_requires_api_key() -> None:
    from loop.config import Settings

    with pytest.raises(RuntimeError):
        build_fast_classify_client(Settings(groq_api_key=""))


def test_build_fast_classify_client_is_constructible_without_network() -> None:
    # Constructing the client must not touch the network or import a provider SDK.
    from loop.config import Settings

    client = build_fast_classify_client(
        Settings(groq_api_key="gsk-test", fast_model="llama-3.1-8b-instant")
    )
    assert callable(client)
