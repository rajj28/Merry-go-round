"""Tests for the rich Block Kit layer: native ``data_visualization`` charts,
the ``data_table`` auto-healed feed, ``container`` grouping, the modal
``alert`` banner, and the failure-safe publish fallback in the app wiring.

Shape assertions follow the official block reference schemas (type strings,
required fields, per-series point caps, table header row, container child
limits), so a passing suite means the JSON we publish is structurally valid.
"""

from __future__ import annotations

from loop.action.app_home import (
    HEALED_SECTION_TITLE,
    aging_spread_chart,
    build_app_home_view,
    build_cycle_modal,
    heal_trend_chart,
    healed_feed_table,
)
from loop.action.cycle_breaker import plan_cycle_break
from loop.graph.chains import find_cycles
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
    closure_reason: str | None = None,
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
        closure_reason=closure_reason,
    )


def _healed(oid: str, owed: str, closed: str) -> Obligation:
    return _edge(
        oid,
        USER,
        owed,
        state=LoopState.HEALED,
        closure_kind=ClosureKind.AUTONOMOUS,
        closure_timestamp=closed,
        closure_reason="PR merged",
    )


# ---------------------------------------------------------------------------
# data_visualization charts
# ---------------------------------------------------------------------------
class TestNativeCharts:
    def test_heal_trend_chart_shape(self) -> None:
        chart = heal_trend_chart([_healed("h1", "U_B", "2025-01-07T12:00:00+00:00")], NOW)
        assert chart is not None
        assert chart["type"] == "data_visualization"
        assert len(chart["title"]) <= 50
        inner = chart["chart"]
        assert inner["type"] == "area"
        (series,) = inner["series"]
        assert len(series["name"]) <= 20
        assert 1 <= len(series["data"]) <= 20  # the block's per-series cap
        labels = [p["label"] for p in series["data"]]
        assert labels == inner["axis_config"]["categories"]  # labels must match
        assert sum(p["value"] for p in series["data"]) == 1

    def test_heal_trend_chart_none_when_window_empty(self) -> None:
        assert heal_trend_chart([], NOW) is None
        old = _healed("h1", "U_B", "2024-01-01T00:00:00+00:00")
        assert heal_trend_chart([old], NOW) is None

    def test_aging_spread_chart_buckets(self) -> None:
        rows = [
            _edge("a", USER, "U_A", touched="2025-01-08T06:00:00+00:00"),  # fresh
            _edge("b", USER, "U_B", touched="2025-01-06T12:00:00+00:00"),  # warning
            _edge("c", USER, "U_C", touched="2025-01-01T12:00:00+00:00"),  # overdue
            _edge("d", USER, "U_D", touched="2025-01-02T12:00:00+00:00"),  # overdue
        ]
        chart = aging_spread_chart(rows, NOW)
        assert chart is not None and chart["type"] == "data_visualization"
        assert chart["chart"]["type"] == "bar"
        (series,) = chart["chart"]["series"]
        values = {p["label"]: p["value"] for p in series["data"]}
        assert values == {"Fresh": 1, "Warning": 1, "Overdue": 2}
        assert chart["chart"]["axis_config"]["categories"] == [
            "Fresh",
            "Warning",
            "Overdue",
        ]

    def test_aging_spread_chart_none_without_rows(self) -> None:
        assert aging_spread_chart([], NOW) is None


# ---------------------------------------------------------------------------
# data_table feed
# ---------------------------------------------------------------------------
class TestHealedTable:
    def test_table_shape_and_header_row(self) -> None:
        healed = [
            _healed("h1", "U_B", "2025-01-07T12:00:00+00:00"),
            _healed("h2", "U_C", "2025-01-06T12:00:00+00:00"),
        ]
        table = healed_feed_table(healed, USER, names={"U_B": "Bob", "U_C": "Carol"})
        assert table is not None
        assert table["type"] == "data_table"
        assert table["caption"]  # required by the block schema
        assert table["page_size"] == 5
        rows = table["rows"]
        assert [c["text"] for c in rows[0]] == ["Person", "Loop", "Closed", "Why"]
        assert len(rows) == 3  # header + 2 data rows
        assert all(len(r) == len(rows[0]) for r in rows)  # rectangular
        assert rows[1][0] == {"type": "raw_text", "text": "Bob"}
        assert rows[1][3]["text"] == "PR merged"

    def test_table_none_when_feed_empty(self) -> None:
        assert healed_feed_table([], USER) is None

    def test_table_preserves_the_newest_first_feed_order(self) -> None:
        # The feed selector orders newest→oldest (Req 9.3); the table must
        # render rows in exactly the order it is given.
        healed = [
            _healed("h_new", "U_B", "2025-01-07T12:00:00+00:00"),
            _healed("h_old", "U_C", "2025-01-02T12:00:00+00:00"),
        ]
        table = healed_feed_table(healed, USER)
        assert table is not None
        assert table["rows"][1][1]["text"] == "Subject h_new"
        assert table["rows"][2][1]["text"] == "Subject h_old"

    def test_table_caps_at_the_feed_limit(self) -> None:
        healed = [
            _healed(f"h{i:03d}", f"U_{i}", "2025-01-07T12:00:00+00:00")
            for i in range(150)
        ]
        table = healed_feed_table(healed, USER)
        assert table is not None
        assert len(table["rows"]) == 101  # header + the 100-row block maximum


