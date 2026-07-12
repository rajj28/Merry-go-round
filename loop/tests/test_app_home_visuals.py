"""Tests for the rendered-visual layer of the App Home: the first-run pipeline
diagram, the next-best-move recommendation, and the per-section summary lines."""

from __future__ import annotations

from loop.action.app_home import (
    ACTION_NUDGE,
    SPOTLIGHT_EYEBROW,
    build_app_home_view,
    first_run_diagram_url,
    next_best_action_blocks,
)
from loop.graph.chains import analyze
from loop.graph.models import ClosureKind, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph

NOW = "2025-01-08T12:00:00+00:00"
USER = "U_USER"


def _edge(
    oid: str,
    owes: str,
    owed: str,
    *,
    state: LoopState = LoopState.WAITING_ON_OTHER,
    touched: str = "2025-01-06T12:00:00+00:00",
    closure_kind: ClosureKind | None = None,
    closure_timestamp: str | None = None,
) -> Obligation:
    return Obligation(
        obligation_id=oid,
        owes_person_id=owes,
        owed_person_id=owed,
        owner_person_id=owes,
        loop_state=state,
        confidence_score=0.9,
        last_touch_timestamp=touched,
        source_msg_channel="C1",
        source_msg_ts="1700000000.000100",
        subject_summary=f"Subject {oid}",
        closure_kind=closure_kind,
        closure_timestamp=closure_timestamp,
    )


# ---------------------------------------------------------------------------
# First-run diagram
# ---------------------------------------------------------------------------
class TestTrendAndDiagram:
    def test_first_run_diagram_is_the_agent_pipeline(self) -> None:
        url = first_run_diagram_url()
        assert url.startswith("https://quickchart.io/graphviz?")
        for stage in ("Perceive", "Reason", "Verify", "Act", "Learn"):
            assert stage in url
        assert url == first_run_diagram_url()  # deterministic


# ---------------------------------------------------------------------------
# Next best move
# ---------------------------------------------------------------------------
class TestNextBestMove:
    def _blocked(self, oid: str, owed: str, touched: str) -> Obligation:
        return _edge(oid, USER, owed, state=LoopState.BLOCKED_ON_YOU, touched=touched)

    def test_picks_the_heaviest_edge_and_says_why(self) -> None:
        b1 = self._blocked("b1", "U_A", "2025-01-07T12:00:00+00:00")
        b2 = self._blocked("b2", "U_C", "2025-01-08T00:00:00+00:00")
        chain = [_edge("c1", "U_C", "U_L"), _edge("c2", "U_L", "U_J")]
        report = analyze([b1, b2, *chain])
        blocks = next_best_action_blocks([b1, b2], report, NOW)
        assert blocks, "expected a recommendation"
        # Redesigned spotlight: eyebrow → section → meta context → actions.
        assert blocks[0]["elements"][0]["text"] == SPOTLIGHT_EYEBROW
        section_text = blocks[1]["text"]["text"]
        assert "Subject b2" in section_text  # b2 unblocks 3 people; b1 only 1
        assert "has been waiting" in section_text
        assert "unblocks *3 people*" in str(blocks)  # rides the meta context line
        button = blocks[-1]["elements"][0]
        assert button["action_id"] == ACTION_NUDGE
        assert button["value"] == "b2"
        assert button["style"] == "primary"

    def test_age_breaks_ties(self) -> None:
        older = self._blocked("b1", "U_A", "2025-01-01T12:00:00+00:00")
        newer = self._blocked("b2", "U_B", "2025-01-08T00:00:00+00:00")
        report = analyze([older, newer])
        blocks = next_best_action_blocks([older, newer], report, NOW)
        assert "Subject b1" in blocks[1]["text"]["text"]  # the spotlight section

    def test_quiet_when_prioritization_adds_no_signal(self) -> None:
        only = self._blocked("b1", "U_A", "2025-01-07T12:00:00+00:00")
        report = analyze([only])
        assert next_best_action_blocks([only], report, NOW) == []
        assert next_best_action_blocks([], report, NOW) == []
        assert next_best_action_blocks([only], None, NOW) == []


# ---------------------------------------------------------------------------
# View integration
# ---------------------------------------------------------------------------
class TestViewIntegration:
    def test_first_run_includes_the_pipeline_diagram(self) -> None:
        graph = SqliteObligationGraph(database_path=IN_MEMORY)
        view = build_app_home_view(graph, now=NOW, user_id=USER)
        assert first_run_diagram_url() in str(view)

    def test_section_summary_line_counts_and_ages(self) -> None:
        graph = SqliteObligationGraph(database_path=IN_MEMORY)
        graph.upsert(
            _edge("b1", USER, "U_A", state=LoopState.BLOCKED_ON_YOU,
                  touched="2025-01-04T12:00:00+00:00")
        )
        graph.upsert(
            _edge("b2", USER, "U_B", state=LoopState.BLOCKED_ON_YOU,
                  touched="2025-01-08T00:00:00+00:00")
        )
        view = build_app_home_view(graph, now=NOW, user_id=USER)
        # Descriptor + tally merge into one line under the header (redesign).
        assert "2 open, oldest 4d" in str(view)
