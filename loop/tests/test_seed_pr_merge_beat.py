"""Tests for the seeded PR-merge auto-close beat (task 16.3 — Req 15.3, Req 8).

These exercise the *real* demo-control API (:mod:`loop.seed.demo_beat`) over the
*real* seeded workspace (:class:`loop.seed.loader.SeededWorkspace`), the *real*
Verifier, the *real* :meth:`ActionAgent.auto_close`, and the *real* Auto-Healed
feed selector. The only mocked seam is the controllable GitHub MCP PR-status port
(:class:`SeededPullRequestStatusClient`) — exactly the boundary the production
composition root injects.

Requirement 15.3:

    WHEN the seeded GitHub pull request transitions to merged, THE Loop SHALL,
    within 2 seconds, set the associated Obligation Loop_State to `healed`, record
    the closure as autonomous, and add the Obligation to the Auto_Healed_Feed.

Validates: Requirements 15.3, 8
"""

from __future__ import annotations

import time

import pytest

from loop.action.action_agent import AUTO_CLOSE_REASON
from loop.graph.models import ClosureKind, LoopState
from loop.seed.demo_beat import SeededPrMergeBeat, SeededPullRequestStatusClient
from loop.seed.fixtures import SEED_PR_OBLIGATION_ID, SEED_PR_REF
from loop.seed.loader import SeededWorkspace
from loop.verifier.types import VerificationResult


@pytest.fixture()
def beat() -> SeededPrMergeBeat:
    """A beat over a freshly loaded seeded workspace with a fixed closure clock."""
    return SeededPrMergeBeat(now=lambda: "2025-01-06T12:35:00+00:00")


# --------------------------------------------------------------------------- #
# Pre-merge state: OBL_B2 is surfaced (open) and not auto-closed.
# --------------------------------------------------------------------------- #
def test_seeded_pr_obligation_is_initially_surfaced_and_open(
    beat: SeededPrMergeBeat,
) -> None:
    """Before the merge, OBL_B2 is a surfaced `blocked-on-you` loop with an open PR."""
    obligation = beat.obligation()
    assert obligation.obligation_id == SEED_PR_OBLIGATION_ID
    assert obligation.loop_state is LoopState.BLOCKED_ON_YOU
    assert obligation.artifact_ref == SEED_PR_REF
    assert beat.is_pr_open() is True

    # It is part of the surfaced active set (the hero count includes it).
    surfaced_ids = {o.obligation_id for o in beat.workspace.surfaced_obligations()}
    assert SEED_PR_OBLIGATION_ID in surfaced_ids


def test_auto_close_before_merge_leaves_loop_unchanged(beat: SeededPrMergeBeat) -> None:
    """While the seeded PR is open the Verifier reports UNRESOLVED → no closure (Req 8.3)."""
    result = beat.run_auto_close()
    assert result.closed is False
    assert result.verification is VerificationResult.UNRESOLVED

    stored = beat.obligation()
    assert stored.loop_state is LoopState.BLOCKED_ON_YOU
    assert stored.closure_kind is None
    assert beat.in_auto_healed_feed() is False


# --------------------------------------------------------------------------- #
# The beat: merge → auto-close within 2s → healed + autonomous + feed entry.
# --------------------------------------------------------------------------- #
def test_seeded_merge_auto_closes_to_healed_autonomous_in_feed(
    beat: SeededPrMergeBeat,
) -> None:
    """The Req 15.3 beat: a seeded merge heals OBL_B2 autonomously into the feed."""
    # Sanity: surfaced & open before the merge.
    assert beat.obligation().loop_state is LoopState.BLOCKED_ON_YOU
    assert beat.is_pr_open() is True

    # Drive the seeded PR open → merged on cue.
    beat.merge_seeded_pr()
    assert beat.is_pr_open() is False

    # Auto-close must complete within 2 seconds (Req 15.3).
    started = time.perf_counter()
    result = beat.run_auto_close()
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"auto-close took {elapsed:.3f}s (>2s)"

    # RESOLVED → the loop was autonomously closed.
    assert result.closed is True
    assert result.verification is VerificationResult.RESOLVED

    # Healed + autonomous + closure metadata, observable from the graph (Req 8.2, 8.5).
    stored = beat.obligation()
    assert stored.loop_state is LoopState.HEALED
    assert stored.closure_kind is ClosureKind.AUTONOMOUS
    assert stored.closure_reason == AUTO_CLOSE_REASON
    assert stored.closure_timestamp == "2025-01-06T12:35:00+00:00"
    assert stored.artifact_ref == SEED_PR_REF  # source artifact retained

    # And it now appears in the real Auto-Healed feed (Req 9.1 / 15.3).
    assert beat.in_auto_healed_feed() is True
    feed_ids = [o.obligation_id for o in beat.auto_healed_feed()]
    assert SEED_PR_OBLIGATION_ID in feed_ids

    # Once healed it is no longer in the surfaced active set (hero count dropped).
    surfaced_ids = {o.obligation_id for o in beat.workspace.surfaced_obligations()}
    assert SEED_PR_OBLIGATION_ID not in surfaced_ids


def test_beat_uses_an_independent_workspace_each_construction() -> None:
    """Two beats over fresh workspaces don't share state (reproducible demo runs)."""
    first = SeededPrMergeBeat()
    first.merge_seeded_pr()
    assert first.run_auto_close().closed is True

    second = SeededPrMergeBeat()
    # The second beat starts from a clean seed: its PR is still open.
    assert second.is_pr_open() is True
    assert second.obligation().loop_state is LoopState.BLOCKED_ON_YOU


def test_injected_pr_client_can_be_observed() -> None:
    """The controllable PR-status client is injectable and reports merge state."""
    client = SeededPullRequestStatusClient()
    workspace = SeededWorkspace()
    beat = SeededPrMergeBeat(workspace, pr_client=client)

    assert client.is_merged(SEED_PR_REF) is False
    beat.merge_seeded_pr()
    assert client.is_merged(SEED_PR_REF) is True

    # The client returns a merged payload for the seeded PR after merging.
    from loop.verifier.verifier import PrRef

    pr = PrRef.parse(SEED_PR_REF)
    payload = client(pr.owner, pr.repo, pr.number, timeout=1.0)
    assert payload["merged"] is True
    assert payload["state"] == "closed"