# ---------------------------------------------------------------------------
# View integration + containers
# ---------------------------------------------------------------------------
def _store() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


class TestRichViewIntegration:
    def test_rich_off_is_byte_identical_to_classic(self) -> None:
        graph = _store()
        graph.upsert(_edge("b1", USER, "U_A", state=LoopState.BLOCKED_ON_YOU))
        assert build_app_home_view(graph, now=NOW, user_id=USER) == build_app_home_view(
            graph, now=NOW, user_id=USER, rich=False
        )

    def test_rich_healed_feed_is_a_table_under_the_section_header(self) -> None:
        graph = _store()
        graph.upsert(_healed("h1", "U_B", "2025-01-07T12:00:00+00:00"))
        view = build_app_home_view(graph, now=NOW, user_id=USER, rich=True)
        blocks = view["blocks"]
        header_idx = next(
            i
            for i, b in enumerate(blocks)
            if b.get("type") == "header"
            and b["text"]["text"] == HEALED_SECTION_TITLE
        )
        assert blocks[header_idx + 1]["type"] == "data_table"
        # Classic renderer untouched when rich is off.
        classic = build_app_home_view(graph, now=NOW, user_id=USER)
        assert "data_table" not in str(classic)

    def test_rich_impact_uses_native_chart_not_image_sparkline(self) -> None:
        graph = _store()
        graph.upsert(_healed("h1", "U_B", "2025-01-07T12:00:00+00:00"))
        view = build_app_home_view(
            graph, now=NOW, user_id=USER, show_impact=True, rich=True
        )
        text = str(view)
        assert "data_visualization" in text
        assert "quickchart.io/chart" not in text  # no image sparkline in rich mode

    def test_rich_map_rides_in_a_collapsible_container(self) -> None:
        graph = _store()
        url = "https://quickchart.io/graphviz?graph=digraph%7B%7D"
        view = build_app_home_view(graph, now=NOW, user_id=USER, map_url=url, rich=True)
        container = next(
            b
            for b in view["blocks"]
            if b.get("type") == "container" and b["title"]["text"] == "The Map"
        )
        assert container["is_collapsible"] is True
        assert len(container["child_blocks"]) <= 10
        assert container["child_blocks"][0]["type"] == "image"

    def test_rich_disabled_degrades_each_type_independently(self) -> None:
        # A workspace that refuses charts + containers (but accepts data_table)
        # must still get the native table while the rest render classic.
        graph = _store()
        graph.upsert(_healed("h1", "U_B", "2025-01-07T12:00:00+00:00"))
        graph.upsert(_edge("b1", USER, "U_A", state=LoopState.BLOCKED_ON_YOU))
        url = "https://quickchart.io/graphviz?graph=digraph%7B%7D"
        view = build_app_home_view(
            graph,
            now=NOW,
            user_id=USER,
            show_impact=True,
            map_url=url,
            rich=True,
            rich_disabled=frozenset({"data_visualization", "container"}),
        )
        text = str(view)
        assert "data_visualization" not in text
        assert "'container'" not in text
        assert "data_table" in text  # the supported type still upgrades
        # Charts degrade to the classic image sparkline; the map to a bare image.
        assert "quickchart.io/chart" in text
        types = [b.get("type") for b in view["blocks"]]
        assert "image" in types

    def test_rich_with_every_type_disabled_matches_classic(self) -> None:
        graph = _store()
        graph.upsert(_healed("h1", "U_B", "2025-01-07T12:00:00+00:00"))
        graph.upsert(_edge("b1", USER, "U_A", state=LoopState.BLOCKED_ON_YOU))
        all_off = frozenset({"data_visualization", "data_table", "container"})
        assert build_app_home_view(
            graph, now=NOW, user_id=USER, show_impact=True, rich=True,
            rich_disabled=all_off,
        ) == build_app_home_view(graph, now=NOW, user_id=USER, show_impact=True)

    def test_rich_deadlocks_are_grouped_in_containers(self) -> None:
        edges = [
            _edge("o1", "A", "B"),
            _edge("o2", "B", "C"),
            _edge("o3", "C", "A"),
        ]
        plans = [plan_cycle_break(c) for c in find_cycles(edges)]
        graph = _store()
        view = build_app_home_view(graph, now=NOW, user_id=USER, break_plans=plans, rich=True)
        container = next(
            b
            for b in view["blocks"]
            if b.get("type") == "container"
            and b["title"]["text"].startswith("Deadlock:")
        )
        child_types = [c["type"] for c in container["child_blocks"]]
        assert child_types == ["image", "section", "context", "actions"]


# ---------------------------------------------------------------------------
# Modal alert banner
# ---------------------------------------------------------------------------
def test_cycle_modal_leads_with_a_warning_alert() -> None:
    edges = [_edge("o1", "A", "B"), _edge("o2", "B", "A")]
    (cycle,) = find_cycles(edges)
    modal = build_cycle_modal(plan_cycle_break(cycle))
    alert = modal["blocks"][0]
    assert alert["type"] == "alert"
    assert alert["level"] == "warning"
    assert len(alert["text"]["text"]) <= 200
    assert "Deadlock" in alert["text"]["text"]
