"""Live end-to-end rehearsal harness for the Loop demo (operational tooling).

This is **not** a test. It is the script the presenter runs repeatedly before
recording to confirm the *live* pipeline reliably lands on the intended loops
(e.g. "3 people are blocked on you"). It builds the REAL pipeline from config and
runs ONE full live cycle:

    build_rts_client            (real Slack RTS, assistant.search.context, user token)
        -> Watcher.run_sweep    (scoped to LOOP_WATCH_CHANNELS, fast-tier high-recall classify)
        -> AdjudicationQueue.drain
        -> Adjudicator           (real the smart tier "whose court is the ball in?")
        -> SqliteObligationGraph (fresh in-memory store, per rehearsal)
        -> App Home hero count + blocked-on-you rows (loop.action.app_home)

It then prints a concise, NON-SECRET report:

  * total candidates swept (returned by live RTS, before scoping/dedup/classify);
  * how many were forwarded to the Adjudicator;
  * how many obligations were written to the graph;
  * the App Home hero count (surfaced blocked-on-you obligations);
  * a one-line summary per surfaced blocked-on-you obligation (person + subject).

Requirements:
  * ``SLACK_USER_TOKEN`` (xoxp-…) — RTS runs on the user token (verified).
  * ``ANTHROPIC_API_KEY`` — the fast-tier classifier and the smart tier adjudicator.
If either is missing the script prints a clear message and exits non-zero.

Respects ``LOOP_WATCH_CHANNELS`` so a rehearsal only processes the demo channel.
No secrets are ever printed.

Usage (from the repo root):
    python -m loop.spikes.live_demo
or:
    python loop/spikes/live_demo.py

Render-check flags (eyeball the Block Kit in-client, fast):
    # publish the deterministic seeded App Home in CARD style to your App Home
    python -m loop.spikes.live_demo --seeded --style cards
    # also DM yourself the card + aging-chart assistant reply (Messages surface)
    python -m loop.spikes.live_demo --seeded --style cards --assistant
    # run the real live cycle, then publish its result in card style
    python -m loop.spikes.live_demo --style cards

``--seeded`` skips the live pipeline (no RTS / no LLM) and just renders the seeded
demo workspace, so it only needs ``SLACK_BOT_TOKEN`` + ``LOOP_USER_ID`` (the Slack
user id of the App Home viewer — that's you). It is the fastest way to confirm the
newer ``card`` / ``data_visualization`` blocks actually render in your workspace
before committing the demo to them.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Mapping, Optional

from loop.action.app_home import (
    blocked_on_you_rows,
    build_app_home_compact_view,
    build_app_home_view,
    hero_count,
)
from loop.adjudicator.adjudicator import (
    Adjudicator,
    AdjudicationOutcome,
    build_smart_reasoning_client,
)
from loop.config import get_settings
from loop.graph.models import utc_now_iso
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.pipeline import AdjudicationQueue
from loop.watcher.rts_contract import parse_rts_response
from loop.watcher.watcher import (
    build_fast_classify_client,
    build_rts_client,
)
from loop.watcher.watcher import Watcher


def _short(text: str, limit: int = 70) -> str:
    """Trim a string to ``limit`` chars for a tidy one-line report entry."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _resolved_user_id() -> str:
    """The tracked User's Slack id (``LOOP_USER_ID`` env, else a placeholder)."""
    return os.getenv("LOOP_USER_ID", "") or "U_LOOP_USER"


def _web_client(token: str) -> Any:
    """Build a Slack ``WebClient`` for the bot token (lazy import; ``None`` on failure)."""
    try:
        from slack_sdk import WebClient
    except Exception:  # noqa: BLE001
        print("slack_sdk is not installed — cannot publish to Slack.")
        return None
    return WebClient(token=token)


def _publish_home(view: Mapping[str, Any], viewer_id: str, client: Any) -> bool:
    """Publish a home view to ``viewer_id``'s App Home; return success (never raises)."""
    try:
        client.views_publish(user_id=viewer_id, view=view)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"views_publish failed: {exc}")
        return False


