"""Tests for the App Home intelligence layer: deadlock cards, chain pressure,
impact strip, and Learn visibility (all opt-in — the classic layout must be
byte-identical when the new parameters are omitted)."""

from __future__ import annotations

from loop.action.app_home import (
    ACTION_CYCLE_SEND,
    ACTION_DEMO_RESET,
    ACTION_DISMISS,
    DEADLOCK_SECTION_TITLE,
    NUDGE_MODAL_CALLBACK,
    NUDGE_MODAL_INPUT_ACTION,
    NUDGE_MODAL_INPUT_BLOCK,
    build_app_home_view,
    build_cycle_modal,
    chain_pressure_text,
    deadlock_graph_url,
    deadlock_ring_text,
    impact_stats_text,
    learn_text,
)
from loop.action.cycle_breaker import plan_cycle_break
from loop.graph.chains import analyze, find_cycles
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


def _ring_plans():
    edges = [
        _edge("o1", "A", "B", touched="2025-01-05T12:00:00+00:00"),
        _edge("o2", "B", "C", touched="2025-01-02T12:00:00+00:00"),
        _edge("o3", "C", "A", touched="2025-01-06T12:00:00+00:00"),
    ]
    return [plan_cycle_break(c) for c in find_cycles(edges)]


def _store() -> SqliteObligationGraph:
    return SqliteObligationGraph(database_path=IN_MEMORY)


# ---------------------------------------------------------------------------
# Opt-out invariance: omitting the new params changes nothing
# ---------------------------------------------------------------------------
def test_default_view_is_unchanged_without_new_params() -> None:
    graph = _store()
    graph.upsert(_edge("B1", USER, "U_OTHER", state=LoopState.BLOCKED_ON_YOU))
    baseline = build_app_home_view(graph, now=NOW, user_id=USER)
    explicit = build_app_home_view(
        graph,
        now=NOW,
        user_id=USER,
        break_plans=(),
        chain_report=None,
        learn_threshold=None,
        show_impact=False,
    )
    assert baseline == explicit
    text = str(baseline)
    assert DEADLOCK_SECTION_TITLE not in text
    assert "Surfacing gate" not in text


# ---------------------------------------------------------------------------
# Deadlock section
# ---------------------------------------------------------------------------
def test_deadlock_section_renders_ring_plan_and_actions() -> None:
    graph = _store()
    view = build_app_home_view(
        graph, now=NOW, user_id=USER, break_plans=_ring_plans()
    )
    text = str(view)
    assert DEADLOCK_SECTION_TITLE in text
    assert "A → B → C → A" in text  # the ring, closed back onto itself
    assert "Subject o2" in text  # the stalest edge is the break edge
    action_ids = [
        el.get("action_id")
        for b in view["blocks"]
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    ]
    assert ACTION_CYCLE_SEND in action_ids
    assert ACTION_DISMISS in action_ids  # "Not a deadlock" reuses the dismiss flow
    send = next(
        el
        for b in view["blocks"]
        if b.get("type") == "actions"
        for el in b.get("elements", [])
        if el.get("action_id") == ACTION_CYCLE_SEND
    )
    assert send["value"] == "o2"


def test_deadlock_ring_text_uses_display_names() -> None:
    plans = _ring_plans()
    names = {"A": "Alice", "B": "Bob", "C": "Carol"}
    assert deadlock_ring_text(plans[0].cycle.people, names) == (
        "Alice → Bob → Carol → Alice"
    )


def test_deadlock_card_carries_the_ring_diagram() -> None:
    graph = _store()
    view = build_app_home_view(
        graph, now=NOW, user_id=USER, break_plans=_ring_plans()
    )
    images = [b for b in view["blocks"] if b.get("type") == "image"]
    diagram = next(
        b for b in images if str(b.get("alt_text", "")).startswith("Deadlock:")
    )
    assert "quickchart.io/graphviz" in diagram["image_url"]
    assert diagram["alt_text"] == "Deadlock: A → B → C → A"
    assert diagram["title"]["text"] == "A → B → C → A"


def test_deadlock_graph_url_highlights_the_break_edge() -> None:
    (plan,) = _ring_plans()
    names = {"A": "Alice", "B": "Bob", "C": "Carol"}
    url = deadlock_graph_url(plan, names)
    assert url.startswith("https://quickchart.io/graphviz?")
    assert "%23E01E5A" in url  # the Slack-red break edge
    assert "start%20here" in url  # ...labeled as the first move
    assert "Alice" in url and "Bob" in url and "Carol" in url
    # Deterministic: same plan, same URL.
    assert url == deadlock_graph_url(plan, names)


