"""Property-based test for the App Home footer totals (task 9.7, Property 22).

This exercises the *real* App Home view builder
(:func:`loop.action.app_home.build_app_home_view` / :func:`footer_text`) against
the *real* in-memory SQLite Obligation Graph store — no mocking — over randomized
graph states, mirroring the store-isolation + generator pattern in
``test_app_home_properties.py``.

Property 22 (design.md → "Correctness Properties"): the footer context line states
per-section totals (Req 6.6), and each of those totals must be *consistent with the
sections actually rendered in the view*. Concretely, for any graph state, each
footer count equals the number of non-dismissed at/above-threshold obligations
rendered in its corresponding section:

  * "Blocked on you: *N*"      == rows rendered under the Blocked on You header,
  * "Waiting on others: *M*"   == rows rendered under the Waiting on Others header,
  * "Auto-healed: *K*"         == rows rendered under the Auto-Healed Loops header.

The footer count is parsed back out of the rendered footer block and the per-section
row counts are derived by walking the rendered ``blocks`` list — so the test pins the
footer numbers to what the view *actually shows*, not to the builder's internal
intermediates.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from loop.action.app_home import (
    BLOCKED_EMPTY_TEXT,
    BLOCKED_SECTION_TITLE,
    HEALED_EMPTY_TEXT,
    HEALED_SECTION_TITLE,
    WAITING_EMPTY_TEXT,
    WAITING_SECTION_TITLE,
    build_app_home_view,
)
from loop.graph.models import ArtifactType, ClosureKind, LoopState, Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph

# Fixed reference "now" so every snooze/age decision is deterministic across runs.
NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# ---------------------------------------------------------------------------
# Hypothesis strategies — randomized but in-contract obligations + threshold.
# (Mirrors test_app_home_properties.py so the two property tests share a generator
# shape against the same real store.)
# ---------------------------------------------------------------------------
_IDS = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
    min_size=1,
    max_size=10,
)

_SAFE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=30,
)

_CONFIDENCE = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_THRESHOLD = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


_TIMESTAMPS = st.datetimes(
    min_value=(NOW - timedelta(days=10)).replace(tzinfo=None),
    max_value=(NOW + timedelta(days=10)).replace(tzinfo=None),
    timezones=st.just(timezone.utc),
).map(_iso)


@st.composite
def obligations(draw: st.DrawFn) -> Obligation:
    """A randomized, in-contract :class:`Obligation`.

    Loop_State ranges over all three values; confidence spans the full inclusive
    range; ``dismissed`` and ``snoozed_until`` (None / past / future) vary so the
    full surfacing gate is exercised across every section. Healed obligations carry
    closure metadata so they are realistic Auto-Healed feed members.
    """
    state = draw(st.sampled_from(list(LoopState)))
    has_artifact = draw(st.booleans())
    closure = draw(st.one_of(st.none(), st.sampled_from(list(ClosureKind))))
    return Obligation(
        obligation_id=draw(_IDS),
        owes_person_id=draw(_IDS),
        owed_person_id=draw(_IDS),
        owner_person_id=draw(_IDS),
        loop_state=state,
        confidence_score=draw(_CONFIDENCE),
        last_touch_timestamp=draw(_TIMESTAMPS),
        source_msg_channel=draw(_IDS),
        source_msg_ts=draw(_SAFE_TEXT),
        subject_summary=draw(_SAFE_TEXT),
        dismissed=draw(st.booleans()),
        snoozed_until=draw(st.one_of(st.none(), _TIMESTAMPS)),
        artifact_type=ArtifactType.GITHUB_PR if has_artifact else None,
        artifact_ref=draw(_SAFE_TEXT) if has_artifact else None,
        closure_kind=closure,
        closure_timestamp=draw(st.one_of(st.none(), _TIMESTAMPS)),
        closure_reason=draw(st.one_of(st.none(), _SAFE_TEXT)),
    )


_GRAPH_STATES = st.lists(obligations(), max_size=25, unique_by=lambda o: o.obligation_id)


def _graph(threshold: float, obs: list[Obligation]) -> SqliteObligationGraph:
    """A fresh in-memory store seeded with ``obs`` at the given surfacing threshold."""
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    graph.set_threshold(threshold)
    for o in obs:
        graph.upsert(o)
    return graph


# ---------------------------------------------------------------------------
# View-walking helpers — derive what's *actually rendered* per section.
# ---------------------------------------------------------------------------
_FOOTER_RE = re.compile(
    r"Blocked on you:\s*\*(\d+)\*.*?"
    r"Waiting on others:\s*\*(\d+)\*.*?"
    r"Auto-closed:\s*\*(\d+)\*",
    re.DOTALL,
)


def _parse_footer_counts(view: dict) -> tuple[int, int, int]:
    """Pull the three per-section counts out of the rendered footer context block.

    The footer is the last ``context`` block in the view; its mrkdwn text carries the
    "Blocked on you / Waiting on others / Auto-healed" totals (Req 6.6).
    """
    context_blocks = [b for b in view["blocks"] if b.get("type") == "context"]
    assert context_blocks, "expected a footer context block"
    footer = context_blocks[-1]["elements"][0]["text"]
    match = _FOOTER_RE.search(footer)
    assert match is not None, f"footer text not in expected format: {footer!r}"
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _section_bounds(blocks: list[dict]) -> dict[str, tuple[int, int]]:
    """Map each section title to the ``[start, end)`` block-index span after its header.

    A section spans from just after its header block up to (but not including) the
    next header block, or the end of the block list for the last section.
    """
    titles = {BLOCKED_SECTION_TITLE, WAITING_SECTION_TITLE, HEALED_SECTION_TITLE}
    header_indices: list[tuple[int, str]] = []
    for i, b in enumerate(blocks):
        if b.get("type") == "header":
            text = b["text"]["text"]
            if text in titles:
                header_indices.append((i, text))

    bounds: dict[str, tuple[int, int]] = {}
    for pos, (idx, title) in enumerate(header_indices):
        end = header_indices[pos + 1][0] if pos + 1 < len(header_indices) else len(blocks)
        bounds[title] = (idx + 1, end)
    return bounds


def _count_active_rows(blocks: list[dict], span: tuple[int, int], empty_text: str) -> int:
    """Rows rendered in an active section = number of per-row ``actions`` blocks.

    Each surfaced active-section obligation renders one ``section`` + one ``actions``
    block (with a ``row_actions::`` block_id); an empty section renders only the
    empty-state ``section`` and no ``actions`` block, so counting ``actions`` blocks
    gives the rendered row count directly. We also assert the empty-state marker only
    appears when there are zero rows, keeping the two render paths honest.
    """
    start, end = span
    segment = blocks[start:end]
    action_rows = sum(1 for b in segment if b.get("type") == "actions")
    has_empty = any(
        b.get("type") == "section" and b.get("text", {}).get("text") == empty_text
        for b in segment
    )
    assert has_empty == (action_rows == 0), (
        f"empty-state marker / row mismatch: empty={has_empty} rows={action_rows}"
    )
    return action_rows


def _count_healed_rows(blocks: list[dict], span: tuple[int, int]) -> int:
    """Rows rendered in the Auto-Healed feed = ``section`` blocks that aren't empty-state.

    The feed renders one ``section`` block per healed entry (no ``actions`` block);
    an empty feed renders only the empty-state ``section``. Counting non-empty-state
    ``section`` blocks therefore yields the rendered row count.
    """
    start, end = span
    segment = blocks[start:end]
    rows = sum(
        1
        for b in segment
        if b.get("type") == "section" and b.get("text", {}).get("text") != HEALED_EMPTY_TEXT
    )
    has_empty = any(
        b.get("type") == "section" and b.get("text", {}).get("text") == HEALED_EMPTY_TEXT
        for b in segment
    )
    assert has_empty == (rows == 0), (
        f"healed empty-state marker / row mismatch: empty={has_empty} rows={rows}"
    )
    return rows


# ---------------------------------------------------------------------------
# Property 22 (task 9.7) — Footer totals are consistent with rendered sections
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 22: Footer totals are consistent with rendered sections
@settings(max_examples=200)
@given(threshold=_THRESHOLD, obs=_GRAPH_STATES)
def test_property_22_footer_totals_consistent_with_rendered_sections(
    threshold: float, obs: list[Obligation]
) -> None:
    """Validates: Requirements 6.6.

    For any graph state, each footer count equals the number of (non-dismissed,
    at/above-threshold) obligations actually rendered in its corresponding section:
    Blocked-On-You, Waiting-On-Others, and Auto-Healed.
    """
    graph = _graph(threshold, obs)
    view = build_app_home_view(graph, NOW_ISO)
    blocks = view["blocks"]

    footer_blocked, footer_waiting, footer_healed = _parse_footer_counts(view)

    bounds = _section_bounds(blocks)
    rendered_blocked = _count_active_rows(
        blocks, bounds[BLOCKED_SECTION_TITLE], BLOCKED_EMPTY_TEXT
    )
    rendered_waiting = _count_active_rows(
        blocks, bounds[WAITING_SECTION_TITLE], WAITING_EMPTY_TEXT
    )
    rendered_healed = _count_healed_rows(blocks, bounds[HEALED_SECTION_TITLE])

    assert footer_blocked == rendered_blocked
    assert footer_waiting == rendered_waiting
    assert footer_healed == rendered_healed
