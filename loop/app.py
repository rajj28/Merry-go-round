"""Loop composition root — wires the whole agent together (task 17.1).

This is the monolith-with-workers assembly described in design.md → "Deployment
shape": a single Slack Bolt process that hosts the Slack event/interaction
handlers and the Action + Conversational agents, an in-process APScheduler driving
the ≤60s Watcher sweep and the daily digest, and the embedded SQLite Obligation
Graph — all inside the workspace trust boundary (Req 14.2).

What this module wires (and the requirements each wiring satisfies):

  * **Detection pipeline (Req 2.1).** The Watcher's ``forward`` seam is bound to an
    :class:`~loop.pipeline.AdjudicationQueue`, so a swept/observed candidate flows
    Watcher → work queue → Adjudicator → Obligation Graph. APScheduler triggers
    :meth:`Watcher.run_sweep` every ``settings.sweep_interval_seconds`` (validated
    ≤ 60s by the Watcher itself).

  * **Daily digest (Req 11.1).** APScheduler triggers
    :meth:`ActionAgent.send_daily_digest` once per day; the agent enforces the
    at-most-one-per-24h rule itself (Req 11.5).

  * **Slack surfaces.** ``app_home_opened`` publishes the real App Home view
    (task 9 builder); per-row Nudge / Snooze / Delegate / Dismiss buttons and the
    Assistant-pane messages route into the Action and Conversational agents.

  * **Autonomy boundaries (Req 13).** Sensing, adjudication, verification,
    auto-close, and digest run autonomously with no confirmation (Req 13.1). Every
    path that *sends a message as the user* (Polite Nudge, delegation) is withheld
    behind a one-tap confirmation with a **24-hour** confirmation timeout
    (Req 13.2): a :class:`ConfirmationRegistry` holds the pending send until the
    user confirms (send), declines (cancel — Req 13.6), or the 24h timeout elapses
    (retain unsent — Req 13.5). A scheduler job sweeps expired confirmations.

Import-safety (read before editing): importing this module pulls in **no** network
SDK. ``slack_bolt`` and ``apscheduler`` are imported lazily inside :meth:`start`
(and the scheduler builder), and the Anthropic / Slack / GitHub-MCP clients are the
existing lazily-wired ports. This keeps the wiring unit-testable without any live
credential, SDK, or socket — the tests drive :class:`LoopApp` through its pure
handler methods and a fake Bolt app.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from loop.action.action_agent import ActionAgent, NudgeDraft
from loop.action.app_home import (
    ACTION_CYCLE_SEND,
    ACTION_DELEGATE,
    ACTION_DEMO_RESET,
    ACTION_DISMISS,
    ACTION_NUDGE,
    ACTION_QUICK_NUDGE,
    ACTION_REVIEW_BLOCKED,
    ACTION_ROW_OVERFLOW,
    ACTION_SCHEDULE,
    ACTION_SNOOZE,
    aging_chip,
    auto_healed_rows,
    blocked_on_you_rows,
    build_app_home_compact_view,
    build_app_home_view,
    build_composer_status_modal,
    build_cycle_modal,
    build_nudge_modal,
    meeting_calendar_url,
    nudge_recipient_id,
    BLOCKED_SECTION_DESCRIPTOR,
    SPOTLIGHT_EYEBROW,
    NUDGE_MODAL_CALLBACK,
    NUDGE_MODAL_INPUT_ACTION,
    NUDGE_MODAL_INPUT_BLOCK,
    resolved_person_id,
    slack_date,
    waiting_on_other_rows,
)
from loop.adjudicator.adjudicator import Adjudicator
from loop.config import Settings, get_settings
from loop.conversational.assistant_view import (
    ACTION_ASSISTANT_CONFIRM,
    ACTION_ASSISTANT_DECLINE,
    build_assistant_blocks,
    people_in_reply,
)
from loop.conversational.conversational_agent import ConversationalAgent
from loop.graph.models import (
    ClosureKind,
    LoopState,
    Obligation,
    ObligationId,
    UserId,
    utc_now_iso,
)
from loop.graph.sqlite_store import SqliteObligationGraph
from loop.learn.feedback import LearnEngine
from loop.pipeline import AdjudicationQueue
from loop.seed.fixtures import seed_avatars, seed_names
from loop.verifier.verifier import Verifier
from loop.watcher.watcher import Watcher

logger = logging.getLogger("loop.app")

# Req 13.2 / 13.5: a message-as-the-user confirmation is withheld until the user
# confirms, declines, or this 24-hour timeout elapses. (The Conversational pane
# uses a tighter 60s in-conversation timeout — Req 12.7 — owned by that agent.)
CONFIRM_TIMEOUT = timedelta(hours=24)

# Block Kit action_ids for the one-tap confirm / decline of a send-as-user action.
ACTION_CONFIRM_SEND = "loop_confirm_send"
ACTION_DECLINE_SEND = "loop_decline_send"
# One-tap Undo for the reversible autonomous actions (dismiss / snooze). Posted on
# the confirmation DM so a destructive-feeling action is never a dead end.
ACTION_UNDO_DISMISS = "loop_undo_dismiss"
ACTION_UNDO_SNOOZE = "loop_undo_snooze"
# The in-channel meeting proposal's one-tap confirm. Anyone on the thread can
# confirm; a confirmed meeting heals its loop autonomously (Auto-Healed feed).
ACTION_MEETING_CONFIRM = "loop_meeting_confirm"


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a tz-aware UTC ``datetime`` (shared coercion)."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Avatar resolution (the premium lever) — best-effort Slack users.info lookup
# ---------------------------------------------------------------------------
class AvatarResolver:
    """Resolves ``person_id → https avatar URL`` via Slack ``users.info`` (best-effort).

    The pure Block Kit builders never touch the network; this resolver is the wiring
    seam that fetches the ``image_72`` profile avatar for the handful of people about
    to be rendered, caches them per process, and is **best-effort** — any error (no
    client, SDK failure, missing field) yields no URL for that person, so rendering
    degrades gracefully to no-avatar rather than ever breaking.

    It is lazy/optional: when no ``client`` is supplied it returns an empty map, so
    tests and offline preview generation never hit the network.
    """

    def __init__(self, *, image_field: str = "image_72") -> None:
        self._cache: dict[str, str] = {}
        self._name_cache: dict[str, str] = {}
        self._field = image_field

    def _profile(self, person_id: str, client: Any) -> dict:
        """Fetch (and cache) a user's profile dict; never raises (``{}`` on failure)."""
        try:
            resp = client.users_info(user=person_id)
            return (resp.get("user") or {}).get("profile") or {}
        except Exception:  # noqa: BLE001 — profile lookups are best-effort.
            logger.debug("profile lookup failed for %s", person_id, exc_info=True)
            return {}

    def resolve(self, person_ids, client: Any) -> dict[str, str]:
        """Return ``{person_id: url}`` for those with a resolvable avatar (best-effort)."""
        if client is None:
            return {}
        out: dict[str, str] = {}
        for pid in person_ids:
            if not pid:
                continue
            if pid in self._cache:
                url = self._cache[pid]
            else:
                prof = self._profile(pid, client)
                url = prof.get(self._field) or ""
                self._cache[pid] = url
                # Opportunistically warm the name cache from the same response.
                if pid not in self._name_cache:
                    self._name_cache[pid] = (
                        prof.get("display_name") or prof.get("real_name") or ""
                    )
            if url:
                out[pid] = url
        return out

    def resolve_names(self, person_ids, client: Any) -> dict[str, str]:
        """Return ``{person_id: display name}`` (best-effort; cached, never raises).

        Prefers the Slack ``display_name``, falling back to ``real_name``. Used to make
        a card's title the person's name (e.g. "Alice") instead of a long, truncating
        subject sentence. Any failure / missing client yields no entry for that person.
        """
        if client is None:
            return {}
        out: dict[str, str] = {}
        for pid in person_ids:
            if not pid:
                continue
            if pid not in self._name_cache:
                prof = self._profile(pid, client)
                self._name_cache[pid] = (
                    prof.get("display_name") or prof.get("real_name") or ""
                )
                if pid not in self._cache:
                    self._cache[pid] = prof.get(self._field) or ""
            name = self._name_cache[pid]
            if name:
                out[pid] = name
        return out

    def _fetch(self, person_id: str, client: Any) -> str:
        """Fetch one avatar URL; never raises (returns "" on any failure)."""
        return self._profile(person_id, client).get(self._field) or ""


# ---------------------------------------------------------------------------
# Pending send-as-user confirmations (the 24h autonomy gate — Req 13.2/13.5/13.6)
# ---------------------------------------------------------------------------
@dataclass
class PendingSend:
    """A message-as-the-user action awaiting the user's one-tap confirm (Req 13.2).

    Held per-user between presenting the confirmation prompt and the user's
    confirm/decline (or the 24h timeout). While this exists, **nothing has been
    sent** — the send-as-user step has not run.

    Attributes:
        kind: ``"nudge"`` or ``"delegate"`` — which send-as-user action is pending.
        created_at: ISO 8601 UTC instant the prompt was presented; anchors the 24h
            confirmation timeout (Req 13.5).
        nudge_draft: the drafted :class:`NudgeDraft` to send on confirm (nudge only).
        obligation: the target obligation (delegate carries it for the send/owner
            reassignment; nudge carries it inside the draft).
        teammate_id: the delegation recipient (delegate only, Req 10.6).
    """

    kind: str
    created_at: str
    nudge_draft: Optional[NudgeDraft] = None
    obligation: Optional[Obligation] = None
    teammate_id: Optional[str] = None


class ConfirmationRegistry:
    """Per-user store of pending send-as-user confirmations with a 24h timeout.

    This is the in-process realization of the autonomy boundary for actions that
    speak as the user (Req 13.2): a pending send is added when the prompt is shown,
    popped on confirm/decline, and swept when the 24h timeout elapses (Req 13.5).

    ``now`` is injectable purely so the timeout is deterministic in tests.
    """

    def __init__(self, *, now: Callable[[], str] = utc_now_iso) -> None:
        self._now = now
        self._pending: dict[UserId, PendingSend] = {}

    def add(self, user: UserId, pending: PendingSend) -> None:
        """Record a pending send-as-user confirmation for ``user`` (replaces any prior)."""
        self._pending[user] = pending

    def get(self, user: UserId) -> Optional[PendingSend]:
        """Return the user's pending confirmation, or None."""
        return self._pending.get(user)

    def pop(self, user: UserId) -> Optional[PendingSend]:
        """Remove and return the user's pending confirmation, or None."""
        return self._pending.pop(user, None)

    def has_pending(self, user: UserId) -> bool:
        """True iff ``user`` has a send-as-user action awaiting confirmation."""
        return user in self._pending

    def is_expired(self, pending: PendingSend, now_iso: Optional[str] = None) -> bool:
        """True iff ``pending`` has sat unconfirmed past the 24h timeout (Req 13.5)."""
        now = _parse_iso_utc(now_iso or self._now())
        return now - _parse_iso_utc(pending.created_at) >= CONFIRM_TIMEOUT

    def sweep_expired(self, now_iso: Optional[str] = None) -> list[tuple[UserId, PendingSend]]:
        """Drop every confirmation past its 24h timeout; return the dropped ones.

        Req 13.5: on timeout the message is not sent and the action is retained in
        its unsent state — which here means simply discarding the pending send
        without ever invoking the send-as-user port. The dropped entries are returned
        so the caller can notify each user that the prompt expired.
        """
        now = now_iso or self._now()
        expired = [
            (user, pending)
            for user, pending in self._pending.items()
            if self.is_expired(pending, now)
        ]
        for user, _ in expired:
            self._pending.pop(user, None)
        return expired


