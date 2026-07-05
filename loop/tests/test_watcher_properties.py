"""Property-based + fault-injection tests for the Watcher (Perceive) (tasks 4.4–4.6).

These exercise the *real* :class:`loop.watcher.watcher.Watcher` (no mocking of the
Watcher itself) against the universal correctness properties from
design.md → "Correctness Properties". The external ports are injected as test
doubles, matching the design's testing rule that "external dependencies (fast-tier,
the smart tier, ...) are mocked so properties test Loop's logic":

  * the RTS port is a mock callable returning a fixed ``assistant.search.context``
    response (the shape :func:`loop.watcher.rts_contract.parse_rts_response` reads);
  * the ``classify`` port (the fast tier) is a mock per-candidate boolean verdict;
  * the ``forward`` port (Adjudicator hand-off) is a mock recorder;
  * the dedup state is a real ``:memory:`` :class:`SqliteObligationGraph`.

Coverage:
  * 4.4 → Property 6 — High-recall forwarding loses no positive candidate (Req 2.5)
  * 4.5 → Property 7 — No duplicate candidate per source message (Req 2.9)
  * 4.6 → fault-injection unit tests — RTS empty / RTS error+retry / fast-tier failure
    + re-evaluate next sweep (Req 2.6, 2.7, 2.8)

Each property test runs ≥100 generated examples (enforced by the root ``conftest.py``)
and carries the required traceability tag.
"""

from __future__ import annotations

from typing import Any, Mapping

from hypothesis import given
from hypothesis import strategies as st

from loop.graph.models import LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.watcher.rts_contract import CandidateMessage
from loop.watcher.watcher import CandidateOutcome, Watcher


# --------------------------------------------------------------------------- #
# Test doubles / fixtures (shared with the example-based watcher tests' shape)
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
    """A fresh, isolated in-memory store (matches the graph property tests)."""
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
# Hypothesis strategies
# --------------------------------------------------------------------------- #
# Small pools of channels/timestamps so duplicate source refs and seed/candidate
# overlaps recur across examples — exercising the dedup-collapse paths (Req 2.9).
_CHANNELS = st.sampled_from(["C1", "C2", "C3"])
_TIMESTAMPS = st.sampled_from(["100.1", "200.2", "300.3", "400.4"])
_SOURCE_REF = st.tuples(_CHANNELS, _TIMESTAMPS)


@st.composite
def candidate_specs(draw: st.DrawFn) -> list[tuple[str, str, bool]]:
    """A list of ``(channel, ts, classify_verdict)`` candidate specs.

    The list MAY contain repeated ``(channel, ts)`` source refs (duplicates) so the
    in-pass dedup collapse is covered. Verdicts per source ref are made consistent
    by the caller so the injected classifier is well defined per candidate.
    """
    return draw(
        st.lists(
            st.tuples(_CHANNELS, _TIMESTAMPS, st.booleans()),
            min_size=0,
            max_size=10,
        )
    )


# --------------------------------------------------------------------------- #
# Property 6 (task 4.4) — High-recall forwarding loses no positive candidate
# --------------------------------------------------------------------------- #
# Feature: loop-obligation-agent, Property 6: High-recall forwarding loses no positive candidate
@given(specs=candidate_specs())
def test_property_6_high_recall_forwarding_loses_no_positive(
    specs: list[tuple[str, str, bool]],
) -> None:
    """Validates: Requirements 2.5.

    For any set of candidate messages and a per-candidate classifier verdict, every
    candidate the classifier marks as a potential open loop is forwarded to the
    Adjudicator. The forwarded set contains exactly all classify-positive source
    refs, with duplicates collapsed (Req 2.9 dedup), and never a classify-negative.
    """
    # Resolve a single verdict per source ref (first occurrence wins) so the injected
    # classifier is deterministic per candidate even when refs repeat.
    verdict_by_ref: dict[tuple[str, str], bool] = {}
    for channel, ts, verdict in specs:
        verdict_by_ref.setdefault((channel, ts), verdict)

    forwarded: list[CandidateMessage] = []
    client = StaticRtsClient(_rts_ok(*[_rts_message(c, t) for c, t, _ in specs]))
    watcher = Watcher(
        _graph(),
        client,
        classify=lambda c: verdict_by_ref[c.dedup_key],
        forward=forwarded.append,
        interval_seconds=30,
    )

    result = watcher.run_sweep()

    forwarded_keys = [c.dedup_key for c in forwarded]
    expected_positive_refs = {ref for ref, v in verdict_by_ref.items() if v}

    # High recall: every classify-positive (incl. low-certainty) source ref is forwarded.
    assert set(forwarded_keys) == expected_positive_refs
    # No positive candidate is dropped, and no negative is forwarded.
    assert expected_positive_refs.isdisjoint(
        {ref for ref, v in verdict_by_ref.items() if not v}
    )
    # Duplicates collapse: each forwarded source ref appears exactly once (Req 2.9).
    assert len(forwarded_keys) == len(set(forwarded_keys))
    # The sweep result mirrors what was forwarded.
    assert [c.dedup_key for c in result.new_candidates] == forwarded_keys


