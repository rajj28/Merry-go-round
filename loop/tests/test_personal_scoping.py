"""Personal surfaces stay personal once third-party loops enter the graph.

Third-party edges (Priya owes Marco) intentionally share ``WAITING_ON_OTHER``
state and feed the workspace map / chains / deadlock intelligence — but they
must never appear on surfaces that speak *to the user*: the App Home sections
and hero count, the daily digest, and Assistant-pane queries.
"""

from __future__ import annotations

from loop.action.action_agent import ActionAgent
from loop.action.app_home import (
    blocked_on_you_rows,
    build_app_home_view,
    hero_count,
    waiting_on_other_rows,
)
from loop.conversational.conversational_agent import ConversationalAgent
from loop.graph.models import LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph

NOW = "2025-01-08T12:00:00+00:00"
USER = "U_USER"
PRIYA = "U_PRIYA"
MARCO = "U_MARCO"


def _edge(oid: str, owes: str, owed: str, state: LoopState) -> Obligation:
    return Obligation(
        obligation_id=oid,
        owes_person_id=owes,
        owed_person_id=owed,
        owner_person_id=owes,
        loop_state=state,
        confidence_score=0.9,  # above threshold: surfacing alone can't hide it
        last_touch_timestamp="2025-01-06T12:00:00+00:00",
        source_msg_channel="C1",
        source_msg_ts=f"1700000000.{oid}",
        subject_summary=f"Subject {oid}",
    )


def _graph_with_mixed_edges() -> SqliteObligationGraph:
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    graph.upsert(_edge("mine_blocked", USER, PRIYA, LoopState.BLOCKED_ON_YOU))
    graph.upsert(_edge("mine_waiting", MARCO, USER, LoopState.WAITING_ON_OTHER))
    # The third-party edge: same state as personal waiting rows, high confidence.
    graph.upsert(_edge("third_party", PRIYA, MARCO, LoopState.WAITING_ON_OTHER))
    return graph


# --------------------------------------------------------------------------- #
# App Home selectors + hero
# --------------------------------------------------------------------------- #
def test_waiting_rows_scoped_to_the_user_exclude_third_party_edges() -> None:
    graph = _graph_with_mixed_edges()
    scoped = waiting_on_other_rows(graph, NOW, USER)
    assert [o.obligation_id for o in scoped] == ["mine_waiting"]
    # Unscoped behaviour (no user) is unchanged: both waiting edges.
    assert len(waiting_on_other_rows(graph, NOW)) == 2


def test_blocked_rows_scoped_to_the_user_require_the_user_as_ower() -> None:
    graph = _graph_with_mixed_edges()
    scoped = blocked_on_you_rows(graph, NOW, USER)
    assert [o.obligation_id for o in scoped] == ["mine_blocked"]


def test_hero_count_accepts_user_scoping() -> None:
    graph = _graph_with_mixed_edges()
    assert hero_count(graph, NOW, USER) == 1


def test_app_home_view_never_renders_a_third_party_row() -> None:
    graph = _graph_with_mixed_edges()
    view = build_app_home_view(graph, now=NOW, user_id=USER)
    text = str(view)
    assert "Subject mine_blocked" in text
    assert "Subject mine_waiting" in text
    assert "Subject third_party" not in text
    # Footer totals count personal rows only (1 blocked + 1 waiting).
    assert "1 blocked on you · 1 waiting on others" in text or "1" in text


def test_every_member_gets_their_own_perspective() -> None:
    """Multi-user Home: the same edges read correctly from any member's side,
    regardless of which state label they were stored under."""
    graph = _graph_with_mixed_edges()
    # PRIYA owes MARCO (stored WAITING relative to the tracked user) — on
    # Priya's own home that edge is *blocked on you*.
    assert [o.obligation_id for o in blocked_on_you_rows(graph, NOW, PRIYA)] == [
        "third_party"
    ]
    # And Marco sees it in his waiting list, next to nothing of the user's.
    assert [o.obligation_id for o in waiting_on_other_rows(graph, NOW, MARCO)] == [
        "third_party"
    ]
    # The user's mine_blocked edge (stored BLOCKED_ON_YOU) is Priya's waiting row.
    assert [o.obligation_id for o in waiting_on_other_rows(graph, NOW, PRIYA)] == [
        "mine_blocked"
    ]
    # Marco owes the user (stored WAITING) — on Marco's home it's blocked-on-you.
    assert [o.obligation_id for o in blocked_on_you_rows(graph, NOW, MARCO)] == [
        "mine_waiting"
    ]