# ---------------------------------------------------------------------------
# Outcome value objects for the interaction handlers (testable, Slack-free)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HandlerResult:
    """The outcome of a Slack interaction handler, as a pure value.

    The Bolt handlers translate this into ``views_publish`` / ``chat_postMessage``
    calls; the value object keeps the wiring logic testable without Slack. ``text``
    is the user-facing message (when any) and ``requires_confirmation`` flags a
    send-as-user action now waiting on the 24h one-tap confirm (Req 13.2). ``blocks``
    optionally carries a rich Block Kit rendering (the Assistant pane, Req 12); when
    present the Bolt handlers post it with ``text`` as the accessibility fallback.
    """

    text: str = ""
    requires_confirmation: bool = False
    sent: bool = False
    cancelled: bool = False
    obligation: Optional[Obligation] = None
    blocks: Optional[list[dict]] = None


class LoopApp:
    """The wired Loop application — agents + pipeline + scheduler + Slack handlers.

    Construct via :func:`build_loop_app` (which wires the lazy real ports from
    config) or directly with injected agents in tests. The class owns:

      * the shared :class:`SqliteObligationGraph` (single source of truth);
      * the five agents (Watcher, Adjudicator, Verifier, Action, Conversational)
        plus the :class:`LearnEngine`;
      * the :class:`AdjudicationQueue` connecting Watcher → Adjudicator;
      * the :class:`ConfirmationRegistry` enforcing the 24h send-as-user gate.

    :meth:`start` boots Socket Mode and the scheduler; the individual handler
    methods (:meth:`open_home`, :meth:`handle_nudge_click`, …) hold the wiring logic
    and are exercised directly by tests.
    """

    def __init__(
        self,
        *,
        graph: SqliteObligationGraph,
        watcher: Watcher,
        adjudicator: Adjudicator,
        verifier: Verifier,
        action: ActionAgent,
        conversational: ConversationalAgent,
        learn: LearnEngine,
        queue: AdjudicationQueue,
        user_id: UserId,
        settings: Optional[Settings] = None,
        now: Callable[[], str] = utc_now_iso,
        send_on_behalf: Optional[Callable[[str, str], None]] = None,
        send_as_member: Optional[Callable[[UserId, str, str], bool]] = None,
    ) -> None:
        self.graph = graph
        self.watcher = watcher
        self.adjudicator = adjudicator
        self.verifier = verifier
        self.action = action
        self.conversational = conversational
        self.learn = learn
        self.queue = queue
        self.user_id = user_id
        self._settings = settings or get_settings()
        self._now = now
        # Posting ports for members other than the workspace's primary token
        # owner. ``send_as_member`` posts AS a member who granted their own
        # xoxp (LOOP_USER_TOKENS), returning False when no token is held;
        # ``send_on_behalf`` is the attributed bot fallback, because posting
        # *as* someone requires that person's own token.
        self._send_as_member = send_as_member
        self._send_on_behalf = send_on_behalf
        self.confirmations = ConfirmationRegistry(now=now)
        self._avatars = AvatarResolver()
        self._scheduler: Any = None
        self._slack_app: Any = None
        # Deadlock intelligence: the smart-tier cycle-break client is built
        # lazily (None => stalest-edge heuristic), and plans are cached per ring
        # so a home refresh never re-runs reasoning for a deadlock it has
        # already planned.
        self._cycle_client_built = False
        self._cycle_client: Any = None
        self._cycle_plan_cache: dict[tuple[str, ...], Any] = {}
        # Rich block types this workspace has refused (learned from the
        # views.publish error at runtime). Each type degrades independently, so
        # e.g. a workspace without `data_visualization` still gets the native
        # `data_table` feed.
        self._rich_unsupported: set[str] = set()
        # Members with a live App Home — everyone who has opened the Home tab at
        # least once. A background graph change proactively republishes the Home
        # for the affected members in this set, so a newly-caught loop appears
        # without the viewer having to reopen the tab. Bounded by real openers,
        # so we never call views.publish for non-app users (seeded personas,
        # third-party mentions) or hammer a rate-limited workspace.
        self._home_viewers: set[UserId] = set()

    # ==================================================================
    # App Home (Req 6) — publish the real dashboard view
    # ==================================================================
    def open_home(self, user_id: UserId, client: Any) -> None:
        """Publish the App Home dashboard for ``user_id`` (Req 6.1).

        Builds the pure Block Kit view from the current graph contents (task 9
        builder) and publishes it. Any publish failure is logged and swallowed so a
        single bad publish cannot crash the socket loop.
        """
        # Remember this member has a live Home so background graph changes can
        # proactively refresh it (see ``_refresh_home_for``).
        self._home_viewers.add(user_id)
        try:
            now = self._now()
            chain_report, break_plans, edges = self._chain_intelligence()
            # Resolve names/avatars for everyone the intelligence layer renders:
            # deadlock rings and chain-pressure samples.
            ring_people: set[str] = set()
            for plan in break_plans:
                ring_people.update(plan.cycle.people)
            for edge in edges:
                ring_people.add(edge.owes_person_id)
                ring_people.add(edge.owed_person_id)
            avatars = self._home_avatars(now, client, ring_people, viewer=user_id)
            names = self._home_names(now, client, ring_people, viewer=user_id)
            # The viewer's own display name for the greeting eyebrow (best-effort;
            # cached). Falls back to the names map if they were also a counterparty.
            viewer_name = names.get(user_id)
            if not viewer_name and client is not None:
                try:
                    viewer_name = self._avatars.resolve_names({user_id}, client).get(user_id)
                except Exception:  # noqa: BLE001 — the greeting is cosmetic.
                    viewer_name = None
            if self._settings.home_layout == "compact":
                view = build_app_home_compact_view(
                    self.graph,
                    now=now,
                    user_id=user_id,
                    avatars=avatars,
                    names=names,
                    logo_url=self._settings.logo_url,
                )
                client.views_publish(user_id=user_id, view=view)
                return

            def _build(rich: bool) -> dict:
                return build_app_home_view(
                    self.graph,
                    now=now,
                    user_id=user_id,
                    avatars=avatars,
                    names=names,
                    viewer_name=viewer_name,
                    logo_url=self._settings.logo_url,
                    style=self._settings.home_style,
                    chain_report=chain_report,
                    break_plans=break_plans,
                    learn_threshold=self.graph.get_threshold(),
                    show_impact=True,
                    demo_controls=self._settings.demo_mode,
                    rich=rich,
                    rich_disabled=frozenset(self._rich_unsupported),
                )

            rich = bool(self._settings.rich_blocks)
            # Diagnostic: prove exactly what we hand Slack for this user's Home.
            _classic = _build(False)
            _txt = str(_classic)
            logger.info(
                "publishing App Home for %s: layout=%s blocks=%d greeting=%s spotlight=%s descriptors=%s",
                user_id,
                self._settings.home_layout,
                len(_classic.get("blocks", [])),
                bool(viewer_name),
                SPOTLIGHT_EYEBROW in _txt,
                BLOCKED_SECTION_DESCRIPTOR in _txt,
            )
            try:
                client.views_publish(user_id=user_id, view=_build(rich))
            except Exception as exc:  # noqa: BLE001 — rich blocks may be unsupported here.
                if not rich:
                    raise
                # Failure-safe publish, in two steps. First, adapt: the
                # views.publish error names each refused type ("unsupported
                # type: data_visualization"), so learn those, remember them for
                # the session, and republish rich minus exactly those types —
                # a workspace that refuses charts can still get the native
                # data_table. If the error names nothing (or the retry also
                # fails), fall back to the proven classic layout so the Home
                # tab can never go dark.
                newly = _unsupported_block_types(exc) - self._rich_unsupported
                if newly:
                    self._rich_unsupported |= newly
                    logger.warning(
                        "workspace refused rich block types %s; republishing without them",
                        sorted(newly),
                    )
                    try:
                        client.views_publish(user_id=user_id, view=_build(rich))
                        return
                    except Exception as retry_exc:  # noqa: BLE001
                        exc = retry_exc
                logger.warning(
                    "rich Block Kit view rejected; republishing the classic layout: %s",
                    exc,
                )
                client.views_publish(user_id=user_id, view=_build(False))
        except Exception:  # noqa: BLE001
            logger.exception("failed to publish App Home view for %s", user_id)

    # ==================================================================
    # Deadlock intelligence (chains + cycle-break plans)
    # ==================================================================
    def _chain_intelligence(self) -> tuple[Any, list[Any], list[Any]]:
        """The chain report, one break plan per ring, and the active edge list.

        Every failure is contained (an analysis error renders the classic view
        rather than crashing the publish), and plans are cached per ring so the
        smart tier reasons about each deadlock exactly once. The edge list
        feeds the rendered workspace map.
        """
        from loop.graph.chains import active_edges, analyze
        from loop.graph.store import ObligationFilter

        try:
            edges = active_edges(self.graph.query(ObligationFilter()))
            report = analyze(edges)
        except Exception:  # noqa: BLE001 — intelligence is additive, never fatal.
            logger.exception("chain analysis failed; rendering the classic home view")
            return None, [], []

        from loop.action.cycle_breaker import plan_cycle_break

        plans: list[Any] = []
        for cycle in report.cycles:
            key = tuple(e.obligation_id for e in cycle.edges)
            plan = self._cycle_plan_cache.get(key)
            if plan is None:
                plan = plan_cycle_break(cycle, client=self._get_cycle_client())
                self._cycle_plan_cache[key] = plan
            plans.append(plan)
        return report, plans, edges

    def _get_cycle_client(self) -> Any:
        """The smart-tier cycle-break Reason port, built lazily; None => heuristic."""
        if not self._cycle_client_built:
            self._cycle_client_built = True
            try:
                from loop.action.cycle_breaker import build_cycle_break_client

                self._cycle_client = build_cycle_break_client(self._settings)
            except Exception:  # noqa: BLE001 — missing credential is not fatal.
                logger.warning(
                    "smart-tier cycle-break client unavailable; "
                    "deadlock plans will use the stalest-edge heuristic"
                )
                self._cycle_client = None
        return self._cycle_client

    def prepare_cycle_modal(
        self,
        obligation_id: ObligationId,
        actor: Optional[UserId] = None,
        client: Any = None,
    ) -> tuple[Optional[dict], str]:
        """The deadlock first-move composer: pre-filled, editable, gated.

        Same transparent-agent flow as the nudge composer (Req 13.2): the
        smart-tier draft is shown editable in a modal, a :class:`PendingSend`
        is registered, and only the explicit "Send as you" submit posts it —
        through the same send path, to the break edge's debtor. ``actor`` is
        whoever clicked; only someone *inside* the ring can make the first
        move. Returns ``(view, "")`` or ``(None, message)`` when the plan is
        gone.
        """
        actor = actor or self.user_id
        plan = next(
            (
                p
                for p in self._cycle_plan_cache.values()
                if p.break_obligation_id == obligation_id
            ),
            None,
        )
        if plan is None:
            return None, "That deadlock has changed — reopen the Home tab for a fresh plan."
        if actor not in set(plan.cycle.people):
            return None, (
                "Only someone inside this deadlock can make the first move."
            )
        edge = plan.break_edge
        self.confirmations.add(
            actor,
            PendingSend(
                kind="nudge",
                created_at=self._now(),
                nudge_draft=NudgeDraft(
                    obligation=edge,
                    text=plan.draft_message,
                    channel=edge.owes_person_id,
                ),
            ),
        )
        # Human names in the modal copy (the ring, "First move", the footer) —
        # live-resolved from Slack, with the seeded names as the demo fallback;
        # any resolution failure degrades to raw ids, never an error.
        names: dict[str, str] = {}
        if self._settings.demo_mode:
            names.update(seed_names())
        try:
            names.update(self._avatars.resolve_names(set(plan.cycle.people), client))
        except Exception:  # noqa: BLE001 — names are cosmetic; ids still work.
            logger.exception("cycle modal name resolution failed")
        return build_cycle_modal(plan, names=names or None), ""

    def _home_avatars(
        self,
        now: str,
        client: Any,
        extra_people: Optional[set[str]] = None,
        viewer: Optional[str] = None,
    ) -> dict[str, str]:
        """Best-effort avatar map for the people about to be rendered on the App Home.

        Gathers the counterparties across the three sections — plus any
        ``extra_people`` from the intelligence layer (deadlock rings, chains)
        — and resolves their avatars via :class:`AvatarResolver` (cached,
        best-effort). Any failure yields an empty map so the view still renders
        without images.
        """
        people = self._home_people(now, viewer) | (extra_people or set())
        resolved = self._avatars.resolve(people, client)
        # In demo mode the seeded counterparties are not real Slack users, so a
        # live users.info lookup returns nothing. Fall back to the seeded avatar
        # map (real, reachable photos) so the dashboard still renders premium
        # avatars; any genuinely-resolved live URL takes precedence.
        if self._settings.demo_mode:
            merged = {pid: url for pid, url in seed_avatars().items() if pid in people}
            merged.update(resolved)
            return merged
        return resolved

    def _home_people(self, now: str, viewer: Optional[str] = None) -> set[str]:
        """The set of counterparties rendered across the three App Home sections.

        ``viewer`` is whoever opened the Home tab — every member gets their own
        dashboard; ``None`` falls back to the tracked user (scheduled refreshes).
        """
        viewer = viewer or self.user_id
        people: set[str] = set()
        for o in blocked_on_you_rows(self.graph, now, viewer):
            if o.owed_person_id:
                people.add(o.owed_person_id)
        for o in waiting_on_other_rows(self.graph, now, viewer):
            if o.owes_person_id:
                people.add(o.owes_person_id)
        for o in auto_healed_rows(self.graph, now, viewer):
            person = resolved_person_id(o, viewer)
            if person:
                people.add(person)
        return people

    def _home_names(
        self,
        now: str,
        client: Any,
        extra_people: Optional[set[str]] = None,
        viewer: Optional[str] = None,
    ) -> dict[str, str]:
        """Best-effort ``person_id → display name`` map for the App Home people.

        Lets the card layout lead with the person's name (e.g. "Alice") instead of a
        long, truncating subject. Best-effort: any failure yields an empty map, so the
        cards gracefully fall back to the subject-as-title rendering. Includes any
        ``extra_people`` from the intelligence layer so deadlock rings render
        human names rather than raw Slack ids.
        """
        people = self._home_people(now, viewer) | (extra_people or set())
        names = self._avatars.resolve_names(people, client)
        # Demo-mode fallback: seeded people aren't real Slack users, so use the
        # seeded display names (live-resolved names take precedence).
        if self._settings.demo_mode:
            merged = {pid: nm for pid, nm in seed_names().items() if pid in people}
            merged.update(names)
            return merged
        return names

    def _refresh_home(self, client: Any) -> None:
        """Re-publish the tracked user's App Home (after an autonomous graph change)."""
        if client is None:
            return
        self.open_home(self.user_id, client)

    @staticmethod
    def _affected_members(results: list[Any]) -> set[UserId]:
        """The edge endpoints a drain touched — the people whose Home changed.

        Only CREATED/UPDATED results carry an obligation; DISCARDED/ERROR ones
        do not, so they contribute nothing.
        """
        members: set[UserId] = set()
        for result in results:
            obligation = getattr(result, "obligation", None)
            if obligation is None:
                continue
            if obligation.owes_person_id:
                members.add(obligation.owes_person_id)
            if obligation.owed_person_id:
                members.add(obligation.owed_person_id)
        return members

    def _refresh_home_for(self, members: set[UserId], client: Any) -> None:
        """Proactively re-publish App Home for every affected member with a live Home.

        After a background graph change (detection sweep or a live message), push
        a fresh dashboard to each edge endpoint that has opened the app at least
        once — so a newly-caught loop shows up without the viewer reopening the
        tab. The tracked user is always refreshed (prior behaviour); other
        members are refreshed only when they're known Home viewers, so we never
        call ``views.publish`` for people who aren't app users and we touch just
        the one or two people actually on a changed edge — cheap even on a
        rate-limited workspace. Each publish is best-effort (``open_home``
        swallows failures).
        """
        if client is None:
            return
        targets = {self.user_id} | (members & self._home_viewers)
        logger.info(
            "refreshing App Home for %d member(s) after graph change: %s",
            len(targets),
            ", ".join(sorted(targets)),
        )
        for user_id in targets:
            self.open_home(user_id, client)

    # ==================================================================
    # Row actions (Req 6 buttons → Action Agent / Learn)
    # ==================================================================
    def handle_nudge_click(
        self, obligation_id: ObligationId, actor: Optional[UserId] = None
    ) -> HandlerResult:
        """Draft a Polite Nudge and present the 24h one-tap confirm (Req 7.1, 7.2, 13.2).

        Drafting is autonomous (no send), but the *send* is withheld behind a one-tap
        confirmation: on a successful draft a :class:`PendingSend` is registered and a
        confirmation-required result is returned. Nothing is posted as the user yet
        (Req 7.2, 13.2). A draft failure sends nothing and reports the failure
        (Req 7.8).
        """
        actor = actor or self.user_id
        obligation = self.graph.get(obligation_id)
        if obligation is None:
            return HandlerResult(text="That loop no longer exists.")

        draft_result = self.action.draft_polite_nudge(obligation)
        if not draft_result.drafted or draft_result.draft is None:
            return HandlerResult(text=draft_result.message or "Drafting failed; nothing was sent.")

        self.confirmations.add(
            actor,
            PendingSend(kind="nudge", created_at=self._now(), nudge_draft=draft_result.draft),
        )
        return HandlerResult(
            text=(
                f"Draft nudge:\n>{draft_result.draft.text}\n\n"
                "Tap Confirm to send this as you, or Decline to cancel."
            ),
            requires_confirmation=True,
            obligation=obligation,
        )

    def prepare_nudge_modal(
        self, obligation_id: ObligationId, actor: Optional[UserId] = None
    ) -> tuple[Optional[dict], str]:
        """Draft a Polite Nudge and return the editable composer **modal** (Best-UX).

        The modal flow replaces the blind one-tap: it shows the AI-drafted message
        pre-filled and editable so the user sees and approves exactly what will be
        posted as them. Drafting is autonomous (no send); a :class:`PendingSend` is
        registered so the same 24h gate / send path backs the modal submission.

        ``actor`` is whoever clicked (defaults to the tracked user). Any member
        may nudge a loop **owed to them** — the pending send is keyed to the
        actor so their own submit (and nobody else's) releases it.

        Returns ``(view, "")`` on a successful draft, or ``(None, message)`` when the
        loop is gone or drafting failed — the caller DMs ``message`` (Req 7.8).
        """
        actor = actor or self.user_id
        obligation = self.graph.get(obligation_id)
        if obligation is None:
            return None, "That loop no longer exists."
        if actor not in (obligation.owed_person_id, obligation.owes_person_id):
            return None, (
                "This loop is between "
                f"<@{obligation.owes_person_id}> and <@{obligation.owed_person_id}> — "
                "only they can act on it."
            )

        draft_result = self.action.draft_polite_nudge(obligation)
        if not draft_result.drafted or draft_result.draft is None:
            return None, (
                draft_result.message or "Couldn't draft a nudge right now; nothing was sent."
            )

        self.confirmations.add(
            actor,
            PendingSend(kind="nudge", created_at=self._now(), nudge_draft=draft_result.draft),
        )
        return build_nudge_modal(obligation, draft_result.draft.text), ""

    def handle_nudge_modal_submit(
        self, user_id: UserId, obligation_id: ObligationId, edited_text: str
    ) -> HandlerResult:
        """Send the (possibly edited) nudge after the composer-modal submit (Req 7.3).

        Pops the pending draft, applies the user's edited text (falling back to the
        original draft when the box is left untouched), and sends it as the user via
        the real Verifier-gated send path. An expired pending send is treated as the
        24h timeout: nothing is sent (Req 13.5).
        """
        pending = self.confirmations.get(user_id)
        if pending is None or pending.nudge_draft is None:
            return HandlerResult(text="That nudge is no longer pending; nothing was sent.")

        if self.confirmations.is_expired(pending):
            self.confirmations.pop(user_id)
            return HandlerResult(
                text="That nudge expired after 24 hours; nothing was sent.", cancelled=True
            )

        self.confirmations.pop(user_id)
        base = pending.nudge_draft
        text = (edited_text or "").strip() or base.text

        if user_id != self.user_id:
            # Posting *as* a member uses their own xoxp when they granted one
            # (LOOP_USER_TOKENS); otherwise the bot posts the approved text on
            # their behalf, clearly attributed (Req 13.2 still holds either
            # way: they saw and approved exactly this text).
            try:
                sent_as_self = bool(
                    self._send_as_member
                    and self._send_as_member(user_id, base.channel, text)
                )
                if not sent_as_self:
                    if self._send_on_behalf is None:
                        return HandlerResult(
                            text="Sending for other members isn't configured yet; "
                            "nothing was sent."
                        )
                    self._send_on_behalf(base.channel, f"From <@{user_id}>:\n{text}")
            except Exception:  # noqa: BLE001
                logger.exception("member nudge send failed")
                return HandlerResult(text="The nudge could not be sent.")
            healed = base.obligation
            if healed is not None:
                self.graph.upsert(
                    healed.model_copy(update={"last_touch_timestamp": self._now()})
                )
            return HandlerResult(
                text="Sent as you." if sent_as_self else "Sent on your behalf.",
                sent=True,
                obligation=healed,
                blocks=self._sent_nudge_blocks(healed, text, base.channel),
            )

        draft = NudgeDraft(obligation=base.obligation, text=text, channel=base.channel)
        send = self.action.send_polite_nudge(draft, confirmed=True)
        if send.cancelled:
            return HandlerResult(text=send.message or "Nudge cancelled.", cancelled=True)
        if not send.sent:
            return HandlerResult(text=send.error or "The nudge could not be sent.")
        return HandlerResult(
            text="Sent as you.",
            sent=True,
            obligation=send.obligation,
            blocks=self._sent_nudge_blocks(send.obligation, text, base.channel),
        )

    def _sent_nudge_blocks(
        self, obligation: Optional[Obligation], message: str, channel: str
    ) -> list[dict]:
        """A professional "nudge sent" confirmation card: status + quoted message + who/where/when."""
        person = obligation.owed_person_id if obligation else None
        ctx = "Sent as you"
        if person:
            ctx += f" to <@{person}>"
        if channel:
            ctx += f" · in <#{channel}>"
        ctx += f" · {slack_date(self._now())}"
        return _confirmation_blocks("*Nudge sent*", quote=message, context=ctx)

    def handle_delegate_click(
        self, obligation_id: ObligationId, teammate_id: str
    ) -> HandlerResult:
        """Present the delegation confirmation prompt and gate the send (Req 10.1, 13.2).

        Builds the confirmation prompt (teammate identity + obligation summary) and
        registers a pending send; nothing is sent and the obligation is unchanged
        until the user confirms (Req 10.1, 10.2).
        """
        obligation = self.graph.get(obligation_id)
        if obligation is None:
            return HandlerResult(text="That loop no longer exists.")

        prompt = self.action.delegation_prompt(obligation, teammate_id)
        self.confirmations.add(
            self.user_id,
            PendingSend(
                kind="delegate",
                created_at=self._now(),
                obligation=obligation,
                teammate_id=teammate_id,
            ),
        )
        return HandlerResult(text=prompt, requires_confirmation=True, obligation=obligation)

    def handle_snooze_click(
        self, obligation_id: ObligationId, duration: Optional[timedelta] = None
    ) -> HandlerResult:
        """Snooze an obligation — autonomous, no confirmation (Req 13.1, 13.3).

        Snoozing is a safe internal action: it routes straight to the Action Agent
        with no send-as-user gate.
        """
        obligation = self.graph.get(obligation_id)
        if obligation is None:
            return HandlerResult(text="That loop no longer exists.")
        result = self.action.snooze(obligation, duration)
        if not result.snoozed:
            return HandlerResult(text="Snooze could not be saved; please try again.")
        ob = result.obligation
        oid = ob.obligation_id if ob else obligation_id
        msg = f"Snoozed until {result.snoozed_until}."
        ctx_parts: list[str] = []
        if ob and ob.subject_summary:
            ctx_parts.append(ob.subject_summary)
        if ob and ob.owed_person_id:
            ctx_parts.append(f"<@{ob.owed_person_id}>")
        ctx_parts.append(f"hidden until {slack_date(result.snoozed_until)}")
        return HandlerResult(
            text=msg,
            obligation=ob,
            blocks=_confirmation_blocks(
                "*Snoozed*",
                context=" · ".join(ctx_parts),
                undo_action=ACTION_UNDO_SNOOZE,
                oid=oid,
            ),
        )

    def handle_dismiss_click(self, obligation_id: ObligationId) -> HandlerResult:
        """Dismiss an obligation via the Learn loop — autonomous (Req 5.2, 13.1).

        Records negative feedback and sets the dismissed state through the
        :class:`LearnEngine`; on a record failure the prior state is retained and the
        user is told it was not saved (Req 5.7, 14.7).
        """
        result = self.learn.record_dismiss(obligation_id)
        if not result.saved:
            return HandlerResult(text=result.message or "Dismissal was not saved.")
        ob = self.graph.get(obligation_id)
        ctx_parts: list[str] = []
        if ob and ob.subject_summary:
            ctx_parts.append(ob.subject_summary)
        if ob and ob.owed_person_id:
            ctx_parts.append(f"<@{ob.owed_person_id}>")
        msg = "Dismissed — you won't see this loop again."
        return HandlerResult(
            text=msg,
            blocks=_confirmation_blocks(
                "*Dismissed*",
                context=" · ".join(ctx_parts) or None,
                undo_action=ACTION_UNDO_DISMISS,
                oid=obligation_id,
            ),
        )

    def handle_undo_dismiss(self, obligation_id: ObligationId) -> HandlerResult:
        """Reverse a dismissal — restore the loop to the dashboard (Undo, Req 6).

        Dismiss is reversible, so the confirmation DM offers a one-tap Undo. This
        clears the ``dismissed`` flag and re-publishes the home; the prior negative
        feedback event is left as-is (it only nudged the threshold), so Undo is a
        clean visibility restore.
        """
        from loop.graph.store import is_ok

        obligation = self.graph.get(obligation_id)
        if obligation is None:
            return HandlerResult(text="That loop no longer exists.")
        restored = obligation.model_copy(update={"dismissed": False})
        if not is_ok(self.graph.upsert(restored)):
            return HandlerResult(text="Couldn't restore that loop; please try again.")
        return HandlerResult(
            text="Restored — this loop is back on your dashboard.",
            obligation=restored,
            blocks=_confirmation_blocks("*Restored*", context=restored.subject_summary or None),
        )

    def handle_undo_snooze(self, obligation_id: ObligationId) -> HandlerResult:
        """Reverse a snooze — bring the loop back immediately (Undo, Req 13).

        Clears ``snoozed_until`` so the shared surfacing predicate resumes showing the
        loop right away, then re-publishes the home.
        """
        from loop.graph.store import is_ok

        obligation = self.graph.get(obligation_id)
        if obligation is None:
            return HandlerResult(text="That loop no longer exists.")
        restored = obligation.model_copy(update={"snoozed_until": None})
        if not is_ok(self.graph.upsert(restored)):
            return HandlerResult(text="Couldn't un-snooze that loop; please try again.")
        return HandlerResult(
            text="Un-snoozed — this loop is back on your dashboard.",
            obligation=restored,
            blocks=_confirmation_blocks("*Un-snoozed*", context=restored.subject_summary or None),
        )

    # ==================================================================
    # Meeting loops — Schedule it → in-thread confirm → autonomous heal
    # ==================================================================
    def handle_schedule_click(self, obligation_id: ObligationId, client: Any) -> HandlerResult:
        """Kick off scheduling for a meeting loop (the *Schedule it* button).

        The button itself is a link button that already opened the prefilled
        Google Calendar event in the user's browser; this handler does the agentic
        half: it posts a confirmation proposal into the loop's **source thread**
        (bot message — proposing a time is safe, so no send-as-user gate) with a
        one-tap Confirm. When the counterparty confirms, the loop heals
        autonomously (:meth:`handle_meeting_confirm`).
        """
        obligation = self.graph.get(obligation_id)
        if obligation is None:
            return HandlerResult(text="That loop no longer exists.")
        if not obligation.due_at:
            return HandlerResult(text="No proposed time is attached to this meeting yet.")

        counterparty = nudge_recipient_id(obligation)
        when = slack_date(obligation.due_at)
        proposal = (
            f"<@{counterparty}> — proposing *{obligation.subject_summary}* "
            f"on {when}. One tap to lock it in."
        )
        posted = False
        if client is not None and obligation.source_msg_channel:
            try:
                client.chat_postMessage(
                    channel=obligation.source_msg_channel,
                    thread_ts=obligation.source_msg_ts or None,
                    text=proposal,
                    blocks=_meeting_proposal_blocks(obligation, proposal),
                )
                posted = True
            except Exception:  # noqa: BLE001 — a failed post is reported, not raised.
                logger.exception(
                    "failed to post meeting proposal for %s", obligation_id
                )
        if not posted:
            return HandlerResult(
                text=(
                    "Couldn't post the proposal to the source channel — "
                    "the calendar event is still in your browser."
                )
            )
        ctx = f"<@{counterparty}> · {when} · in <#{obligation.source_msg_channel}>"
        return HandlerResult(
            text=f"Proposal posted — the loop closes itself when <@{counterparty}> confirms.",
            obligation=obligation,
            blocks=_confirmation_blocks(
                "*Meeting proposed*",
                context=ctx + " · confirms heal this loop automatically",
            ),
        )

    def handle_meeting_confirm(self, obligation_id: ObligationId) -> HandlerResult:
        """Heal a meeting loop after the in-thread one-tap confirm.

        The confirmation is the verified artifact-state for a meeting (the
        counterparty said yes), so the closure is **autonomous**: the loop lands in
        the Auto-Healed feed with the confirmed time as the closure reason.
        """
        from loop.graph.store import is_ok

        obligation = self.graph.get(obligation_id)
        if obligation is None:
            return HandlerResult(text="That loop no longer exists.")
        if obligation.loop_state == LoopState.HEALED:
            return HandlerResult(text="This meeting is already confirmed.")
        now = self._now()
        when = slack_date(obligation.due_at) if obligation.due_at else "the proposed time"
        healed = obligation.model_copy(
            update={
                "loop_state": LoopState.HEALED,
                "closure_kind": ClosureKind.AUTONOMOUS,
                "closure_reason": f"Meeting confirmed for {when}",
                "closure_timestamp": now,
                "last_touch_timestamp": now,
            }
        )
        if not is_ok(self.graph.upsert(healed)):
            return HandlerResult(text="Couldn't record the confirmation; please try again.")
        return HandlerResult(
            text=f"Meeting confirmed for {when} — Loop closed this loop for you.",
            obligation=healed,
            blocks=_confirmation_blocks(
                "*Meeting confirmed*",
                context=f"{healed.subject_summary} · {when} · closed automatically",
            ),
        )

    def handle_review_blocked(self, user_id: UserId) -> HandlerResult:
        """Summarize the loops currently blocked on the user as a DM body (Req 6.1).

        Autonomous and read-only: reuses :func:`blocked_on_you_rows` against the live
        graph (the same surfacing gate the App Home hero uses) to build a concise,
        oldest→newest summary. ``user_id`` is accepted for symmetry with the other
        handlers; the tracked user's graph is the single source of truth. Returns the
        message body as a value so the Bolt handler can DM it without this method
        touching Slack.
        """
        return self._review_blocked_result(user_id)

    def open_review_modal(self, user_id: UserId, trigger_id: str, client: Any) -> None:
        """Open the "Blocked on you" summary modal for the user (Req 6.1).

        The Review button's on-screen result: instead of DMing a summary (which lands
        in the Loop DM and is easy to miss), this pops a modal open immediately. Any
        failure is logged and swallowed so a bad open can't crash the socket loop.
        """
        if client is None or not trigger_id:
            return
        from loop.action.app_home import build_review_modal

        try:
            view = build_review_modal(self.graph, self._now(), user_id)
            client.views_open(trigger_id=trigger_id, view=view)
        except Exception:  # noqa: BLE001
            logger.exception("failed to open review-blocked modal for %s", user_id)

    def _review_blocked_result(self, user_id: UserId) -> HandlerResult:
        """Summarize the loops blocked on the user as a :class:`HandlerResult`.

        Retained as the text/blocks summary shared by tests and any DM fallback;
        the live Review button renders it on-screen via :meth:`open_review_modal`.
        """
        rows = blocked_on_you_rows(self.graph, self._now(), user_id)
        if not rows:
            return HandlerResult(
                text="You're all caught up — nobody's blocked on you.",
                blocks=_confirmation_blocks(
                    "*You're all caught up*",
                    context="Nobody's blocked on you right now.",
                ),
            )
        lines = [f"*{len(rows)}* loop{'s' if len(rows) != 1 else ''} blocked on you right now:"]
        for o in rows:
            chip = aging_chip(o, self._now())
            lines.append(
                f"• {chip.text}  *{o.subject_summary}* — <@{o.owed_person_id}> is waiting"
            )
        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{len(rows)} blocked on you",
                    "emoji": True,
                },
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines[1:])}},
        ]
        return HandlerResult(text="\n".join(lines), blocks=blocks)

    # ==================================================================
    # The 24h send-as-user confirmation gate (Req 13.2, 13.5, 13.6)
    # ==================================================================
    def handle_confirm_send(self, user_id: UserId) -> HandlerResult:
        """Complete a pending send-as-user action after the one-tap confirm (Req 13.2).

        If the confirm arrives inside the 24h window the message is sent (nudge →
        :meth:`ActionAgent.send_polite_nudge`, delegate → :meth:`ActionAgent.delegate`,
        both with ``confirmed=True``). A confirm arriving after the 24h timeout is
        treated as expired: nothing is sent and the action stays unsent (Req 13.5).
        """
        pending = self.confirmations.get(user_id)
        if pending is None:
            return HandlerResult(text="There's nothing waiting to confirm.")

        if self.confirmations.is_expired(pending):
            self.confirmations.pop(user_id)
            return HandlerResult(
                text="That confirmation expired after 24 hours; nothing was sent.",
                cancelled=True,
            )

        self.confirmations.pop(user_id)
        if pending.kind == "nudge" and pending.nudge_draft is not None:
            draft_text = pending.nudge_draft.text
            channel = pending.nudge_draft.channel
            send = self.action.send_polite_nudge(pending.nudge_draft, confirmed=True)
            if send.cancelled:
                return HandlerResult(text=send.message or "Nudge cancelled.", cancelled=True)
            if not send.sent:
                return HandlerResult(text=send.error or "The nudge could not be sent.")
            return HandlerResult(
                text="Sent as you.",
                sent=True,
                obligation=send.obligation,
                blocks=self._sent_nudge_blocks(send.obligation, draft_text, channel),
            )

        if pending.kind == "delegate" and pending.obligation is not None:
            result = self.action.delegate(
                pending.obligation, pending.teammate_id or "", confirmed=True
            )
            if not result.delegated:
                return HandlerResult(text=result.error or "Delegation was not completed.")
            return HandlerResult(
                text=f"Delegated to {result.teammate_id}.",
                sent=True,
                obligation=result.obligation,
                blocks=_confirmation_blocks(
                    "*Delegated*",
                    context=f"to <@{result.teammate_id}> · {slack_date(self._now())}",
                ),
            )

        return HandlerResult(text="Nothing to send.")

    def handle_decline_send(self, user_id: UserId) -> HandlerResult:
        """Cancel a pending send-as-user action on an explicit decline (Req 13.6).

        Leaves the obligation unchanged and tells the user nothing was sent.
        """
        pending = self.confirmations.pop(user_id)
        if pending is None:
            return HandlerResult(text="There's nothing waiting to confirm.")
        return HandlerResult(
            text="Cancelled — no message was sent and nothing changed.", cancelled=True
        )

    def sweep_confirmation_timeouts(self, client: Any = None) -> list[UserId]:
        """Drop send-as-user confirmations past their 24h timeout (Req 13.5).

        Intended to be driven by the scheduler. Returns the users whose pending
        action expired; for each, the message is not sent and the action is left in
        its unsent state (simply discarded). When a Slack ``client`` is supplied, each
        affected user is DM'd that the prompt expired.
        """
        expired = self.confirmations.sweep_expired()
        users = [user for user, _ in expired]
        if client is not None:
            for user in users:
                try:
                    client.chat_postMessage(
                        channel=user,
                        text="A pending confirmation expired after 24 hours; nothing was sent.",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("failed to notify %s of expired confirmation", user)
        return users

    # ==================================================================
    # Detection pipeline (Req 2.1) — sweep + live message evaluation
    # ==================================================================
    def run_sweep(self, client: Any = None) -> None:
        """Run one Watcher sweep, drain the queue into the Adjudicator, refresh home.

        This is the scheduled job (≤60s cadence, Req 2.1). The Watcher forwards new
        candidates onto the :class:`AdjudicationQueue` (its ``forward`` seam is the
        queue's ``enqueue``); we then drain synchronously so the cycle completes
        deterministically and refresh the App Home if anything was written.
        """
        try:
            self.watcher.run_sweep()
            results = self.queue.drain()
        except Exception:  # noqa: BLE001 — a sweep must never crash the scheduler.
            logger.exception("watcher sweep failed")
            return
        # Autonomous auto-heal (Req 8): re-verify every open PR-linked loop and
        # close the ones whose PR is now merged, so a loop heals *within a sweep*
        # after its work is verifiably done — no user action, no dashboard touch.
        healed = self.reconcile_pr_closures()
        if client is not None:
            # Only republish on a real graph change — a drain that merely
            # discarded candidates leaves every Home identical, so skip the
            # needless views.publish (matters on a rate-limited workspace). A
            # loop that just auto-closed is a real change too, so fold in the
            # members on every healed edge.
            affected = self._affected_members(results) | self._affected_members(healed)
            if affected:
                self._refresh_home_for(affected, client)

    def reconcile_pr_closures(self) -> list[Any]:
        """Auto-close every open PR-linked loop whose PR is now merged (Req 8).

        The background half of the auto-heal story: the scheduled sweep re-verifies
        each active obligation that references a GitHub PR and, for any PR the
        Verifier confirms *merged*, closes the loop itself (``closure_kind=
        autonomous``). The three-valued interlock inside
        :meth:`ActionAgent.auto_close` leaves non-merged / unverifiable PRs
        untouched, so this is safe to run on every sweep. Returns the closed
        results (each carrying the healed obligation) so the caller can refresh
        the affected members' App Home.
        """
        from loop.adjudicator.pr_ref import extract_pr_ref
        from loop.graph.store import ObligationFilter

        try:
            active = self.graph.query(
                ObligationFilter(
                    loop_states=frozenset(
                        {LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER}
                    )
                )
            )
        except Exception:  # noqa: BLE001 — a sweep must never crash the scheduler.
            logger.exception("auto-close reconciliation query failed")
            return []

        closed: list[Any] = []
        for o in active:
            # Only PR-referencing loops are auto-closable; skip the rest without
            # spending a Verifier round-trip on them.
            if not (o.artifact_ref and extract_pr_ref(o.artifact_ref)):
                continue
            try:
                result = self.action.auto_close(o)
            except Exception:  # noqa: BLE001 — one bad verify must not stop the rest.
                logger.exception(
                    "auto-close reconciliation failed for %s", o.obligation_id
                )
                continue
            if getattr(result, "closed", False):
                logger.info(
                    "autonomously auto-closed %s (%s) after verified merge",
                    o.obligation_id,
                    o.artifact_ref,
                )
                closed.append(result)
        return closed

    def on_message_event(self, event: dict[str, Any], client: Any = None) -> None:
        """Evaluate a live Slack message through the detection pipeline (Req 2.2)."""
        try:
            outcome = self.watcher.on_message_event(event)
            logger.info(
                "watcher outcome for %s/%s: %s",
                event.get("channel"),
                event.get("ts"),
                getattr(outcome, "value", outcome),
            )
            results = self.queue.drain()
        except Exception:  # noqa: BLE001
            logger.exception("live message evaluation failed")
            return
        if client is not None:
            # Only republish on a real graph change (see run_sweep).
            affected = self._affected_members(results)
            if affected:
                self._refresh_home_for(affected, client)

    # ==================================================================
    # Daily digest (Req 11.1)
    # ==================================================================
    def send_daily_digest(self) -> None:
        """Send the daily digest (scheduled). The agent enforces ≤1/24h (Req 11.5)."""
        try:
            self.action.send_daily_digest(self.user_id)
        except Exception:  # noqa: BLE001
            logger.exception("daily digest failed")

    # ==================================================================
    # Conversational assistant (Req 12)
    # ==================================================================
    def handle_assistant_message(
        self, user_id: UserId, text: str, client: Any = None
    ) -> HandlerResult:
        """Route an Assistant-pane message to the Conversational Agent (Req 12).

        The Conversational Agent owns its own 60s in-conversation confirmation gate
        (Req 12.7) for send-as-user commands, so this simply forwards and renders the
        reply into rich Block Kit for the Assistant pane (the second Slack surface).
        The ``text`` field is kept as a complete accessibility / notification fallback
        that still carries the answer and a ``Tools: {trace}`` summary of the
        Tool_Use_Trace (Req 12.5), so screen readers and the existing wiring tests see
        the same content the blocks render visually. When a ``client`` is supplied,
        person avatars are resolved best-effort for the rendered rows.
        """
        reply = self.conversational.handle(user_id, text)
        trace = reply.trace_text
        text_out = reply.text + (f"\n\n_Tools: {trace}_" if trace else "")
        blocks = self._assistant_blocks(reply, client, viewer=user_id)
        return HandlerResult(
            text=text_out,
            blocks=blocks,
            requires_confirmation=reply.requires_confirmation,
        )

    def _assistant_blocks(
        self, reply: Any, client: Any = None, viewer: Optional[UserId] = None
    ) -> list[dict]:
        """Render an :class:`AssistantReply` into Block Kit with best-effort avatars.

        ``viewer`` is whoever asked — rows resolve their counterparty relative to
        them, so a member never sees their own face on their own loops. Falls back
        to the tracked user for legacy callers.
        """
        target = viewer or self.user_id
        avatars = self._avatars.resolve(people_in_reply(reply, target), client)
        return build_assistant_blocks(
            reply,
            now=self._now(),
            user_id=target,
            avatars=avatars,
            style=self._settings.assistant_style,
            # The aging bar chart is a ``data_visualization`` block, which this
            # workspace refuses — and a chat message can't degrade like the Home,
            # so including it would drop the whole reply. Card rows carry the value.
            chart=False,
        )

    def post_assistant_reply(
        self,
        user: UserId,
        text: str,
        client: Any,
        *,
        channel: Optional[str] = None,
        thread_ts: Optional[str] = None,
    ) -> HandlerResult:
        """Handle an Assistant-thread user message and post the rich reply (Req 12).

        Routes ``text`` through :meth:`handle_assistant_message` and posts the
        resulting Block Kit blocks (with the text fallback) back into the assistant
        thread when ``channel``/``thread_ts`` are given (so the reply renders inside
        the pane), falling back to a plain DM to ``user`` otherwise. Returns the
        :class:`HandlerResult` so the wiring is testable without Slack.
        """
        result = self.handle_assistant_message(user, text, client)
        if channel and client is not None:
            try:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=result.text,
                    blocks=result.blocks or None,
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to post assistant reply to %s", channel)
        else:
            _post_reply(client, user, result)
        return result

    @staticmethod
    def _set_assistant_status(event: dict, client: Any) -> None:
        """Best-effort 'is thinking…' status in the Assistant pane while the LLM runs.

        Only meaningful inside an assistant thread (needs a ``thread_ts``); outside
        one, or on any API refusal, it is silently skipped — status is cosmetic.
        """
        thread_ts = event.get("thread_ts")
        if client is None or not thread_ts:
            return
        try:
            client.assistant_threads_setStatus(
                channel_id=event.get("channel"),
                thread_ts=thread_ts,
                status="is thinking...",
            )
        except Exception:  # noqa: BLE001
            logger.debug("assistant setStatus unavailable", exc_info=True)

    def handle_assistant_confirm(self, user_id: UserId, client: Any = None) -> HandlerResult:
        """Complete the in-pane (60s) send-as-user confirm and render the reply (Req 12.6).

        Routes the one-tap confirm to the Conversational Agent's own confirmation gate
        and renders the resulting :class:`AssistantReply` into Block Kit so the pane
        updates in place.
        """
        reply = self.conversational.confirm(user_id)
        text = reply.text + (f"\n\n_Tools: {reply.trace_text}_" if reply.trace_text else "")
        blocks = self._assistant_blocks(reply, client, viewer=user_id)
        return HandlerResult(text=text, blocks=blocks, sent=not reply.requires_confirmation)

    def handle_assistant_decline(self, user_id: UserId, client: Any = None) -> HandlerResult:
        """Cancel the in-pane (60s) send-as-user command and render the reply (Req 12.7)."""
        reply = self.conversational.decline(user_id)
        text = reply.text + (f"\n\n_Tools: {reply.trace_text}_" if reply.trace_text else "")
        blocks = self._assistant_blocks(reply, client, viewer=user_id)
        return HandlerResult(text=text, blocks=blocks, cancelled=True)

    # ==================================================================
    # Slack handler registration (Bolt)
    # ==================================================================
    def register_handlers(self, app: Any) -> None:
        """Register all Slack event/interaction handlers on a Bolt ``App``.

        Separated from construction so handlers can be unit-tested against a fake app.
        Wires: App Home open; the four per-row actions (Nudge/Snooze/Delegate/Dismiss);
        the confirm/decline of a send-as-user action (the 24h gate); and Assistant
        messages.
        """

        @app.event("app_home_opened")
        def _on_home_open(event, client, logger):  # noqa: ANN001
            user = event.get("user")
            if user:
                self.open_home(user, client)

        @app.event("assistant_thread_started")
        def _on_assistant_thread_started(event, client):  # noqa: ANN001
            # Slack-native onboarding: tappable suggested prompts the moment
            # the Assistant pane opens, matched to the planner's real tools so
            # every suggestion actually works. Best-effort — a failure just
            # leaves the pane without suggestions.
            thread = event.get("assistant_thread") or {}
            channel_id = thread.get("channel_id")
            thread_ts = thread.get("thread_ts")
            if not channel_id or not thread_ts:
                return
            # A short welcome opens the pane (redesign) before the suggestion
            # chips — sets context and the trust line in one message.
            try:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=(
                        "I'm watching your open loops across your workspace. Ask me "
                        "who's blocking you or what you owe — or tap a suggestion "
                        "below. I can act on your behalf, and I always show you first."
                    ),
                )
            except Exception:  # noqa: BLE001 — the welcome is best-effort.
                logger.debug("assistant welcome post failed", exc_info=True)
            try:
                client.assistant_threads_setSuggestedPrompts(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    title="Ask Loop about your open loops",
                    prompts=[
                        {
                            "title": "What am I blocking?",
                            "message": "Who's blocked on me right now?",
                        },
                        {
                            "title": "Show my open loops",
                            "message": "Show me all my open loops.",
                        },
                        {
                            "title": "What am I waiting on?",
                            "message": "What am I waiting on from others?",
                        },
                    ],
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to set Assistant suggested prompts")

        def _open_composer(body, client, prepare, *, title, surface):  # noqa: ANN001
            # The smart-tier draft can outlive the trigger_id's 3-second
            # validity, so claim the trigger with a status modal first and
            # swap in the composer (or the failure notice) via views.update.
            # Ownership (who may act on this loop) is enforced by ``prepare``.
            try:
                opened = client.views_open(
                    trigger_id=_trigger_id(body),
                    view=build_composer_status_modal(
                        "*Drafting your message…*\n"
                        "Loop is writing it in your voice.",
                        title=title,
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.exception("failed to open the %s composer modal", surface)
                return
            view_id = ((opened or {}).get("view") or {}).get("id")
            view, message = prepare()
            try:
                if view is not None:
                    client.views_update(view_id=view_id, view=view)
                elif view_id:
                    client.views_update(
                        view_id=view_id,
                        view=build_composer_status_modal(message, title=title),
                    )
                else:
                    _post_dm(client, _user_of(body), message)
            except Exception:  # noqa: BLE001
                logger.exception("failed to update the %s composer modal", surface)

        @app.action(ACTION_NUDGE)
        def _on_nudge(ack, body, client):  # noqa: ANN001
            ack()
            oid, actor = _action_value(body), _user_of(body)
            _open_composer(
                body,
                client,
                lambda: self.prepare_nudge_modal(oid, actor),
                title="Send a nudge",
                surface="nudge",
            )

        @app.view(NUDGE_MODAL_CALLBACK)
        def _on_nudge_modal_submit(ack, body, client):  # noqa: ANN001
            ack()
            oid = (body.get("view") or {}).get("private_metadata", "")
            edited = _modal_input_value(body, NUDGE_MODAL_INPUT_BLOCK, NUDGE_MODAL_INPUT_ACTION)
            result = self.handle_nudge_modal_submit(_user_of(body), oid, edited)
            _post_dm(client, _user_of(body), result.text, result.blocks)
            self._refresh_home(client)

        @app.action(ACTION_QUICK_NUDGE)
        def _on_quick_nudge(ack, body, client):  # noqa: ANN001
            ack()
            oid, actor = _selected_option_value(body), _user_of(body)
            _open_composer(
                body,
                client,
                lambda: self.prepare_nudge_modal(oid, actor),
                title="Send a nudge",
                surface="nudge",
            )

        @app.action(ACTION_SNOOZE)
        def _on_snooze(ack, body, client):  # noqa: ANN001
            ack()
            result = self.handle_snooze_click(_action_value(body))
            _post_dm(client, _user_of(body), result.text, result.blocks)
            self._refresh_home(client)

        @app.action(ACTION_DELEGATE)
        def _on_delegate(ack, body, client):  # noqa: ANN001
            ack()
            teammate = _delegate_target(body)
            result = self.handle_delegate_click(_action_value(body), teammate)
            _post_dm(client, _user_of(body), result.text, result.blocks)

        @app.action(ACTION_DISMISS)
        def _on_dismiss(ack, body, client):  # noqa: ANN001
            ack()
            result = self.handle_dismiss_click(_action_value(body))
            _post_dm(client, _user_of(body), result.text, result.blocks)
            self._refresh_home(client)

        @app.action(ACTION_CYCLE_SEND)
        def _on_cycle_send(ack, body, client):  # noqa: ANN001
            ack()
            oid, actor = _action_value(body), _user_of(body)
            _open_composer(
                body,
                client,
                lambda: self.prepare_cycle_modal(oid, actor, client),
                title="Break the deadlock",
                surface="deadlock",
            )

        @app.action(ACTION_DEMO_RESET)
        def _on_demo_reset(ack, body, client):  # noqa: ANN001
            ack()
            if not self._settings.demo_mode:
                return  # the control never renders outside demo mode; belt & braces
            from loop.seed.loader import load_into_graph

            load_into_graph(self.graph)
            self._cycle_plan_cache.clear()
            self._refresh_home(client)
            _post_dm(
                client,
                _user_of(body),
                "Demo data reloaded — the dashboard is back to its seeded state.",
            )

        @app.action(ACTION_ROW_OVERFLOW)
        def _on_row_overflow(ack, body, client):  # noqa: ANN001
            ack()
            verb, oid = _overflow_selection(body)
            if verb == "snooze":
                result = self.handle_snooze_click(oid)
                _post_dm(client, _user_of(body), result.text, result.blocks)
                self._refresh_home(client)
            elif verb == "delegate":
                result = self.handle_delegate_click(oid, _delegate_target(body))
                _post_dm(client, _user_of(body), result.text, result.blocks)
            elif verb == "dismiss":
                result = self.handle_dismiss_click(oid)
                _post_dm(client, _user_of(body), result.text, result.blocks)
                self._refresh_home(client)

        @app.action(ACTION_UNDO_DISMISS)
        def _on_undo_dismiss(ack, body, client):  # noqa: ANN001
            ack()
            result = self.handle_undo_dismiss(_action_value(body))
            _post_dm(client, _user_of(body), result.text, result.blocks)
            self._refresh_home(client)

        @app.action(ACTION_UNDO_SNOOZE)
        def _on_undo_snooze(ack, body, client):  # noqa: ANN001
            ack()
            result = self.handle_undo_snooze(_action_value(body))
            _post_dm(client, _user_of(body), result.text, result.blocks)
            self._refresh_home(client)

        @app.action(ACTION_REVIEW_BLOCKED)
        def _on_review_blocked(ack, body, client):  # noqa: ANN001
            ack()
            self.open_review_modal(_user_of(body), _trigger_id(body), client)

        @app.action(ACTION_SCHEDULE)
        def _on_schedule(ack, body, client):  # noqa: ANN001
            # The link button already opened the prefilled calendar event in the
            # browser; this posts the in-thread confirmation proposal.
            ack()
            result = self.handle_schedule_click(_action_value(body), client)
            _post_dm(client, _user_of(body), result.text, result.blocks)

        @app.action("loop_meeting_calendar_link")
        def _on_meeting_calendar_link(ack):  # noqa: ANN001
            ack()  # pure link button — nothing to do beyond the ack

        @app.action(ACTION_MEETING_CONFIRM)
        def _on_meeting_confirm(ack, body, client):  # noqa: ANN001
            ack()
            result = self.handle_meeting_confirm(_action_value(body))
            # Swap the proposal message for the outcome so the thread shows the
            # confirmed state instead of a still-clickable button (best-effort).
            container = (body or {}).get("container") or {}
            channel = container.get("channel_id")
            ts = container.get("message_ts")
            if client is not None and channel and ts and result.obligation is not None:
                try:
                    client.chat_update(
                        channel=channel,
                        ts=ts,
                        text=result.text,
                        blocks=[
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": f"*{result.text}*"},
                            }
                        ],
                    )
                except Exception:  # noqa: BLE001 — the heal already persisted.
                    logger.exception("failed to update the meeting proposal message")
            _post_dm(client, self.user_id, result.text, result.blocks)
            self._refresh_home(client)

        @app.action(ACTION_CONFIRM_SEND)
        def _on_confirm(ack, body, client):  # noqa: ANN001
            ack()
            result = self.handle_confirm_send(_user_of(body))
            _post_dm(client, _user_of(body), result.text, result.blocks)
            self._refresh_home(client)

        @app.action(ACTION_DECLINE_SEND)
        def _on_decline(ack, body, client):  # noqa: ANN001
            ack()
            result = self.handle_decline_send(_user_of(body))
            _post_dm(client, _user_of(body), result.text, result.blocks)

        @app.action(ACTION_ASSISTANT_CONFIRM)
        def _on_assistant_confirm(ack, body, client):  # noqa: ANN001
            ack()
            user = _action_value(body) or _user_of(body)
            result = self.handle_assistant_confirm(user, client)
            _post_reply(client, _user_of(body), result)

        @app.action(ACTION_ASSISTANT_DECLINE)
        def _on_assistant_decline(ack, body, client):  # noqa: ANN001
            ack()
            user = _action_value(body) or _user_of(body)
            result = self.handle_assistant_decline(user, client)
            _post_reply(client, _user_of(body), result)

        @app.event("message")
        def _on_message(event, client):  # noqa: ANN001
            # A typed message in the app DM (the Assistant pane, Req 12) routes to
            # the Conversational Agent; bot echoes and edits are ignored so the app
            # never answers itself. Everything else is autonomous perception of
            # live messages in participating channels.
            if event.get("channel_type") == "im":
                text = (event.get("text") or "").strip()
                if event.get("bot_id") or event.get("subtype") or not text:
                    return
                self._set_assistant_status(event, client)
                self.post_assistant_reply(
                    event.get("user"),
                    text,
                    client,
                    channel=event.get("channel"),
                    thread_ts=event.get("thread_ts"),
                )
                return
            self.on_message_event(event, client)

    # ==================================================================
    # Scheduler (in-process APScheduler — Req 2.1, 11.1, 13.5)
    # ==================================================================
    def build_scheduler(self, client: Any = None) -> Any:
        """Create (but do not start) the APScheduler with all Loop jobs.

        Imports APScheduler lazily so importing this module never requires it. Jobs:
          * Watcher sweep every ``sweep_interval_seconds`` (≤60s, Req 2.1).
          * Daily digest once per day (Req 11.1); the agent enforces ≤1/24h.
          * Confirmation-timeout sweep hourly to expire 24h-old send confirmations
            (Req 13.5).
        """
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(
            lambda: self.run_sweep(client),
            IntervalTrigger(seconds=self._settings.sweep_interval_seconds),
            id="watcher_sweep",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            self.send_daily_digest,
            CronTrigger(hour=8, minute=0),  # 08:00 UTC daily
            id="daily_digest",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            lambda: self.sweep_confirmation_timeouts(client),
            IntervalTrigger(hours=1),
            id="confirmation_timeout_sweep",
            max_instances=1,
            coalesce=True,
        )
        return scheduler

    def start(self) -> None:
        """Boot the app: start the queue worker, the scheduler, and Socket Mode.

        This is the live entry point (``python -m loop.app``). It requires the Slack
        tokens at call time; the SDK imports happen here so importing the module is
        dependency-free.
        """
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        self._settings.require("slack_bot_token", "slack_app_token")
        self._slack_app = App(token=self._settings.slack_bot_token)

        @self._slack_app.use
        def _log_incoming(body, next, logger):  # noqa: ANN001
            # One INFO line per delivered Slack payload — enough to tell
            # "handler misbehaved" from "Slack never delivered the event".
            kind = body.get("type") or ("event:" + str((body.get("event") or {}).get("type")))
            detail = (
                (body.get("view") or {}).get("callback_id")
                or ((body.get("actions") or [{}])[0]).get("action_id")
                or (body.get("event") or {}).get("type")
                or ""
            )
            logger.info("incoming %s %s", kind, detail)
            next()

        self.register_handlers(self._slack_app)

        client = self._slack_app.client

        # Autonomous adjudication worker drains the queue continuously (Req 13.1).
        self.queue.start_worker()

        # In-process scheduler drives the ≤60s sweep, the digest, and the timeout sweep.
        self._scheduler = self.build_scheduler(client)
        self._scheduler.start()

        logger.info("Loop is connecting over Socket Mode...")
        SocketModeHandler(self._slack_app, self._settings.slack_app_token).start()

    def shutdown(self) -> None:
        """Stop the scheduler and the queue worker (best-effort)."""
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                logger.exception("scheduler shutdown failed")
            self._scheduler = None
        self.queue.stop_worker()


# ---------------------------------------------------------------------------
# Rich-block capability discovery (parse views.publish rejections)
# ---------------------------------------------------------------------------
_UNSUPPORTED_TYPE_RE = re.compile(r"unsupported type: ([a-z_]+)")


def _unsupported_block_types(exc: Exception) -> set[str]:
    """Block types a ``views.publish`` rejection names as unsupported.

    Slack's ``invalid_arguments`` response carries ``response_metadata.messages``
    lines like ``"[ERROR] unsupported type: data_visualization
    [json-pointer:/view/blocks/5/type]"`` — one per refused block, all listed in
    a single response. Duck-typed against the exception's ``response`` (a
    ``SlackResponse`` with ``.data`` or a plain dict) so this module never
    imports the SDK; any other exception shape yields the empty set, which the
    caller treats as "fall back to classic".
    """
    response = getattr(exc, "response", None)
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        data = response if isinstance(response, dict) else None
    if not isinstance(data, dict):
        return set()
    meta = data.get("response_metadata") or {}
    messages = meta.get("messages") or [] if isinstance(meta, dict) else []
    found: set[str] = set()
    for message in messages:
        found.update(_UNSUPPORTED_TYPE_RE.findall(str(message)))
    return found


# ---------------------------------------------------------------------------
# Bolt body helpers (extract ids/values from interaction payloads)
# ---------------------------------------------------------------------------
def _user_of(body: dict[str, Any]) -> str:
    """The Slack user id who triggered an interaction payload."""
    return (body.get("user") or {}).get("id", "")


def _action_value(body: dict[str, Any]) -> str:
    """The first action's ``value`` (the obligation id carried by a row button)."""
    actions = body.get("actions") or []
    if actions:
        return actions[0].get("value", "")
    return ""


def _trigger_id(body: dict[str, Any]) -> str:
    """The ``trigger_id`` from an interaction payload (needed to open a modal)."""
    return body.get("trigger_id", "")


def _selected_option_value(body: dict[str, Any]) -> str:
    """The selected option's ``value`` from a ``static_select`` action payload.

    The hero quick-nudge dropdown carries the chosen obligation id here. Returns
    ``""`` when no selection is present so the handler can no-op safely.
    """
    actions = body.get("actions") or []
    if actions:
        return (actions[0].get("selected_option") or {}).get("value", "")
    return ""


def _modal_input_value(body: dict[str, Any], block_id: str, action_id: str) -> str:
    """Read a modal input's submitted value from a ``view_submission`` payload.

    Returns ``""`` when the block/action/value is absent so the caller can fall back
    to the original draft text.
    """
    state = ((body.get("view") or {}).get("state") or {}).get("values") or {}
    return ((state.get(block_id) or {}).get(action_id) or {}).get("value") or ""


def _overflow_selection(body: dict[str, Any]) -> tuple[str, str]:
    """Parse a row overflow (⋮) selection into ``(verb, obligation_id)``.

    The selected option's value is encoded ``{verb}::{obligation_id}`` by the App Home
    builder (e.g. ``"snooze::OBL_B1"``). Returns ``("", "")`` when no selection / a
    malformed value is present so the handler can no-op safely.
    """
    actions = body.get("actions") or []
    if not actions:
        return "", ""
    selected = actions[0].get("selected_option") or {}
    value = selected.get("value", "")
    if "::" in value:
        verb, oid = value.split("::", 1)
        return verb, oid
    return "", ""


def _delegate_target(body: dict[str, Any]) -> str:
    """The delegation teammate id from an interaction payload (best-effort).

    A real delegate flow opens a user-select; the selected user id arrives in the
    action payload. We read ``selected_user`` when present, else fall back to the
    button value's secondary segment ``obligation_id|teammate_id`` if encoded.
    """
    actions = body.get("actions") or []
    if actions:
        selected = actions[0].get("selected_user")
        if selected:
            return selected
        value = actions[0].get("value", "")
        if "|" in value:
            return value.split("|", 1)[1]
    return ""


def _post_dm(client: Any, user: str, text: str, blocks: Any = None) -> None:
    """Post a DM to ``user`` (best-effort; logs and swallows failures).

    When ``blocks`` is supplied it is forwarded to ``chat_postMessage`` with ``text``
    kept as the accessibility / notification fallback (the Assistant pane, Req 12).
    """
    if not text or not user or client is None:
        return
    try:
        if blocks:
            client.chat_postMessage(channel=user, text=text, blocks=blocks)
        else:
            client.chat_postMessage(channel=user, text=text)
    except Exception:  # noqa: BLE001
        logger.exception("failed to post message to %s", user)


def _post_reply(client: Any, user: str, result: "HandlerResult") -> None:
    """Post a :class:`HandlerResult` to ``user``, forwarding its blocks when present."""
    _post_dm(client, user, result.text, result.blocks)


def _meeting_proposal_blocks(obligation: Obligation, proposal: str) -> list[dict]:
    """The in-thread meeting proposal: the ask + one-tap Confirm (+ calendar link).

    Posted as the bot into the loop's source thread by
    :meth:`LoopApp.handle_schedule_click`. The Confirm button carries the
    obligation id so :data:`ACTION_MEETING_CONFIRM` can heal the right loop; the
    calendar link button lets the counterparty add the same prefilled event.
    """
    elements: list[dict] = [
        {
            "type": "button",
            "style": "primary",
            "action_id": ACTION_MEETING_CONFIRM,
            "text": {"type": "plain_text", "text": "Confirm meeting", "emoji": True},
            "value": obligation.obligation_id,
        }
    ]
    url = meeting_calendar_url(obligation)
    if url:
        elements.append(
            {
                "type": "button",
                "action_id": "loop_meeting_calendar_link",
                "text": {"type": "plain_text", "text": "Add to Google Calendar", "emoji": True},
                "url": url,
                "value": obligation.obligation_id,
            }
        )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": proposal}},
        {"type": "actions", "block_id": f"meeting_confirm::{obligation.obligation_id}", "elements": elements},
    ]


def _undo_dm_blocks(text: str, action_id: str, oid: str, *, label: str = "Undo") -> list[dict]:
    """A confirmation DM body carrying the message plus a one-tap **Undo** button.

    Makes a reversible autonomous action (dismiss / snooze) feel safe: the DM states
    what happened and offers an immediate Undo whose ``value`` is the obligation id, so
    the matching handler can restore it. ``text`` is kept as the notification fallback.
    """
    return _confirmation_blocks(text, undo_action=action_id, oid=oid)


def _blockquote(text: str) -> str:
    """Render ``text`` as a Slack mrkdwn blockquote (each line prefixed with ``>``)."""
    lines = text.splitlines() or [text]
    return "\n".join(f">{line}" if line else ">" for line in lines)


def _confirmation_blocks(
    status: str,
    *,
    context: Optional[str] = None,
    quote: Optional[str] = None,
    undo_action: Optional[str] = None,
    oid: Optional[str] = None,
    undo_label: str = "Undo",
) -> list[dict]:
    """A consistent, professional confirmation card for the Loop DM "history".

    Every bot notification (sent / snoozed / dismissed / restored) renders the same
    shape so the DM thread reads as a clean activity log rather than a pile of plain
    text:

      * a bold **status** section (e.g. "*Nudge sent*"),
      * an optional **quote** of the message that was posted (blockquoted),
      * an optional **context** subline (who · where · when), and
      * an optional one-tap **Undo** button for reversible actions.
    """
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": status}}]
    if quote:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _blockquote(quote)}})
    if context:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})
    if undo_action and oid:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": undo_action,
                        "text": {"type": "plain_text", "text": undo_label, "emoji": True},
                        "value": oid,
                    }
                ],
            }
        )
    return blocks


