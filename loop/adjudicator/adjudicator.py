"""The Adjudicator — the smart tier 4.8 "whose court is the ball in?" reasoning (task 5.1).

This module implements the Adjudicator (Reason / Agent 2). It takes a
``CandidateMessage`` forwarded by the Watcher, asks the smart tier 4.8 whether the candidate
is a *real* open loop that involves the user and in which direction, assigns a
Confidence_Score, and writes the resulting ``Obligation`` to the shared Obligation
Graph (design.md → "Adjudicator (Reason)"; Req 3.1, 3.2, 3.3).

It builds strictly on the frozen day-1 contracts:
  * value shapes — :mod:`loop.graph.models` (``Obligation``, ``LoopState`` …)
  * store contract — :mod:`loop.graph.store` (``ObligationGraph``, ``Result``)
  * watcher input — :class:`loop.watcher.rts_contract.CandidateMessage`

Two-tier funnel (design.md → "Two-tier LLM strategy"): the Watcher (fast-tier, high
recall) produces candidates; the Adjudicator (the smart tier, high precision) is the final
filter. This module realizes the smart-tier stage.

Injectable reasoning client (a thin port)
------------------------------------------
The the smart tier call is reached through an injected callable (:class:`LoopReasoningClient`)
so the Adjudicator never hard-depends on a live Anthropic call and is trivially
mockable in tests. ``build_smart_reasoning_client`` wires the real client from
``loop.config`` (ANTHROPIC_API_KEY + the smart tier model) **lazily** — the ``anthropic``
package and the network connection are imported/opened *inside* the returned
callable, so merely importing this module never touches the network.

Direction mapping (Req 3.2, exact)
----------------------------------
The edge of an Obligation always points from the party who **owes** a response to
the party who is **owed** one (Req 1.1). Therefore:

    user owes the response   -> LoopState.BLOCKED_ON_YOU
                                owes = user,           owed = other party
    the other owes the reply -> LoopState.WAITING_ON_OTHER
                                owes = other party,    owed = user

Scope note (task 5.1 + 5.2)
---------------------------
Task 5.1 implemented the **happy-path write**. Task 5.2 completes the Adjudicator:

  * Discard with no graph change for not-a-loop / no-user / unknown-direction
    candidates (Req 3.4) — the structural seam, now verified and solidified.
  * smart-tier error/unreachable (Req 3.7): the smart-tier port call is wrapped so any raised
    exception yields an ``ERROR`` outcome with **no** graph write and a recorded
    error indication (``AdjudicationResult.error_message``).
  * Quiet-by-default eligibility (Req 3.5, 3.6): after a successful write the
    Adjudicator reports ``surfacing_eligible = confidence_score >= threshold`` using
    the store's current ``get_threshold()``. This only *reports* eligibility — the
    authoritative "is this shown?" predicate lives in
    :func:`loop.graph.surfacing.is_surfaced`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional, Protocol

from loop.adjudicator.pr_ref import extract_pr_ref
from loop.graph.models import (
    ArtifactType,
    LoopState,
    Obligation,
    ObligationKind,
    PersonId,
    UserId,
    clamp_confidence,
    utc_now_iso,
)
from loop.graph.store import GraphError, ObligationGraph, is_ok
from loop.watcher.rts_contract import CandidateMessage

# Stable namespace so the same source message always maps to the same
# obligation_id. Deriving the id from the source message reference (the Watcher's
# dedup key, Req 2.9) keeps re-adjudication of one message *idempotent*: it updates
# the existing row under last-write-wins rather than creating a duplicate.
_OBLIGATION_NAMESPACE = uuid.UUID("6f9b1d8e-0e2a-4d3c-9a1b-1d6c0a4f7e21")


class Direction(str, Enum):
    """Which party owes the response, as judged by the smart tier (maps to Loop_State, Req 3.2).

    USER_OWES   the user owes the reply       -> LoopState.BLOCKED_ON_YOU
    OTHER_OWES  the other party owes the reply -> LoopState.WAITING_ON_OTHER
    UNKNOWN     direction could not be determined -> discard (Req 3.4, task 5.2)
    """

    USER_OWES = "user_owes"
    OTHER_OWES = "other_owes"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SmartAdjudication:
    """The structured judgement the smart-tier reasoning port returns for a candidate.

    This is the port's *output contract* — a pure value object, free of any
    Anthropic types — so the real client and any test double agree on the shape:

      is_loop         is this a genuine open loop at all? (Req 3.1)
      involves_user   is the tracked user one of the two parties? (Req 3.1)
      direction       whose court the ball is in (Req 3.2)
      confidence      certainty in [0.0, 1.0] (clamped on use, Req 3.3)
      subject_summary one-line summary of the loop for the UI
      kind            "reply" for a classic owed response; "meeting" when the
                      message proposes a meeting ("let's meet tomorrow at 4 PM")
      due_at          ISO 8601 proposed meeting time extracted by the smart
                      tier, or None when absent/unparseable
      owes_id/owed_id explicit party extraction (both-or-nothing). When the
                      client names both edge endpoints — e.g. from the author id
                      and an ``<@U…>`` mention — the Adjudicator uses them
                      directly, which is what lets **third-party** loops (Priya
                      owes Marco) enter the graph and feed the workspace map,
                      chains, and deadlock detection. When either is ``None``
                      the legacy user-centric direction mapping applies.
    """

    is_loop: bool
    involves_user: bool
    direction: Direction
    confidence: float
    subject_summary: str = ""
    kind: str = "reply"
    due_at: Optional[str] = None
    owes_id: Optional[PersonId] = None
    owed_id: Optional[PersonId] = None


class LoopReasoningClient(Protocol):
    """The thin port the Adjudicator calls to obtain a smart-tier judgement.

    Implementations take the candidate plus context (the tracked ``user_id``) and
    return a structured :class:`SmartAdjudication`. Keeping this a narrow callable
    makes the Adjudicator trivially mockable and keeps the live Anthropic call out
    of import time (see :func:`build_smart_reasoning_client`).
    """

    def __call__(
        self, candidate: CandidateMessage, *, user_id: UserId
    ) -> SmartAdjudication:
        ...


class AdjudicationOutcome(str, Enum):
    """What the Adjudicator did with a candidate.

    CREATED    a new Obligation was written to the graph (happy path).
    UPDATED    an existing Obligation (same source message) was updated.
    DISCARDED  no graph change — not a loop / no user / unknown direction (Req 3.4).
    ERROR      a graph write failed (persist failure) or smart-tier errored (task 5.2).
    """

    CREATED = "created"
    UPDATED = "updated"
    DISCARDED = "discarded"
    ERROR = "error"


@dataclass(frozen=True)
class AdjudicationResult:
    """The outcome of adjudicating one candidate.

    On a successful write ``outcome`` is CREATED or UPDATED and ``obligation`` carries
    the stored value. On a discard ``outcome`` is DISCARDED with a ``discard_reason``.
    On the ERROR path ``outcome`` is ERROR and the graph is left unchanged:
      * a graph persist failure populates ``error`` with the store's :class:`GraphError`
        (Req 1.8);
      * a smart-tier error/unreachable populates ``error_message`` with the recorded error
        indication (Req 3.7).
    ``surfacing_eligible`` reports quiet-by-default eligibility on a successful write —
    ``confidence_score >= current threshold`` (Req 3.5, 3.6); it stays ``None`` on
    discard/error paths.
    """

    outcome: AdjudicationOutcome
    obligation: Optional[Obligation] = None
    discard_reason: Optional[str] = None
    error: Optional[GraphError] = None
    error_message: Optional[str] = None  # smart-tier error indication (Req 3.7)
    surfacing_eligible: Optional[bool] = None  # confidence >= threshold (Req 3.5/3.6)


# Direction -> (Loop_State, who-owes-resolver). The resolver returns the
# (owes_person_id, owed_person_id) edge endpoints given the user and the other
# party, encoding Req 3.2 exactly: the edge points owes -> owed.
_DIRECTION_TO_STATE: dict[Direction, LoopState] = {
    Direction.USER_OWES: LoopState.BLOCKED_ON_YOU,
    Direction.OTHER_OWES: LoopState.WAITING_ON_OTHER,
}


def _clean_person_id(value: Any) -> Optional[PersonId]:
    """Normalize a judged party id: strip mention syntax, reject junk.

    Accepts a bare Slack id (``U0AB12CD3``), a mention (``<@U0AB12CD3>`` or
    ``<@U0AB12CD3|display>``), or an ``@``-prefixed id. Anything empty,
    non-string, or not id-shaped yields ``None`` so the caller falls back to the
    legacy direction mapping instead of writing a garbage endpoint.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip("<>").lstrip("@").split("|", 1)[0].strip()
    if not cleaned or not cleaned.replace("_", "").isalnum():
        return None
    return cleaned


