"""Workspace-scoped configuration loading for Loop.

Loads environment variables from a local ``.env`` file (via python-dotenv) and
exposes them as a typed, validated ``Settings`` object. Keeping tokens in the
environment / ``.env`` honors the privacy posture: workspace-scoped tokens and
derived data stay within the user's trust boundary (Req 14.2).

Nothing here implements feature logic — it is scaffolding that the five agents
read their credentials and tuning knobs from.

Usage
-----
    from config import get_settings

    settings = get_settings()
    settings.require("slack_bot_token")  # raises if missing
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - python-dotenv not installed
    def load_dotenv(*_args, **_kwargs) -> bool:  # type: ignore[misc]
        return False


# Where Loop persists its embedded SQLite Obligation Graph by default.
_DEFAULT_DB_PATH = "loop.db"
# Watcher sweep interval must not exceed 60s (Req 2.1).
_DEFAULT_SWEEP_INTERVAL_SECONDS = 30


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _load_env_file() -> None:
    """Load ``.env`` from the project root if present (no-op otherwise)."""
    env_path = _project_root() / ".env"
    load_dotenv(dotenv_path=env_path, override=False)


@dataclass(frozen=True)
class Settings:
    """Resolved, workspace-scoped settings for Loop.

    Secrets default to empty strings so the app can boot for scaffolding/tests;
    call :meth:`require` before using a credential that must be present.
    """

    # --- Slack (workspace-scoped) -------------------------------------------
    slack_bot_token: str = ""        # xoxb-... bot token
    slack_app_token: str = ""        # xapp-... Socket Mode app-level token
    slack_user_token: str = ""       # xoxp-... user token: RTS (assistant.search.context)
                                     #          + posting Polite Nudges/delegations AS the user
    slack_signing_secret: str = ""   # request-signature verification

    # --- LLM provider selection (two-tier reasoning) ------------------------
    # Which backend serves the fast/filter and smart/precise tiers. The design is
    # provider-agnostic: "groq" routes through an OpenAI-compatible client (the
    # default, a free provider); "anthropic" preserves the original Claude path.
    llm_provider: str = "groq"  # "groq" | "anthropic"

    # --- Groq (OpenAI-compatible) reasoning tier ----------------------------
    # Primary key env var is GROK_API (falls back to GROQ_API_KEY). The two tiers
    # map to Groq models: fast/filter -> fast_model, smart/precise -> smart_model.
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    fast_model: str = "llama-3.1-8b-instant"      # fast/filter tier (Watcher)
    smart_model: str = "llama-3.3-70b-versatile"  # smart/precise tier (Adjudicator + nudge)

    # --- Claude reasoning tier (optional anthropic path) --------------------
    anthropic_api_key: str = ""
    haiku_model: str = "claude-haiku-4-5"
    opus_model: str = "claude-opus-4-8"

    # --- GitHub MCP (artifact verification) ---------------------------------
    github_mcp_url: str = ""
    github_mcp_token: str = ""

    # --- Storage & scheduling ------------------------------------------------
    database_path: str = _DEFAULT_DB_PATH
    sweep_interval_seconds: int = _DEFAULT_SWEEP_INTERVAL_SECONDS

    # --- Live-detection scoping ----------------------------------------------
    # Channel IDs the Watcher restricts live detection to (loaded from
    # LOOP_WATCH_CHANNELS). Empty tuple = no restriction (watch the whole
    # workspace). Used to scope a live demo to a single predictable channel.
    watch_channels: tuple[str, ...] = ()

    # --- Runtime mode --------------------------------------------------------
    # When true, the demo runs against the seeded deterministic workspace.
    demo_mode: bool = False

    # --- Branding (optional) -------------------------------------------------
    # An https URL for a small Loop logo image rendered above the App Home hero
    # header. Empty (the default) renders no logo block, keeping the hero header as
    # the first block. Loaded from LOOP_LOGO_URL.
    logo_url: str = ""

    # --- UI style (Block Kit row rendering) ----------------------------------
    # "sections" (default) renders obligation rows as section + image accessory +
    # actions; "cards" uses the newer Block Kit `card` block (avatar icon + title +
    # subtitle + ≤3 buttons) and adds an aging bar chart to Assistant query replies.
    # `ui_style` is the global default; `home_style` / `assistant_style` allow a
    # per-surface choice (e.g. cards on App Home, sections in the Assistant pane) and
    # each falls back to `ui_style` when its own env var is unset. Loaded from
    # LOOP_UI_STYLE / LOOP_HOME_STYLE / LOOP_ASSISTANT_STYLE. Kept opt-in so the proven
    # section layout stays the default until a live render check confirms cards.
    ui_style: str = "sections"
    home_style: str = "sections"
    assistant_style: str = "sections"

    # --- App Home layout -----------------------------------------------------
    # "stack" (default) renders the classic three stacked sections via
    # build_app_home_view. "compact" renders the command-center layout
    # (build_app_home_compact_view): a horizontal carousel of blocked-on-you cards,
    # compact one-line waiting rows, and tiny auto-closed context lines — far less
    # scrolling as the org scales. Loaded from LOOP_HOME_LAYOUT; kept opt-in so the
    # proven stacked layout stays the fallback until the new `carousel` block is
    # confirmed to render in the target workspace.
    home_layout: str = "stack"

    # Names that hold secrets and should never be logged verbatim.
    _secret_fields: tuple[str, ...] = field(
        default=(
            "slack_bot_token",
            "slack_app_token",
            "slack_user_token",
            "slack_signing_secret",
            "groq_api_key",
            "anthropic_api_key",
            "github_mcp_token",
        ),
        repr=False,
        compare=False,
    )

    def require(self, *names: str) -> None:
        """Raise ``RuntimeError`` if any named setting is missing/empty."""
        missing = [n for n in names if not getattr(self, n, "")]
        if missing:
            raise RuntimeError(
                "Missing required configuration: "
                + ", ".join(missing)
                + ". Set them in your environment or .env file."
            )

    def __repr__(self) -> str:  # redact secrets
        parts = []
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            value = getattr(self, f.name)
            if f.name in self._secret_fields:
                value = "***" if value else ""
            parts.append(f"{f.name}={value!r}")
        return f"Settings({', '.join(parts)})"


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _as_str_tuple(value: str | None) -> tuple[str, ...]:
    """Parse a comma-separated env value into a trimmed tuple (empty → ``()``).

    Whitespace around each entry is stripped and empty entries are dropped, so
    ``" C1 , , C2 "`` becomes ``("C1", "C2")`` and an unset/blank value yields the
    empty tuple (meaning "no restriction").
    """
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def load_settings() -> Settings:
    """Read the environment (after loading ``.env``) into a ``Settings``."""
    _load_env_file()
    _ui_style = os.getenv("LOOP_UI_STYLE", "sections")
    return Settings(
        slack_bot_token=os.getenv("SLACK_BOT_TOKEN", ""),
        slack_app_token=os.getenv("SLACK_APP_TOKEN", ""),
        slack_user_token=os.getenv("SLACK_USER_TOKEN", ""),
        slack_signing_secret=os.getenv("SLACK_SIGNING_SECRET", ""),
        llm_provider=os.getenv("LLM_PROVIDER", "groq"),
        # GROK_API is the primary key name (per the user's .env); fall back to the
        # conventional GROQ_API_KEY if that is what's set instead.
        groq_api_key=os.getenv("GROK_API", "") or os.getenv("GROQ_API_KEY", ""),
        groq_base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        fast_model=os.getenv("LOOP_FAST_MODEL", "llama-3.1-8b-instant"),
        smart_model=os.getenv("LOOP_SMART_MODEL", "llama-3.3-70b-versatile"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        haiku_model=os.getenv("HAIKU_MODEL", "claude-haiku-4-5"),
        opus_model=os.getenv("OPUS_MODEL", "claude-opus-4-8"),
        github_mcp_url=os.getenv("GITHUB_MCP_URL", ""),
        github_mcp_token=os.getenv("GITHUB_MCP_TOKEN", ""),
        database_path=os.getenv("LOOP_DATABASE_PATH", _DEFAULT_DB_PATH),
        sweep_interval_seconds=_as_int(
            os.getenv("LOOP_SWEEP_INTERVAL_SECONDS"),
            _DEFAULT_SWEEP_INTERVAL_SECONDS,
        ),
        watch_channels=_as_str_tuple(os.getenv("LOOP_WATCH_CHANNELS")),
        demo_mode=_as_bool(os.getenv("LOOP_DEMO_MODE"), False),
        logo_url=os.getenv("LOOP_LOGO_URL", ""),
        ui_style=_ui_style,
        home_style=os.getenv("LOOP_HOME_STYLE", "") or _ui_style,
        assistant_style=os.getenv("LOOP_ASSISTANT_STYLE", "") or _ui_style,
        home_layout=os.getenv("LOOP_HOME_LAYOUT", "stack"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached process-wide settings (loaded once)."""
    return load_settings()
