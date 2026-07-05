"""Tests for the rendered-visual layer of the App Home: the workspace map,
the auto-heal sparkline, the first-run pipeline diagram, the next-best-move
recommendation, and the per-section summary lines."""

from __future__ import annotations

from loop.action.app_home import (
    ACTION_NUDGE,
    MAP_MAX_EDGES,
    build_app_home_view,
    first_run_diagram_url,
    heal_trend_url,
    next_best_action_blocks,
    workspace_map_url,
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
# The workspace map
# ---------------------------------------------------------------------------
class TestWorkspaceMap:
    def test_none_when_nothing_to_draw(self) -> None:
        assert workspace_map_url([], USER) is None

    def test_user_is_labeled_you_in_aubergine(self) -> None:
        edges = [_edge("o1", USER, "U_ALICE", state=LoopState.BLOCKED_ON_YOU)]
        url = workspace_map_url(edges, USER, names={"U_ALICE": "Alice"})
        assert url is not None and url.startswith("https://quickchart.io/graphviz?")
        assert "You" in url
        assert "%234A154B" in url  # the aubergine user node
        assert "Alice" in url

    def test_cycle_edges_are_red_and_impact_sets_weight(self) -> None:
        edges = [
            _edge("o1", "A", "B"),
            _edge("o2", "B", "A"),
            _edge("o3", "C", "D"),
        ]
        report = analyze(edges)
        url = workspace_map_url(
            edges, USER, cycle_edge_ids=frozenset({"o1", "o2"}), chain_report=report
        )
        assert url is not None
        assert "%23E01E5A" in url  # red deadlock edges
        assert "penwidth" in url

    def test_caps_at_the_highest_impact_edges(self) -> None:
        # A long chain head plus > MAP_MAX_EDGES independent pairs: the chain
        # head must survive the cap (highest downstream impact ranks first).
        edges = [_edge("head", "A", "B"), _edge("mid", "B", "C")]
        for i in range(MAP_MAX_EDGES + 5):
            edges.append(_edge(f"x{i:02d}", f"P{i}", f"Q{i}"))
        report = analyze(edges)
        url = workspace_map_url(edges, USER, chain_report=report)
        assert url is not None
        assert url.count("-%3E") == MAP_MAX_EDGES  # "->" url-encoded, one per edge

    def test_deterministic(self) -> None:
        edges = [_edge("o1", "A", "B"), _edge("o2", "B", "C")]
        assert workspace_map_url(edges, USER) == workspace_map_url(edges, USER)


# ---------------------------------------------------------------------------
# Sparkline + first-run diagram
# ---------------------------------------------------------------------------
class TestTrendAndDiagram:
    def test_sparkline_none_when_window_empty(self) -> None:
        old = _edge(
            "h1",
            USER,
            "B",
            state=LoopState.HEALED,
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_timestamp="2024-01-01T00:00:00+00:00",  # far outside 14d
        )
        assert heal_trend_url([old], NOW) is None
        assert heal_trend_url([], NOW) is None

    def test_sparkline_renders_recent_closures(self) -> None:
        healed = [
            _edge(
                "h1",
                USER,
                "B",
                state=LoopState.HEALED,
                closure_kind=ClosureKind.AUTONOMOUS,
                closure_timestamp="2025-01-07T12:00:00+00:00",
            )
        ]
        url = heal_trend_url(healed, NOW)
        assert url is not None and url.startswith("https://quickchart.io/chart?")
        assert "sparkline" in url

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
        text = blocks[0]["text"]["text"]
        assert "Next best move" in text
        assert "Subject b2" in text  # b2 unblocks 3 people; b1 only 1
        assert "unblocks *3 people*" in text
        button = blocks[1]["elements"][0]
        assert button["action_id"] == ACTION_NUDGE
        assert button["value"] == "b2"
        assert button["style"] == "primary"

    def test_age_breaks_ties(self) -> None:
        older = self._blocked("b1", "U_A", "2025-01-01T12:00:00+00:00")
        newer = self._blocked("b2", "U_B", "2025-01-08T00:00:00+00:00")
        report = analyze([older, newer])
        blocks = next_best_action_blocks([older, newer], report, NOW)
        assert "Subject b1" in blocks[0]["text"]["text"]

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
    def test_map_block_renders_when_url_supplied(self) -> None:
        graph = SqliteObligationGraph(database_path=IN_MEMORY)
        url = "https://quickchart.io/graphviz?graph=digraph%7B%7D"
        view = build_app_home_view(graph, now=NOW, user_id=USER, map_url=url)
        images = [b for b in view["blocks"] if b.get("type") == "image"]
        assert any(b["image_url"] == url for b in images)
        # Absent by default.
        plain = build_app_home_view(graph, now=NOW, user_id=USER)
        assert url not in str(plain)

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
        assert "2 open · oldest 4d" in str(view)
