"""The Verifier — three-valued PR-status grounding via the GitHub MCP (task 8.1).

This module implements the Verifier's call logic: it takes a PR-referencing
``Obligation``, asks the GitHub MCP for the real Pull_Request_Status, and reports
one of the three safety-interlock values defined in ``loop.verifier.types``:

    RESOLVED    the referenced PR is merged                         (Req 4.3)
    UNRESOLVED  the PR is reachable but not merged (open / closed)  (Req 4.4, 8.7)
    UNVERIFIED  transport failure after at most 3 attempts          (Req 4.5, 8.6)

It builds strictly on the frozen day-1 contracts and does NOT re-implement the
status mapping — that lives in ``mcp_contract`` (raw payload -> intent) and
``types.from_intent`` (intent -> authoritative result):

    raw MCP payload
        -> PullRequestStatus.from_mcp_response   (mcp_contract)
        -> to_verification_intent                (mcp_contract: merged->resolved else unresolved)
        -> from_intent                           (types: intent -> VerificationResult)

Timeout / retry policy
----------------------
The Verifier owns the timeout budget and the bounded-retry policy, which the
contract layer deliberately does not:

  * Timeout budget is selected by ``VerifyPurpose``:
      - NUDGE      -> 10s   (Req 4.2)
      - AUTO_CLOSE -> 30s   (Req 8.1, "resolve within 30 seconds")
    The budget is a wall-clock deadline enforced across *all* retry attempts using
    a monotonic clock; each attempt is handed the remaining budget as its per-call
    timeout, which the injected client passes through to the MCP call.
  * On an unreachable / timeout / error result, the Verifier retries up to
    ``max_attempts`` (default 3, Req 4.5). If every attempt fails — or the deadline
    elapses — it returns UNVERIFIED. Because auto-close is gated on RESOLVED
    (task 10), returning UNVERIFIED is exactly what forbids auto-close while the PR
    state is unknown (Req 4.6, 8.6).

Injectable client (a thin port)
--------------------------------
The GitHub MCP call is reached through an injected callable so the Verifier never
hard-depends on a live server and is trivially mockable in tests. The port shape
mirrors the spike (``loop/spikes/mcp_spike.py``): tool ``get_pull_request`` with
args ``{owner, repo, pullNumber}``. ``build_github_mcp_client`` wires the real
client from ``loop.config`` (GITHUB_MCP_URL / GITHUB_MCP_TOKEN) lazily, so merely
importing this module never touches the network or requires the ``mcp`` package.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from loop.graph.models import Obligation
from loop.verifier.mcp_contract import (
    PullRequestStatus,
    to_verification_intent,
)
from loop.verifier.types import VerificationResult, VerifyPurpose, from_intent

# Default maximum number of retrieval attempts before reporting UNVERIFIED (Req 4.5).
DEFAULT_MAX_ATTEMPTS = 3

# Per-purpose wall-clock timeout budget, in seconds.
#   NUDGE      -> 10s (Req 4.2)
#   AUTO_CLOSE -> 30s (Req 8.1)
TIMEOUT_BUDGET_SECONDS: dict[VerifyPurpose, float] = {
    VerifyPurpose.NUDGE: 10.0,
    VerifyPurpose.AUTO_CLOSE: 30.0,
}


@dataclass(frozen=True)
class PrRef:
    """A parsed ``owner/repo#number`` pull-request reference (see Obligation.artifact_ref)."""

    owner: str
    repo: str
    number: int

    @classmethod
    def parse(cls, raw: Optional[str]) -> "PrRef":
        """Parse 'owner/repo#number' into a PrRef. Raises ValueError on bad/empty input."""
        if not raw:
            raise ValueError("Obligation has no artifact_ref to verify")
        m = re.fullmatch(r"([^/\s]+)/([^#\s]+)#(\d+)", raw.strip())
        if not m:
            raise ValueError(
                f"Expected artifact_ref 'owner/repo#number' (e.g. acme/throwaway#42), got: {raw!r}"
            )
        return cls(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))