# ---------------------------------------------------------------------------
# Composition root — wire the real (lazy) ports from config
# ---------------------------------------------------------------------------
def build_loop_app(
    *,
    settings: Optional[Settings] = None,
    user_id: Optional[UserId] = None,
) -> LoopApp:
    """Assemble a fully-wired :class:`LoopApp` from configuration (the live wiring).

    Wires the lazy real ports (RTS, fast-tier classify, smart-tier reasoning,
    GitHub MCP, smart-tier nudge drafting, Slack send-as-user) from :func:`loop.config.get_settings`. None of those ports
    open a connection until first used, so this function is side-effect-free beyond
    creating the embedded SQLite store.

    Args:
        settings: resolved settings; defaults to :func:`get_settings`.
        user_id: the tracked User's Slack id; defaults to the ``LOOP_USER_ID`` env
            value carried on settings if present, else a placeholder that the caller
            should override before going live.
    """
    settings = settings or get_settings()
    import os

    user_id = user_id or os.getenv("LOOP_USER_ID", "") or "U_LOOP_USER"

    graph = SqliteObligationGraph(settings.database_path)

    # Demo mode: load the seeded Northwind org into the live graph (timestamps
    # rebased to the current clock) so the dashboard, nudge/quick-nudge, and the
    # PR-merge auto-heal are all fully interactive on the polished demo data —
    # not just a static published view. Idempotent across reboots.
    if settings.demo_mode:
        from loop.seed.loader import load_into_graph

        load_into_graph(graph)

    # Verifier (GitHub MCP) — lazy client.
    from loop.verifier.verifier import build_github_mcp_client

    verifier = Verifier(build_github_mcp_client(settings))

    # Adjudicator (smart tier) — lazy client.
    from loop.adjudicator.adjudicator import build_smart_reasoning_client

    adjudicator = Adjudicator(build_smart_reasoning_client(settings), graph)

    # Action Agent — send-as-user + smart-tier drafting ports wired lazily.
    send_as_user = _build_slack_send_as_user(settings)
    action = ActionAgent(
        graph,
        verifier,
        slack_send_as_user=send_as_user,
        draft=_build_draft(settings),
    )

    learn = LearnEngine(graph)
    # Wire the dynamic-orchestration planner (smart tier) so the live Assistant pane
    # plans which tools to invoke per request and shows that reasoning in the
    # Tool-Use Trace. It degrades gracefully to the deterministic parser path on any
    # planning failure, so this is safe to enable by default.
    from loop.conversational.planner import build_llm_planner

    conversational = ConversationalAgent(
        graph,
        action,
        planner=build_llm_planner(settings=settings),
        user_id=user_id,
    )

    # Detection pipeline: Watcher forwards onto the queue; the queue feeds the Adjudicator.
    queue = AdjudicationQueue(adjudicator, user_id)
    from loop.watcher.watcher import build_fast_classify_client, build_rts_client

    watcher = Watcher(
        graph,
        build_rts_client(settings),
        classify=build_fast_classify_client(settings),
        forward=queue.enqueue,
        interval_seconds=settings.sweep_interval_seconds,
        # Scope live detection to the configured demo channel(s) when set; an empty
        # allow-list means watch the whole workspace (backward compatible).
        watch_channels=set(settings.watch_channels) or None,
    )

    return LoopApp(
        graph=graph,
        watcher=watcher,
        adjudicator=adjudicator,
        verifier=verifier,
        action=action,
        conversational=conversational,
        learn=learn,
        queue=queue,
        user_id=user_id,
        settings=settings,
        send_on_behalf=_build_send_on_behalf(settings),
        send_as_member=_build_send_as_member(settings),
    )


