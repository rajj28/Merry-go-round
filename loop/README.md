# Loop — Obligation-Graph Slack Agent

Loop is a personal Slack agent that maintains a live **obligation graph**: every
open loop where someone is blocked on you (the ball is in your court) or you are
waiting on someone (the ball is in theirs), auto-detected from Slack with no
manual marking. The signature reveal is *"N people are blocked on you right now
and you don't know it."*

This `loop/` folder holds the application source for the Tier 0 spine (detection
engine + obligation-graph store + confirm/dismiss feedback) and the Tier 1 demo
surface. The project tooling (`pyproject.toml`, `conftest.py`) lives one level up
at the workspace root, kept separate from the source. See the spec in
`.kiro/specs/loop-obligation-agent/` for requirements and design.

## Requirements

- **Python 3.12**
- A Slack workspace + app (Socket Mode), an LLM provider key, and access to a
  GitHub MCP server. Loop is **provider-agnostic**: it runs on Groq (free,
  OpenAI-compatible — the default) or Anthropic/Claude, selected by `LLM_PROVIDER`.
  See `.env.example` for every variable.

## Project layout

```
<workspace root>/
├── pyproject.toml          # deps (Python 3.12) + pytest/Hypothesis config
├── conftest.py             # Hypothesis profiles + >=100-examples enforcement
└── loop/                   # application source (this folder)
    ├── README.md           # this file
    ├── config.py           # .env loading -> validated Settings (loop.config)
    ├── .env.example        # documented workspace-scoped env vars
    ├── slack_app.py        # minimal Bolt app (Socket Mode) — task 1.1
    ├── watcher/            # Agent 1 — Perceive (RTS sweep + fast-tier filter)
    ├── adjudicator/        # Agent 2 — Reason (smart-tier whose-court adjudication)
    ├── verifier/           # Agent 3 — Verify (GitHub MCP PR-status)
    ├── action/             # Agent 4 — Act (App Home, nudge, auto-close, digest)
    ├── conversational/     # Agent 5 — Front door (Assistant pane + tool trace)
    ├── graph/              # Shared Obligation Graph store
    ├── seed/               # Seeded deterministic demo workspace
    ├── spikes/             # throwaway dependency spikes (RTS, MCP)
    └── tests/              # pytest + Hypothesis tests
```

Everything is importable under the `loop` package, e.g. `from loop import
slack_app`, `import loop.watcher`, `from loop.config import get_settings`.

## Install

Run all commands from the **workspace root** (the folder that contains
`pyproject.toml`).

```bash
# 1. Create and activate a Python 3.12 virtual environment.
python3.12 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate

# 2. Install the project with its test toolchain.
pip install -e ".[test]"

# 3. Configure secrets.
cp loop/.env.example loop/.env    # Windows: copy loop\.env.example loop\.env
# ...then edit loop/.env with your real tokens.
```

## Run the tests

From the workspace root:

```bash
# Run the suite (the dev Hypothesis profile enforces >=100 examples).
pytest

# In CI, use the derandomized profile:
HYPOTHESIS_PROFILE=ci pytest
```

Property tests must run at least **100 examples** each (design.md → Testing
Strategy). This floor is enforced in `conftest.py`: the session aborts if the
active Hypothesis profile lowers `max_examples` below 100, and any individual
property test that runs fewer than 100 examples is failed.

## Run the app

The fully-wired agent boots over Socket Mode once tokens are set in `loop/.env`:

```bash
python -m loop.slack_app
```

This starts all five agents, the APScheduler-driven Watcher sweep, the daily digest,
and the Slack interaction handlers. With `LOOP_DEMO_MODE=true`, the app loads the
seeded demo org into the live graph on boot (timestamps rebased to now) so the
dashboard, nudge, and PR-merge auto-heal are fully interactive on the demo data.

## Demo & submission

See **`DEMO.md`** for the complete demo bible: the canonical demo path, the Eraser AI
architecture prompt, a click-by-click + verbatim 3-minute video script, judge Q&A,
and the Devpost write-up.

## Notes

- `.env` and the SQLite store (`*.db`) are git-ignored; never commit secrets.
- All derived Slack data stays within the workspace trust boundary (Req 14.2).
