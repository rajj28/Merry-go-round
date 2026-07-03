"""Seed the live Slack sandbox with a demo conversation (demo tooling, not a test).

Purpose: post a believable set of "open loop" messages into a demo channel so that
a **live** Real-Time Search sweep (assistant.search.context) surfaces real,
relevant results during the demo video — proving RTS is genuinely load-bearing,
not mocked.

This is operational tooling. It deliberately lives in ``loop/spikes/`` (not the
pure, network-free ``loop/seed/`` fixture package) because it makes live Slack
calls. The deterministic, judged demo dashboard still runs on ``loop/seed`` +
``LOOP_DEMO_MODE`` for reproducibility; this script feeds the *live RTS* portion.

What it posts (mirrors the personas in loop/seed/fixtures.py so the story is
consistent):

  * Three "blocked-on-you" messages FROM distinct personas (Alice, Bob, Carol),
    each @-mentioning the real demo user — these are what make "3 people are
    blocked on you" true. Posted via the bot token with a per-message
    ``username`` + ``icon_emoji`` override (needs the ``chat:write.customize``
    scope) so they read as different people.
  * Two "waiting-on-other" messages FROM the real user (Dana, Erin), posted via
    the user token so the author is genuinely you.

Prerequisites (one-time):
  1. In the sandbox, create a channel (e.g. ``#loop-demo``) and **invite the bot**
     (``/invite @Loop``) — the bot can only post to channels it is a member of.
  2. Put the channel ID (looks like ``C0123456789``) in the environment as
     ``LOOP_DEMO_CHANNEL`` (or pass ``--channel C…``). Find it via the channel
     details, or the URL when viewing the channel.
  3. Tokens already in ``loop/.env``: ``SLACK_BOT_TOKEN`` + ``SLACK_USER_TOKEN``.
     Re-install the app after adding ``chat:write.customize`` so persona posting
     works.

Usage (from the repo root):
    python -m loop.spikes.seed_sandbox --channel C0123456789
    # or set LOOP_DEMO_CHANNEL in .env and run:
    python -m loop.spikes.seed_sandbox

Add ``--dry-run`` to print what would be posted without calling Slack.

No secrets are printed. Re-running posts the messages again (Slack has no natural
idempotency key for chat.postMessage); use a fresh channel for a clean demo.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

from loop.config import get_settings


# --------------------------------------------------------------------------- #
# The demo conversation. Personas mirror loop/seed/fixtures.py for a consistent
# story across the deterministic dashboard and the live RTS sweep.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SeedMessage:
    """One demo message to post.

    as_user=True  -> posted with the USER token (authored genuinely by you):
                     the "waiting-on-other" loops where you asked someone.
    as_user=False -> posted with the BOT token + persona override (username/icon):
                     the "blocked-on-you" loops where someone is waiting on you.
    """

    text: str
    as_user: bool
    persona_name: Optional[str] = None
    persona_emoji: Optional[str] = None


def _conversation(user_mention: str) -> list[SeedMessage]:
    """Build the demo conversation given the real user's ``<@U…>`` mention."""
    return [
        # --- blocked-on-you ×3 (from distinct personas, @-mentioning you) -----
        SeedMessage(
            text=(
                f"Hey {user_mention}, when you get a sec — could you review the "
                "design doc I shared yesterday? I'm blocked on your sign-off before "
                "I can ship the new onboarding flow. 🙏"
            ),
            as_user=False,
            persona_name="Alice Nguyen",
            persona_emoji=":woman_technologist:",
        ),
        SeedMessage(
            text=(
                f"{user_mention} gentle nudge on my PR "
                "https://github.com/rajj28/loop-demo/pull/2 — it's been sitting "
                "~2 days and I can't merge without your review. Lmk if anything's "
                "unclear!"
            ),
            as_user=False,
            persona_name="Bob Martinez",
            persona_emoji=":man_technologist:",
        ),
        SeedMessage(
            text=(
                f"{user_mention} did you get a chance to look at the launch "
                "checklist? I need your approval on the rollout steps before we can "
                "schedule it for Thursday."
            ),
            as_user=False,
            persona_name="Carol Davis",
            persona_emoji=":woman_office_worker:",
        ),
        # --- waiting-on-other ×2 (authored genuinely by you, via user token) --
        SeedMessage(
            text=(
                "Hey Dana — any update on the refreshed mocks? Still waiting on "
                "those before I can wire up the dashboard. No rush, just tracking it."
            ),
            as_user=True,
        ),
        SeedMessage(
            text=(
                "Erin, can you confirm the deploy window for Thursday? Waiting on "
                "your 👍 before I tell the team."
            ),
            as_user=True,
        ),
    ]