class PullRequestStatusClient(Protocol):
    """The thin port the Verifier calls to retrieve a raw PR-status payload.

    Implementations call the GitHub MCP tool ``get_pull_request`` with
    ``{owner, repo, pullNumber}`` and return the raw GitHub PR object as a mapping
    (the shape ``PullRequestStatus.from_mcp_response`` understands). They MUST raise
    on any transport failure (unreachable / timeout / error) so the Verifier can
    apply its bounded-retry policy. ``timeout`` is the remaining per-call budget in
    seconds and SHOULD bound the underlying call.
    """

    def __call__(
        self, owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        ...


class Verifier:
    """Grounds PR-referencing obligations in real GitHub state (Req 4, 8).

    Construct with an injected ``PullRequestStatusClient`` (a thin port over the
    GitHub MCP) so the network call is mockable. ``max_attempts`` bounds retries
    (Req 4.5). ``monotonic`` is injectable purely to make timeout/deadline behaviour
    deterministic in tests; it defaults to ``time.monotonic``.
    """

    def __init__(
        self,
        client: PullRequestStatusClient,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._client = client
        self._max_attempts = max_attempts
        self._monotonic = monotonic

    @staticmethod
    def timeout_budget(purpose: VerifyPurpose) -> float:
        """Return the wall-clock timeout budget (seconds) for a purpose (Req 4.2, 8.1)."""
        return TIMEOUT_BUDGET_SECONDS[purpose]

    def verify_pr(
        self, obligation: Obligation, *, purpose: VerifyPurpose
    ) -> VerificationResult:
        """Report RESOLVED / UNRESOLVED / UNVERIFIED for a PR-referencing obligation.

        merged                              -> RESOLVED     (Req 4.3)
        reachable but not merged            -> UNRESOLVED   (Req 4.4, 8.7)
        unreachable/timeout/error <=3 tries -> UNVERIFIED   (Req 4.5, 8.6)

        The per-purpose budget (NUDGE 10s / AUTO_CLOSE 30s) is enforced as a deadline
        across all attempts; each attempt receives the remaining budget as its
        per-call timeout.
        """
        ref = PrRef.parse(obligation.artifact_ref)
        budget = self.timeout_budget(purpose)
        deadline = self._monotonic() + budget

        for _attempt in range(self._max_attempts):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                # Budget exhausted before a definite answer -> safe UNVERIFIED.
                break
            try:
                payload = self._client(
                    ref.owner, ref.repo, ref.number, timeout=remaining
                )
            except Exception:
                # Unreachable / timeout / error: retry until attempts are exhausted.
                continue
            # Successful retrieval: map merged->RESOLVED, anything else->UNRESOLVED.
            status = PullRequestStatus.from_mcp_response(payload)
            return from_intent(to_verification_intent(status))

        # At most max_attempts failures (or deadline elapsed) -> UNVERIFIED (Req 4.5).
        return VerificationResult.UNVERIFIED


def is_auto_close_permitted(result: VerificationResult) -> bool:
    """Whether auto-close may proceed for a given Verifier result.

    Auto-close is forbidden while a PR's state is unknown: UNVERIFIED blocks it
    (Req 4.6, 8.6). Only RESOLVED actually triggers closure (Req 8.2, gated in
    task 10); UNRESOLVED leaves the loop unchanged but is not a *safety* block.
    This helper expresses the UNVERIFIED safety interlock so callers cannot
    auto-close on an unverified result.
    """
    return result is not VerificationResult.UNVERIFIED


def build_github_mcp_client(
    settings: Any | None = None,
) -> PullRequestStatusClient:
    """Wire a real GitHub MCP-backed ``PullRequestStatusClient`` from config.

    Reads GITHUB_MCP_URL / GITHUB_MCP_TOKEN via ``loop.config.get_settings`` (or an
    injected ``settings``). The ``mcp`` client and the network connection are
    imported/opened lazily *inside the returned callable*, so importing this module
    — and constructing the client — never touches the network. The call shape is
    verified against the official remote GitHub MCP server
    (``https://api.githubcopilot.com/mcp/``): tool ``pull_request_read`` with
    ``{method: "get", owner, repo, pullNumber}`` returns the PR object whose
    ``merged`` / ``state`` fields the contract maps to the three-valued result.
    """
    if settings is None:
        from loop.config import get_settings

        settings = get_settings()
    settings.require("github_mcp_url", "github_mcp_token")
    url = settings.github_mcp_url
    token = settings.github_mcp_token

    def _client(
        owner: str, repo: str, pull_number: int, *, timeout: float
    ) -> Mapping[str, Any]:
        import asyncio
        import json

        async def _call() -> Mapping[str, Any]:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            headers = {"Authorization": f"Bearer {token}"}
            async with streamablehttp_client(url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "pull_request_read",
                        {
                            "method": "get",
                            "owner": owner,
                            "repo": repo,
                            "pullNumber": pull_number,
                        },
                    )
            # Extract the PR JSON object from the MCP tool-result content blocks.
            blocks = getattr(result, "content", result)
            if isinstance(blocks, Mapping):
                return blocks
            if isinstance(blocks, list):
                for block in blocks:
                    text = getattr(block, "text", None)
                    if text:
                        return json.loads(text)
            if isinstance(blocks, str):
                return json.loads(blocks)
            raise ValueError(f"Could not extract PR payload from tool result: {result!r}")

        # Enforce the per-call timeout budget on the whole MCP round-trip.
        return asyncio.run(asyncio.wait_for(_call(), timeout=timeout))

    return _client


__all__ = [
    "Verifier",
    "PrRef",
    "PullRequestStatusClient",
    "is_auto_close_permitted",
    "build_github_mcp_client",
    "TIMEOUT_BUDGET_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
]