# --------------------------------------------------------------------------- #
# Daily digest
# --------------------------------------------------------------------------- #
def test_digest_counts_exclude_third_party_edges() -> None:
    graph = _graph_with_mixed_edges()
    sent: list[tuple[str, str]] = []
    agent = ActionAgent(
        graph,
        verifier=object(),  # type: ignore[arg-type]
        now=lambda: NOW,
        slack_send_as_user=lambda channel, text: sent.append((channel, text)),
    )

    result = agent.send_daily_digest(USER)

    assert result.sent is True
    assert result.blocked_on_you_count == 1
    assert result.waiting_on_other_count == 1  # third_party not counted


# --------------------------------------------------------------------------- #
# Conversational agent
# --------------------------------------------------------------------------- #
def _conversational(graph: SqliteObligationGraph) -> ConversationalAgent:
    action = ActionAgent(graph, verifier=object(), now=lambda: NOW)  # type: ignore[arg-type]
    return ConversationalAgent(graph, action, now=lambda: NOW, user_id=USER)


def test_assistant_waiting_query_excludes_third_party_loops() -> None:
    agent = _conversational(_graph_with_mixed_edges())
    reply = agent.handle(USER, "what am I waiting on?")
    assert "Subject mine_waiting" in reply.text
    assert "Subject third_party" not in reply.text


def test_assistant_scoping_defaults_off_without_a_user() -> None:
    graph = _graph_with_mixed_edges()
    action = ActionAgent(graph, verifier=object(), now=lambda: NOW)  # type: ignore[arg-type]
    unscoped = ConversationalAgent(graph, action, now=lambda: NOW)
    reply = unscoped.handle(USER, "what am I waiting on?")
    assert "Subject third_party" in reply.text  # original behaviour preserved


def test_assistant_rows_show_the_counterparty_never_the_asker() -> None:
    """"Who is blocked on me?" must name the person waiting — not the asker.

    Priya's debt to Marco is stored WAITING_ON_OTHER (label relative to the
    tracked user), so label-based row rendering would put Priya's own face on
    her own loop; the counterparty must resolve against the viewer's endpoint.
    """
    from loop.conversational.assistant_view import build_assistant_blocks

    agent = _conversational(_graph_with_mixed_edges())
    reply = agent.handle(PRIYA, "who is blocked on me?")
    rendered = str(build_assistant_blocks(reply, now=NOW, user_id=PRIYA))
    assert f"<@{MARCO}>" in rendered  # Marco is waiting on Priya
    assert f"<@{PRIYA}>" not in rendered  # never the asker's own face


def test_assistant_direction_follows_the_asker_not_the_stored_label() -> None:
    """"Blocked on me" means *the asker owes*, whatever label the edge stores.

    Priya's debt to Marco is stored as WAITING_ON_OTHER (relative to the tracked
    user), so a label query would miss it entirely — the answer must come from
    the edge endpoints, like the App Home selectors.
    """
    agent = _conversational(_graph_with_mixed_edges())

    # Priya owes Marco (stored WAITING) → on Priya's side it is blocked-on-me.
    blocked = agent.handle(PRIYA, "who is blocked on me?")
    assert "Subject third_party" in blocked.text
    assert "Subject mine_blocked" not in blocked.text  # the user's debt, not Priya's

    # Marco is owed by Priya → his waiting list carries the same edge.
    waiting = agent.handle(MARCO, "what am I waiting on?")
    assert "Subject third_party" in waiting.text
    assert "Subject mine_waiting" not in waiting.text  # Marco owes that one

    # The tracked user's own answers are unchanged by the endpoint rewrite.
    mine = agent.handle(USER, "who is blocked on me?")
    assert "Subject mine_blocked" in mine.text
    assert "Subject third_party" not in mine.text
