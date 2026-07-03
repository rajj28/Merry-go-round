#!/usr/bin/env python3
"""THROWAWAY SPIKE (task 1.3) — GitHub MCP Pull-Request-status round-trip.

Goal: prove a Python MCP client can connect to a GitHub MCP server, call the tool
that returns a pull request's status for a given ``owner/repo#number``, print the
raw result, and normalize it to the three-valued verification intent the Verifier
needs (Req 4.1, 8.1).

This is a SPIKE: minimal, throwaway, and heavily commented. It is NOT the Verifier
(task 8.1) — there is no retry/timeout/auto-close logic here.

--------------------------------------------------------------------------------
HOW TO RUN (see loop/spikes/SETUP_GITHUB_MCP.md for the full checklist)
--------------------------------------------------------------------------------
Importing this module never touches the network. Only ``__main__`` connects.

    # 1. install deps
    pip install "mcp[cli]"

    # 2. point at a running GitHub MCP server + provide a token
    #    (Streamable-HTTP transport assumed; stdio variant noted below.)
    export GITHUB_MCP_URL="https://api.githubcopilot.com/mcp/"   # or your local server URL
    export GITHUB_TOKEN="ghp_xxx_throwaway_repo_scoped"

    # 3. run against one PR, exercising merged/open/closed
    python loop/spikes/mcp_spike.py acme/throwaway#42

--------------------------------------------------------------------------------
ASSUMPTIONS (clearly flagged — verify against your server version)
--------------------------------------------------------------------------------
* ASSUMED MCP TOOL NAME : "get_pull_request"
* ASSUMED TOOL ARGS     : {"owner": <str>, "repo": <str>, "pullNumber": <int>}
* ASSUMED RESULT        : the GitHub REST Pull Request object as JSON text in the
                          tool result's content (see loop/verifier/mcp_contract.py
                          ASSUMED SCHEMA). Key fields: state, merged, merged_at.
* ASSUMED TRANSPORT     : Streamable HTTP at GITHUB_MCP_URL with a bearer token.
                          For a stdio server (e.g. the official Docker image), swap
                          the `streamablehttp_client(...)` block for
                          `stdio_client(StdioServerParameters(command=..., args=...))`.
Some server versions expose `get_pull_request_status` (commit/check status) which
is a DIFFERENT tool; for merge state we want `get_pull_request`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass

# Import the Verifier contract so the spike demonstrates the exact normalization
# the Verifier (task 8.1) will reuse. This import is network-free.
try:
    from loop.verifier.mcp_contract import (
        PullRequestStatus,
        to_verification_intent,
    )
except ModuleNotFoundError:
    # Allow running the file directly (python loop/spikes/mcp_spike.py ...) without
    # the package being on sys.path.
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from loop.verifier.mcp_contract import (  # noqa: E402
        PullRequestStatus,
        to_verification_intent,
    )

# ---- ASSUMED tool contract (see module docstring) ---------------------------
ASSUMED_TOOL_NAME = "get_pull_request"


@dataclass(frozen=True)
class PrRef:
    """A parsed owner/repo#number reference."""

    owner: str
    repo: str
    number: int

    @classmethod
    def parse(cls, raw: str) -> "PrRef":
        """Parse 'owner/repo#number' into a PrRef. Raises ValueError on bad input."""
        m = re.fullmatch(r"([^/\s]+)/([^#\s]+)#(\d+)", raw.strip())
        if not m:
            raise ValueError(
                f"Expected 'owner/repo#number' (e.g. acme/throwaway#42), got: {raw!r}"
            )
        return cls(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))


def _extract_payload(tool_result_content: object) -> dict:
    """Pull the PR JSON dict out of an MCP tool result's content.

    MCP tool results carry a list of content blocks; GitHub's server returns the
    PR object as JSON in a text block. We tolerate either a raw dict or JSON text.
    """
    # mcp.types.CallToolResult.content is a list of content blocks with `.text`.
    blocks = getattr(tool_result_content, "content", tool_result_content)
    if isinstance(blocks, dict):
        return blocks
    if isinstance(blocks, list):
        for block in blocks:
            text = getattr(block, "text", None)
            if text:
                return json.loads(text)
    if isinstance(blocks, str):
        return json.loads(blocks)
    raise ValueError(f"Could not extract PR payload from tool result: {tool_result_content!r}")


async def fetch_pr_status(pr: PrRef) -> dict:
    """Connect to the GitHub MCP server and call the assumed PR-status tool.

    Returns the raw PR payload (dict). Network access happens only here.
    Reads endpoint + token from env: GITHUB_MCP_URL, GITHUB_TOKEN.
    """
    # Imports are local so that importing this module never requires the MCP client
    # to be installed and never opens a connection (guard requirement of the spike).
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.environ["GITHUB_MCP_URL"]
    token = os.environ["GITHUB_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}

    # NOTE: for a stdio-based GitHub MCP server, replace this block with:
    #   from mcp.client.stdio import stdio_client, StdioServerParameters
    #   async with stdio_client(StdioServerParameters(command="docker",
    #       args=["run","-i","--rm","-e","GITHUB_PERSONAL_ACCESS_TOKEN",
    #             "ghcr.io/github/github-mcp-server"],
    #       env={"GITHUB_PERSONAL_ACCESS_TOKEN": token})) as (read, write):
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            # ASSUMED tool + args (see module docstring).
            result = await session.call_tool(
                ASSUMED_TOOL_NAME,
                {"owner": pr.owner, "repo": pr.repo, "pullNumber": pr.number},
            )
    return _extract_payload(result)


def normalize_and_report(raw_payload: dict) -> None:
    """Print the raw result + the normalized PullRequestStatus + verification intent."""
    print("=== RAW MCP PR-status payload ===")
    print(json.dumps(raw_payload, indent=2, sort_keys=True))

    status: PullRequestStatus = PullRequestStatus.from_mcp_response(raw_payload)
    intent = to_verification_intent(status)

    print("\n=== Normalized PullRequestStatus (Verifier contract) ===")
    print(f"  state            = {status.state!r}")
    print(f"  merged           = {status.merged}")
    print(f"  merged_at        = {status.merged_at!r}")
    print(f"  review_decision  = {status.review_decision!r}")
    print(f"  is_merged                = {status.is_merged}")
    print(f"  is_closed_without_merge  = {status.is_closed_without_merge}")
    print(f"\n  -> VerificationIntent = {intent.value.upper()}  "
          f"(merged->resolved, else unresolved; transport failure->UNVERIFIED is the Verifier's job)")


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        print("\nUSAGE: python loop/spikes/mcp_spike.py owner/repo#number")
        print("       (set GITHUB_MCP_URL and GITHUB_TOKEN in the environment)")
        return 2

    pr = PrRef.parse(argv[1])
    print(f"Connecting to GitHub MCP at {os.environ.get('GITHUB_MCP_URL', '<GITHUB_MCP_URL unset>')} ...")
    print(f"Fetching {ASSUMED_TOOL_NAME}({pr.owner}/{pr.repo}#{pr.number})\n")

    raw_payload = asyncio.run(fetch_pr_status(pr))
    normalize_and_report(raw_payload)

    # To exercise merged/open/closed: run this against three throwaway PRs whose
    # states are merged, open, and closed-without-merge (see SETUP_GITHUB_MCP.md).
    return 0


if __name__ == "__main__":
    # Only the live MCP call lives under __main__: importing this module is safe and
    # never requires a running server or the mcp client to be installed.
    raise SystemExit(_main(sys.argv))
