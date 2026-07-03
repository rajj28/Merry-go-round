"""Tests for the Watcher's demo-channel scoping (live-detection allow-list).

These exercise the *real* :class:`loop.watcher.watcher.Watcher` (no mocking of the
Watcher itself) against the ``watch_channels`` allow-list that scopes live RTS
detection to a predictable set of channels for a live demo. The external ports are
injected as test doubles, matching the repo's watcher-test conventions:

  * the RTS port is a mock callable returning a fixed ``assistant.search.context``
    response (the shape :func:`loop.watcher.rts_contract.parse_rts_response` reads);
  * the ``forward`` port (Adjudicator hand-off) is a mock recorder;
  * the dedup state is a real ``:memory:`` :class:`SqliteObligationGraph`.

Observable behaviour under test:

  * with a non-empty allow-list, candidates whose ``channel_id`` is outside the
    allow-list are NOT forwarded (outcome ``OUT_OF_SCOPE``) and ones inside ARE;
  * this holds for both ``run_sweep`` and ``on_message_event`` (the shared
    per-candidate pipeline);
  * with no allow-list (``None``), behaviour is unchanged — every candidate is
    forwarded (backward compatible).

The property test runs ≥100 generated examples (enforced by the root ``conftest.py``)
and carries the traceability tag used elsewhere in the suite.
"""

from __future__ import annotations

from typing import Any, Mapping

from hypothesis import given
from hypothesis import strategies as st

from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.watcher.rts_contract import CandidateMessage
from loop.watcher.watcher import CandidateOutcome, Watcher