def _publish_targets_ready(settings: Any, viewer_id: str) -> Optional[str]:
    """Return an error string if we cannot publish (missing bot token / viewer id)."""
    if not settings.slack_bot_token:
        return "SLACK_BOT_TOKEN is not set — cannot publish to Slack."
    if not viewer_id or viewer_id == "U_LOOP_USER":
        return (
            "LOOP_USER_ID is not set — set it to YOUR Slack user id (the App Home "
            "viewer) so the view can be published to you."
        )
    return None


def run_seeded_render_check(style: Optional[str], with_assistant: bool) -> int:
    """Publish the deterministic seeded demo view to Slack so cards can be eyeballed.

    Skips the live pipeline entirely (no RTS, no LLM) — it renders the seeded demo
    workspace and publishes it to ``LOOP_USER_ID``'s App Home. When ``style`` is given
    it overrides both surfaces; otherwise each surface uses its own configured style
    (``home_style`` for the App Home, ``assistant_style`` for the DM). With
    ``with_assistant`` it also DMs that user the assistant QUERY_RESULT reply. Needs
    only the bot token + viewer id; avatars resolve best-effort (seeded placeholder ids
    render icon-less).
    """
    from loop.seed.fixtures import SEED_USER_ID
    from loop.seed.loader import SEED_NOW, SeededWorkspace

    settings = get_settings()
    viewer_id = _resolved_user_id()
    err = _publish_targets_ready(settings, viewer_id)
    if err:
        print(err)
        return 1

    client = _web_client(settings.slack_bot_token)
    if client is None:
        return 1

    from loop.app import AvatarResolver  # reuse the production best-effort resolver

    workspace = SeededWorkspace()
    graph = workspace.graph
    resolver = AvatarResolver()

    # Per-surface styles: an explicit --style overrides both; else use config.
    home_style = style or settings.home_style
    assistant_style = style or settings.assistant_style

    # Best-effort real avatars (seeded placeholder ids won't resolve → icon-less cards).
    from loop.action.app_home import waiting_on_other_rows

    people: set[str] = set()
    for o in blocked_on_you_rows(graph, SEED_NOW):
        people.add(o.owed_person_id)
    for o in waiting_on_other_rows(graph, SEED_NOW):
        people.add(o.owes_person_id)
    avatars = resolver.resolve({p for p in people if p}, client)
    names = resolver.resolve_names({p for p in people if p}, client)
    # Seeded persons (U_ALICE, …) aren't real workspace members, so live lookups
    # return nothing — fall back to the seeded org maps. The seeded avatar URLs are
    # real, reachable photos (pravatar), so the premium name-led cards render with
    # genuine avatars rather than broken images. Live-resolved values take precedence.
    if not avatars:
        from loop.seed.fixtures import seed_avatars
        avatars = seed_avatars()
    if not names:
        from loop.seed.fixtures import seed_names
        names = seed_names()

    if settings.home_layout == "compact":
        view = build_app_home_compact_view(
            graph, now=SEED_NOW, user_id=SEED_USER_ID, avatars=avatars, names=names,
            logo_url=settings.logo_url,
        )
    else:
        view = build_app_home_view(
            graph, now=SEED_NOW, user_id=SEED_USER_ID, avatars=avatars, names=names,
            logo_url=settings.logo_url, style=home_style,
        )

    print(f"Publishing seeded App Home (layout={settings.home_layout}, style={home_style}) to {viewer_id} ...")
    ok = _publish_home(view, viewer_id, client)
    if ok:
        print("  ✓ published. Open Loop's App Home in Slack to eyeball it.")
    else:
        print("  ✗ publish failed (see error above).")
        return 1

    if with_assistant:
        ok_dm = _post_seeded_assistant(graph, settings, viewer_id, client, assistant_style)
        if not ok_dm:
            return 1

    print("\nRender check complete.")
    return 0


