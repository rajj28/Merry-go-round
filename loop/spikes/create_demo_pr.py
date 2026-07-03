"""Create a demo pull request in the throwaway repo (demo setup tooling).

Creates an open PR in a GitHub repo using the REST API and the token already in
loop/.env (`GITHUB_MCP_TOKEN`) — so the GitHub-proof-of-work auto-close beat
(Beat 3) has a real PR to verify and, later, merge on cue.

It is idempotent-ish: it makes a branch + a tiny file change + opens a PR. If the
branch/PR already exists it reports the existing PR rather than failing hard.

The token must have WRITE access to the repo (classic PAT with `repo` scope, or a
fine-grained PAT with Contents: read/write + Pull requests: read/write). The same
token works read-only for the MCP probe.

Usage (from the repo root):
    python -m loop.spikes.create_demo_pr --repo rajj28/loop-demo

The token is never printed. On success it prints the PR number + URL, which you
then feed to the MCP probe:  python -m loop.spikes.mcp_probe --ref rajj28/loop-demo#<n>
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

from loop.config import get_settings

API = "https://api.github.com"
BRANCH = "loop-demo-open-loop"
FILE_PATH = "OPEN_LOOP.md"
PR_TITLE = "Add OPEN_LOOP note (Loop demo PR)"
PR_BODY = (
    "Demo PR for **Loop** — the obligation-graph Slack agent.\n\n"
    "Loop's Verifier reads this PR's real merge state via the GitHub MCP server "
    "before it nudges or auto-closes the linked loop. Merging this PR fires the "
    "GitHub proof-of-work auto-close beat."
)
FILE_CONTENT = (
    "# Open loop\n\n"
    "This file exists so Loop has a real PR to verify. When this PR merges, the "
    "linked loop auto-closes (verified via the GitHub MCP server).\n"
)


def _request(
    method: str, url: str, token: str, body: Optional[dict[str, Any]] = None
) -> tuple[int, Any]:
    """Make a GitHub REST call; return (status, parsed_json). Never prints the token."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "loop-demo-pr-script",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
        return exc.code, payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a demo PR in a throwaway repo.")
    parser.add_argument("--repo", required=True, help="owner/repo, e.g. rajj28/loop-demo")
    args = parser.parse_args()

    if "/" not in args.repo:
        print("--repo must be owner/repo (e.g. rajj28/loop-demo)", file=sys.stderr)
        raise SystemExit(2)
    owner, repo = args.repo.split("/", 1)

    settings = get_settings()
    try:
        settings.require("github_mcp_token")
    except RuntimeError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        print("Add GITHUB_MCP_TOKEN=<GitHub PAT with repo write> to loop/.env.", file=sys.stderr)
        raise SystemExit(2)
    token = settings.github_mcp_token

    print(f"Creating demo PR in {owner}/{repo} (token never printed)...")

    # 1) Repo + default branch.
    status, repo_info = _request("GET", f"{API}/repos/{owner}/{repo}", token)
    if status != 200:
        print(f"Cannot read repo ({status}): {repo_info}", file=sys.stderr)
        print("  Check the repo name and that the PAT has access to it.", file=sys.stderr)
        raise SystemExit(1)
    default_branch = repo_info.get("default_branch", "main")

    # 2) Base commit SHA of the default branch.
    status, ref = _request(
        "GET", f"{API}/repos/{owner}/{repo}/git/ref/heads/{default_branch}", token
    )
    if status != 200:
        print(f"Cannot read default branch ref ({status}): {ref}", file=sys.stderr)
        print(
            "  If the repo is empty, add a README first (one commit) so there is a "
            "base branch to open a PR against.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    base_sha = ref["object"]["sha"]

    # 3) Create the feature branch (tolerate 'already exists').
    status, made = _request(
        "POST",
        f"{API}/repos/{owner}/{repo}/git/refs",
        token,
        {"ref": f"refs/heads/{BRANCH}", "sha": base_sha},
    )
    if status not in (201, 422):  # 422 = ref already exists
        print(f"Cannot create branch ({status}): {made}", file=sys.stderr)
        raise SystemExit(1)

    # 4) Create/update a file on the branch (this makes a commit so the PR has a diff).
    #    Need the file's current sha on the branch if it already exists.
    status, existing = _request(
        "GET",
        f"{API}/repos/{owner}/{repo}/contents/{FILE_PATH}?ref={BRANCH}",
        token,
    )
    put_body: dict[str, Any] = {
        "message": "Add OPEN_LOOP.md for the Loop demo",
        "content": base64.b64encode(FILE_CONTENT.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    if status == 200 and isinstance(existing, dict) and existing.get("sha"):
        put_body["sha"] = existing["sha"]  # update in place
    status, put_res = _request(
        "PUT", f"{API}/repos/{owner}/{repo}/contents/{FILE_PATH}", token, put_body
    )
    if status not in (200, 201):
        print(f"Cannot commit file to branch ({status}): {put_res}", file=sys.stderr)
        raise SystemExit(1)

    # 5) Open the PR (tolerate 'already exists' → find it).
    status, pr = _request(
        "POST",
        f"{API}/repos/{owner}/{repo}/pulls",
        token,
        {"title": PR_TITLE, "head": BRANCH, "base": default_branch, "body": PR_BODY},
    )
    if status == 201:
        print(f"\nCreated PR #{pr['number']}: {pr['html_url']}")
    elif status == 422:
        # A PR for this branch likely already exists — find and report it.
        s2, prs = _request(
            "GET",
            f"{API}/repos/{owner}/{repo}/pulls?head={owner}:{BRANCH}&state=open",
            token,
        )
        if s2 == 200 and prs:
            print(f"\nPR already open: #{prs[0]['number']}: {prs[0]['html_url']}")
            pr = prs[0]
        else:
            print(f"PR not created and none found ({status}): {pr}", file=sys.stderr)
            raise SystemExit(1)
    else:
        print(f"Cannot create PR ({status}): {pr}", file=sys.stderr)
        raise SystemExit(1)

    print(
        f"\nNext: verify it via the GitHub MCP probe —\n"
        f"  python -m loop.spikes.mcp_probe --ref {owner}/{repo}#{pr['number']}\n"
        "It should read UNRESOLVED while open; merge the PR and re-run to see RESOLVED."
    )


if __name__ == "__main__":
    main()
