# GitHub MCP connection spike — human checklist (task 1.3)

Goal: stand up / connect to a **GitHub MCP server** from a Python MCP client,
authenticate against a **throwaway repo**, and run `mcp_spike.py` to retrieve a
PR's status and exercise the **merged / open / closed** outcomes. This grounds the
Verifier contract (`loop/verifier/mcp_contract.py`) for task 8.1 (Req 4.1, 8.1).

> The Python files (`mcp_spike.py`, `mcp_contract.py`) are runnable to the line.
> Everything below is the part a human must do: provision a server, a repo, a token.

---

## 1. Create a throwaway repo + three PRs

Use a **disposable** repo (delete it after the spike).

1. Create a new private repo, e.g. `acme/throwaway`.
2. Create three pull requests that cover all three states the Verifier maps:
   - **merged**  — open a PR with a trivial change, then **merge** it → `acme/throwaway#42`
   - **open**    — open a PR and leave it open → `acme/throwaway#43`
   - **closed without merge** — open a PR and **close** it without merging → `acme/throwaway#44`

Record the three PR numbers; you will run the spike once per number.

## 2. Create a token (minimum scope)

Create a **fine-grained personal access token** scoped to the throwaway repo only:

- Repository access: **Only select repositories → `acme/throwaway`**
- Repository permissions: **Pull requests → Read-only**, **Contents → Read-only**
- (Classic token alternative: the `repo` scope works but is broader than needed —
  prefer fine-grained, read-only, single-repo.)

Keep the token out of source control. Do not paste it into files; use the env var below.

## 3. Stand up / connect to a GitHub MCP server

Pick **one** transport. The spike defaults to **Streamable HTTP**; a stdio note is
included inline in `mcp_spike.py`.

### Option A — Remote GitHub MCP server (Streamable HTTP, simplest)
- Endpoint: `https://api.githubcopilot.com/mcp/`
- Auth: send the token as a bearer header (the spike does this from `GITHUB_TOKEN`).

```bash
export GITHUB_MCP_URL="https://api.githubcopilot.com/mcp/"
export GITHUB_TOKEN="<your throwaway, repo-scoped token>"
```

### Option B — Local server via Docker (stdio)
```bash
docker run -i --rm \
  -e GITHUB_PERSONAL_ACCESS_TOKEN="<token>" \
  ghcr.io/github/github-mcp-server
```
Then switch `mcp_spike.py` to the `stdio_client(...)` block noted in `fetch_pr_status`
(replace the `streamablehttp_client(...)` context manager).

## 4. Install the Python MCP client

```bash
pip install "mcp[cli]"
```

## 5. Run the spike (once per PR state)

```bash
python loop/spikes/mcp_spike.py acme/throwaway#42   # expect VerificationIntent = RESOLVED   (merged)
python loop/spikes/mcp_spike.py acme/throwaway#43   # expect VerificationIntent = UNRESOLVED (open)
python loop/spikes/mcp_spike.py acme/throwaway#44   # expect VerificationIntent = UNRESOLVED (closed-without-merge)
```

Each run prints:
- the **raw** MCP PR-status payload, and
- the **normalized** `PullRequestStatus` plus the mapped `VerificationIntent`.

## 6. Record the response shape for the Verifier contract

Compare the printed **raw** payload against the **ASSUMED SCHEMA** documented in
`loop/verifier/mcp_contract.py`. Confirm or correct:

- The **tool name** — the spike assumes `get_pull_request`. If your server version
  differs (e.g. only exposes `get_pull_request_status`, which is commit/check status,
  not merge state), update `ASSUMED_TOOL_NAME` in `mcp_spike.py`.
- The **argument key** for the PR number — assumed `pullNumber`; some versions use
  `pull_number` or `number`.
- The presence/shape of `state`, `merged`, `merged_at`, and `review_decision`.

If anything differs, update the `ASSUMED SCHEMA` docstring and the `from_mcp_response`
parser in `mcp_contract.py` so task 8.1 builds against the real shape.

---

## Mapping the spike proves (the Verifier contract)

| GitHub MCP PR state              | `is_merged` | VerificationIntent | Req        |
|----------------------------------|-------------|--------------------|------------|
| merged (`state=closed, merged=true`)   | true  | **RESOLVED**   | 4.3        |
| open (`state=open, merged=false`)      | false | **UNRESOLVED** | 4.4        |
| closed-without-merge (`closed,false`)  | false | **UNRESOLVED** | 4.4, 8.7   |
| server unreachable / timeout / error   | n/a   | **UNVERIFIED** (Verifier's call logic, task 8.1) | 4.5, 8.6 |

> **Scope reminder:** this spike + contract stop at the mapping shape. The
> retry/timeout (10s nudge / 30s auto-close, ≤3 attempts → UNVERIFIED) and the
> auto-close gate are **task 8.1**, not this spike.
