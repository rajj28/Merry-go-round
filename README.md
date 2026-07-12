# Loop — the obligation-graph Slack agent

**Slack shows you messages. Loop shows you your obligations.**

Every day, thousands of dependencies are created inside team conversations — questions awaiting answers, promised code reviews, proposed meetings — and not a single one is tracked. Loop is a Slack AI agent that maintains a live **obligation graph**: every open loop where someone is blocked on you, or you are waiting on someone, auto-detected from plain conversation with no forms, no commands, and no manual marking.

The signature reveal: **"N people are blocked on you right now and you don't know it."**

![Loop App Home — the live obligation dashboard](https://raw.githubusercontent.com/rajj28/loop-demo/main/projectgallery/homeimage.png)

## What it does

- **Detects commitments in real time.** "Can you review my PR?" / "I'll send it by Friday" / "Can we meet tomorrow at 4?" each become a tracked obligation with parties, a summary, and a deadline.
- **Verifies against reality before acting.** PR-review loops are grounded in actual GitHub state via the GitHub MCP server. Loop never nudges anyone about work that's already merged.
- **Heals loops autonomously.** When the PR merges, Loop closes the loop itself and files it in the Auto-Healed feed.
- **Understands meetings.** Proposed times are extracted in the author's own timezone and become scheduled obligations with one-click calendar creation — no OAuth.
- **Acts as you, with consent.** Polite nudges drafted in your voice, sent as you, only after a one-tap confirmation. Snooze, delegate, and a daily digest included.
- **Talks back.** Ask the Assistant pane "what am I waiting on?" — an LLM planner composes a plan over Loop's real tools and shows a visible tool-use trace.
- **Learns.** Every Confirm or Dismiss tunes the surfacing threshold. Dismissed loops never resurface.

## Gallery

### Meetings — both sides of the loop

Sender side — one plain sentence becomes a meeting obligation with a proposed time and a Schedule It action:

![Meeting sender side](https://raw.githubusercontent.com/rajj28/loop-demo/main/projectgallery/meetingsendersideui.png)

Receiver side — Loop proposes the meeting in-thread with one-tap Confirm and Add to Google Calendar:

![Meeting receiver side](https://raw.githubusercontent.com/rajj28/loop-demo/main/projectgallery/meetingrecieversideui.png)

### Auto-heal — end to end

A review request with a PR link becomes a loop grounded to the real GitHub artifact:

![Auto-heal sender side](https://raw.githubusercontent.com/rajj28/loop-demo/main/projectgallery/autohealsendersideui.png)

The PR merges — the Verifier reads the real merge state through the GitHub MCP server and closes the loop autonomously:

![Auto-heal verification and close](https://raw.githubusercontent.com/rajj28/loop-demo/main/projectgallery/autohealverifiersideuiandautohealcloseui.png)

## Architecture

![Loop system architecture](https://raw.githubusercontent.com/rajj28/loop-demo/main/projectgallery/architecture.png)

Five cooperating agents around one SQLite obligation graph:

| Agent | Role |
|---|---|
| **Watcher** (Perceive) | Fast-tier LLM classifier over live message events + Real-Time Search sweeps — high-recall open-loop candidates |
| **Adjudicator** (Reason) | Smart-tier LLM decides who owes what to whom and by when; extracts meeting times anchored to the author's timezone; parses GitHub PR references from free text |
| **Verifier** (Verify) | Grounds PR loops in real GitHub state via the GitHub MCP server — RESOLVED / UNRESOLVED / UNVERIFIED before any nudge or auto-close |
| **Action Agent** (Act) | App Home dashboard, nudges-as-you behind a confirmation gate, auto-heal, snooze, delegate, daily digest |
| **Learn Engine** (Learn) | Confirm/Dismiss feedback tunes the surfacing threshold in bounded, clamped steps |

A sixth surface, the **Conversational Agent**, fronts everything in the Slack Assistant pane with a ReAct-style planner and a visible tool-use trace, degrading gracefully to a deterministic parser when planning fails.

**Stack:** Python, Slack Bolt (Socket Mode — no public endpoint), SQLite, two-tier LLM inference on Groq (Llama 3.1 8B triage + GPT-OSS 120B adjudication), APScheduler, GitHub MCP. Core invariants are enforced by property-based tests (Hypothesis, 100+ examples per property); a single pure `is_surfaced` predicate is the one source of truth for "is this shown?" across every surface.

## Run it yourself

1. Create a Slack app from [`slack_app_manifest.yaml`](slack_app_manifest.yaml) in a sandbox workspace and install it — the full step-by-step checklist is in [`loop/SETUP_SLACK.md`](loop/SETUP_SLACK.md).
2. Put your tokens in `loop/.env` (see [`loop/.env.example`](loop/.env.example)).
3. Install and run:

```bash
pip install -e .
python -m loop.slack_app
```

4. Open the **Loop** app in Slack and check the Home tab. Post "can you review my PR?" in a watched channel and watch the loop appear.

Run the tests:

```bash
pytest loop/tests
```

## Roadmap

![Loop roadmap — one engine, every vertical](https://raw.githubusercontent.com/rajj28/loop-demo/main/projectgallery/roadmap.png)

The obligation graph is deliberately general: a meeting is just an obligation of kind `meeting`; a code review is an obligation with a GitHub artifact. Docs and approvals, tasks and tickets, CRM follow-ups, DevOps incident action items, HR requests, and finance sign-offs are each just new obligation kinds on the same engine — and because the Verifier speaks MCP, every new MCP server is a new source of ground truth with no bespoke integration.

One engine. Every promise your team makes.
