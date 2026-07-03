"""Integration / latency smoke tests for the wired Loop app (task 17.2).

These are deliberately **example-based** (not property-based): they pin the three
user-visible latency / cadence guarantees the wiring must honor end to end, on a
representative seeded graph, with every external boundary mocked:

  * **App Home renders within 2s (Req 6.1).** Building *and* publishing the App
    Home view from a representative multi-obligation graph completes well under the
    2-second budget, and a real ``home`` view (hero banner present) is published.
  * **The Assistant responds within 5s (Req 12.1).** A natural-language query
    routed through the Conversational Agent returns its matching set within the
    5-second budget.
  * **The Watcher sweep runs at the configured interval (Req 2.1).** APScheduler
    registers the ``watcher_sweep`` job at exactly the configured interval, and that
    interval is ≤ 60s.

External boundaries (Slack, APScheduler execution, Anthropic, GitHub MCP) are
mocked: the app is built through the established :func:`_build_app` fakes from
:mod:`loop.tests.test_app_wiring` (fake Bolt app, fake Slack client, injected agent
ports), so these tests measure Loop's own wiring/latency, never a network call.

The budgets are generous relative to the in-memory work involved (the operations
are milliseconds), so the assertions are robust rather than flaky — they fail only
if the wiring regresses into doing something pathologically slow (e.g. a real
network call sneaking onto the hot path).
"""

from __future__ import annotations

import time

import pytest

from loop.config import get_settings
from loop.graph.models import LoopState
from loop.tests.test_app_wiring import (
    FakeClient,
    OTHER,
    USER,
    _build_app,
    _obligation,
)
from loop.watcher.watcher import MAX_SWEEP_INTERVAL_SECONDS

# Latency budgets straight from the acceptance criteria.
APP_HOME_BUDGET_SECONDS = 2.0     # Req 6.1
ASSISTANT_BUDGET_SECONDS = 5.0    # Req 12.1


def _seed_representative_graph(app, *, blocked=5, waiting=4, healed=3) -> None:
    """Seed the app's graph with a representative spread of obligations.

    A realistic App Home has several surfaced loops in each active section plus an
    Auto-Healed feed, so timing the render against a single row would be unrealistic.
    Every obligation is given a high confidence (≥ the default threshold) so the
    active ones surface, and distinct ids/timestamps so ordering is exercised.
    """
    for i in range(blocked):
        app.graph.upsert(
            _obligation(
                oid=f"b{i}",
                state=LoopState.BLOCKED_ON_YOU,
                last_touch_timestamp=f"2024-01-0{1 + (i % 9)}T00:00:00+00:00",
                subject_summary=f"please review PR #{i}",
            )
        )
    for i in range(waiting):
        app.graph.upsert(
            _obligation(
                oid=f"w{i}",
                state=LoopState.WAITING_ON_OTHER,
                owes_person_id=OTHER,
                owed_person_id=USER,
                last_touch_timestamp=f"2024-01-0{1 + (i % 9)}T00:00:00+00:00",
                subject_summary=f"waiting on design doc {i}",
            )
        )
    from loop.graph.models import ClosureKind

    for i in range(healed):
        app.graph.upsert(
            _obligation(
                oid=f"h{i}",
                state=LoopState.HEALED,
                closure_kind=ClosureKind.AUTONOMOUS,
                closure_reason="merged",
                closure_timestamp=f"2024-02-0{1 + (i % 9)}T00:00:00+00:00",
                last_touch_timestamp=f"2024-02-0{1 + (i % 9)}T00:00:00+00:00",
                subject_summary=f"auto-healed loop {i}",
            )
        )


# --------------------------------------------------------------------------- #
# (a) App Home renders within 2s (Req 6.1)
# --------------------------------------------------------------------------- #
def test_app_home_publishes_within_2s_on_representative_graph():
    app = _build_app()
    _seed_representative_graph(app)
    client = FakeClient()

    start = time.perf_counter()
    app.open_home(USER, client)
    elapsed = time.perf_counter() - start

    assert elapsed < APP_HOME_BUDGET_SECONDS, (
        f"App Home took {elapsed:.3f}s, exceeding the {APP_HOME_BUDGET_SECONDS}s "
        "budget (Req 6.1)"
    )
    # A real home view was published (hero banner header present), not a placeholder.
    assert len(client.published) == 1
    view = client.published[0]["view"]
    assert view["type"] == "home"
    assert any(b.get("type") == "header" for b in view["blocks"])


# --------------------------------------------------------------------------- #
# (b) The Assistant responds within 5s (Req 12.1)
# --------------------------------------------------------------------------- #
def test_assistant_query_responds_within_5s():
    app = _build_app()
    _seed_representative_graph(app)

    start = time.perf_counter()
    result = app.handle_assistant_message(USER, "who is blocked on me?")
    elapsed = time.perf_counter() - start

    assert elapsed < ASSISTANT_BUDGET_SECONDS, (
        f"Assistant query took {elapsed:.3f}s, exceeding the "
        f"{ASSISTANT_BUDGET_SECONDS}s budget (Req 12.1)"
    )
    # The query returned a real answer referencing surfaced loops + the trace.
    assert result.text
    assert "please review PR" in result.text
    assert "Tools:" in result.text


def test_assistant_no_match_query_also_responds_within_5s():
    # An empty graph still answers (with a no-match message) inside the budget.
    app = _build_app()

    start = time.perf_counter()
    result = app.handle_assistant_message(USER, "who is blocked on me?")
    elapsed = time.perf_counter() - start

    assert elapsed < ASSISTANT_BUDGET_SECONDS
    assert result.text  # explicit no-match reply (Req 12.2), still within budget


# --------------------------------------------------------------------------- #
# (c) The Watcher sweep is registered at the configured interval, ≤ 60s (Req 2.1)
# --------------------------------------------------------------------------- #
def test_watcher_sweep_registered_at_configured_interval():
    pytest.importorskip("apscheduler")
    app = _build_app()
    scheduler = app.build_scheduler(client=FakeClient())

    sweep = scheduler.get_job("watcher_sweep")
    assert sweep is not None, "watcher_sweep job must be registered (Req 2.1)"

    interval = sweep.trigger.interval.total_seconds()
    # Registered at exactly the configured cadence...
    assert interval == get_settings().sweep_interval_seconds
    # ...and that cadence must not exceed 60s (Req 2.1).
    assert interval <= MAX_SWEEP_INTERVAL_SECONDS