def _build_slack_send_as_user(settings: Settings) -> Callable[[str, str], None]:
    """Wire a lazy Slack 'send as user' port (chat.postMessage as the user).

    Posting a Polite Nudge / delegation AS the user (Req 7.3, 10) requires the
    **user token** (``SLACK_USER_TOKEN``, xoxp-), which carries the user ``chat:write``
    scope — the bot token would post as the bot, not as you. Falls back to the bot
    token only if no user token is configured (e.g. scaffolding/tests). The
    ``slack_sdk`` import and the network call happen inside the returned callable so
    importing this module is dependency-free."""
    token = settings.slack_user_token or settings.slack_bot_token

    def _send(recipient_or_channel: str, text: str) -> None:
        from slack_sdk import WebClient

        WebClient(token=token).chat_postMessage(channel=recipient_or_channel, text=text)

    return _send


def _build_send_as_member(settings: Settings) -> Callable[[str, str, str], bool]:
    """Wire the lazy per-member send port (multi-user send-as-you).

    Members listed in ``LOOP_USER_TOKENS`` granted the app their own user
    scopes, so their approved composer messages post AS them. Returns False
    for anyone without a token so the caller can fall back to attributed
    bot posting.
    """
    tokens = dict(settings.user_tokens)

    def _send(member_id: str, channel: str, text: str) -> bool:
        token = tokens.get(member_id)
        if not token:
            return False
        from slack_sdk import WebClient

        WebClient(token=token).chat_postMessage(channel=channel, text=text)
        return True

    return _send