def _post_seeded_assistant(
    graph: Any, settings: Any, viewer_id: str, client: Any, style: str
) -> bool:
    """DM the viewer a seeded QUERY_RESULT reply (card rows + aging chart)."""
    from loop.conversational.assistant_view import build_assistant_blocks, people_in_reply
    from loop.conversational.conversational_agent import (
        AssistantReply,
        ReplyKind,
        ToolUseTrace,
    )
    from loop.app import AvatarResolver
    from loop.seed.fixtures import SEED_USER_ID
    from loop.seed.loader import SEED_NOW

    blocked = blocked_on_you_rows(graph, SEED_NOW)
    trace = ToolUseTrace()
    trace.add("Obligation Graph", "query")
    reply = AssistantReply(
        kind=ReplyKind.QUERY_RESULT,
        text="Here are the loops blocked on you.",
        trace=trace,
        obligations=tuple(blocked),
    )
    avatars = AvatarResolver().resolve(people_in_reply(reply, SEED_USER_ID), client)
    cards = style == "cards"
    blocks = build_assistant_blocks(
        reply, now=SEED_NOW, user_id=SEED_USER_ID, avatars=avatars,
        style=style, chart=cards,
    )
    # Resolve the DM channel explicitly (more reliable than posting to a raw user id).
    channel = viewer_id
    try:
        opened = client.conversations_open(users=viewer_id)
        channel = (opened.get("channel") or {}).get("id") or viewer_id
    except Exception:  # noqa: BLE001 — fall back to the user id as channel.
        pass
    try:
        client.chat_postMessage(
            channel=channel,
            text="Here are the loops blocked on you.",
            blocks=blocks,
        )
        extra = " (card rows + aging chart)" if cards else " (section rows)"
        print(f"  ✓ DMed the assistant reply{extra}. Check your DMs.")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ chat_postMessage failed: {exc}")
        return False