# --------------------------------------------------------------------------- #
# Posting
# --------------------------------------------------------------------------- #
def _auth_user_id(token: str) -> str:
    """Return the authed user id for a token (used to @-mention the real you)."""
    from slack_sdk import WebClient

    resp = WebClient(token=token).auth_test()
    return resp.get("user_id", "")


def seed(channel: str, *, dry_run: bool = False) -> int:
    """Post the demo conversation to ``channel``. Returns the number posted."""
    settings = get_settings()
    settings.require("slack_bot_token", "slack_user_token")

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    bot = WebClient(token=settings.slack_bot_token)
    user = WebClient(token=settings.slack_user_token)

    user_id = _auth_user_id(settings.slack_user_token)
    mention = f"<@{user_id}>" if user_id else "@you"
    messages = _conversation(mention)

    posted = 0
    for i, msg in enumerate(messages, start=1):
        who = msg.persona_name if not msg.as_user else "you (user token)"
        label = f"[{i}/{len(messages)}] as {who}"
        if dry_run:
            print(f"DRY-RUN {label}: {msg.text}")
            posted += 1
            continue
        try:
            if msg.as_user:
                user.chat_postMessage(channel=channel, text=msg.text)
            else:
                bot.chat_postMessage(
                    channel=channel,
                    text=msg.text,
                    username=msg.persona_name,
                    icon_emoji=msg.persona_emoji,
                )
            print(f"posted {label}")
            posted += 1
            time.sleep(0.6)  # gentle pacing so the timeline ordering is natural
        except SlackApiError as exc:
            err = exc.response.get("error", "unknown_error")
            print(f"FAILED {label}: {err}", file=sys.stderr)
            if err in {"not_in_channel", "channel_not_found"}:
                print(
                    "  -> Invite the bot to the channel (/invite @Loop) and confirm "
                    "the channel ID. The bot can only post where it is a member.",
                    file=sys.stderr,
                )
            elif err == "missing_scope":
                print(
                    "  -> Add the missing scope (persona posting needs "
                    "chat:write.customize) and REINSTALL the app, then retry.",
                    file=sys.stderr,
                )
            return posted
    return posted


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Slack sandbox with a demo conversation.")
    parser.add_argument(
        "--channel",
        default=os.getenv("LOOP_DEMO_CHANNEL", ""),
        help="Target channel ID (e.g. C0123456789). Defaults to $LOOP_DEMO_CHANNEL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be posted without calling Slack.",
    )
    args = parser.parse_args()

    if not args.channel:
        print(
            "No channel given. Create a channel (e.g. #loop-demo), invite the bot "
            "(/invite @Loop), then pass --channel C… or set LOOP_DEMO_CHANNEL in .env.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    count = seed(args.channel, dry_run=args.dry_run)
    print(f"\nDone. {count} message(s) {'previewed' if args.dry_run else 'posted'}.")
    if not args.dry_run:
        print(
            "Next: run a live RTS sweep to confirm they surface —\n"
            "  python -c \"from loop.watcher.watcher import build_rts_client; "
            "from loop.watcher.rts_contract import parse_rts_response; "
            "print(len(parse_rts_response(build_rts_client()())), 'candidates')\""
        )


if __name__ == "__main__":
    main()
