"""RTS access probe (task 1.2 spike) — does the sandbox support Real-Time Search?

Run this once after installing the Loop app to the sandbox. It answers the single
question that validates-or-changes the Watcher design:

    Can Loop call the Real-Time Search (RTS) API — assistant.search.context —
    on this sandbox, and with which token?

It is deliberately dependency-free (standard library only) and **never prints any
token**. It reads tokens from the environment or from loop/.env, calls a few
read-only Slack Web API methods, and prints a compact, NON-SECRET report you can
paste back.

Tokens read (from env or loop/.env; none are printed):
    SLACK_BOT_TOKEN   (xoxb-…)   bot token
    SLACK_USER_TOKEN  (xoxp-…)   user token (optional but recommended)

Usage (Windows cmd, from the repo root):
    python -m loop.spikes.rts_probe
or:
    python loop/spikes/rts_probe.py

What it checks:
    1. auth.test            — confirms each token is valid + which team/user it is.
    2. assistant.search.info — reports is_ai_search_enabled (the RTS gate).
    3. assistant.search.context — the actual RTS call, attempted with the bot
       token and (if present) the user token. Captures whether it needs an
       action_token, a missing scope, or returns results.

Nothing here writes to Slack or to the graph; all calls are read-only.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

SLACK_API = "https://slack.com/api/"

# Methods we probe, in order. Each is a read-only Web API call.
AUTH_TEST = "auth.test"
SEARCH_INFO = "assistant.search.info"
SEARCH_CONTEXT = "assistant.search.context"

# A harmless natural-language query — phrasing it as a question is what triggers
# Slack's semantic (RTS) search per the docs.
PROBE_QUERY = "what is someone waiting on me to do?"


# --------------------------------------------------------------------------- #
# Token loading (env first, then loop/.env) — values are never printed.
# --------------------------------------------------------------------------- #
def _load_env_file() -> None:
    """Load loop/.env into os.environ if present (without overriding real env)."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        # Strip optional surrounding quotes.
        if value and value[0] in {'"', "'"} and value[-1:] == value[0]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _redact(token: Optional[str]) -> str:
    """Describe a token by prefix only, never revealing its value."""
    if not token:
        return "(absent)"
    prefix = token.split("-", 1)[0]
    return f"present ({prefix}-…, len={len(token)})"