def run_live_cycle(style: Optional[str] = None, publish: bool = False) -> int:
    """Run one full live detection→adjudication cycle and print the report.

    Returns a process exit code: ``0`` on a completed cycle, non-zero when the
    required credentials are missing.
    """
    settings = get_settings()

    # --- credential gate (Req: live cycle needs the user token + Anthropic key) ---
    missing = [
        name
        for name, present in (
            ("SLACK_USER_TOKEN", bool(settings.slack_user_token)),
            ("ANTHROPIC_API_KEY", bool(settings.anthropic_api_key)),
        )
        if not present
    ]
    if missing:
        print(
            "Cannot run the live rehearsal — missing required credentials: "
            + ", ".join(missing)
            + ".\nSet them in your environment or loop/.env and try again."
        )
        return 1

    user_id = _resolved_user_id()
    watch_channels = set(settings.watch_channels) or None

    scope_label = (
        ", ".join(sorted(watch_channels)) if watch_channels else "whole workspace"
    )
    print("Loop live rehearsal — one full real cycle. No secrets are printed.")
    print(f"  tracked user: {user_id}")
    print(f"  scope (LOOP_WATCH_CHANNELS): {scope_label}\n")

    # --- build the REAL pipeline against a fresh in-memory graph ---------------
    graph = SqliteObligationGraph(database_path=IN_MEMORY)
    adjudicator = Adjudicator(build_smart_reasoning_client(settings), graph)
    queue = AdjudicationQueue(adjudicator, user_id)

    # Wrap the real RTS client so we can also count the TOTAL candidates the live
    # sweep returned (before scoping/dedup/classify) for the report — one network call.
    real_rts = build_rts_client(settings)
    captured: dict[str, Mapping[str, Any]] = {}

    def _counting_rts() -> Mapping[str, Any]:
        response = real_rts()
        captured["response"] = response
        return response

    watcher = Watcher(
        graph,
        _counting_rts,
        classify=build_fast_classify_client(settings),
        forward=queue.enqueue,
        interval_seconds=settings.sweep_interval_seconds,
        watch_channels=watch_channels,
    )

    # --- run ONE live cycle: sweep -> drain into the Adjudicator ---------------
    print("Sweeping live RTS and adjudicating... (this calls Slack + Anthropic)\n")
    sweep = watcher.run_sweep()
    results = queue.drain()

    if not sweep.rts_ok:
        print(f"RTS sweep failed: {sweep.error}")
        print("Nothing was adjudicated. Check the user token / RTS entitlement.")
        return 1

    total_swept = len(parse_rts_response(captured.get("response", {})))
    forwarded = len(sweep.new_candidates)
    written = sum(
        1
        for r in results
        if r.outcome in (AdjudicationOutcome.CREATED, AdjudicationOutcome.UPDATED)
    )

    now = utc_now_iso()
    hero = hero_count(graph, now)
    blocked_rows = blocked_on_you_rows(graph, now)

    # --- concise report --------------------------------------------------------
    print("=== Live rehearsal report ===")
    print(f"  candidates swept (RTS):       {total_swept}")
    print(f"  forwarded to Adjudicator:     {forwarded}")
    print(f"  obligations written to graph: {written}")
    print(f"  duplicates skipped:           {sweep.duplicates_skipped}")
    print(f"  App Home hero count:          {hero}")
    print()
    if hero:
        print(f"{hero} {'person is' if hero == 1 else 'people are'} blocked on you:")
        for ob in blocked_rows:
            person = ob.owed_person_id or "(unknown)"
            print(f"   - {person}: {_short(ob.subject_summary) or '(no subject)'}")
    else:
        print("No one is blocked on you right now (no surfaced blocked-on-you loops).")
    print()

    # --- optional: publish the live result to App Home in the chosen style -----
    if publish:
        viewer_id = _resolved_user_id()
        err = _publish_targets_ready(settings, viewer_id)
        if err:
            print(f"Skipping publish — {err}")
        else:
            client = _web_client(settings.slack_bot_token)
            if client is not None:
                from loop.app import AvatarResolver
                from loop.action.app_home import waiting_on_other_rows

                home_style = style or settings.home_style
                people: set[str] = set()
                for ob in blocked_rows:
                    people.add(ob.owed_person_id)
                for ob in waiting_on_other_rows(graph, now):
                    people.add(ob.owes_person_id)
                _resolver = AvatarResolver()
                avatars = _resolver.resolve({p for p in people if p}, client)
                names = _resolver.resolve_names({p for p in people if p}, client)
                view = build_app_home_view(
                    graph, now=now, user_id=user_id, avatars=avatars, names=names,
                    logo_url=settings.logo_url, style=home_style,
                )
                print(f"Publishing live App Home (style={home_style}) to {viewer_id} ...")
                if _publish_home(view, viewer_id, client):
                    print("  ✓ published. Open Loop's App Home to eyeball it.")
        print()

    print("Cycle complete. Re-run to confirm the live pipeline reproduces it.")
    return 0


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Loop live rehearsal + Block Kit render-check harness.",
    )
    parser.add_argument(
        "--style",
        choices=("sections", "cards"),
        default=None,
        help="Row rendering style to publish (default: LOOP_UI_STYLE / sections).",
    )
    parser.add_argument(
        "--seeded",
        action="store_true",
        help="Skip the live pipeline; publish the deterministic seeded view (fast eyeball).",
    )
    parser.add_argument(
        "--assistant",
        action="store_true",
        help="With --seeded, also DM the card + aging-chart assistant reply.",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Live mode only: run the cycle and report, but do not publish to Slack.",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    if args.seeded:
        sys.exit(run_seeded_render_check(style=args.style, with_assistant=args.assistant))
    sys.exit(run_live_cycle(style=args.style, publish=not args.no_publish))


if __name__ == "__main__":
    main()
