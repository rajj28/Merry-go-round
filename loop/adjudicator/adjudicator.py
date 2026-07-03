"""The Adjudicator — Opus 4.8 "whose court is the ball in?" reasoning (task 5.1).

This module implements the Adjudicator (Reason / Agent 2). It takes a
``CandidateMessage`` forwarded by the Watcher, asks Opus 4.8 whether the candidate
is a *real* open loop that involves the user and in which direction, assigns a
Confidence_Score, and writes the resulting ``Obligation`` to the shared Obligation
Graph (design.md → "Adjudicator (Reason)"; Req 3.1, 3.2, 3.3).

It builds strictly on the frozen day-1 contracts:
  * value shapes — :mod:`loop.graph.models` (``Obligation``, ``LoopState`` …)
  * store contract — :mod:`loop.graph.store` (``ObligationGraph``, ``Result``)
  * watcher input — :class:`loop.watcher.rts_contract.CandidateMessage`

Two-tier funnel (design.md → "Two-tier LLM strategy"): the Watcher (Haiku, high
recall) produces candidates; the Adjudicator (Opus, high precision) is the final
filter. This module realizes the Opus stage.

Injectable reasoning client (a thin port)
------------------------------------------
The Opus call is reached through an injected callable (:class:`LoopReasoningClient`)
so the Adjudicator never hard-depends on a live Anthropic call and is trivially
mockable in tests. ``build_opus_reasoning_client`` wires the real client from
``loop.config`` (ANTHROPIC_API_KEY + Opus model) **lazily** — the ``anthropic``
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
  * Opus error/unreachable (Req 3.7): the Opus port call is wrapped so any raised
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
from typing import Any, Optional, Protocol

from loop.adjudicator.pr_ref import extract_pr_ref
from loop.graph.models import (
    ArtifactType,
    LoopState,
    Obligation,
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
    """Which party owes the response, as judged by Opus (maps to Loop_State, Req 3.2).

    USER_OWES   the user owes the reply       -> LoopState.BLOCKED_ON_YOU
    OTHER_OWES  the other party owes the reply -> LoopState.WAITING_ON_OTHER
    UNKNOWN     direction could not be determined -> discard (Req 3.4, task 5.2)
    """

    USER_OWES = "user_owes"
    OTHER_OWES = "other_owes"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OpusAdjudication:
    """The structured judgement the Opus reasoning port returns for a candidate.

    This is the port's *output contract* — a pure value object, free of any
    Anthropic types — so the real client and any test double agree on the shape:

      is_loop         is this a genuine open loop at all? (Req 3.1)
      involves_user   is the tracked user one of the two parties? (Req 3.1)
      direction       whose court the ball is in (Req 3.2)
      confidence      certainty in [0.0, 1.0] (clamped on use, Req 3.3)
      subject_summary one-line summary of the loop for the UI
    """

    is_loop: bool
    involves_user: bool
    direction: Direction
    confidence: float
    subject_summary: str = ""


class LoopReasoningClient(Protocol):
    """The thin port the Adjudicator calls to obtain an Opus judgement.

    Implementations take the candidate plus context (the tracked ``user_id``) and
    return a structured :class:`OpusAdjudication`. Keeping this a narrow callable
    makes the Adjudicator trivially mockable and keeps the live Anthropic call out
    of import time (see :func:`build_opus_reasoning_client`).
    """

    def __call__(
        self, candidate: CandidateMessage, *, user_id: UserId
    ) -> OpusAdjudication:
        ...


class AdjudicationOutcome(str, Enum):
    """What the Adjudicator did with a candidate.

    CREATED    a new Obligation was written to the graph (happy path).
    UPDATED    an existing Obligation (same source message) was updated.
    DISCARDED  no graph change — not a loop / no user / unknown direction (Req 3.4).
    ERROR      a graph write failed (persist failure) or Opus errored (task 5.2).
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
      * an Opus error/unreachable populates ``error_message`` with the recorded error
        indication (Req 3.7).
    ``surfacing_eligible`` reports quiet-by-default eligibility on a successful write —
    ``confidence_score >= current threshold`` (Req 3.5, 3.6); it stays ``None`` on
    discard/error paths.
    """

    outcome: AdjudicationOutcome
    obligation: Optional[Obligation] = None
    discard_reason: Optional[str] = None
    error: Optional[GraphError] = None
    error_message: Optional[str] = None  # Opus error indication (Req 3.7)
    surfacing_eligible: Optional[bool] = None  # confidence >= threshold (Req 3.5/3.6)


# Direction -> (Loop_State, who-owes-resolver). The resolver returns the
# (owes_person_id, owed_person_id) edge endpoints given the user and the other
# party, encoding Req 3.2 exactly: the edge points owes -> owed.
_DIRECTION_TO_STATE: dict[Direction, LoopState] = {
    Direction.USER_OWES: LoopState.BLOCKED_ON_YOU,
    Direction.OTHER_OWES: LoopState.WAITING_ON_OTHER,
}


def _slack_ts_to_iso(ts: str) -> Optional[str]:
    """Convert a Slack message ts (epoch seconds, e.g. '1700000000.000100') to ISO
    8601 UTC. Returns ``None`` if the ts is missing or unparseable, so the caller
    can fall back to the current time."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _obligation_id_for(channel: str, ts: str) -> str:
    """Generate a stable obligation_id from the source message reference.

    Deterministic by ``(channel, ts)`` so the same Slack message always resolves to
    the same id — re-adjudication updates the existing edge rather than duplicating
    it (complements the Watcher's dedup, Req 2.9, and the store's last-write-wins,
    Req 1.9/1.10)."""
    return uuid.uuid5(_OBLIGATION_NAMESPACE, f"{channel}:{ts}").hex


