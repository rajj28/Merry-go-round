"""Seeded PR-merge auto-close beat — demo-control API (task 16.3, Req 15.3, Req 8).

This is the *controllable* half of demo Beat 3 (design.md → "Seeded Deterministic
Demo Workspace"). The committed fixture pack (:mod:`loop.seed.fixtures`) pins an
``blocked-on-you`` obligation (``OBL_B2``) referencing a seeded mergeable GitHub PR
(``SEED_PR_REF``). During a live demo the presenter needs to fire the auto-close
beat *on cue*: flip that PR from open → merged, then let the **real** Verifier /
Action Agent resolve it so the loop lands ``healed`` + ``autonomous`` in the
Auto-Healed feed within 2 seconds (Req 15.3).

Nothing here re-implements Loop's logic. It wires the genuine production
components against the seeded graph and the only mocked seam — the GitHub MCP
PR-status port (:class:`SeededPullRequestStatusClient`):

    SeededPullRequestStatusClient   (controllable PR status; the Verifier's port)
        -> Verifier                 (real three-valued grounding, Req 4/8)
        -> ActionAgent.auto_close    (real RESOLVED-gated autonomous closure, Req 8)
        -> SeededWorkspace.graph     (the real shared Obligation Graph)
        -> auto_healed_rows          (the real Auto-Healed feed selector, Req 9)

The control surface is deliberately tiny:

  * :meth:`SeededPrMergeBeat.is_pr_open` — confirm the seeded PR reads as open
    (the obligation is still surfaced / not yet healed) before the beat.
  * :meth:`SeededPrMergeBeat.merge_seeded_pr` — flip the seeded PR to merged.
  * :meth:`SeededPrMergeBeat.run_auto_close` — invoke the real
    :meth:`ActionAgent.auto_close` on ``OBL_B2`` and return its outcome.
  * :meth:`SeededPrMergeBeat.auto_healed_feed` — read the real feed afterwards.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from loop.action.action_agent import ActionAgent, AutoCloseResult
from loop.action.app_home import auto_healed_rows
from loop.graph.models import Obligation, utc_now_iso
from loop.seed.fixtures import (
    SEED_PR_OBLIGATION_ID,
    SEED_PR_REF,
    SEED_USER_ID,
)
from loop.seed.loader import SeededWorkspace
from loop.verifier.verifier import PrRef, Verifier


# Raw GitHub MCP PR payloads, in exactly the shape
# ``PullRequestStatus.from_mcp_response`` reads (verifier/mcp_contract.py). The
# seeded client returns the *open* body until the PR is merged, then the *merged*
# body — so the real Verifier maps open → UNRESOLVED and merged → RESOLVED with no
# special-casing (Req 4.3, 4.4).
def _open_payload(pull_number: int) -> dict[str, Any]:
    return {
        "number": pull_number,
        "state": "open",
        "merged": False,
        "merged_at": None,
    }


def _merged_payload(pull_number: int) -> dict[str, Any]:
    return {
        "number": pull_number,
        "state": "closed",
        "merged": True,
        "merged_at": "2025-01-06T12:30:00+00:00",
    }


class SeededPullRequestStatusClient:
    """A controllable GitHub MCP PR-status client (the Verifier's injected port).

    Implements the :class:`loop.verifier.verifier.PullRequestStatusClient` protocol.
    Every referenced PR reads as *open* until it is explicitly merged via
    :meth:`merge`, after which it reads as *merged*. This lets a presenter drive the
    seeded PR from open → merged on cue without touching a live GitHub server, while
    the real Verifier does the actual three-valued grounding.
    """

    def __init__(self) -> None:
        # Set of merged PRs, keyed by (owner, repo, number) so distinct seeded PRs
        # can be merged independently.
        self._merged: set[tuple[str, str, int]] = set()

    def merge(self, ref: str) -> None:
        """Mark the PR identified by ``owner/repo#number`` as merged."""
        pr = PrRef.parse(ref)
        self._merged.add((pr.owner, pr.repo, pr.number))

    def is_merged(self, ref: str) -> bool:
        """Whether the PR identified by ``owner/repo#number`` is currently merged."""
        pr = PrRef.parse(ref)
        return (pr.owner, pr.repo, pr.number) in self._merged

    def __call__(
        self, owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        if (owner, repo, pull_number) in self._merged:
            return _merged_payload(pull_number)
        return _open_payload(pull_number)


class SeededPrMergeBeat:
    """Drives the seeded PR-merge auto-close beat end to end (Req 15.3).

    Wires the *real* Verifier and Action Agent over a loaded
    :class:`SeededWorkspace`'s graph, with a controllable PR-status client as the
    only mocked seam. Construct it after the workspace has loaded (the default
    ``SeededWorkspace()`` autoloads), then drive the beat:

        beat = SeededPrMergeBeat()
        assert beat.is_pr_open()            # OBL_B2 surfaced, PR open
        beat.merge_seeded_pr()              # flip the seeded PR → merged
        result = beat.run_auto_close()      # real Verifier + auto_close
        assert result.closed                # healed + autonomous
        assert beat.in_auto_healed_feed()   # lands in the Auto-Healed feed

    Args:
        workspace: a loaded seeded workspace; a fresh autoloaded one by default.
        now: injectable ISO-8601 UTC clock for the recorded closure timestamp
            (defaults to :func:`loop.graph.models.utc_now_iso`); injected purely to
            make the closure timestamp deterministic in tests.
        pr_client: an injectable controllable PR-status client; a fresh one by
            default. Exposed so a caller can pre-merge other seeded PRs if needed.
    """

    def __init__(
        self,
        workspace: Optional[SeededWorkspace] = None,
        *,
        now: Callable[[], str] = utc_now_iso,
        pr_client: Optional[SeededPullRequestStatusClient] = None,
    ) -> None:
        self.workspace = workspace or SeededWorkspace()
        self.pr_client = pr_client or SeededPullRequestStatusClient()
        self.verifier = Verifier(self.pr_client)
        # The real Action Agent over the real seeded graph. No Slack/the LLM ports are
        # needed: auto-close is fully autonomous (no send-as-user), so the lazy
        # defaults are never reached on this path.
        self.action = ActionAgent(self.workspace.graph, self.verifier, now=now)

    # ------------------------------------------------------------------
    # The seeded PR-referencing obligation (OBL_B2)
    # ------------------------------------------------------------------
    @property
    def graph(self):  # noqa: ANN201 — returns the workspace's ObligationGraph
        """The shared seeded Obligation Graph the beat reads and writes."""
        return self.workspace.graph

    def obligation(self) -> Obligation:
        """Return the current stored state of the seeded PR obligation (``OBL_B2``)."""
        obligation = self.graph.get(SEED_PR_OBLIGATION_ID)
        if obligation is None:
            raise LookupError(
                f"seeded PR obligation {SEED_PR_OBLIGATION_ID!r} is not loaded; "
                "is the SeededWorkspace initialized?"
            )
        return obligation

    def is_pr_open(self) -> bool:
        """Whether the seeded PR currently reads as open (not yet merged)."""
        return not self.pr_client.is_merged(SEED_PR_REF)

    # ------------------------------------------------------------------
    # Driving the beat
    # ------------------------------------------------------------------
    def merge_seeded_pr(self) -> None:
        """Flip the seeded PR (``SEED_PR_REF`` on ``OBL_B2``) to merged (Req 15.3).

        After this, the real Verifier will report RESOLVED for ``OBL_B2`` and
        :meth:`run_auto_close` will autonomously heal the loop.
        """
        self.pr_client.merge(SEED_PR_REF)

    def run_auto_close(self) -> AutoCloseResult:
        """Run the real RESOLVED-gated auto-close on the seeded obligation (Req 8).

        Invokes :meth:`ActionAgent.auto_close` on the current ``OBL_B2`` state. While
        the seeded PR is open this reports UNRESOLVED and changes nothing; once
        :meth:`merge_seeded_pr` has been called it reports RESOLVED and the loop is
        set ``healed`` + ``autonomous`` with closure metadata, persisted to the graph
        (Req 8.2, 8.5). Returns the :class:`AutoCloseResult` the Action Agent produced.
        """
        return self.action.auto_close(self.obligation())

    # ------------------------------------------------------------------
    # Observing the result (the real Auto-Healed feed)
    # ------------------------------------------------------------------
    def auto_healed_feed(self, now: Optional[str] = None) -> list[Obligation]:
        """Return the real Auto-Healed feed for the seeded workspace (Req 9)."""
        return auto_healed_rows(self.graph, now, SEED_USER_ID)

    def in_auto_healed_feed(self, now: Optional[str] = None) -> bool:
        """Whether the seeded PR obligation now appears in the Auto-Healed feed."""
        return any(
            o.obligation_id == SEED_PR_OBLIGATION_ID
            for o in self.auto_healed_feed(now)
        )


__all__ = [
    "SeededPullRequestStatusClient",
    "SeededPrMergeBeat",
]
