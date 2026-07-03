# RTS Spike Setup — confirm entitlement & verify the query→results round-trip

**Task 1.2 (Dev A).** Goal: prove the Slack **Real-Time Search (RTS)** API is
provisioned on the sandbox and returns candidate messages, then capture the
response shape for the Watcher contract (`loop/watcher/rts_contract.py`).

> ⚠️ The code in `rts_spike.py` is written **to the line** but the live
> authenticated call still needs a human with an entitled token. Work the
> checklist below, then run the spike.

---

## What RTS actually is (important)

RTS is **not** a standalone `rts.*` endpoint. It is the Web API method
**`assistant.search.context`** (`POST https://slack.com/api/assistant.search.context`).
That method *is* the Real-Time Search API. Do **not** use the legacy
`search.messages` endpoint.

---

## 1. Confirm RTS API entitlement on the sandbox

RTS is a gated/partner capability. Confirm, in order:

- [ ] The sandbox workspace is on a plan where the **Real-Time Search API** is
      enabled (it ships with the new agent platform / MCP + RTS rollout).
- [ ] In the app config at <https://api.slack.com/apps> → your app, AI/agent
      features are enabled (the app is an **AI app / agent**; `assistant.search.context`
      is only available to apps using AI features).
- [ ] You can call `auth.test` with your token and it succeeds (sanity check the
      token is valid for the workspace).
- [ ] A bare `assistant.search.context` call does **not** return
      `feature_not_enabled` or `team_access_not_granted`. Either error means the
      entitlement is missing — request RTS access for the sandbox before continuing.

If entitlement is missing, that is the blocker to escalate — the spike cannot
pass until it is granted.

## 2. Token & scopes required

`assistant.search.context` needs **search scopes**, and the token type changes
whether an `action_token` is required:

| Token type | Scopes to add | `action_token` needed? |
|---|---|---|
| **User token** (preferred for timer-driven sweeps) | `search:read.public`, `search:read.users`, `search:read.private`, `search:read.im`, `search:read.mpim`, `search:read.files` | **No** |
| **Bot token** | `search:read.public`, `search:read.users`, `search:read.files` | **Yes** — must pass an `action_token` from a triggering message/assistant event |

- [ ] Add the scopes above to the app and **reinstall** to the sandbox.
- [ ] Decide token type. The Watcher's **periodic sweep (Req 2.1)** is driven by
      a timer with no triggering event, so it has **no `action_token`** →
      use a **user token** for the sweep. (A bot token only works on the
      event-driven path, Req 2.2, where an `action_token` is present in the event.)
- [ ] Copy the token somewhere safe; you'll export it as an env var (below).
      Do **not** commit it.

## 3. Run the spike to verify the round-trip

From the repo root:

```powershell
# PowerShell (Windows)
$env:SLACK_RTS_TOKEN = "xoxp-...your-user-token..."   # user token preferred
# Only if you must use a bot token, also set an action_token from a real event:
# $env:SLACK_RTS_ACTION_TOKEN = "12345.98765.abcd..."

pip install slack_sdk          # spike's only runtime dependency
python loop/spikes/rts_spike.py
# optional: pass your own query
python loop/spikes/rts_spike.py "who is waiting on a reply from me"
```

```bash
# bash/zsh (macOS/Linux)
export SLACK_RTS_TOKEN="xoxp-...your-user-token..."
pip install slack_sdk
python loop/spikes/rts_spike.py
```

### Success looks like

- [ ] The script prints `=== RAW RTS RESPONSE ===` with `"ok": true` and a
      `results.messages` array.
- [ ] It prints `=== NORMALIZED CANDIDATES (N) ===` with at least one candidate
      showing `channel`, `ts`, `author`, text and a permalink.
- [ ] It ends with `round-trip OK ✅`.

### Common failures → meaning

| Printed error | Meaning / fix |
|---|---|
| `not_authed` / `invalid_auth` | Token missing or wrong — recheck `SLACK_RTS_TOKEN`. |
| `missing_scope` | Add the search scopes in step 2 and reinstall. |
| `feature_not_enabled` / `team_access_not_granted` | RTS entitlement missing — back to step 1. |
| `invalid_action_token` / action token errors | You used a bot token without a valid `action_token`; switch to a user token for the sweep. |
| `ratelimited` | RTS has a ~10 req/min user limit; wait and retry (don't loop the spike). |

## 4. Hand-off to the Watcher contract

Once you get a real `=== RAW RTS RESPONSE ===`, **paste a sanitized copy** into a
comment on Task 4.1 and confirm the fields used by
`loop/watcher/rts_contract.py` (`channel_id`, `message_ts`, `author_user_id`,
`content`, `permalink`, `is_author_bot`) match the live shape. If anything
differs, update **only** `rts_contract.py` — the Watcher inherits the fix.
