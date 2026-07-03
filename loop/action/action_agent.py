"""The Action Agent (Act) — Agent 4.

The Action Agent owns every outward effect in the Loop cycle: rendering the App
Home, drafting/sending Polite Nudges as the user, autonomously closing
verified-merged loops, snoozing, delegating, and sending the daily digest
(design.md → "Components and Interfaces" → "Action Agent", Req 6-11, 13).

Scope realized **here**:

task 10 — GitHub proof-of-work auto-close:
  * task 10.1 — :meth:`ActionAgent.auto_close` gated on the Verifier's three-valued
    result. The loop is set to ``healed`` with ``closure_kind=autonomous`` *iff* the
    Verifier reports RESOLVED (a real merge); on UNRESOLVED or UNVERIFIED the
    Loop_State is left untouched and no autonomous closure is recorded (Req 8.2, 8.3,
    8.4, 8.6, 8.7). Closed-without-merge maps to UNRESOLVED upstream, so it can never
    be auto-closed (Req 8.7). The UNVERIFIED safety interlock is expressed through
    :func:`loop.verifier.verifier.is_auto_close_permitted` so an unknown PR state can
    never trigger a close (Req 4.6, 8.6).
  * task 10.2 — on auto-close the closure metadata is recorded: the source artifact
    reference is retained, a human-readable closure reason is set, and a closure
    timestamp in ISO 8601 UTC is stamped (Req 8.5).

task 13 — snooze and manual delegate:
  * task 13.1 — :meth:`ActionAgent.snooze` clamps the requested duration to the
    inclusive ``[1h, 30d]`` window (defaulting to ``24h`` when none is given) and
    stamps ``snoozed_until = now + clamped`` on the obligation. Surfacing stops while
    ``now < snoozed_until`` and resumes automatically afterwards — the resume is not a
    separate write, it falls straight out of :func:`loop.graph.surfacing.is_surfaced`
    comparing ``now`` against ``snoozed_until`` (Req 13.3, 13.4).
  * task 13.2 — :meth:`ActionAgent.delegate` models the one-tap confirmation as an
    explicit parameter (the Block Kit prompt is wired in task 17). Without confirmation
    it aborts with no change (Req 10.1, 10.2); on confirmation it sends a message
    authored as the user to one teammate through an injected, mockable Slack
    "send as user" port within 5s (Req 10.3), retrying up to 3 times. On send success
    it records the teammate as the new owner and sets Last_Touch_Timestamp to the
    confirmation time (Req 10.4); on failure after 3 attempts it retains the original
    owner and timestamp and reports an error (Req 10.5). Delegation is limited to one
    teammate with no bandwidth/skill matching (Req 10.6).

The class is intentionally structured with clear method boundaries so the
remaining Action Agent surfaces (Polite Nudge — task 12) slot in alongside the
implemented methods without churn.

task 14 — daily digest:
  * task 14.1 — :meth:`ActionAgent.send_daily_digest` sends a once-per-day summary of
    the user's open loops grouped by Loop_State. It reads only the surfaced
    (at/above-threshold, non-dismissed) `blocked-on-you`/`waiting-on-other`
    Obligations through the shared surfacing gate, states the count of each, and sends
    a "no open loops require attention" digest when none qualify (Req 11.1-11.4). It
    enforces at most one digest per rolling 24h window (Req 11.5) and, on send failure,
    retries up to 3 times, records an error, leaves Obligation state unchanged, and
    re-attempts at the next scheduled time (Req 11.6). The APScheduler trigger is wired
    in task 17.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from loop.graph.models import (
    ArtifactType,
    ClosureKind,
    LoopState,
    Obligation,
    PersonId,
    UserId,
    utc_now_iso,
)
from loop.graph.store import ObligationFilter, ObligationGraph, Result, is_ok
from loop.verifier.types import VerificationResult, VerifyPurpose
from loop.verifier.verifier import Verifier, is_auto_close_permitted

# The reason recorded against an autonomously-healed obligation (Req 8.5). The
# Auto-Healed feed (task 11) surfaces this string verbatim.
AUTO_CLOSE_REASON = "PR merged"

# Snooze duration bounds (Req 13.3): inclusive [1h, 30d], default 24h.
SNOOZE_MIN = timedelta(hours=1)
SNOOZE_MAX = timedelta(days=30)
SNOOZE_DEFAULT = timedelta(hours=24)

# Number of times a delegation notification send is attempted before it is treated
# as failed (Req 10.5: "fails after 3 retry attempts").
DELEGATE_MAX_ATTEMPTS = 3

# Number of times a Daily_Digest send is attempted before it is treated as failed
# (Req 11.6: "re-attempt the send up to 3 times").
DIGEST_MAX_ATTEMPTS = 3

# At most one Daily_Digest is delivered to the user within each rolling 24h window
# (Req 11.5). The window is measured against the last *successful* delivery.
DIGEST_WINDOW = timedelta(hours=24)

# A "send as user" port: post ``text`` to ``recipient_id`` authored as the user.
# It returns normally on success and raises on failure (unreachable/timeout/error),
# which is what drives the bounded-retry logic in :meth:`ActionAgent.delegate`.
SlackSendAsUser = Callable[[str, str], None]

# Polite Nudge drafting (task 12.1 — Req 7.1, 7.8).
#
# A Claude drafting port: given the Obligation to nudge, return the drafted reminder
# text. The Action Agent owns the *constraints* around the port — it clips the result
# to at most :data:`NUDGE_DRAFT_MAX_CHARS` characters (Req 7.1) and treats any raise
# (including a timeout) as a draft failure so nothing is sent (Req 7.8). The port MUST
# honour the 10-second drafting budget and raise on timeout, mirroring how the Verifier
# delegates its timeout budget to its injected client; the lazy real wiring below passes
# the budget straight through to the Anthropic client.
ClaudeDraftPort = Callable[[Obligation], str]

# Req 7.1: a drafted Polite_Nudge is "no more than 1000 characters".
NUDGE_DRAFT_MAX_CHARS = 1000

# Req 7.1 / 7.8: Claude must draft "within 10 seconds"; the real port passes this
# budget to the Anthropic client and raises on timeout (handled as a draft failure).
NUDGE_DRAFT_TIMEOUT_SECONDS = 10.0


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a tz-aware UTC ``datetime``.

    Mirrors the coercion used across the store and surfacing predicate: a bare
    trailing ``Z`` becomes ``+00:00`` and a naive value is assumed to be UTC, so the
    snooze arithmetic here is consistent with how ``is_surfaced`` later compares
    ``now`` against ``snoozed_until``.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clamp_snooze(duration: Optional[timedelta]) -> timedelta:
    """Clamp a requested snooze ``duration`` into the inclusive ``[1h, 30d]`` window.

    ``None`` selects the ``24h`` default (Req 13.3). Any shorter request is raised to
    the 1h floor and any longer one is lowered to the 30d ceiling, so a snooze can
    never hide an obligation for less than an hour or more than thirty days.
    """
    if duration is None:
        return SNOOZE_DEFAULT
    if duration < SNOOZE_MIN:
        return SNOOZE_MIN
    if duration > SNOOZE_MAX:
        return SNOOZE_MAX
    return duration


def _lazy_send_as_user(recipient_id: str, text: str) -> None:
    """Default Slack "send as user" port — real wiring is deferred to task 17.

    The Action Agent never hard-depends on a live Slack call: sending is performed
    through an injected port so it stays mockable in tests. When no port is injected
    this lazy default is used; it reads workspace-scoped credentials from
    :func:`loop.config.get_settings` and, until the Bolt app is wired end to end
    (task 17), raises to make the missing wiring explicit rather than silently
    dropping a user-authored message.
    """
    from loop.config import get_settings  # lazy import to avoid load-time coupling

    settings = get_settings()
    settings.require("slack_bot_token")
    raise NotImplementedError(
        "Live Slack 'send as user' is wired in task 17; inject a send port to send."
    )


def _lazy_claude_draft(obligation: Obligation) -> str:
    """Default Claude drafting port — real wiring is deferred to task 17.

    The Action Agent never hard-depends on a live Claude call: drafting is performed
    through an injected :data:`ClaudeDraftPort` so it stays mockable in tests. When no
    port is injected this lazy default is used; it defers the live wiring to
    :func:`build_claude_draft_client`, raising to make the missing wiring explicit
    (a raise is treated as a draft failure, so nothing is sent — Req 7.8).
    """
    raise NotImplementedError(
        "Live Claude nudge drafting is wired in task 17; inject a draft port to draft."
    )


@dataclass(frozen=True)
class AutoCloseResult:
    """Outcome of an :meth:`ActionAgent.auto_close` evaluation.

    Attributes:
        closed: True iff the loop was autonomously closed (Verifier RESOLVED and the
            healed write persisted). False whenever the Loop_State was left unchanged
            — UNRESOLVED, UNVERIFIED, or a persist failure on the closing write.
        verification: The three-valued Verifier result that drove the decision
            (RESOLVED / UNRESOLVED / UNVERIFIED).
        obligation: The obligation as it stands after the evaluation — the healed,
            metadata-stamped value when ``closed`` is True, otherwise the unchanged
            input obligation.
    """

    closed: bool
    verification: VerificationResult
    obligation: Obligation


@dataclass(frozen=True)
class SnoozeResult:
    """Outcome of an :meth:`ActionAgent.snooze` call.

    Attributes:
        snoozed: True iff the snooze write persisted. False only when the underlying
            ``upsert`` reported a persist failure (the prior value is then retained,
            Req 1.8).
        snoozed_until: The ISO 8601 UTC instant until which surfacing is suppressed —
            ``now + clamped_duration`` (Req 13.3).
        obligation: The obligation as it stands after the call — carrying the new
            ``snoozed_until`` when ``snoozed`` is True, otherwise the unchanged input.
    """

    snoozed: bool
    snoozed_until: str
    obligation: Obligation


@dataclass(frozen=True)
class DelegateResult:
    """Outcome of an :meth:`ActionAgent.delegate` evaluation.

    Attributes:
        delegated: True iff ownership was reassigned and the change persisted —
            requires a confirmation and a successful send (Req 10.4). False on cancel
            (Req 10.2), send failure (Req 10.5), or persist failure.
        sent: True iff the notification message was actually sent as the user.
        teammate_id: The single teammate the delegation targeted (Req 10.6).
        obligation: The obligation after the evaluation — reassigned owner + updated
            Last_Touch_Timestamp when ``delegated`` is True, otherwise the unchanged
            input obligation (Req 10.2, 10.5).
        prompt: The confirmation-prompt text shown before sending (teammate identity +
            obligation summary, Req 10.1). Present regardless of outcome so the caller
            can render it.
        error: A human-readable error indication when the delegation was not completed
            (send failure after 3 attempts, or persist failure); None otherwise
            (Req 10.5).
    """

    delegated: bool
    sent: bool
    teammate_id: PersonId
    obligation: Obligation
    prompt: str
    error: Optional[str] = None


@dataclass(frozen=True)
class DigestResult:
    """Outcome of a :meth:`ActionAgent.send_daily_digest` evaluation.

    Attributes:
        sent: True iff the digest message was actually delivered to the user (a send
            succeeded within :data:`DIGEST_MAX_ATTEMPTS`). False on suppression
            (already sent within the 24h window) or on send failure after all
            attempts (Req 11.5, 11.6).
        suppressed: True iff the call was a no-op because a digest was already sent to
            the user within the rolling 24h window (Req 11.5). When True, no message is
            sent and ``error`` is None.
        empty: True iff no Obligations qualified, so the "no open loops require
            attention" digest was composed (Req 11.4).
        blocked_on_you_count: Count of included `blocked-on-you` Obligations (Req 11.3).
        waiting_on_other_count: Count of included `waiting-on-other` Obligations
            (Req 11.3).
        message: The composed digest text (the body that was sent, or would have been
            sent on a send failure). None when the call was suppressed.
        error: A human-readable error indication when delivery failed after all
            attempts (Req 11.6); None otherwise.
    """

    sent: bool
    suppressed: bool
    empty: bool
    blocked_on_you_count: int
    waiting_on_other_count: int
    message: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class NudgeDraft:
    """A drafted Polite_Nudge ready to be shown for one-tap confirmation (Req 7.1, 7.2).

    Attributes:
        obligation: The Obligation the nudge is for. Carried so the send step can
            apply the Verifier gate and stamp the Last_Touch_Timestamp on the right
            edge without re-deriving it (Req 7.4, 7.6).
        text: The drafted reminder text — clipped to at most
            :data:`NUDGE_DRAFT_MAX_CHARS` characters and referencing the sender, the
            summarized subject, and the source message timestamp (Req 7.1).
        channel: The channel of the source Slack message; the nudge is posted here, as
            the user, on confirmation (Req 7.3).
    """

    obligation: Obligation
    text: str
    channel: str


@dataclass(frozen=True)
class NudgeDraftResult:
    """Outcome of an :meth:`ActionAgent.draft_polite_nudge` call.

    Attributes:
        drafted: True iff Claude returned a draft within budget. False on a draft
            failure/timeout, in which case nothing is sent (Req 7.8).
        draft: The :class:`NudgeDraft` to show for one-tap confirmation when
            ``drafted`` is True; None on failure.
        message: A user-facing notice — on failure, the "drafting failed" indication
            (Req 7.8); None on success.
        error: The underlying error detail on a draft failure; None otherwise.
    """

    drafted: bool
    draft: Optional[NudgeDraft] = None
    message: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class NudgeSendResult:
    """Outcome of an :meth:`ActionAgent.send_polite_nudge` evaluation.

    Attributes:
        sent: True iff the nudge was actually posted as the user in the source channel
            (Req 7.3). False before confirmation, on a Verifier cancel, or on a send
            failure.
        cancelled: True iff the nudge was cancelled because the Verifier reported the
            referenced PR as already resolved (Req 7.5).
        verification: The Verifier's result when a PR gate was applied, else None.
        obligation: The obligation after the evaluation — carrying the updated
            Last_Touch_Timestamp when ``sent`` is True (Req 7.6), otherwise unchanged
            (Req 7.7).
        draft: The draft, retained for retry on a send failure so the user can try
            again (Req 7.7); None once sent or when not applicable.
        message: A user-facing notice (e.g. "loop already resolved", or the
            confirmation-required prompt) when relevant; None otherwise.
        error: A human-readable error indication when the send did not complete
            (Req 7.7); None otherwise.
    """

    sent: bool
    cancelled: bool
    obligation: Obligation
    verification: Optional[VerificationResult] = None
    draft: Optional[NudgeDraft] = None
    message: Optional[str] = None
    error: Optional[str] = None


class ActionAgent:
    """Agent 4 — every outward effect. This task implements auto-close only.

    Args:
        graph: The shared Obligation Graph store (single source of truth). Closure
            writes go through :meth:`ObligationGraph.upsert` so validation,
            last-write-wins, and persistence guarantees are honoured centrally.
        verifier: The Verifier used to ground a PR-referencing obligation in real
            GitHub state before any autonomous closure (Req 8.1).
        now: Injectable clock returning an ISO 8601 UTC timestamp string; defaults to
            :func:`loop.graph.models.utc_now_iso`. Injected purely to make the
            recorded closure/snooze/confirmation timestamps deterministic in tests.
        slack_send_as_user: Injectable "send as user" port used by :meth:`delegate`
            (and, later, Polite Nudge). It posts a message authored as the user and
            raises on failure. Injected so sending is mockable; when omitted, a lazy
            default reads workspace credentials from config and defers live wiring to
            task 17 (it never hard-depends on a live Slack call at construction time).
        claude_draft: Injectable Claude drafting port used by
            :meth:`draft_polite_nudge` (Req 7.1). Given the Obligation to nudge, it
            returns the drafted reminder text and MUST raise on failure/timeout (the
            10s drafting budget, Req 7.8). Injected so drafting is mockable; when
            omitted, a lazy default defers live wiring to task 17.
    """

    def __init__(
        self,
        graph: ObligationGraph,
        verifier: Verifier,
        *,
        now: Callable[[], str] = utc_now_iso,
        slack_send_as_user: Optional[SlackSendAsUser] = None,
        claude_draft: Optional[ClaudeDraftPort] = None,
    ) -> None:
        self._graph = graph
        self._verifier = verifier
        self._now = now
        self._send_as_user: SlackSendAsUser = slack_send_as_user or _lazy_send_as_user
        self._claude_draft: ClaudeDraftPort = claude_draft or _lazy_claude_draft
        # In-process record of the last *successful* digest delivery time (ISO 8601
        # UTC), used to enforce at most one digest per 24h (Req 11.5). Task 17 wires
        # the APScheduler trigger; persisting this across restarts is out of scope here.
        self._last_digest_sent_at: Optional[str] = None

    # ------------------------------------------------------------------
    # Auto-close (task 10 — Req 8)
    # ------------------------------------------------------------------
    def auto_close(self, obligation: Obligation) -> AutoCloseResult:
        """Evaluate a PR-referencing obligation for autonomous closure (Req 8).

        Grounds the decision in the Verifier's real GitHub PR status (purpose
        AUTO_CLOSE → 30s budget, Req 8.1) and acts on the three-valued result:

          * RESOLVED (PR merged) → set Loop_State ``healed`` with
            ``closure_kind=autonomous``, stamp the closure metadata (artifact ref
            retained, reason, ISO 8601 UTC timestamp), and upsert to the graph
            (Req 8.2, 8.5).
          * UNRESOLVED (reachable but not merged — open, or closed-without-merge) →
            leave the Loop_State unchanged; never record an autonomous closure
            (Req 8.3, 8.7).
          * UNVERIFIED (transport failure after ≤3 attempts) → leave the Loop_State
            unchanged; the UNVERIFIED safety interlock forbids closing on an unknown
            state (Req 8.4, 8.6).

        Returns an :class:`AutoCloseResult` indicating whether the loop was closed.
        """
        verification = self._verifier.verify_pr(
            obligation, purpose=VerifyPurpose.AUTO_CLOSE
        )

        # Safety interlock first: an UNVERIFIED state must never auto-close, even
        # defensively, before we look at RESOLVED/UNRESOLVED (Req 4.6, 8.4, 8.6).
        if not is_auto_close_permitted(verification):
            return AutoCloseResult(
                closed=False, verification=verification, obligation=obligation
            )

        # Only a verified merge (RESOLVED) closes the loop; UNRESOLVED — including a
        # PR closed without merging — leaves Loop_State untouched (Req 8.3, 8.7).
        if verification is not VerificationResult.RESOLVED:
            return AutoCloseResult(
                closed=False, verification=verification, obligation=obligation
            )

        # RESOLVED: heal the loop and stamp the closure metadata (Req 8.2, 8.5).
        healed = obligation.model_copy(
            update={
                "loop_state": LoopState.HEALED,
                "closure_kind": ClosureKind.AUTONOMOUS,
                "closure_reason": AUTO_CLOSE_REASON,
                "closure_timestamp": self._now(),
                # artifact_ref (the source artifact reference) is retained as-is.
            }
        )

        result: Result[Obligation] = self._graph.upsert(healed)
        if not is_ok(result):
            # Persist failure: the store retained the prior value (Req 1.8); the
            # loop was not closed. Report the unchanged input obligation.
            return AutoCloseResult(
                closed=False, verification=verification, obligation=obligation
            )

        return AutoCloseResult(
            closed=True, verification=verification, obligation=result.value
        )


    # ------------------------------------------------------------------
    # Context-aware Polite Nudge (task 12 — Req 7, 13.2)
    # ------------------------------------------------------------------
    def draft_polite_nudge(self, obligation: Obligation) -> NudgeDraftResult:
        """Draft a context-aware Polite_Nudge for ``obligation`` (task 12.1 — Req 7.1, 7.8).

        Uses the injected Claude drafting port to produce, within the 10-second budget
        (enforced by the port, which raises on timeout), a reminder message that
        references the sender, the summarized subject, and the timestamp of the source
        Slack message (Req 7.1). The Action Agent owns the hard length constraint: the
        returned text is clipped to at most :data:`NUDGE_DRAFT_MAX_CHARS` characters so
        a drafted nudge can never exceed 1000 characters regardless of what the model
        returns (Req 7.1).

        On any draft failure — the port raising, including a timeout — nothing is sent
        and the user is told that drafting failed (Req 7.8). The draft is **not**
        sent here; it is returned for one-tap confirmation and only sent by
        :meth:`send_polite_nudge` after the user confirms (Req 7.2).

        Returns a :class:`NudgeDraftResult`; ``drafted`` is False on failure.
        """
        try:
            text = self._claude_draft(obligation)
        except Exception as exc:  # noqa: BLE001 — any draft failure/timeout: send nothing
            # Req 7.8: drafting failed — inform the user, send nothing.
            return NudgeDraftResult(
                drafted=False,
                message="Drafting the nudge failed — no message was sent.",
                error=str(exc),
            )

        # Req 7.1: a drafted nudge is "no more than 1000 characters". Clip defensively
        # so the constraint holds whatever the model returned.
        clipped = text[:NUDGE_DRAFT_MAX_CHARS]
        draft = NudgeDraft(
            obligation=obligation,
            text=clipped,
            channel=obligation.source_msg_channel,
        )
        return NudgeDraftResult(drafted=True, draft=draft)

    def send_polite_nudge(
        self, draft: NudgeDraft, *, confirmed: bool = False
    ) -> NudgeSendResult:
        """Send a drafted Polite_Nudge as the user, gated on confirm + Verifier (task 12.2, 12.3).

        The one-tap confirmation is modelled as the ``confirmed`` parameter (the Block
        Kit confirm button is wired in task 17). Behaviour:

          * **Not confirmed** (``confirmed=False``) → never send: return ``sent=False``
            with the draft retained and nothing changed. No message is ever posted as
            the user before an explicit confirmation (Req 7.2, 13.2).
          * **Confirmed, PR-referencing obligation** → require the Verifier to report
            the loop as *not* RESOLVED before sending (purpose NUDGE → 10s budget). If
            the Verifier reports RESOLVED, cancel the nudge and inform the user the
            loop is already resolved; send nothing (Req 7.4, 7.5). UNRESOLVED and
            UNVERIFIED both clear the gate — a reminder on an unknown PR state is safe
            and still user-confirmed (unlike auto-close, which forbids UNVERIFIED).
          * **Send** → post the draft as the user in the channel of the source Slack
            message, within 5s of confirmation (Req 7.3).
              * **Send success** → update the obligation's Last_Touch_Timestamp to the
                send time and persist it (Req 7.6).
              * **Send failure** → display an error, retain the draft for retry, and
                leave the Last_Touch_Timestamp unchanged — no graph write occurs
                (Req 7.7).

        Returns a :class:`NudgeSendResult` describing the outcome.
        """
        obligation = draft.obligation

        # Req 7.2 / 13.2: nothing is posted as the user until they confirm.
        if not confirmed:
            return NudgeSendResult(
                sent=False,
                cancelled=False,
                obligation=obligation,
                draft=draft,
                message="Tap Confirm to send this nudge as you.",
            )

        # Req 7.4 / 7.5: for a PR-referencing obligation, gate on the Verifier. Only a
        # RESOLVED (merged) PR cancels the nudge; UNRESOLVED/UNVERIFIED proceed.
        verification: Optional[VerificationResult] = None
        if (
            obligation.artifact_type is ArtifactType.GITHUB_PR
            and obligation.artifact_ref
        ):
            verification = self._verifier.verify_pr(
                obligation, purpose=VerifyPurpose.NUDGE
            )
            if verification is VerificationResult.RESOLVED:
                # Req 7.5: the loop is already resolved — cancel, inform, send nothing.
                return NudgeSendResult(
                    sent=False,
                    cancelled=True,
                    obligation=obligation,
                    verification=verification,
                    message="This loop is already resolved — the nudge was cancelled.",
                )

        # Confirmation time anchors both the send and (on success) the new
        # Last_Touch_Timestamp so they agree exactly (Req 7.3, 7.6).
        send_time = self._now()

        # Req 7.3: post as the user in the source channel (single attempt — the draft
        # is retained for a user-initiated retry on failure, Req 7.7).
        try:
            self._send_as_user(draft.channel, draft.text)
        except Exception as exc:  # noqa: BLE001 — any send failure is reported.
            # Req 7.7: send did not complete — error, retain draft, do NOT touch the
            # Last_Touch_Timestamp (no graph write happens on this path).
            return NudgeSendResult(
                sent=False,
                cancelled=False,
                obligation=obligation,
                verification=verification,
                draft=draft,
                error=f"The nudge could not be sent: {exc}. The draft was kept for retry.",
            )

        # Req 7.6: send succeeded — advance Last_Touch_Timestamp to the send time.
        touched = obligation.model_copy(update={"last_touch_timestamp": send_time})
        result: Result[Obligation] = self._graph.upsert(touched)
        if not is_ok(result):
            # The message was sent, but the timestamp write did not persist (Req 1.8);
            # report the partial failure while retaining the draft for clarity.
            return NudgeSendResult(
                sent=True,
                cancelled=False,
                obligation=obligation,
                verification=verification,
                draft=draft,
                error="The nudge was sent but the Last_Touch_Timestamp update failed.",
            )

        return NudgeSendResult(
            sent=True,
            cancelled=False,
            obligation=result.value,
            verification=verification,
        )


    # ------------------------------------------------------------------
    # Snooze (task 13.1 — Req 13.3, 13.4)
    # ------------------------------------------------------------------
    def snooze(
        self, obligation: Obligation, duration: Optional[timedelta] = None
    ) -> SnoozeResult:
        """Suppress surfacing of ``obligation`` for a bounded period (Req 13.3, 13.4).

        The requested ``duration`` is clamped to the inclusive ``[1h, 30d]`` window and
        defaults to ``24h`` when ``None`` (Req 13.3). ``snoozed_until`` is stamped at
        ``now + clamped_duration`` and the obligation is upserted.

        Surfacing stops immediately because :func:`loop.graph.surfacing.is_surfaced`
        hides any obligation while ``now < snoozed_until``, and it resumes
        **automatically** once that instant passes — there is no separate "unsnooze"
        write; the same predicate simply starts returning the obligation again once
        ``now >= snoozed_until`` (Req 13.4). The snooze deliberately does not advance
        Last_Touch_Timestamp: snoozing is not a touch, and leaving it unchanged keeps
        aging honest.

        Returns a :class:`SnoozeResult`; ``snoozed`` is False only when the upsert hit
        a persist failure, in which case the prior value is retained (Req 1.8).
        """
        clamped = _clamp_snooze(duration)
        snoozed_until = (_parse_iso_utc(self._now()) + clamped).isoformat()

        snoozed = obligation.model_copy(update={"snoozed_until": snoozed_until})
        result: Result[Obligation] = self._graph.upsert(snoozed)
        if not is_ok(result):
            # Persist failure: the store retained the prior value (Req 1.8).
            return SnoozeResult(
                snoozed=False, snoozed_until=snoozed_until, obligation=obligation
            )

        return SnoozeResult(
            snoozed=True, snoozed_until=snoozed_until, obligation=result.value
        )

    # ------------------------------------------------------------------
    # Manual delegate (task 13.2 — Req 10)
    # ------------------------------------------------------------------
    def delegation_prompt(self, obligation: Obligation, teammate_id: PersonId) -> str:
        """Build the confirmation-prompt text shown before a delegation (Req 10.1).

        The prompt displays the selected teammate identity and the obligation summary
        so the user can review exactly who the loop is being handed to and what it is
        before the single required confirmation. The Block Kit rendering of this prompt
        is wired in task 17; here it is the plain text the prompt carries.
        """
        return (
            f"Delegate this loop to {teammate_id}?\n"
            f"Loop: {obligation.subject_summary}\n"
            "Tap Confirm to send a handoff message as you, or Cancel to keep it."
        )

    def delegate(
        self,
        obligation: Obligation,
        teammate_id: PersonId,
        *,
        confirmed: bool = False,
    ) -> DelegateResult:
        """Hand ``obligation`` to a single teammate, gated on a one-tap confirm (Req 10).

        The one-tap confirmation is modelled as the ``confirmed`` parameter (the Block
        Kit prompt itself is wired in task 17); :meth:`delegation_prompt` produces the
        prompt content (teammate identity + obligation summary, Req 10.1). Behaviour:

          * **Not confirmed / cancel** (``confirmed=False``) → abort: send nothing and
            leave the obligation, its owner, and its Last_Touch_Timestamp unchanged
            (Req 10.1, 10.2).
          * **Confirmed** → send a message authored as the user to the one selected
            teammate through the injected send port, within 5s of confirmation
            (Req 10.3), retrying up to :data:`DELEGATE_MAX_ATTEMPTS` times on failure.
              * **Send success** → record the teammate as the new ``owner_person_id``
                and set Last_Touch_Timestamp to the confirmation time, then upsert
                (Req 10.4).
              * **Send failure after 3 attempts** → retain the original owner and
                timestamp unchanged and return an error indication (Req 10.5).

        Delegation targets exactly one existing teammate with no bandwidth/skill
        matching (Req 10.6).
        """
        prompt = self.delegation_prompt(obligation, teammate_id)

        # Req 10.1 / 10.2: nothing is sent and nothing changes until the user confirms.
        if not confirmed:
            return DelegateResult(
                delegated=False,
                sent=False,
                teammate_id=teammate_id,
                obligation=obligation,
                prompt=prompt,
            )

        # Confirmation time anchors both the send and (on success) the new
        # Last_Touch_Timestamp, so they agree exactly (Req 10.4).
        confirmation_time = self._now()
        message = (
            f"Handing off a loop to you: {obligation.subject_summary}. "
            "Could you take this one from here?"
        )

        # Req 10.3 / 10.5: send as the user, retrying up to 3 attempts on failure.
        send_error: Optional[str] = None
        sent = False
        for _ in range(DELEGATE_MAX_ATTEMPTS):
            try:
                self._send_as_user(teammate_id, message)
                sent = True
                break
            except Exception as exc:  # noqa: BLE001 — any send failure is retryable.
                send_error = str(exc)

        if not sent:
            # Req 10.5: all attempts failed — retain original owner + timestamp and
            # report the error. No write occurs, so the obligation is unchanged.
            return DelegateResult(
                delegated=False,
                sent=False,
                teammate_id=teammate_id,
                obligation=obligation,
                prompt=prompt,
                error=(
                    f"Delegation to {teammate_id} was not completed after "
                    f"{DELEGATE_MAX_ATTEMPTS} attempts: {send_error}"
                ),
            )

        # Req 10.4: send succeeded — reassign owner and stamp the confirmation time.
        reassigned = obligation.model_copy(
            update={
                "owner_person_id": teammate_id,
                "last_touch_timestamp": confirmation_time,
            }
        )
        result: Result[Obligation] = self._graph.upsert(reassigned)
        if not is_ok(result):
            # The message was sent, but the ownership write did not persist: retain the
            # original owner/timestamp (Req 1.8) and report the partial failure.
            return DelegateResult(
                delegated=False,
                sent=True,
                teammate_id=teammate_id,
                obligation=obligation,
                prompt=prompt,
                error=f"Delegation message sent but owner reassignment failed for {teammate_id}.",
            )

        return DelegateResult(
            delegated=True,
            sent=True,
            teammate_id=teammate_id,
            obligation=result.value,
            prompt=prompt,
        )


    # ------------------------------------------------------------------
    # Daily digest (task 14 — Req 11)
    # ------------------------------------------------------------------
    def _compose_digest(
        self, blocked_on_you_count: int, waiting_on_other_count: int
    ) -> str:
        """Compose the Daily_Digest body grouped by Loop_State (Req 11.1, 11.3, 11.4).

        When both counts are zero the digest states that no open loops require
        attention (Req 11.4); otherwise it states the count of included
        `blocked-on-you` and `waiting-on-other` Obligations (Req 11.3), grouped by
        Loop_State (Req 11.1).
        """
        if blocked_on_you_count == 0 and waiting_on_other_count == 0:
            return "Your daily Loop digest: no open loops require attention right now."

        blocked_people = "person is" if blocked_on_you_count == 1 else "people are"
        return (
            "Your daily Loop digest:\n"
            f"🔴 Blocked on you: {blocked_on_you_count} {blocked_people} waiting on you.\n"
            f"⏳ Waiting on others: you're waiting on {waiting_on_other_count} loop"
            f"{'' if waiting_on_other_count == 1 else 's'}."
        )

    def _within_digest_window(self, now_iso: str) -> bool:
        """True iff a digest was already delivered within the rolling 24h window.

        Compares ``now`` against the last *successful* delivery time. A failed send
        never advances the window, so a previously-failed digest is free to retry at
        the next scheduled time (Req 11.5, 11.6).
        """
        if self._last_digest_sent_at is None:
            return False
        elapsed = _parse_iso_utc(now_iso) - _parse_iso_utc(self._last_digest_sent_at)
        return elapsed < DIGEST_WINDOW

    def send_daily_digest(self, user_id: UserId) -> DigestResult:
        """Send the user their once-per-day Daily_Digest of open loops (Req 11).

        Behaviour:
          * **At most one per 24h** — if a digest was already delivered within the
            rolling 24h window, the call is a no-op and returns ``suppressed=True``
            with nothing sent (Req 11.5).
          * **Content** — queries the surfaced Obligations (at/above threshold,
            non-dismissed, `blocked-on-you`/`waiting-on-other`) via the shared
            surfacing gate, groups them by Loop_State, and states the count of each
            (Req 11.1, 11.2, 11.3). When none qualify, a "no open loops require
            attention" digest is composed instead (Req 11.4).
          * **Delivery** — sends the digest as a message to the user through the
            injected send port, retrying up to :data:`DIGEST_MAX_ATTEMPTS` times on
            failure (Req 11.1, 11.6).
          * **On failure** — after all attempts fail, records an error indication,
            leaves all Obligation state unchanged (the digest is read-only over the
            graph), and does **not** advance the 24h window so delivery is re-attempted
            at the next scheduled time (Req 11.6).

        Returns a :class:`DigestResult` describing the outcome.
        """
        now_iso = self._now()

        # Req 11.5: at most one digest per 24h — suppress a second call in the window.
        if self._within_digest_window(now_iso):
            return DigestResult(
                sent=False,
                suppressed=True,
                empty=False,
                blocked_on_you_count=0,
                waiting_on_other_count=0,
                message=None,
            )

        # Req 11.2: only surfaced (at/above-threshold, non-dismissed) active loops.
        # surfaced_only applies the shared is_surfaced gate against the current
        # threshold and ``now``; restricting loop_states keeps healed loops out.
        surfaced = self._graph.query(
            ObligationFilter(
                loop_states=frozenset(
                    {LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER}
                ),
                surfaced_only=True,
                now=now_iso,
            )
        )

        # Req 11.1 / 11.3: group by Loop_State and count each group.
        blocked_on_you_count = sum(
            1 for o in surfaced if o.loop_state is LoopState.BLOCKED_ON_YOU
        )
        waiting_on_other_count = sum(
            1 for o in surfaced if o.loop_state is LoopState.WAITING_ON_OTHER
        )
        empty = blocked_on_you_count == 0 and waiting_on_other_count == 0

        message = self._compose_digest(blocked_on_you_count, waiting_on_other_count)

        # Req 11.1 / 11.6: deliver to the user, retrying up to 3 attempts on failure.
        send_error: Optional[str] = None
        sent = False
        for _ in range(DIGEST_MAX_ATTEMPTS):
            try:
                self._send_as_user(user_id, message)
                sent = True
                break
            except Exception as exc:  # noqa: BLE001 — any send failure is retryable.
                send_error = str(exc)

        if not sent:
            # Req 11.6: all attempts failed — record an error, leave Obligation state
            # unchanged (this method never writes the graph), and do NOT advance the
            # 24h window so the next scheduled time retries delivery.
            return DigestResult(
                sent=False,
                suppressed=False,
                empty=empty,
                blocked_on_you_count=blocked_on_you_count,
                waiting_on_other_count=waiting_on_other_count,
                message=message,
                error=(
                    f"Daily digest was not delivered after {DIGEST_MAX_ATTEMPTS} "
                    f"attempts: {send_error}"
                ),
            )

        # Successful delivery advances the 24h window (Req 11.5).
        self._last_digest_sent_at = now_iso
        return DigestResult(
            sent=True,
            suppressed=False,
            empty=empty,
            blocked_on_you_count=blocked_on_you_count,
            waiting_on_other_count=waiting_on_other_count,
            message=message,
        )


def build_claude_draft_client(settings: Any | None = None) -> ClaudeDraftPort:
    """Wire a real Claude-backed :data:`ClaudeDraftPort` for Polite_Nudge drafting.

    Reads ANTHROPIC_API_KEY and the drafting model via ``loop.config.get_settings``
    (or an injected ``settings``). The ``anthropic`` SDK and the network call are
    imported/made lazily *inside* the returned callable, so importing this module —
    and constructing the client — never touches the network or requires the
    ``anthropic`` package to be installed.

    The returned port enforces the 10-second drafting budget (Req 7.1, 7.8) by
    bounding the call and **raising on timeout**; the Action Agent treats any raise as
    a draft failure and sends nothing (Req 7.8). The prompt instructs the model to
    reference the sender, the summarized subject, and the source message timestamp so
    the drafted text satisfies Req 7.1; the Action Agent additionally clips the result
    to :data:`NUDGE_DRAFT_MAX_CHARS`.

    NOTE: this is the minimal real-call wiring; the Action Agent's nudge logic and all
    tests exercise drafting through an injected mock port, never this live client.
    """
    if settings is None:
        from loop.config import get_settings

        settings = get_settings()
    settings.require("anthropic_api_key")
    api_key = settings.anthropic_api_key
    # Drafting is a cheap, high-volume task — use the Haiku tier (design: Claude
    # drafting port). The model name is configurable via settings.
    model = settings.haiku_model

    def _client(obligation: Obligation) -> str:
        import concurrent.futures

        def _draft() -> str:
            from anthropic import Anthropic

            client = Anthropic(api_key=api_key)
            prompt = (
                "Draft a short, polite Slack reminder I will send as myself to nudge "
                "an open loop. Keep it under 1000 characters, warm and concise.\n"
                f"Person to nudge (sender of the source message): {obligation.owed_person_id}\n"
                f"Subject of the loop: {obligation.subject_summary}\n"
                f"Source message timestamp: {obligation.source_msg_ts}\n"
                "Reference the sender, the subject, and roughly when the original "
                "message was, but do not invent details. Respond with ONLY the "
                "message text."
            )
            message = client.messages.create(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(
                getattr(block, "text", "") for block in getattr(message, "content", [])
            )

        # Enforce the 10s drafting budget across the whole round-trip; a timeout
        # raises (handled as a draft failure -> send nothing, Req 7.8).
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_draft)
            return future.result(timeout=NUDGE_DRAFT_TIMEOUT_SECONDS)

    return _client


__all__ = [
    "ActionAgent",
    "AutoCloseResult",
    "SnoozeResult",
    "DelegateResult",
    "DigestResult",
    "NudgeDraft",
    "NudgeDraftResult",
    "NudgeSendResult",
    "AUTO_CLOSE_REASON",
    "NUDGE_DRAFT_MAX_CHARS",
    "NUDGE_DRAFT_TIMEOUT_SECONDS",
    "SNOOZE_MIN",
    "SNOOZE_MAX",
    "SNOOZE_DEFAULT",
    "DELEGATE_MAX_ATTEMPTS",
    "DIGEST_MAX_ATTEMPTS",
    "DIGEST_WINDOW",
    "build_claude_draft_client",
]