def test_build_cycle_modal_reuses_the_composer_contract() -> None:
    (plan,) = _ring_plans()
    modal = build_cycle_modal(plan)
    # Same callback + input ids as the nudge composer: one submit handler
    # backs both, and the send stays behind the explicit "Send as you".
    assert modal["callback_id"] == NUDGE_MODAL_CALLBACK
    assert modal["private_metadata"] == plan.break_obligation_id
    assert modal["title"]["text"] == "Break the deadlock"
    assert modal["submit"]["text"] == "Send as you"
    input_block = next(
        b for b in modal["blocks"] if b.get("block_id") == NUDGE_MODAL_INPUT_BLOCK
    )
    assert input_block["element"]["action_id"] == NUDGE_MODAL_INPUT_ACTION
    assert input_block["element"]["initial_value"] == plan.draft_message
    assert "A → B → C → A" in str(modal["blocks"])


def test_demo_controls_render_after_the_footer_only_when_enabled() -> None:
    graph = _store()
    view = build_app_home_view(graph, now=NOW, user_id=USER, demo_controls=True)
    last = view["blocks"][-1]
    assert last["type"] == "actions"
    assert last["elements"][0]["action_id"] == ACTION_DEMO_RESET
    # The footer is still the last *context* block (Property 22 invariant).
    context_blocks = [b for b in view["blocks"] if b.get("type") == "context"]
    assert "Blocked on you" in context_blocks[-1]["elements"][0]["text"]
    # Off by default.
    plain = build_app_home_view(graph, now=NOW, user_id=USER)
    assert ACTION_DEMO_RESET not in str(plain)


# ---------------------------------------------------------------------------
# Chain pressure line
# ---------------------------------------------------------------------------
def test_chain_pressure_names_the_heaviest_blocked_edge() -> None:
    # USER owes B; B owes C; C owes D → the user's edge holds up 3 people.
    edges = [
        _edge("u1", USER, "B", state=LoopState.BLOCKED_ON_YOU),
        _edge("o2", "B", "C"),
        _edge("o3", "C", "D"),
    ]
    report = analyze(edges)
    line = chain_pressure_text(report, [edges[0]])
    assert line is not None
    assert "Subject u1" in line
    assert "3 people" in line


def test_chain_pressure_stays_quiet_below_two_downstream() -> None:
    edges = [_edge("u1", USER, "B", state=LoopState.BLOCKED_ON_YOU)]
    report = analyze(edges)
    assert chain_pressure_text(report, [edges[0]]) is None
    assert chain_pressure_text(report, []) is None


# ---------------------------------------------------------------------------
# Impact strip + Learn footer
# ---------------------------------------------------------------------------
def test_impact_stats_counts_week_window_and_people() -> None:
    healed = [
        _edge(
            "h1",
            USER,
            "B",
            state=LoopState.HEALED,
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_timestamp="2025-01-07T12:00:00+00:00",  # within 7d of NOW
        ),
        _edge(
            "h2",
            USER,
            "C",
            state=LoopState.HEALED,
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_timestamp="2024-12-01T12:00:00+00:00",  # long ago
        ),
    ]
    text = impact_stats_text(healed, NOW)
    assert "2 loops auto-healed" in text
    assert "1 in the last 7 days" in text
    assert "2 people unblocked" in text


def test_first_run_onboarding_renders_only_on_an_untracked_graph() -> None:
    from loop.action.app_home import FIRST_RUN_TEXT

    empty = _store()
    view = build_app_home_view(empty, now=NOW, user_id=USER)
    texts = str(view)
    assert FIRST_RUN_TEXT in texts
    assert "Real-Time Search" in texts
    # Footer is still the last block (the onboarding sits under the hero).
    assert view["blocks"][-1]["type"] == "context"

    # Any tracked loop — even one the user isn't blocked by — ends first-run.
    tracked = _store()
    tracked.upsert(_edge("W1", "U_OTHER", USER))
    assert FIRST_RUN_TEXT not in str(build_app_home_view(tracked, now=NOW, user_id=USER))


def test_impact_strip_stays_quiet_with_no_healed_loops() -> None:
    graph = _store()
    graph.upsert(_edge("B1", USER, "U_OTHER", state=LoopState.BLOCKED_ON_YOU))
    view = build_app_home_view(graph, now=NOW, user_id=USER, show_impact=True)
    assert "loops auto-healed" not in str(view)


def test_learn_footer_rides_in_the_footer_context_block() -> None:
    graph = _store()
    view = build_app_home_view(graph, now=NOW, user_id=USER, learn_threshold=0.72)
    context_blocks = [b for b in view["blocks"] if b.get("type") == "context"]
    footer = context_blocks[-1]
    # Property 22 invariant: element[0] is still the totals line.
    assert "Blocked on you" in footer["elements"][0]["text"]
    assert learn_text(0.72) == footer["elements"][1]["text"]
    assert "0.72" in footer["elements"][1]["text"]
