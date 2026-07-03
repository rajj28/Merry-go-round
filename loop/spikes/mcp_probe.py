"""GitHub MCP access probe (task 1.3 spike) — does the Verifier's MCP path work?

Run this once after adding GITHUB_MCP_URL + GITHUB_MCP_TOKEN to loop/.env and
creating a throwaway repo with a pull request. It answers the question that
unlocks Demo Beat 3 (GitHub proof-of-work auto-close):

    Can Loop reach the official GitHub MCP server and read a PR's real merge state
    via the `get_pull_request` tool, mapping it to the Verifier's three-valued
    result (RESOLVED / UNRESOLVED / UNVERIFIED)?

It reuses the REAL wiring — `loop.verifier.verifier.build_github_mcp_client` and the
real `Verifier` — so a green probe means the production auto-close path works. It
never prints the token.

Prereqs (one-time):
  1. A throwaway GitHub repo with at least one pull request (open is fine; you'll
     merge it live during the demo to fire the auto-close beat).
  2. A GitHub PAT with read access to that repo, in loop/.env as GITHUB_MCP_TOKEN.
  3. GITHUB_MCP_URL=https://api.githubcopilot.com/mcp/ in loop/.env.
  4. `pip install mcp` (the MCP client) if not already installed.

Usage (from the repo root):
    python -m loop.spikes.mcp_probe --ref owner/repo#123

It prints, non-secretly:
  * the raw PR fields the contract reads (number, state, merged, merged_at),
  * the Verifier's NUDGE-purpose result on that PR,
  * a clear interpretation (merged -> RESOLVED, etc.).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Mapping

from loop.config import get_settings


def _summarize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Pull the non-secret PR fields the Verifier's contract actually reads."""
    return {
        "number": payload.get("number"),
        "state": payload.get("state"),
        "merged": payload.get("merged"),
        "merged_at": payload.get("merged_at"),
        "title_present": bool(payload.get("title")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe the GitHub MCP PR-status path.")
    parser.add_argument(
        "--ref",
        required=True,
        help="PR reference as owner/repo#number (e.g. octocat/Hello-World#1).",
    )
    args = parser.parse_args()

    settings = get_settings()
    try:
        settings.require("github_mcp_url", "github_mcp_token")
    except RuntimeError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        print(
            "Add GITHUB_MCP_URL=https://api.githubcopilot.com/mcp/ and "
            "GITHUB_MCP_TOKEN=<your GitHub PAT> to loop/.env.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    print("GitHub MCP probe — read-only; the token is never printed.")
    print(f"  MCP URL: {settings.github_mcp_url}")
    print(f"  PAT present: {bool(settings.github_mcp_token)}")
    print(f"  PR ref: {args.ref}")

    from loop.graph.models import ArtifactType, LoopState, Obligation
    from loop.verifier.types import VerifyPurpose
    from loop.verifier.verifier import (
        PrRef,
        Verifier,
        build_github_mcp_client,
    )

    # Validate the ref shape early with the real parser.
    try:
        ref = PrRef.parse(args.ref)
    except ValueError as exc:
        print(f"Bad --ref: {exc}", file=sys.stderr)
        raise SystemExit(2)

    client = build_github_mcp_client(settings)

    # 1) Raw tool call — show the fields the contract maps.
    try:
        payload = client(ref.owner, ref.repo, ref.number, timeout=30.0)
        print("\nget_pull_request OK. PR fields:")
        print("  " + str(_summarize_payload(payload)))
    except Exception as exc:  # noqa: BLE001 — report the transport/auth error clearly.
        print(f"\nget_pull_request FAILED: {exc!r}", file=sys.stderr)
        print(
            "  Common causes: PAT lacks access to the repo; the remote MCP server "
            "rejects PAT bearer auth (try the local Docker GitHub MCP server); or "
            "the `mcp` package isn't installed (pip install mcp).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # 2) Run the REAL Verifier end-to-end on a synthetic PR-referencing obligation.
    obligation = Obligation(
        obligation_id="PROBE",
        owes_person_id="U_OTHER",
        owed_person_id="U_USER",
        owner_person_id="U_OTHER",
        loop_state=LoopState.WAITING_ON_OTHER,
        confidence_score=0.9,
        last_touch_timestamp="2025-01-08T12:00:00+00:00",
        source_msg_channel="C_PROBE",
        source_msg_ts="1700000000.000100",
        subject_summary="probe",
        artifact_type=ArtifactType.GITHUB_PR,
        artifact_ref=args.ref,
    )
    result = Verifier(client).verify_pr(obligation, purpose=VerifyPurpose.NUDGE)
    print(f"\nVerifier result: {result.value}")
    print(
        "Interpretation: merged -> RESOLVED (auto-close would fire); "
        "open/closed-without-merge -> UNRESOLVED (loop stays); "
        "transport/auth failure -> UNVERIFIED (auto-close safely blocked)."
    )
    print(
        "\nFor the demo: with the PR OPEN this should read UNRESOLVED; after you "
        "merge it, re-run and it should read RESOLVED — that transition is Beat 3."
    )


if __name__ == "__main__":
    main()
