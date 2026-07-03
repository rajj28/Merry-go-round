"""Loop — minimal Bolt-for-Python app (Socket Mode).

Task 1.1 scaffolding: boots from environment tokens, connects over Socket Mode,
and responds to ``app_home_opened`` with a placeholder App Home view.

Design context: this is the single Bolt process that will later host the Action
and Conversational agents (see design.md, "Deployment shape"). For now it only
proves the Slack wiring works end to end on the sandbox workspace.

Environment variables (never hardcode tokens — Req 14 privacy posture):
    SLACK_BOT_TOKEN   Bot token,        starts with ``xoxb-``
    SLACK_APP_TOKEN   App-level token,  starts with ``xapp-`` (Socket Mode)

Run once tokens are supplied (see loop/SETUP_SLACK.md):
    python -m loop.slack_app

The module is import-safe: importing it does NOT require slack-bolt to be
installed and does NOT open any network connection. The Slack client is only
constructed inside ``build_app`` / ``main``. This lets tests import the pure
view-building logic (e.g. ``build_home_view``) without live credentials.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("loop.slack_app")

# Environment variable names for the two tokens the app boots from.
BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"
APP_TOKEN_ENV = "SLACK_APP_TOKEN"


def build_home_view() -> dict[str, Any]:
    """Return the placeholder App Home view published on ``app_home_opened``.

    A single header block, per task 1.1. The real dashboard (hero banner +
    Blocked-On-You / Waiting-On-Other / Auto-Healed sections) replaces this in
    task 9 (Req 6). Kept pure (no Slack SDK import) so it is trivially testable.
    """
    return {
        "type": "home",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Loop — coming online", "emoji": True},
            }
        ],
    }


def handle_app_home_opened(event: dict[str, Any], client: Any, logger: logging.Logger) -> None:
    """Publish the placeholder App Home view for the user who opened the tab.

    Bolt injects ``event``, ``client``, and ``logger`` by name. Req 6.1: render
    a view when the App Home opens (placeholder for now).
    """
    user_id = event.get("user")
    if not user_id:
        logger.warning("app_home_opened event missing 'user' field: %s", event)
        return
    try:
        client.views_publish(user_id=user_id, view=build_home_view())
    except Exception:  # noqa: BLE001 — log and swallow so one bad publish can't crash the socket loop
        logger.exception("Failed to publish App Home view for user %s", user_id)


def register_handlers(app: Any) -> None:
    """Register all Slack event/interaction handlers on a Bolt ``App``.

    Separated from construction so handlers can be unit-tested against a fake
    app and so future agents can plug their handlers in here.
    """
    app.event("app_home_opened")(handle_app_home_opened)


def _require_env(name: str) -> str:
    """Read a required token from the environment or fail with a clear message."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            f"Set it in your .env (see loop/SETUP_SLACK.md). Do not hardcode tokens."
        )
    return value


def build_app() -> Any:
    """Construct the Bolt ``App`` from the bot token and register handlers.

    Imports slack-bolt lazily so this module is import-safe without the package
    installed and without live tokens (the import/handlers can be tested in
    isolation). Requires ``SLACK_BOT_TOKEN`` at call time.
    """
    from slack_bolt import App  # lazy import — keeps module import dependency-free

    bot_token = _require_env(BOT_TOKEN_ENV)
    app = App(token=bot_token)
    register_handlers(app)
    return app


def main() -> None:
    """Boot Loop over Socket Mode.

    As of task 17.1 the live entry point is the fully-wired composition root in
    :mod:`loop.app` (Bolt handlers + in-process APScheduler + detection pipeline).
    This thin shim delegates there so the historical ``python -m loop.slack_app``
    invocation keeps working while all wiring lives in one place. The task-1.1
    helpers above (``build_home_view``, ``register_handlers``, ``build_app``) remain
    for the scaffolding tests and as a dependency-free Slack smoke surface.
    """
    from loop.app import main as app_main  # lazy import — keep this module light

    app_main()


if __name__ == "__main__":
    main()