def _build_send_on_behalf(settings: Settings) -> Callable[[str, str], None]:
    """Wire the lazy bot-token posting port used for attributed on-behalf sends.

    Members other than the workspace's token owner have no xoxp of their own, so
    their approved composer messages are posted by the bot, attributed to them.
    """

    def _send(channel: str, text: str) -> None:
        from slack_sdk import WebClient

        WebClient(token=settings.slack_bot_token).chat_postMessage(
            channel=channel, text=text
        )

    return _send


def _build_draft(settings: Settings) -> Callable[[Obligation], str]:
    """Wire a lazy smart-tier nudge-drafting port (≤10s — Req 7.1).

    Provider-agnostic: routes through :func:`loop.llm.chat` at the **smart tier**
    (the configured Groq ``llama-3.3-70b-versatile`` by default, or Anthropic Opus
    when ``llm_provider="anthropic"``). The provider SDK and the network call happen
    lazily inside the returned callable, so importing this module never requires a
    provider SDK. Raises on failure/timeout, which the Action Agent treats as a
    draft failure (send nothing, Req 7.8)."""

    def _draft(obligation: Obligation) -> str:
        from loop.llm import chat

        from loop.action.app_home import nudge_recipient_id

        # The recipient is always the *counterparty* (never the user): the person
        # waiting on you (blocked-on-you) or the person who owes you
        # (waiting-on-other). Slack renders <@ID> as their real name (and notifies
        # them), so the model must not invent a name of its own.
        recipient = nudge_recipient_id(obligation)
        mention = f"<@{recipient}>" if recipient else "there"
        if obligation.loop_state == LoopState.BLOCKED_ON_YOU:
            # Your court: a proactive status *reply* to whoever is waiting on you —
            # you own this, so acknowledge it and reassure them, don't ask them.
            prompt = (
                "Draft a brief, warm, professional Slack status update to someone who "
                "is waiting on you for an open commitment (a proactive reply).\n"
                f"Start by addressing them exactly as {mention} — Slack renders that "
                "as their name; do not use any other name. Acknowledge that you own it, "
                "reassure them it's in progress, and say you'll follow up shortly. 2-4 "
                "sentences, under 1000 characters. Output ONLY the message text; do not "
                "invent names, channels, dates, links, or reference lines.\n\n"
                f"Subject: {obligation.subject_summary}\n"
            )
        else:
            # Their court: a polite *nudge* to whoever owes you, asking for an update.
            prompt = (
                "Draft a brief, warm, professional Slack reminder (a 'polite nudge') "
                "about an open commitment.\n"
                f"Start by addressing the recipient exactly as {mention} — Slack "
                "renders that as their name; do not use any other name. Reference the "
                "subject naturally, note it has been a little while, and ask for an "
                "update. 2-4 sentences, under 1000 characters. Output ONLY the message "
                "text; do not invent names, channels, dates, links, or reference "
                "lines.\n\n"
                f"Subject: {obligation.subject_summary}\n"
            )
        return chat(
            [{"role": "user", "content": prompt}],
            tier="smart",
            settings=settings,
            max_tokens=512,
        )

    return _draft


def main() -> None:
    """Boot the fully-wired Loop app over Socket Mode (``python -m loop.app``)."""
    logging.basicConfig(level=logging.INFO)
    build_loop_app().start()


if __name__ == "__main__":
    main()


__all__ = [
    "LoopApp",
    "build_loop_app",
    "ConfirmationRegistry",
    "PendingSend",
    "HandlerResult",
    "CONFIRM_TIMEOUT",
    "ACTION_CONFIRM_SEND",
    "ACTION_DECLINE_SEND",
    "ACTION_ASSISTANT_CONFIRM",
    "ACTION_ASSISTANT_DECLINE",
    "main",
]
