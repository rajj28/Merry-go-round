#!/usr/bin/env python3
"""THROWAWAY SPIKE — Slack Real-Time Search (RTS) query -> results round-trip.

Purpose (Task 1.2): prove that an authenticated RTS call returns candidate
messages and capture the response shape for the Watcher contract
(``loop/watcher/rts_contract.py``). This is a spike: minimal, assumption-heavy,
no production error handling beyond what proves the round-trip.

WHAT RTS ACTUALLY IS
--------------------
The Slack Real-Time Search API is exposed through the Web API method
``assistant.search.context`` (POST https://slack.com/api/assistant.search.context).
There is no separate "rts.*" endpoint — this method *is* RTS. See SETUP_RTS.md
for entitlement/scope details.

KEY ASSUMPTIONS (flagged because they are unverified without a live token):
  1. The sandbox app is entitled to RTS and has the search scopes granted
     (search:read.public + search:read.users, and the private/im/mpim variants
     for a user token). See SETUP_RTS.md.
  2. With a BOT token, ``assistant.search.context`` requires an ``action_token``
     taken from a triggering message/assistant event. A sweep driven by a timer
     (Req 2.1) has no such event, so for the periodic-sweep use case we assume a
     USER token (which does NOT require an action_token). This spike supports
     both: set SLACK_RTS_ACTION_TOKEN if you must use a bot token.
  3. Open-loop candidates are retrieved with a natural-language query so RTS uses
     semantic search. The query below is a first cut; tuning it for recall is
     Watcher work (Task 4.3), not this spike.

RUN
---
    # PowerShell
    $env:SLACK_RTS_TOKEN="xoxp-..."      # user token (preferred for sweeps)
    python loop/spikes/rts_spike.py
    python loop/spikes/rts_spike.py "what is blocked or waiting on a reply"

Importing this module does NOT require a token or the slack_sdk package — the
live call and the slack_sdk import happen only inside ``main()`` / ``run_sweep``.
"""

from __future__ import annotations

import json
import os
import sys

# --- make the sibling `loop` package importable when run as a script ----------
# (so `python loop/spikes/rts_spike.py` works from the repo root without install)
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# RTS endpoint, scopes and the candidate-retrieval query live here as constants
# so the Watcher (Task 4.1) can lift them straight out of the spike.
RTS_API_METHOD = "assistant.search.context"

# A high-recall, natural-language query phrased so RTS triggers semantic search.
# Intentionally broad: the Watcher optimizes for recall (Req 2.5); precision is
# the Adjudicator's job downstream.
DEFAULT_OPEN_LOOP_QUERY = (
    "messages where someone is waiting on a reply, blocked on someone, "
    "asked a question that was never answered, or promised to follow up"
)


def run_sweep(query: str) -> dict:
    """Make ONE authenticated RTS call and return the raw JSON response dict.

    Reads the token (and optional action_token) from the environment. Raises a
    clear RuntimeError if the token is missing so the spike fails loudly.
    """
    # Imported lazily so this module imports cleanly without slack_sdk installed.
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.environ.get("SLACK_RTS_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "No Slack token found. Set SLACK_RTS_TOKEN (a user token is preferred "
            "for timer-driven sweeps; see SETUP_RTS.md)."
        )

    action_token = os.environ.get("SLACK_RTS_ACTION_TOKEN")  # only for bot tokens

    client = WebClient(token=token)

    params = {
        "query": query,
        # Sweep the whole workspace the user can see — high recall (Req 2.1/2.5).
        "channel_types": "public_channel,private_channel,mpim,im",
        "content_types": "messages",
        "include_context_messages": True,
        "include_bots": False,
        "limit": 20,  # RTS hard max per page
    }
    if action_token:
        params["action_token"] = action_token

    try:
        # api_call works across slack_sdk versions regardless of whether a typed
        # `assistant_search_context` helper exists yet.
        response = client.api_call(RTS_API_METHOD, params=params)
    except SlackApiError as e:
        # Spike-level handling: surface the Slack error payload and re-raise.
        print(f"[rts_spike] Slack API error: {e.response.get('error')!r}", file=sys.stderr)
        raise

    # slack_sdk responses behave like dicts; normalize to a plain dict.
    return dict(response.data) if hasattr(response, "data") else dict(response)


def main(argv: list[str]) -> int:
    from loop.watcher.rts_contract import parse_rts_response, next_cursor

    query = argv[1] if len(argv) > 1 else DEFAULT_OPEN_LOOP_QUERY
    print(f"[rts_spike] querying RTS ({RTS_API_METHOD}) with:\n  {query!r}\n")

    response = run_sweep(query)

    # 1) Prove the round-trip: dump the raw JSON exactly as RTS returned it.
    print("=== RAW RTS RESPONSE ===")
    print(json.dumps(response, indent=2, default=str))

    if not response.get("ok", False):
        print(f"\n[rts_spike] RTS returned ok=false: {response.get('error')!r}",
              file=sys.stderr)
        return 1

    # 2) Prove the contract: normalize into the Watcher's CandidateMessage list.
    candidates = parse_rts_response(response)
    print(f"\n=== NORMALIZED CANDIDATES ({len(candidates)}) ===")
    for c in candidates:
        print(
            f"- channel={c.channel_id} ts={c.message_ts} "
            f"author={c.author_id} ({c.author_name}) bot={c.is_author_bot}\n"
            f"    text: {c.text[:120]!r}\n"
            f"    link: {c.permalink}"
        )

    cursor = next_cursor(response)
    print(f"\n[rts_spike] next_cursor={cursor!r} "
          f"({'more pages available' if cursor else 'last page'})")
    print("[rts_spike] round-trip OK ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