class Adjudicator:
    """Opus-backed reasoning over Watcher candidates (Req 3.1–3.3).

    Construct with an injected :class:`LoopReasoningClient` (the Opus port) and the
    shared :class:`ObligationGraph` store. ``adjudicate`` runs one candidate through
    Opus and, on a real user-involving loop, writes the directed Obligation.
    """

    def __init__(self, client: LoopReasoningClient, graph: ObligationGraph) -> None:
        self._client = client
        self._graph = graph

    def adjudicate(
        self, candidate: CandidateMessage, *, user_id: UserId
    ) -> AdjudicationResult:
        """Adjudicate one candidate and, on a real loop, upsert the Obligation.

        Happy path (Req 3.1–3.3): Opus reports a real loop involving the user with a
        known direction. We map direction -> Loop_State (Req 3.2), build the directed
        Obligation, clamp the Confidence_Score into [0.0, 1.0] (Req 3.3), and upsert
        it to the graph, returning CREATED or UPDATED with the stored value and the
        quiet-by-default ``surfacing_eligible`` flag (Req 3.5, 3.6).

        Discard (Req 3.4): not-a-loop / no-user / unknown-direction → DISCARDED with a
        reason and no graph write.

        Opus error (Req 3.7): if the Opus port raises, return ERROR with the graph
        left unchanged and the error indication recorded; no Obligation is created.
        """
        # --- Opus error/unreachable (Req 3.7): wrap the port call --------------
        try:
            judgement = self._client(candidate, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 — any Opus failure is recorded, not raised
            # Discard the candidate, leave the graph unchanged, record the error
            # indication. No Obligation is created.
            return AdjudicationResult(
                AdjudicationOutcome.ERROR,
                error_message=f"opus adjudication failed: {exc!r}",
            )

        # --- discard guard (Req 3.4): no graph change on these branches --------
        if not judgement.is_loop:
            return AdjudicationResult(
                AdjudicationOutcome.DISCARDED, discard_reason="not a loop"
            )
        if not judgement.involves_user:
            return AdjudicationResult(
                AdjudicationOutcome.DISCARDED, discard_reason="does not involve user"
            )
        if judgement.direction not in _DIRECTION_TO_STATE:
            return AdjudicationResult(
                AdjudicationOutcome.DISCARDED, discard_reason="unknown direction"
            )

        # --- happy path: build the directed Obligation and write it -------------
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


def build_opus_reasoning_client(settings: Any | None = None) -> LoopReasoningClient:
    """Wire a real smart-tier ``LoopReasoningClient`` from config.

    Provider-agnostic: routes through :func:`loop.llm.chat` at the **smart tier**
    (high-precision reasoning), which by default is the configured Groq model
    (``llama-3.3-70b-versatile``) and falls back to the Anthropic Opus model when
    ``llm_provider="anthropic"``. The provider SDK and the network call are made
    lazily *inside* the returned callable (via ``loop.llm``), so importing this
    module — and constructing the client — never touches the network or requires a
    provider SDK installed. The required credential for the active provider is
    validated at call time.

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

    def _client(
        candidate: CandidateMessage, *, user_id: UserId
    ) -> OpusAdjudication:
        import json

        from loop.llm import chat

        prompt = (
            "You decide whose court the ball is in for a Slack message.\n"
            f"Tracked user id: {user_id}\n"
            f"Message author id: {candidate.author_id}\n"
            f"Channel: {candidate.channel_id}\n"
            f"Message: {candidate.text}\n\n"
            "Respond ONLY with JSON: {\"is_loop\": bool, \"involves_user\": bool, "
            "\"direction\": \"user_owes\"|\"other_owes\"|\"unknown\", "
            "\"confidence\": float 0..1, \"subject_summary\": string}."
        )
        text = chat(
            [{"role": "user", "content": prompt}],
            tier="smart",
            settings=settings,
            json_mode=True,
            max_tokens=512,
        )
        data = json.loads(text)
        return OpusAdjudication(
            is_loop=bool(data["is_loop"]),
            involves_user=bool(data["involves_user"]),
            direction=Direction(data.get("direction", "unknown")),
            confidence=float(data.get("confidence", 0.0)),
            subject_summary=str(data.get("subject_summary", "")),
        )

    return _client


__all__ = [
    "Direction",
    "OpusAdjudication",
    "LoopReasoningClient",
    "AdjudicationOutcome",
    "AdjudicationResult",
    "Adjudicator",
    "build_opus_reasoning_client",
]
