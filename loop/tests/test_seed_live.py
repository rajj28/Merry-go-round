"""Tests for live demo-seeding (loader.load_into_graph) — the interactive demo path.

``load_into_graph`` upserts the seeded Northwind org into a real graph with every
timestamp rebased to the live clock, so the running app's dashboard / nudge /
auto-heal all work on the polished demo data (drawback #1 fix). These tests assert:

  * the surfaced set matches the deterministic contract (hero == 3) when evaluated
    at the rebased ``now``;
  * timestamps are rebased to be *recent* relative to ``now`` (not the pinned
    Jan-2025 reference), so aging chips read sensibly in the live app;
  * the still-future snooze stays hidden after rebasing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loop.action.app_home import blocked_on_you_rows, hero_count
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.seed.fixtures import seed_obligations
from loop.seed.loader import load_into_graph

NOW = "2026-06-28T00:00:00+00:00"


def _parse(ts: str) -> datetime:
    text = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def test_load_into_graph_seeds_full_org_with_hero_three() -> None:
    graph = SqliteObligationGraph(IN_MEMORY)
    count = load_into_graph(graph, now=NOW)

    assert count == len(seed_obligations())
    # The surfaced contract holds against the rebased clock.
    assert hero_count(graph, NOW) == 3
    assert len(blocked_on_you_rows(graph, NOW)) == 3


def test_load_into_graph_rebases_timestamps_to_recent() -> None:
    graph = SqliteObligationGraph(IN_MEMORY)
    load_into_graph(graph, now=NOW)

    now_dt = _parse(NOW)
    for o in blocked_on_you_rows(graph, NOW):
        age = now_dt - _parse(o.last_touch_timestamp)
        # Seeded blocked loops span ~1–4 days old, not ~500 days (verbatim Jan 2025).
        assert timedelta(0) <= age <= timedelta(days=14)


def test_load_into_graph_is_idempotent() -> None:
    graph = SqliteObligationGraph(IN_MEMORY)
    load_into_graph(graph, now=NOW)
    load_into_graph(graph, now=NOW)  # re-seed (reboot) overwrites in place
    assert hero_count(graph, NOW) == 3
