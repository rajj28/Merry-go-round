"""GitHub MCP Pull-Request-status contract for the Verifier (task 1.3 spike output).

This module is the **contract + mapping shape** the Verifier (task 8.1) builds
against. It deliberately does NOT implement the Verifier's retry/timeout/auto-close
policy (that is task 8.1). It only:

  1. Documents the assumed GitHub MCP PR-status response schema (see ASSUMED SCHEMA).
  2. Provides a typed `PullRequestStatus` capturing exactly the fields the Verifier
     needs to decide merged / open / closed-without-merge.
  3. Maps a `PullRequestStatus` to the three-valued verification *intent*
     (merged -> resolved, any other reachable state -> unresolved).

Requirements grounding:
  - Req 4.3  : merged PR  -> resolved
  - Req 4.4  : any other reachable state (open, closed-without-merge) -> unresolved
  - Req 8.7  : closed-without-merge is NOT a merge (stays unresolved, never autonomous-close)
  - Req 4.1 / 8.1 : the field the Verifier reads is the GitHub MCP Pull_Request_Status

NOTE on UNVERIFIED: the third value of the Verifier's result enum
(`VerificationResult.UNVERIFIED`, Req 4.5/8.6) represents *transport* failure
(unreachable / timeout / error after <=3 attempts). That is decided by the
Verifier's call logic (task 8.1), not by the response body, so it is intentionally
NOT produced by this contract's mapping. This module maps a *successfully retrieved*
PR status to RESOLVED or UNRESOLVED only.

================================================================================
ASSUMED GitHub MCP PR-status response schema
================================================================================
Assumed MCP tool: ``get_pull_request`` on the GitHub MCP server
(github/github-mcp-server). Arguments: ``{owner, repo, pullNumber}``.

The tool returns the GitHub REST "Pull Request" object (a JSON dict). The Verifier
only cares about this subset (other fields are ignored):

    {
      "number": 42,                       # int   - PR number
      "state": "closed",                  # str   - "open" | "closed" (GitHub never
                                          #         reports "merged" in `state`;
                                          #         merge is signalled by `merged`)
      "merged": true,                     # bool  - true iff the PR was merged
      "merged_at": "2025-01-08T12:30:00Z",# str|null - ISO-8601 UTC, null if not merged
      "title": "Fix the thing",           # str   - (informational)
      "html_url": "https://github.com/o/r/pull/42",
      "merge_commit_sha": "abc123",       # str|null
      # review state may arrive either as a top-level decision or be derived
      # from a separate reviews call depending on server version:
      "review_decision": "APPROVED"       # str|null - "APPROVED" | "CHANGES_REQUESTED"
                                          #            | "REVIEW_REQUIRED" | null
    }

The three demo-relevant outcomes map as:
  - MERGED                : state == "closed" AND merged == true   -> resolved
  - OPEN                  : state == "open"   AND merged == false  -> unresolved
  - CLOSED-WITHOUT-MERGE  : state == "closed" AND merged == false  -> unresolved (Req 8.7)
================================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional


class VerificationIntent(str, Enum):
    """Three-valued verification result *intent* shared with the Verifier (Req 4.3-4.5).

    This mirrors the design's ``VerificationResult`` enum (task 2.4). It lives here
    only as the mapping target for the contract; the Verifier owns the authoritative
    enum and the UNVERIFIED transport-failure path.
    """

    RESOLVED = "resolved"      # PR is merged (Req 4.3)
    UNRESOLVED = "unresolved"  # PR reachable but not merged: open or closed-without-merge (Req 4.4, 8.7)
    UNVERIFIED = "unverified"  # transport failure; produced by the Verifier, not this mapping (Req 4.5, 8.6)


@dataclass(frozen=True)
class PullRequestStatus:
    """The subset of GitHub MCP PR fields the Verifier needs.

    Fields:
        state:           GitHub PR state, "open" or "closed".
        merged:          True iff the PR was merged. This is the authoritative
                         merge signal (GitHub's `state` is only open/closed).
        merged_at:       ISO-8601 UTC merge time, or None when not merged.
        review_decision: Optional review decision; not used for the merge gate
                         but carried for nudge context (Req 7) and future use.
        number:          PR number (informational / for logging).
    """

    state: str
    merged: bool
    merged_at: Optional[str] = None
    review_decision: Optional[str] = None
    number: Optional[int] = None

    @property
    def is_merged(self) -> bool:
        """True iff this PR counts as merged (Req 4.3). `merged` is authoritative."""
        return bool(self.merged)

    @property
    def is_closed_without_merge(self) -> bool:
        """True iff the PR was closed but not merged (Req 8.7)."""
        return self.state == "closed" and not self.merged

    @classmethod
    def from_mcp_response(cls, payload: Mapping[str, Any]) -> "PullRequestStatus":
        """Build a PullRequestStatus from a raw GitHub MCP ``get_pull_request`` result.

        Tolerant of missing optional keys so it can parse real responses and the
        recorded sample shapes in this spike. Assumes the documented schema above.
        """
        return cls(
            state=str(payload.get("state", "")),
            merged=bool(payload.get("merged", False)),
            merged_at=payload.get("merged_at"),
            review_decision=payload.get("review_decision"),
            number=payload.get("number"),
        )


def to_verification_intent(status: PullRequestStatus) -> VerificationIntent:
    """Map a retrieved PR status to the Verifier's three-valued intent.

    merged           -> RESOLVED   (Req 4.3)
    any other state  -> UNRESOLVED (Req 4.4; closed-without-merge per Req 8.7)

    UNVERIFIED is never returned here: it is a transport-failure outcome owned by
    the Verifier's call logic (Req 4.5 / 8.6), not derivable from a response body.
    """
    if status.is_merged:
        return VerificationIntent.RESOLVED
    return VerificationIntent.UNRESOLVED


# Recorded sample response shapes for the three demo-relevant outcomes.
# These document the exact bodies the spike exercises (merged / open / closed)
# so the Verifier (task 8.1) can build and test against them without a live server.
SAMPLE_MERGED: dict[str, Any] = {
    "number": 42,
    "state": "closed",
    "merged": True,
    "merged_at": "2025-01-08T12:30:00Z",
    "title": "Add obligation graph store",
    "html_url": "https://github.com/acme/throwaway/pull/42",
    "merge_commit_sha": "abc123def456",
    "review_decision": "APPROVED",
}

SAMPLE_OPEN: dict[str, Any] = {
    "number": 43,
    "state": "open",
    "merged": False,
    "merged_at": None,
    "title": "Wire up the watcher sweep",
    "html_url": "https://github.com/acme/throwaway/pull/43",
    "merge_commit_sha": None,
    "review_decision": "REVIEW_REQUIRED",
}

SAMPLE_CLOSED_WITHOUT_MERGE: dict[str, Any] = {
    "number": 44,
    "state": "closed",
    "merged": False,
    "merged_at": None,
    "title": "Abandoned experiment",
    "html_url": "https://github.com/acme/throwaway/pull/44",
    "merge_commit_sha": None,
    "review_decision": "CHANGES_REQUESTED",
}