def _explicit_endpoints(
    judgement: SmartAdjudication,
) -> Optional[tuple[PersonId, PersonId]]:
    """The judgement's explicit ``(owes, owed)`` endpoints, or ``None``.

    Both-or-nothing: a judgement that names only one party gives no usable edge,
    so it falls back to the legacy user-centric mapping.
    """
    owes = _clean_person_id(judgement.owes_id)
    owed = _clean_person_id(judgement.owed_id)
    if owes is None or owed is None:
        return None
    return owes, owed


def _slack_ts_to_iso(ts: str) -> Optional[str]:
    """Convert a Slack message ts (epoch seconds, e.g. '1700000000.000100') to ISO
    8601 UTC. Returns ``None`` if the ts is missing or unparseable, so the caller
    can fall back to the current time."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _judged_kind(judgement: SmartAdjudication) -> ObligationKind:
    """Map the judgement's ``kind`` string onto the frozen enum, defaulting to REPLY.

    Anything the smart tier returns outside the frozen vocabulary is treated as a
    classic reply loop, so a hallucinated kind can never break the write path.
    """
    if str(judgement.kind).strip().lower() == ObligationKind.MEETING.value:
        return ObligationKind.MEETING
    return ObligationKind.REPLY


def _judged_due_at(judgement: SmartAdjudication) -> Optional[str]:
    """The judgement's ``due_at`` as normalized ISO 8601 UTC, or ``None``.

    The smart tier extracts free text ("tomorrow at 4 PM") into ISO; this guard
    re-parses it so only a real timestamp is ever persisted. A naive value is
    assumed UTC, matching the coercion used across the store and surfaces.
    """
    raw = judgement.due_at
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _obligation_id_for(channel: str, ts: str) -> str:
    """Generate a stable obligation_id from the source message reference.

    Deterministic by ``(channel, ts)`` so the same Slack message always resolves to
    the same id — re-adjudication updates the existing edge rather than duplicating
    it (complements the Watcher's dedup, Req 2.9, and the store's last-write-wins,
    Req 1.9/1.10)."""
    return uuid.uuid5(_OBLIGATION_NAMESPACE, f"{channel}:{ts}").hex


class Adjudicator:
    """the smart tier-backed reasoning over Watcher candidates (Req 3.1–3.3).

    Construct with an injected :class:`LoopReasoningClient` (the smart-tier port) and the
    shared :class:`ObligationGraph` store. ``adjudicate`` runs one candidate through
    the smart tier and, on a real user-involving loop, writes the directed Obligation.
    """

    def __init__(self, client: LoopReasoningClient, graph: ObligationGraph) -> None:
        self._client = client
        self._graph = graph

    def adjudicate(
        self, candidate: CandidateMessage, *, user_id: UserId
    ) -> AdjudicationResult:
        """Adjudicate one candidate and, on a real loop, upsert the Obligation.

        Happy path (Req 3.1–3.3): the smart tier reports a real loop involving the user with a
        known direction. We map direction -> Loop_State (Req 3.2), build the directed
        Obligation, clamp the Confidence_Score into [0.0, 1.0] (Req 3.3), and upsert
        it to the graph, returning CREATED or UPDATED with the stored value and the
        quiet-by-default ``surfacing_eligible`` flag (Req 3.5, 3.6).

        Discard (Req 3.4): not-a-loop / no-user / unknown-direction → DISCARDED with a
        reason and no graph write.

        smart-tier error (Req 3.7): if the smart-tier port raises, return ERROR with the graph
        left unchanged and the error indication recorded; no Obligation is created.
        """
        # --- smart-tier error/unreachable (Req 3.7): wrap the port call --------------
        try:
            judgement = self._client(candidate, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 — any the smart tier failure is recorded, not raised
            # Discard the candidate, leave the graph unchanged, record the error
            # indication. No Obligation is created.
            return AdjudicationResult(
                AdjudicationOutcome.ERROR,
                error_message=f"smart-tier adjudication failed: {exc!r}",
            )

        # --- discard guard (Req 3.4): no graph change on these branches --------
        if not judgement.is_loop:
            return AdjudicationResult(
                AdjudicationOutcome.DISCARDED, discard_reason="not a loop"
            )

        explicit = _explicit_endpoints(judgement)
        if explicit is not None:
            # Explicit party extraction: the edge endpoints came straight from
            # the judgement, so loops between two *other* people are written
            # too — they feed the workspace map, chains, and deadlock rings.
            # State stays user-centric: BLOCKED_ON_YOU iff the user owes;
            # everything else (including third-party edges) is an open loop
            # the user is not on the hook for. Personal surfaces scope by
            # endpoint, so a third-party edge never lands on someone's list.
            owes_id, owed_id = explicit
            if owes_id == owed_id:
                return AdjudicationResult(
                    AdjudicationOutcome.DISCARDED,
                    discard_reason="self-loop endpoints",
                )
            loop_state = (
                LoopState.BLOCKED_ON_YOU
                if owes_id == user_id
                else LoopState.WAITING_ON_OTHER
            )
        else:
            # Legacy user-centric mapping: the counterparty is the author.
            if not judgement.involves_user:
                return AdjudicationResult(
                    AdjudicationOutcome.DISCARDED,
                    discard_reason="does not involve user",
                )
            if judgement.direction not in _DIRECTION_TO_STATE:
                return AdjudicationResult(
                    AdjudicationOutcome.DISCARDED, discard_reason="unknown direction"
                )
            loop_state = _DIRECTION_TO_STATE[judgement.direction]
            owes_id, owed_id = self._edge_endpoints(
                judgement.direction, user_id=user_id, other_id=candidate.author_id
            )

        obligation_id = _obligation_id_for(
            candidate.channel_id, candidate.message_ts
        )
        last_touch = _slack_ts_to_iso(candidate.message_ts) or utc_now_iso()

        # Live auto-close wiring: if the message names a GitHub PR (shorthand or URL),
        # ground the obligation in it so the Verifier/Action auto-close beat can fire
        # (Req 4, 8). No PR reference -> both fields stay None (unchanged behaviour).
        pr_ref = extract_pr_ref(candidate.text)
        artifact_type = ArtifactType.GITHUB_PR if pr_ref else None

        obligation = Obligation(
            obligation_id=obligation_id,
            owes_person_id=owes_id,
            owed_person_id=owed_id,
            owner_person_id=owes_id,  # the party who owes currently owns the loop
            loop_state=loop_state,
            confidence_score=clamp_confidence(judgement.confidence),  # Req 3.3
            last_touch_timestamp=last_touch,
            source_msg_channel=candidate.channel_id,  # Req 1.6 source reference
            source_msg_ts=candidate.message_ts,
            subject_summary=judgement.subject_summary,
            kind=_judged_kind(judgement),
            due_at=_judged_due_at(judgement),
            artifact_type=artifact_type,
            artifact_ref=pr_ref,
        )

        existed = self._graph.get(obligation_id) is not None
        result = self._graph.upsert(obligation)

        if not is_ok(result):
            # Persist failure: the prior value (if any) was retained by the store
            # (Req 1.8). Surface the error indication to the caller.
            return AdjudicationResult(
                AdjudicationOutcome.ERROR, error=result.error
            )

        # Quiet-by-default eligibility (Req 3.5, 3.6): the obligation is eligible to
        # surface iff its Confidence_Score is at or above the current threshold. This
        # only *reports* eligibility — the authoritative surfacing predicate lives in
        # loop.graph.surfacing.is_surfaced.
        stored = result.value
        surfacing_eligible = stored.confidence_score >= self._graph.get_threshold()

        return AdjudicationResult(
            AdjudicationOutcome.UPDATED if existed else AdjudicationOutcome.CREATED,
            obligation=stored,
            surfacing_eligible=surfacing_eligible,
        )

    @staticmethod
    def _edge_endpoints(
        direction: Direction, *, user_id: PersonId, other_id: PersonId
    ) -> tuple[PersonId, PersonId]:
        """Return ``(owes_person_id, owed_person_id)`` for a direction (Req 3.2, 1.1).

        The edge points from who owes to who is owed:
          USER_OWES  -> (user,  other)   => blocked-on-you
          OTHER_OWES -> (other, user)    => waiting-on-other
        """
        if direction is Direction.USER_OWES:
            return user_id, other_id
        return other_id, user_id


def _build_default_tz_lookup(settings: Any) -> "Callable[[str], Optional[int]]":
    """A lazy, cached ``author_id -> tz_offset seconds`` resolver via ``users.info``.

    The slack_sdk import and the network call happen inside the returned callable
    (matching this module's lazy-wiring discipline), results are cached per
    process, and any failure — no token, SDK missing, API error — yields ``None``
    so the caller falls back to treating the send time as UTC.
    """
    cache: dict[str, Optional[int]] = {}

    def _lookup(author_id: str) -> Optional[int]:
        if not author_id:
            return None
        if author_id in cache:
            return cache[author_id]
        offset: Optional[int] = None
        try:
            from slack_sdk import WebClient

            token = getattr(settings, "slack_bot_token", "")
            if token:
                resp = WebClient(token=token).users_info(user=author_id)
                raw = (resp.get("user") or {}).get("tz_offset")
                if isinstance(raw, (int, float)):
                    offset = int(raw)
        except Exception:  # noqa: BLE001 — tz is best-effort; UTC is the fallback.
            offset = None
        cache[author_id] = offset
        return offset

    return _lookup


def build_smart_reasoning_client(
    settings: Any | None = None,
    tz_lookup: "Optional[Callable[[str], Optional[int]]]" = None,
) -> LoopReasoningClient:
    """Wire a real smart-tier ``LoopReasoningClient`` from config.

    Provider-agnostic: routes through :func:`loop.llm.chat` at the **smart tier**
    (high-precision reasoning), which by default is the configured Groq model
    (``llama-3.3-70b-versatile``) and falls back to the Anthropic Opus model when
    ``llm_provider="anthropic"``. The provider SDK and the network call are made
    lazily *inside* the returned callable (via ``loop.llm``), so importing this
    module — and constructing the client — never touches the network or requires a
    provider SDK installed. The required credential for the active provider is
    validated at call time.

    ``tz_lookup`` resolves an author id to their Slack ``tz_offset`` (seconds from
    UTC) so a spoken time like "4 PM" is interpreted in the **author's** local
    timezone, not UTC. When omitted, a lazy cached ``users.info`` resolver is
    built from the settings' bot token (:func:`_build_default_tz_lookup`); a
    ``None`` offset falls back to UTC.

    NOTE: the prompt/response parsing here is the minimal real-call wiring; the
    Watcher/Adjudicator logic and all tests exercise the Adjudicator through an
    injected mock port, never this live client.
    """
    if settings is None:
        from loop.config import get_settings

        settings = get_settings()
    # Validate the active provider's credential at build time (provider-agnostic).
    from loop.llm import require_provider_key

    require_provider_key(settings)
    if tz_lookup is None:
        tz_lookup = _build_default_tz_lookup(settings)

    def _client(
        candidate: CandidateMessage, *, user_id: UserId
    ) -> SmartAdjudication:
        import json
        from datetime import timedelta

        from loop.llm import chat

        sent_at = _slack_ts_to_iso(candidate.message_ts) or utc_now_iso()
        # Anchor spoken times ("4 PM", "tomorrow") to the author's wall clock:
        # shift the UTC send instant by their Slack tz_offset (best-effort).
        offset_seconds = tz_lookup(candidate.author_id)
        if offset_seconds is not None:
            local_tz = timezone(timedelta(seconds=offset_seconds))
            sent_local = datetime.fromisoformat(sent_at).astimezone(local_tz)
            author_time_line = (
                f"Author's local time when sent: {sent_local.isoformat()} "
                f"(UTC offset {offset_seconds / 3600:+.1f}h)\n"
            )
        else:
            author_time_line = (
                "Author's local time when sent: unknown — assume UTC.\n"
            )
        prompt = (
            "You analyze a Slack message for an 'open loop': one person owing "
            "another a concrete response, deliverable, or decision.\n"
            f"Tracked user id: {user_id}\n"
            f"Message author id: {candidate.author_id}\n"
            f"Channel: {candidate.channel_id}\n"
            f"Message sent at (UTC): {sent_at}\n"
            f"{author_time_line}"
            f"Message: {candidate.text}\n\n"
            "Identify the two parties of the loop as Slack user ids:\n"
            "- owes_id: who owes the next action (must deliver/reply)\n"
            "- owed_id: who is waiting on it\n"
            "Use ids visible in the message (<@U...> mentions), the author id, "
            "or the tracked user id. The parties do NOT need to include the "
            "tracked user — a loop between two other people counts. An ask "
            "('can you review X?') means the person asked owes the author; a "
            "promise ('I'll send X tomorrow') means the author owes the "
            "recipient. If you cannot identify both parties, use null.\n\n"
            "subject_summary: a short imperative description of the deliverable "
            "itself (e.g. 'Send load-test results by EOD') — never refer to "
            "'the author', 'the user', or 'the mentioned user'.\n\n"
            "kind: 'meeting' when the message proposes a meeting/call/sync "
            "('let's meet tomorrow at 4 PM', 'can we hop on a call Friday?'); "
            "otherwise 'reply'. A meeting proposal IS an open loop — it stays "
            "open until the other party confirms.\n"
            "due_at: for a meeting, the proposed time as an ISO 8601 UTC "
            "timestamp. Interpret spoken times ('4 PM', 'tomorrow') on the "
            "author's local clock (see the author's local time above), then "
            "convert the result to UTC (e.g. 'tomorrow at 4 PM' said at local "
            "2026-07-09, offset +2.0h -> '2026-07-10T14:00:00+00:00'); null "
            "when no concrete time is stated or kind is 'reply'.\n\n"
            "Respond ONLY with JSON: {\"is_loop\": bool, "
            "\"owes_id\": string|null, \"owed_id\": string|null, "
            "\"confidence\": float 0..1, \"subject_summary\": string, "
            "\"kind\": \"reply\"|\"meeting\", \"due_at\": string|null}."
        )
        text = chat(
            [{"role": "user", "content": prompt}],
            tier="smart",
            settings=settings,
            json_mode=True,
            max_tokens=512,
        )
        data = json.loads(text)
        owes = _clean_person_id(data.get("owes_id"))
        owed = _clean_person_id(data.get("owed_id"))
        # Derive the legacy user-centric fields from the extracted parties so
        # the dataclass stays coherent for both the endpoint path and (when a
        # party is missing) the direction-mapping fallback.
        involves_user = user_id in (owes, owed) if owes and owed else False
        if owes and owed and owes == user_id:
            direction = Direction.USER_OWES
        elif owes and owed and owed == user_id:
            direction = Direction.OTHER_OWES
        else:
            direction = Direction.UNKNOWN
        return SmartAdjudication(
            is_loop=bool(data["is_loop"]),
            involves_user=involves_user,
            direction=direction,
            confidence=float(data.get("confidence", 0.0)),
            subject_summary=str(data.get("subject_summary", "")),
            kind=str(data.get("kind", "reply")),
            due_at=data.get("due_at") if isinstance(data.get("due_at"), str) else None,
            owes_id=owes,
            owed_id=owed,
        )

    return _client


__all__ = [
    "Direction",
    "SmartAdjudication",
    "LoopReasoningClient",
    "AdjudicationOutcome",
    "AdjudicationResult",
    "Adjudicator",
    "build_smart_reasoning_client",
]
