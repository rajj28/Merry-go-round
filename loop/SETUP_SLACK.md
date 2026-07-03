# Loop — Slack Setup & Judge-Access Checklist (Task 1.1)

This is the **human-only** part of task 1.1. The code and manifest are committed;
these steps need your Slack login and a real sandbox workspace, so they cannot
be automated. Do them once, then anyone with the tokens can run the app.

> **Never commit tokens.** All tokens live in `.env` (git-ignored) and are read
> from the environment by `loop/slack_app.py`. The manifest contains no secrets.

---

## 0. Prerequisites

- A Slack account you can use to create a **sandbox workspace** (a free, throwaway
  workspace is fine — do **not** use a production company workspace).
- Python 3.12 (3.11 also works for this scaffolding) and `pip`.
- The files from this task: `slack_app_manifest.yaml` (repo root), `loop/slack_app.py`.

---

## 1. Create a sandbox workspace

1. Go to <https://slack.com/get-started> and **create a new workspace** (e.g.
   "Loop Sandbox"). Use a name/email you control.
2. Confirm you land in the new, empty workspace before continuing.

## 2. Create the Slack app from the manifest

1. Go to <https://api.slack.com/apps> and click **Create New App**.
2. Choose **From a manifest**.
3. Select your **sandbox workspace** as the development workspace.
4. Paste the contents of `slack_app_manifest.yaml` (repo root). Switch the editor
   to **YAML** if it defaults to JSON.
5. Review the scopes/events summary Slack shows, then **Create**.

## 3. Generate the app-level token (Socket Mode)

1. In the app settings, open **Settings → Socket Mode** and confirm Socket Mode
   is **Enabled** (the manifest enables it; verify it stuck).
2. Open **Settings → Basic Information → App-Level Tokens → Generate Token and Scopes**.
3. Name it `socket`, add the scope **`connections:write`**, and **Generate**.
4. Copy the token — it starts with **`xapp-`**. This is your `SLACK_APP_TOKEN`.

## 4. Install to the sandbox and get the bot + user tokens

1. Open **Settings → Install App** (or **OAuth & Permissions → Install to Workspace**).
2. Click **Install to <your sandbox>** and **Allow** the requested scopes.
   - You will be asked to authorize both **bot** and **user** scopes (the user
     scopes let Loop post Polite Nudges as you — Req 7/10/13).
3. After install, on **OAuth & Permissions** copy:
   - **Bot User OAuth Token** → starts with **`xoxb-`** → this is `SLACK_BOT_TOKEN`.
   - **User OAuth Token** → starts with **`xoxp-`** → save as `SLACK_USER_TOKEN`
     (used later by the Action Agent to post as you; not required just to boot).

## 5. Put the tokens in `.env`

1. Create a `.env` file in the repo root (it is git-ignored — never commit it):

   ```dotenv
   SLACK_BOT_TOKEN=xoxb-...your-bot-token...
   SLACK_APP_TOKEN=xapp-...your-app-level-token...
   SLACK_USER_TOKEN=xoxp-...your-user-token...
   ```

2. Load it into your shell before running (pick one):
   - PowerShell: `Get-Content .env | ForEach-Object { if ($_ -match '^(.+?)=(.+)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2]) } }`
   - bash/zsh: `set -a && source .env && set +a`
   - or rely on the project's `python-dotenv` loading added in task 1.4.

## 6. Install dependencies and run the app

```bash
pip install slack-bolt        # full project deps are pinned in task 1.4
python -m loop.slack_app
```

You should see `Loop is connecting over Socket Mode...` followed by Bolt's
"connected" log. Leave it running.

## 7. Verify the App Home placeholder

1. In the sandbox Slack client, find **Loop** under **Apps** in the sidebar.
2. Click it and open the **Home** tab.
3. You should see the header **"Loop — coming online"**. That confirms the
   `app_home_opened` event reached the app and the view published (Req 6.1).

---

## 8. Grant sandbox access to the judges and verify they can open the app

The challenge requires the judges to be able to open Loop. Invite **both**
emails to the **sandbox workspace** and confirm each can open the App Home.

1. In the sandbox workspace, open **Workspace menu → Settings & administration →
   Manage members → Invite people**.
2. Invite both judge emails:
   - `slackhack@salesforce.com`
   - `testing@devpost.com`
3. Choose **Member** (full member, not single-channel guest) so they can browse
   to the **Apps** section and open the App Home.
4. Send the invitations. (Optionally also paste the shared invite link into the
   Devpost submission notes.)
5. **Verify each judge can open the app:**
   - Easiest: have each judge accept the invite, sign in, click **Loop** under
     **Apps**, and confirm they see the **"Loop — coming online"** Home tab.
   - If you cannot coordinate live: as the workspace owner, confirm both emails
     show **Active** (not "Invited/pending") under **Manage members**, and that
     **Loop** appears in the workspace **Apps** list (so any member, including
     the judges, can open it). The app must be **installed to the workspace**
     (step 4), which makes the Home tab available to all members.
6. Keep the app process from step 6 running while judges test, so their
   `app_home_opened` events get a published view.

### Quick troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| App Home tab is blank / not present | App not installed, or process not running | Re-run step 4 install; ensure `python -m loop.slack_app` is running |
| `Missing required environment variable 'SLACK_BOT_TOKEN'` | `.env` not loaded | Re-do step 5 |
| Socket Mode won't connect | App-level token missing `connections:write`, or wrong token | Re-generate in step 3; ensure `SLACK_APP_TOKEN` starts with `xapp-` |
| Judge can't find the app | Invited as guest, or app not installed | Re-invite as **Member**; confirm install in step 4 |

---

## Done-when (acceptance for task 1.1)

- [ ] App created from `slack_app_manifest.yaml` with **Socket Mode enabled**.
- [ ] App **installed to the sandbox**; bot + user tokens captured in `.env`.
- [ ] `python -m loop.slack_app` connects and the **Home** tab shows
      "Loop — coming online".
- [ ] `slackhack@salesforce.com` and `testing@devpost.com` are **Active members**
      of the sandbox and can open the App Home.
