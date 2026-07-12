"""The emoji strip is the single guarantee that no emoji reaches a rendered
card, a drafted nudge, or a message sent as the user."""

from __future__ import annotations

from loop.action.action_agent import ActionAgent
from loop.emoji_utils import strip_emoji
from loop.graph.models import LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph


def test_strips_pictographs_but_keeps_typography() -> None:
    assert strip_emoji("Thanks for your patience! 🙏") == "Thanks for your patience!"
    assert strip_emoji("All done ✅") == "All done"
    assert strip_emoji("Give it a 👍 when you can") == "Give it a when you can"
    # The design's typographic symbols survive: arrows, mid-dot, dashes, ellipsis.
    assert strip_emoji("A → B ⇄ C · wait — really… 🎉") == "A → B ⇄ C · wait — really…"


def test_none_and_empty_are_safe() -> None:
    assert strip_emoji("") == ""
    assert strip_emoji(None) is None  # type: ignore[arg-type]


def test_collapses_gaps_and_trims() -> None:
    assert strip_emoji("hi   😀   there") == "hi there"
    assert strip_emoji("🚀 leading") == "leading"
    assert strip_emoji("trailing 🔥") == "trailing"


def _obligation() -> Obligation:
    return Obligation(
        obligation_id="o1",
        owes_person_id="U_OTHER",
        owed_person_id="U_USER",
        owner_person_id="U_OTHER",
        loop_state=LoopState.WAITING_ON_OTHER,
        confidence_score=0.9,
        last_touch_timestamp="2025-01-06T12:00:00+00:00",
        source_msg_channel="C1",
        source_msg_ts="1700000000.0001",
        subject_summary="the deck",
    )


def test_drafted_nudge_is_emoji_free_even_when_the_model_adds_them() -> None:
    graph = SqliteObligationGraph(IN_MEMORY)
    agent = ActionAgent(
        graph,
        verifier=object(),  # type: ignore[arg-type]
        draft=lambda o: "Hey! Circling back on the deck — no rush 🙏 thanks so much! 🎉",
    )
    result = agent.draft_polite_nudge(_obligation())
    assert result.drafted is True
    assert "🙏" not in result.draft.text and "🎉" not in result.draft.text
    assert result.draft.text == "Hey! Circling back on the deck — no rush thanks so much!"