# --------------------------------------------------------------------------- #
# Property 7 (task 4.5) — No duplicate candidate per source message
# --------------------------------------------------------------------------- #
# Feature: loop-obligation-agent, Property 7: No duplicate candidate per source message
@given(
    seeded_refs=st.sets(_SOURCE_REF, max_size=6),
    candidate_refs=st.lists(_SOURCE_REF, min_size=0, max_size=10),
)
def test_property_7_no_duplicate_candidate_per_source_message(
    seeded_refs: set[tuple[str, str]],
    candidate_refs: list[tuple[str, str]],
) -> None:
    """Validates: Requirements 2.9.

    For any graph state and any candidate, the Watcher forwards the candidate to the
    Adjudicator only when its source Slack message reference does not already
    correspond to an existing obligation. Candidates whose source ref is already in
    the graph are never forwarded; new, unique refs are forwarded exactly once.
    """
    graph = _graph()
    for channel, ts in seeded_refs:
        _seed_obligation(graph, channel, ts)

    forwarded: list[CandidateMessage] = []
    client = StaticRtsClient(_rts_ok(*[_rts_message(c, t) for c, t in candidate_refs]))
    # classify accepts everything so the only filter under test is source-ref dedup.
    watcher = Watcher(
        graph,
        client,
        classify=lambda c: True,
        forward=forwarded.append,
        interval_seconds=30,
    )

    result = watcher.run_sweep()

    forwarded_keys = [c.dedup_key for c in forwarded]

    # No candidate whose source ref already exists in the graph is ever forwarded.
    assert all(ref not in seeded_refs for ref in forwarded_keys)
    # Exactly the new (non-pre-existing) unique source refs are forwarded.
    expected_new_refs = {ref for ref in candidate_refs if ref not in seeded_refs}
    assert set(forwarded_keys) == expected_new_refs
    # Each forwarded source ref appears exactly once — no duplicate submission.
    assert len(forwarded_keys) == len(set(forwarded_keys))
    assert [c.dedup_key for c in result.new_candidates] == forwarded_keys


# --------------------------------------------------------------------------- #
# Task 4.6 — Fault-injection unit tests (example-based): RTS + fast-tier failures
# --------------------------------------------------------------------------- #
def test_rts_empty_creates_no_obligations() -> None:
    """Validates: Requirement 2.6.

    When RTS returns no results, the sweep completes creating no obligations and
    forwarding nothing to the Adjudicator.
    """
    forwarded: list[CandidateMessage] = []
    client = StaticRtsClient(_rts_ok())  # ok=true, zero messages
    watcher = Watcher(
        _graph(), client, forward=forwarded.append, interval_seconds=30
    )

    result = watcher.run_sweep()

    assert result.rts_ok is True
    assert result.new_candidates == []
    assert forwarded == []


def test_rts_error_creates_no_obligations_and_retries_next_sweep() -> None:
    """Validates: Requirement 2.7.

    When RTS is unreachable/errors, the sweep completes creating no obligations
    (the failure is contained, not raised) and the next scheduled sweep re-attempts
    retrieval and can succeed.
    """
    forwarded: list[CandidateMessage] = []

    class FlakyRts:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> Mapping[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("RTS unreachable")
            return _rts_ok(_rts_message("C1", "100.1"))

    client = FlakyRts()
    watcher = Watcher(
        _graph(), client, forward=forwarded.append, interval_seconds=30
    )

    first = watcher.run_sweep()  # must NOT raise
    assert first.rts_ok is False
    assert first.new_candidates == []
    assert first.error is not None
    assert forwarded == []

    second = watcher.run_sweep()  # retry on the next scheduled sweep
    assert second.rts_ok is True
    assert [c.dedup_key for c in second.new_candidates] == [("C1", "100.1")]
    assert [c.dedup_key for c in forwarded] == [("C1", "100.1")]
    assert client.calls == 2


def test_rts_error_is_fully_contained_for_all_failure_modes() -> None:
    """Validates: Requirement 2.7.

    Any RTS transport failure (unreachable / timeout / generic error) is contained:
    the sweep returns a clean empty result without raising.
    """
    for exc in (
        ConnectionError("unreachable"),
        TimeoutError("timed out"),
        RuntimeError("boom"),
    ):
        forwarded: list[CandidateMessage] = []
        watcher = Watcher(
            _graph(),
            RaisingRtsClient(exc),
            forward=forwarded.append,
            interval_seconds=30,
        )

        result = watcher.run_sweep()

        assert result.rts_ok is False
        assert result.new_candidates == []
        assert result.error is not None
        assert forwarded == []


def test_fast_tier_failure_excludes_candidate_and_reevaluates_next_sweep() -> None:
    """Validates: Requirement 2.8.

    When the fast-tier classifier fails (raises) for a candidate, the Watcher excludes
    it from forwarding without marking it processed, so the next sweep that retrieves
    it re-evaluates it. Here fast-tier fails on the first sweep and succeeds on the
    second; the candidate is forwarded exactly once, on the second sweep.
    """
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

    first = watcher.run_sweep()  # fast-tier raises → excluded, no crash
    assert first.rts_ok is True  # RTS itself succeeded; only fast-tier failed
    assert first.new_candidates == []
    assert forwarded == []

    second = watcher.run_sweep()  # candidate re-evaluated and forwarded
    assert [c.dedup_key for c in second.new_candidates] == [("C1", "100.1")]
    assert [c.dedup_key for c in forwarded] == [("C1", "100.1")]


def test_fast_tier_failure_on_live_message_is_reevaluated_by_a_later_sweep() -> None:
    """Validates: Requirement 2.8.

    The live message path shares the same exclusion semantics: a fast-tier failure on a
    live message excludes it (CLASSIFY_FAILED) without marking it processed, so a
    later sweep that retrieves the same source message re-evaluates and forwards it.
    """
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

    outcome = watcher.on_message_event(
        {"channel": "C1", "ts": "100.1", "user": "U_AUTHOR", "text": "any update?"}
    )
    assert outcome is CandidateOutcome.CLASSIFY_FAILED
    assert forwarded == []

    sweep = watcher.run_sweep()
    assert [c.dedup_key for c in sweep.new_candidates] == [("C1", "100.1")]
    assert [c.dedup_key for c in forwarded] == [("C1", "100.1")]
