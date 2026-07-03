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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from loop.action.action_agent import ActionAgent, NudgeDraft
from loop.action.app_home import (
    ACTION_DELEGATE,
    ACTION_DISMISS,
    ACTION_NUDGE,
    ACTION_QUICK_NUDGE,
    ACTION_REVIEW_BLOCKED,
    ACTION_ROW_OVERFLOW,
    ACTION_SNOOZE,
    aging_chip,
    auto_healed_rows,
    blocked_on_you_rows,
    build_app_home_compact_view,
    build_app_home_view,
    build_nudge_modal,
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
from loop.graph.models import Obligation, ObligationId, UserId, utc_now_iso
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
        self.confirmations = ConfirmationRegistry(now=now)
        self._avatars = AvatarResolver()
        self._scheduler: Any = None
        self._slack_app: Any = None

    # ==================================================================
    # App Home (Req 6) — publish the real dashboard view
    # ==================================================================
    def open_home(self, user_id: UserId, client: Any) -> None:
        """Publish the App Home dashboard for ``user_id`` (Req 6.1).

        Builds the pure Block Kit view from the current graph contents (task 9
        builder) and publishes it. Any publish failure is logged and swallowed so a
        single bad publish cannot crash the socket loop.
        """
        try:
            now = self._now()
            avatars = self._home_avatars(now, client)
            names = self._home_names(now, client)
            if self._settings.home_layout == "compact":
                view = build_app_home_compact_view(
                    self.graph,
                    now=now,
                    user_id=self.user_id,
                    avatars=avatars,
                    names=names,
                    logo_url=self._settings.logo_url,
                )
            else:
                view = build_app_home_view(
                    self.graph,
                    now=now,
                    user_id=self.user_id,
                    avatars=avatars,
                    names=names,
                    logo_url=self._settings.logo_url,
                    style=self._settings.home_style,
                )
            client.views_publish(user_id=user_id, view=view)
        except Exception:  # noqa: BLE001
            logger.exception("failed to publish App Home view for %s", user_id)

    def _home_avatars(self, now: str, client: Any) -> dict[str, str]:
        """Best-effort avatar map for the people about to be rendered on the App Home.

        Gathers the counterparties across the three sections and resolves their
        avatars via :class:`AvatarResolver` (cached, best-effort). Any failure yields
        an empty map so the view still renders without images.
        """
        people = self._home_people(now)
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

    def _home_people(self, now: str) -> set[str]:
        """The set of counterparties rendered across the three App Home sections."""
        people: set[str] = set()
        for o in blocked_on_you_rows(self.graph, now):
            if o.owed_person_id:
                people.add(o.owed_person_id)
        for o in waiting_on_other_rows(self.graph, now):
            if o.owes_person_id:
                people.add(o.owes_person_id)
        for o in auto_healed_rows(self.graph, now, self.user_id):
            person = resolved_person_id(o, self.user_id)
            if person:
                people.add(person)
        return people

    def _home_names(self, now: str, client: Any) -> dict[str, str]:
        """Best-effort ``person_id → display name`` map for the App Home people.

        Lets the card layout lead with the person's name (e.g. "Alice") instead of a
        long, truncating subject. Best-effort: any failure yields an empty map, so the
        cards gracefully fall back to the subject-as-title rendering.
        """
        names = self._avatars.resolve_names(self._home_people(now), client)
        # Demo-mode fallback: seeded people aren't real Slack users, so use the
        # seeded display names (live-resolved names take precedence).
        if self._settings.demo_mode:
            people = self._home_people(now)
            merged = {pid: nm for pid, nm in seed_names().items() if pid in people}
            merged.update(names)
            return merged
        return names

    def _refresh_home(self, client: Any) -> None:
        """Re-publish the tracked user's App Home (after an autonomous graph change)."""
        if client is None:
            return
        self.open_home(self.user_id, client)

    # ==================================================================
    # Row actions (Req 6 buttons → Action Agent / Learn)
    # ==================================================================
    def handle_nudge_click(self, obligation_id: ObligationId) -> HandlerResult:
        """Draft a Polite Nudge and present the 24h one-tap confirm (Req 7.1, 7.2, 13.2).

        Drafting is autonomous (no send), but the *send* is withheld behind a one-tap
        confirmation: on a successful draft a :class:`PendingSend` is registered and a
        confirmation-required result is returned. Nothing is posted as the user yet
        (Req 7.2, 13.2). A draft failure sends nothing and reports the failure
        (Req 7.8).
        """
        obligation = self.graph.get(obligation_id)
        if obligation is None:
            return HandlerResult(text="That loop no longer exists.")

        draft_result = self.action.draft_polite_nudge(obligation)
        if not draft_result.drafted or draft_result.draft is None:
            return HandlerResult(text=draft_result.message or "Drafting failed; nothing was sent.")

        self.confirmations.add(
            self.user_id,
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
        self, obligation_id: ObligationId
    ) -> tuple[Optional[dict], str]:
        """Draft a Polite Nudge and return the editable composer **modal** (Best-UX).

        The modal flow replaces the blind one-tap: it shows the AI-drafted message
        pre-filled and editable so the user sees and approves exactly what will be
        posted as them. Drafting is autonomous (no send); a :class:`PendingSend` is
        registered so the same 24h gate / send path backs the modal submission.

        Returns ``(view, "")`` on a successful draft, or ``(None, message)`` when the
        loop is gone or drafting failed — the caller DMs ``message`` (Req 7.8).
        """
        obligation = self.graph.get(obligation_id)
        if obligation is None:
            return None, "That loop no longer exists."

        draft_result = self.action.draft_polite_nudge(obligation)
        if not draft_result.drafted or draft_result.draft is None:
            return None, (
                draft_result.message or "Couldn't draft a nudge right now; nothing was sent."
            )

        self.confirmations.add(
            self.user_id,
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
        draft = NudgeDraft(obligation=base.obligation, text=text, channel=base.channel)
        send = self.action.send_polite_nudge(draft, confirmed=True)
        if send.cancelled:
            return HandlerResult(text=send.message or "Nudge cancelled.", cancelled=True)
        if not send.sent:
            return HandlerResult(text=send.error or "The nudge could not be sent.")
        return HandlerResult(
            text="Sent as you. ✅",
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
        return _confirmation_blocks("✅ *Nudge sent*", quote=message, context=ctx)

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
                "🕓 *Snoozed*",
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
                "🗑️ *Dismissed*",
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
            blocks=_confirmation_blocks("↩️ *Restored*", context=restored.subject_summary or None),
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
            blocks=_confirmation_blocks("↩️ *Un-snoozed*", context=restored.subject_summary or None),
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
        rows = blocked_on_you_rows(self.graph, self._now())
        if not rows:
            return HandlerResult(
                text="You're all caught up — nobody's blocked on you. 🎉",
                blocks=_confirmation_blocks(
                    "🎉 *You're all caught up*",
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
                text="Sent as you. ✅",
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
                text=f"Delegated to {result.teammate_id}. ✅",
                sent=True,
                obligation=result.obligation,
                blocks=_confirmation_blocks(
                    "📨 *Delegated*",
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
        if results and client is not None:
            self._refresh_home(client)

    def on_message_event(self, event: dict[str, Any], client: Any = None) -> None:
        """Evaluate a live Slack message through the detection pipeline (Req 2.2)."""
        try:
            self.watcher.on_message_event(event)
            results = self.queue.drain()
        except Exception:  # noqa: BLE001
            logger.exception("live message evaluation failed")
            return
        if results and client is not None:
            self._refresh_home(client)

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
        blocks = self._assistant_blocks(reply, client)
        return HandlerResult(
            text=text_out,
            blocks=blocks,
            requires_confirmation=reply.requires_confirmation,
        )

    def _assistant_blocks(self, reply: Any, client: Any = None) -> list[dict]:
        """Render an :class:`AssistantReply` into Block Kit with best-effort avatars."""
        avatars = self._avatars.resolve(people_in_reply(reply, self.user_id), client)
        cards = self._settings.assistant_style == "cards"
        return build_assistant_blocks(
            reply,
            now=self._now(),
            user_id=self.user_id,
            avatars=avatars,
            style=self._settings.assistant_style,
            chart=cards,
        )

    def post_assistant_reply(self, user: UserId, text: str, client: Any) -> HandlerResult:
        """Handle an Assistant-thread user message and post the rich reply (Req 12).

        Routes ``text`` through :meth:`handle_assistant_message` and posts the
        resulting Block Kit blocks (with the text fallback) back to ``user``. Returns
        the :class:`HandlerResult` so the wiring is testable without Slack.
        """
        result = self.handle_assistant_message(user, text, client)
        _post_reply(client, user, result)
        return result

    def handle_assistant_confirm(self, user_id: UserId, client: Any = None) -> HandlerResult:
        """Complete the in-pane (60s) send-as-user confirm and render the reply (Req 12.6).

        Routes the one-tap confirm to the Conversational Agent's own confirmation gate
        and renders the resulting :class:`AssistantReply` into Block Kit so the pane
        updates in place.
        """
        reply = self.conversational.confirm(user_id)
        text = reply.text + (f"\n\n_Tools: {reply.trace_text}_" if reply.trace_text else "")
        blocks = self._assistant_blocks(reply, client)
        return HandlerResult(text=text, blocks=blocks, sent=not reply.requires_confirmation)

    def handle_assistant_decline(self, user_id: UserId, client: Any = None) -> HandlerResult:
        """Cancel the in-pane (60s) send-as-user command and render the reply (Req 12.7)."""
        reply = self.conversational.decline(user_id)
        text = reply.text + (f"\n\n_Tools: {reply.trace_text}_" if reply.trace_text else "")
        blocks = self._assistant_blocks(reply, client)
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

        @app.action(ACTION_NUDGE)
        def _on_nudge(ack, body, client):  # noqa: ANN001
            ack()
            view, message = self.prepare_nudge_modal(_action_value(body))
            if view is not None:
                try:
                    client.views_open(trigger_id=_trigger_id(body), view=view)
                except Exception:  # noqa: BLE001
                    logger.exception("failed to open the nudge composer modal")
            else:
                _post_dm(client, _user_of(body), message)

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
            view, message = self.prepare_nudge_modal(_selected_option_value(body))
            if view is not None:
                try:
                    client.views_open(trigger_id=_trigger_id(body), view=view)
                except Exception:  # noqa: BLE001
                    logger.exception("failed to open the nudge composer modal")
            else:
                _post_dm(client, _user_of(body), message)

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
            result = self.handle_review_blocked(_user_of(body))
            _post_dm(client, _user_of(body), result.text, result.blocks)

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
            # Autonomous perception of live messages in participating channels.
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

      * a bold **status** section (e.g. "✅ *Nudge sent*"),
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
    from loop.adjudicator.adjudicator import build_opus_reasoning_client

    adjudicator = Adjudicator(build_opus_reasoning_client(settings), graph)

    # Action Agent — send-as-user + smart-tier drafting ports wired lazily.
    send_as_user = _build_slack_send_as_user(settings)
    action = ActionAgent(
        graph,
        verifier,
        slack_send_as_user=send_as_user,
        claude_draft=_build_claude_draft(settings),
    )

    learn = LearnEngine(graph)
    # Wire the dynamic-orchestration planner (smart tier) so the live Assistant pane
    # plans which tools to invoke per request and shows that reasoning in the
    # Tool-Use Trace. It degrades gracefully to the deterministic parser path on any
    # planning failure, so this is safe to enable by default.
    from loop.conversational.planner import build_llm_planner

    conversational = ConversationalAgent(
        graph, action, planner=build_llm_planner(settings=settings)
    )

    # Detection pipeline: Watcher forwards onto the queue; the queue feeds the Adjudicator.
    queue = AdjudicationQueue(adjudicator, user_id)
    from loop.watcher.watcher import build_haiku_classify_client, build_rts_client

    watcher = Watcher(
        graph,
        build_rts_client(settings),
        classify=build_haiku_classify_client(settings),
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


def _build_claude_draft(settings: Settings) -> Callable[[Obligation], str]:
    """Wire a lazy smart-tier nudge-drafting port (≤10s — Req 7.1).

    Provider-agnostic: routes through :func:`loop.llm.chat` at the **smart tier**
    (the configured Groq ``llama-3.3-70b-versatile`` by default, or Anthropic Opus
    when ``llm_provider="anthropic"``). The provider SDK and the network call happen
    lazily inside the returned callable, so importing this module never requires a
    provider SDK. Raises on failure/timeout, which the Action Agent treats as a
    draft failure (send nothing, Req 7.8)."""

    def _draft(obligation: Obligation) -> str:
        from loop.llm import chat

        prompt = (
            "Draft a brief, warm, professional Slack reminder (a 'polite nudge') for "
            "an open loop. Reference the person, the subject, and that it has been a "
            "while. Keep it under 1000 characters.\n\n"
            f"Subject: {obligation.subject_summary}\n"
            f"Source channel: {obligation.source_msg_channel}\n"
            f"Source message ts: {obligation.source_msg_ts}\n"
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