# --------------------------------------------------------------------------- #
# Minimal Slack Web API client (stdlib only).
# --------------------------------------------------------------------------- #
def _call(method: str, token: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """POST to a Slack Web API method with a bearer token; return the parsed JSON.

    Uses application/x-www-form-urlencoded (the universally accepted Web API
    content type). Network/transport errors are returned as a synthetic error dict
    so the probe always prints a result rather than crashing.
    """
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(
        SLACK_API + method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — report transport errors, don't crash.
        return {"ok": False, "error": f"transport_error: {exc!r}"}


def _summarize(method: str, result: dict[str, Any]) -> dict[str, Any]:
    """Pull the NON-SECRET, decision-relevant fields out of a response."""
    summary: dict[str, Any] = {
        "method": method,
        "ok": result.get("ok"),
        "error": result.get("error"),
        "warning": result.get("warning"),
        "needed": result.get("needed"),       # scopes Slack says are needed
        "provided": result.get("provided"),   # scopes the token actually has
        "response_keys": sorted(k for k in result.keys() if k != "ok"),
    }
    if method == AUTH_TEST:
        summary["team"] = result.get("team")
        summary["is_enterprise_install"] = result.get("is_enterprise_install")
    if method == SEARCH_INFO:
        # The RTS gate. True => semantic (real-time) search is available.
        summary["is_ai_search_enabled"] = result.get("is_ai_search_enabled")
    if method == SEARCH_CONTEXT:
        results = result.get("results") or {}
        if isinstance(results, dict):
            summary["results_buckets"] = sorted(results.keys())
            summary["results_counts"] = {
                k: (len(v) if isinstance(v, list) else "n/a")
                for k, v in results.items()
            }
        else:
            summary["results_type"] = type(results).__name__
    return summary


def _probe_token(label: str, token: Optional[str]) -> None:
    """Run the three probes against one token and print a compact report."""
    print(f"\n=== {label}: {_redact(token)} ===")
    if not token:
        print("  (skipped — token not set)")
        return

    auth = _call(AUTH_TEST, token)
    print("  " + json.dumps(_summarize(AUTH_TEST, auth)))
    if not auth.get("ok"):
        print("  -> token invalid/unusable; skipping search probes for this token.")
        return

    info = _call(SEARCH_INFO, token)
    print("  " + json.dumps(_summarize(SEARCH_INFO, info)))

    # The real RTS call. We try WITHOUT an action_token (a bot token is documented
    # to require one from a triggering event; a user token may not). The error
    # surface here is exactly what tells us how the Watcher must be wired.
    context = _call(SEARCH_CONTEXT, token, {"query": PROBE_QUERY})
    print("  " + json.dumps(_summarize(SEARCH_CONTEXT, context)))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Probe / watch Slack Real-Time Search access.")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll a live RTS sweep until seeded demo loops appear (indexing wait).",
    )
    parser.add_argument(
        "--channel",
        default=os.environ.get("LOOP_DEMO_CHANNEL", ""),
        help="Seed channel ID to watch for (defaults to $LOOP_DEMO_CHANNEL).",
    )
    parser.add_argument(
        "--interval", type=int, default=60, help="Seconds between polls in --watch mode."
    )
    parser.add_argument(
        "--max-minutes", type=int, default=30, help="Give up after this many minutes."
    )
    args = parser.parse_args()

    _load_env_file()
    if args.watch:
        _watch_for_seed(args.channel, interval=args.interval, max_minutes=args.max_minutes)
        return

    bot = os.environ.get("SLACK_BOT_TOKEN")
    user = os.environ.get("SLACK_USER_TOKEN")

    print("Loop RTS access probe — read-only; no tokens are printed.")
    print(f"  bot token:  {_redact(bot)}")
    print(f"  user token: {_redact(user)}")

    _probe_token("BOT TOKEN", bot)
    _probe_token("USER TOKEN", user)

    print(
        "\nPaste everything above back to your teammate. Key signals:\n"
        "  • assistant.search.info -> is_ai_search_enabled: true|false (the RTS gate)\n"
        "  • assistant.search.context -> ok:true with results = RTS works on this token\n"
        "  • error 'missing_scope' (+ needed/provided) = add that scope and reinstall\n"
        "  • an action_token / not_allowed error on the BOT token = RTS is user/event-driven"
    )


# Seeded demo phrases (mirror loop/spikes/seed_sandbox.py) used to detect that the
# seeded conversation has been indexed and is now retrievable via live RTS.
_SEED_KEYWORDS = ["design doc", "widgets#42", "launch checklist", "mocks", "deploy window"]


def _watch_for_seed(channel: str, *, interval: int, max_minutes: int) -> None:
    """Poll the wired RTS client until the seeded demo loops are searchable.

    Slack's AI search does not index freshly-posted messages instantly. This polls a
    live ``assistant.search.context`` sweep (the same call the Watcher uses) on a
    fixed cadence and reports when the seeded conversation surfaces — either by the
    seed ``channel`` id appearing in results, or by a seeded phrase matching. Use it
    to know the sandbox is demo-ready before recording.
    """
    import time

    from loop.watcher.rts_contract import parse_rts_response
    from loop.watcher.watcher import build_rts_client

    client = build_rts_client()
    deadline = time.time() + max_minutes * 60
    attempt = 0
    print(
        f"Watching live RTS for the seeded demo loops "
        f"(channel={channel or 'any'}, every {interval}s, up to {max_minutes}m)...\n"
    )
    while time.time() < deadline:
        attempt += 1
        candidates = parse_rts_response(client())
        by_channel = [c for c in candidates if channel and c.channel_id == channel]
        by_keyword = [
            c for c in candidates
            if any(k.lower() in c.text.lower() for k in _SEED_KEYWORDS)
        ]
        hits = by_channel or by_keyword
        stamp = time.strftime("%H:%M:%S")
        print(
            f"[{stamp}] attempt {attempt}: total={len(candidates)} "
            f"seed-channel={len(by_channel)} seed-keyword={len(by_keyword)}"
        )
        if hits:
            print("\nSeeded demo loops are now indexed and retrievable via live RTS:")
            for c in hits[:5]:
                who = c.author_name or c.author_id
                print(f"   - {who}: {c.text[:70]}")
            print("\nThe sandbox is demo-ready for a live RTS sweep.")
            return
        time.sleep(interval)

    print(
        f"\nGave up after {max_minutes}m — seeded loops not yet indexed. "
        "Indexing can take a while on a fresh channel; try again later, and rely on "
        "LOOP_DEMO_MODE for the deterministic dashboard beats in the meantime."
    )


if __name__ == "__main__":
    main()