# --------------------------------------------------------------------------- #
# Test doubles / fixtures (match the existing watcher tests' shape)
# --------------------------------------------------------------------------- #
class StaticRtsClient:
    """A mock RTS port returning a fixed raw response and recording call count."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        self._response = response
        self.calls = 0

    def __call__(self) -> Mapping[str, Any]:
        self.calls += 1
        return self._response


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


def _message_event(channel: str, ts: str, text: str = "any update?") -> dict:
    """A Slack Events-API ``message`` event payload."""
    return {"channel": channel, "ts": ts, "user": "U_AUTHOR", "text": text}


# --------------------------------------------------------------------------- #
# Example-based: run_sweep honours the allow-list
# --------------------------------------------------------------------------- #
def test_sweep_drops_candidates_outside_the_allow_list() -> None:
    forwarded: list[CandidateMessage] = []
    client = StaticRtsClient(
        _rts_ok(
            _rts_message("C_DEMO", "100.1"),   # inside the allow-list  -> forwarded
            _rts_message("C_OTHER", "200.2"),  # outside the allow-list -> dropped
        )
    )
    watcher = Watcher(
        _graph(),
        client,
        forward=forwarded.append,
        interval_seconds=30,
        watch_channels={"C_DEMO"},
    )

    result = watcher.run_sweep()

    assert [c.dedup_key for c in forwarded] == [("C_DEMO", "100.1")]
    assert [c.dedup_key for c in result.new_candidates] == [("C_DEMO", "100.1")]


def test_sweep_with_no_allow_list_is_unchanged() -> None:
    # Backward compatible: watch_channels=None forwards every candidate.
    forwarded: list[CandidateMessage] = []
    client = StaticRtsClient(
        _rts_ok(_rts_message("C_DEMO", "100.1"), _rts_message("C_OTHER", "200.2"))
    )
    watcher = Watcher(
        _graph(),
        client,
        forward=forwarded.append,
        interval_seconds=30,
        watch_channels=None,
    )

    result = watcher.run_sweep()

    assert [c.dedup_key for c in forwarded] == [("C_DEMO", "100.1"), ("C_OTHER", "200.2")]
    assert [c.dedup_key for c in result.new_candidates] == [
        ("C_DEMO", "100.1"),
        ("C_OTHER", "200.2"),
    ]


def test_empty_allow_list_set_is_treated_as_no_restriction() -> None:
    # An empty set is normalized to "no restriction" (watch the whole workspace).
    forwarded: list[CandidateMessage] = []
    client = StaticRtsClient(_rts_ok(_rts_message("C_OTHER", "200.2")))
    watcher = Watcher(
        _graph(),
        client,
        forward=forwarded.append,
        interval_seconds=30,
        watch_channels=set(),
    )

    result = watcher.run_sweep()

    assert [c.dedup_key for c in forwarded] == [("C_OTHER", "200.2")]
    assert [c.dedup_key for c in result.new_candidates] == [("C_OTHER", "200.2")]


# --------------------------------------------------------------------------- #
# Example-based: on_message_event shares the same scoping
# --------------------------------------------------------------------------- #
def test_on_message_event_drops_out_of_scope_channel() -> None:
    forwarded: list[CandidateMessage] = []
    watcher = Watcher(
        _graph(),
        StaticRtsClient(_rts_ok()),
        forward=forwarded.append,
        interval_seconds=30,
        watch_channels={"C_DEMO"},
    )

    outcome = watcher.on_message_event(_message_event("C_OTHER", "200.2"))

    assert outcome is CandidateOutcome.OUT_OF_SCOPE
    assert forwarded == []


def test_on_message_event_forwards_in_scope_channel() -> None:
    forwarded: list[CandidateMessage] = []
    watcher = Watcher(
        _graph(),
        StaticRtsClient(_rts_ok()),
        forward=forwarded.append,
        interval_seconds=30,
        watch_channels={"C_DEMO"},
    )

    outcome = watcher.on_message_event(_message_event("C_DEMO", "100.1"))

    assert outcome is CandidateOutcome.FORWARDED
    assert [c.dedup_key for c in forwarded] == [("C_DEMO", "100.1")]


# --------------------------------------------------------------------------- #
# Property: the allow-list is exactly the forwarding filter
# --------------------------------------------------------------------------- #
_CHANNELS = st.sampled_from(["C1", "C2", "C3", "C4"])
_TIMESTAMPS = st.sampled_from(["100.1", "200.2", "300.3", "400.4", "500.5"])


# Feature: loop-obligation-agent, Property: Watcher demo-channel scoping forwards only allowed channels
@given(
    allow=st.sets(_CHANNELS, min_size=1, max_size=4),
    refs=st.lists(st.tuples(_CHANNELS, _TIMESTAMPS), min_size=0, max_size=10),
)
def test_property_scoping_forwards_only_allowed_channels(
    allow: set[str],
    refs: list[tuple[str, str]],
) -> None:
    """Validates: Live-detection scoping (LOOP_WATCH_CHANNELS).

    For any non-empty allow-list and any set of candidate messages, the Watcher
    forwards a candidate to the Adjudicator only when its ``channel_id`` is in the
    allow-list. Candidates in disallowed channels are never forwarded; allowed,
    unique source refs are forwarded exactly once (dedup still holds). With an
    allow-list the forwarded channels are always a subset of the allow-list.
    """
    forwarded: list[CandidateMessage] = []
    client = StaticRtsClient(_rts_ok(*[_rts_message(c, t) for c, t in refs]))
    watcher = Watcher(
        _graph(),
        client,
        classify=lambda c: True,  # isolate scoping as the only forwarding filter
        forward=forwarded.append,
        interval_seconds=30,
        watch_channels=allow,
    )

    result = watcher.run_sweep()

    forwarded_keys = [c.dedup_key for c in forwarded]

    # Every forwarded candidate's channel is in the allow-list.
    assert all(channel in allow for channel, _ in forwarded_keys)
    # Exactly the unique in-scope source refs are forwarded.
    expected = {ref for ref in refs if ref[0] in allow}
    assert set(forwarded_keys) == expected
    # Dedup still holds: each forwarded source ref appears exactly once.
    assert len(forwarded_keys) == len(set(forwarded_keys))
    # The sweep result mirrors what was forwarded.
    assert [c.dedup_key for c in result.new_candidates] == forwarded_keys
