"""The Watcher (Perceive) — RTS periodic sweep with source-message dedup (task 4.1).

This module realizes design.md → "Components and Interfaces" → "1. Watcher
(Perceive)" for the **periodic Real-Time Search (RTS) sweep** path. It turns one
RTS round-trip into a stream of de-duplicated open-loop candidates, optimizing for
high recall (the precision filter is the Adjudicator, task 5).

Scope realized **here**:
  * ``run_sweep`` — triggered at a fixed configured interval ≤ 60s (Req 2.1). It
    calls the injected RTS port, parses the response via
    :func:`loop.watcher.rts_contract.parse_rts_response`, drops any candidate whose
    ``(channel_id, message_ts)`` source reference already corresponds to an existing
    Obligation in the graph (dedup, Req 2.9), and returns the **new** (non-duplicate)
    candidates as a :class:`SweepResult` (task 4.1).
  * RTS empty / ``ok=false`` → the sweep completes creating no obligations (Req 2.6).
  * RTS raising / unreachable → the failure is caught, the sweep completes creating
    no obligations, and the *next* scheduled sweep can retry because ``run_sweep``
    never lets the exception escape (Req 2.7).
  * ``on_message_event`` — live evaluation of a new message in a participating
    channel (Req 2.2, task 4.2). It turns a Slack message event into a
    :class:`CandidateMessage` and runs it through the **same** dedup → classify →
    forward pipeline as ``run_sweep`` via the shared :meth:`Watcher._handle_candidate`.
  * Haiku 4.5 high-recall classification + Adjudicator forwarding (task 4.3). The
    injected ``classify`` port (a :class:`HaikuClassifyClient`, candidate → bool) is
    the binary potential-loop classifier (Req 2.3); the Watcher forwards **every**
    positive candidate including low-certainty ones to the Adjudicator with its
    source message ref (Req 2.4, 2.5). When the classifier **raises** (Haiku
    failure), the candidate is excluded from forwarding and is **not** marked
    processed, so the next sweep that retrieves it re-evaluates it (Req 2.8).
    :func:`build_haiku_classify_client` wires the real Haiku client from
    :mod:`loop.config` lazily (network-free import).

Deliberately **out of scope here** (left as clean seams for later tasks):
  * Adjudicator hand-off — the real Adjudicator plugs into the ``forward`` seam
    (task 5); its task-4.x default is a no-op.
  * APScheduler wiring of the ≤60s cadence — task 17. This module only *exposes*
    ``run_sweep`` and the validated :attr:`Watcher.interval_seconds`.

Injectable RTS port
--------------------
The RTS call is reached through an injected callable (:class:`RtsClient`) so the
Watcher never hard-depends on a live Slack call and is trivially mockable in tests.
:func:`build_rts_client` wires the real client from :mod:`loop.config` lazily — the
``slack_sdk`` import and the network call happen only inside the returned callable,
so merely importing this module never touches the network. The call shape mirrors
the spike (``loop/spikes/rts_spike.py``: method ``assistant.search.context``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol

from loop.graph.store import ObligationFilter, ObligationGraph
from loop.watcher.rts_contract import CandidateMessage, parse_rts_response

logger = logging.getLogger(__name__)

# Watcher sweep interval must not exceed 60 seconds (Req 2.1). Mirrors the spike's
# high-recall, natural-language query so RTS triggers semantic search.
MAX_SWEEP_INTERVAL_SECONDS = 60

# RTS endpoint and the candidate-retrieval query, lifted from the task-1.2 spike so
# the real client and the Watcher share one definition.
RTS_API_METHOD = "assistant.search.context"
DEFAULT_OPEN_LOOP_QUERY = (
    "messages where someone is waiting on a reply, blocked on someone, "
    "asked a question that was never answered, or promised to follow up"
)


# --------------------------------------------------------------------------- #
# Injectable ports / seams
# --------------------------------------------------------------------------- #
class RtsClient(Protocol):
    """The thin port the Watcher calls to perform one RTS sweep query.

    Implementations issue the ``assistant.search.context`` query and return the raw
    response mapping (the shape :func:`parse_rts_response` understands). They MAY
    raise on a transport failure (unreachable / timeout / error); ``run_sweep``
    catches that and completes the sweep with no obligations so the next scheduled
    sweep can retry (Req 2.7).
    """

    def __call__(self) -> Mapping[str, Any]:
        ...


# A classify seam = the injectable Haiku classification port (task 4.3). Given a
# candidate it returns True to keep it (potential open loop, forward to Adjudicator)
# or False to drop it (Req 2.3). It MAY **raise** to signal a Haiku failure: the
# Watcher then excludes that candidate without marking it processed, so the next
# sweep that retrieves it re-evaluates it (Req 2.8). The default is pass-through so
# the Watcher works (and forwards every de-duplicated candidate) without Haiku wired.
ClassifyFn = Callable[[CandidateMessage], bool]


class HaikuClassifyClient(Protocol):
    """The thin port the Watcher calls to obtain a Haiku binary classification.

    Implementations take a :class:`CandidateMessage` and return ``True`` when the
    Haiku_Model judges it a *potential* open loop (keep, forward to the Adjudicator)
    or ``False`` when it is clearly not (drop) — tuned for **high recall**, so any
    plausible loop including low-certainty ones returns ``True`` (Req 2.3, 2.5).
    Implementations MAY **raise** on a model failure (timeout / error); the Watcher
    catches that, excludes the candidate from forwarding, and leaves it unprocessed
    so the next sweep re-evaluates it (Req 2.8). This is structurally a
    :data:`ClassifyFn`; the Protocol exists to name the role for ``build_*`` wiring.
    """

    def __call__(self, candidate: CandidateMessage) -> bool:
        ...


# A forward seam (task 5 plugs the Adjudicator here): receive a kept, non-duplicate
# candidate for adjudication. The default is a no-op so the Watcher does not depend
# on the Adjudicator existing yet.
ForwardFn = Callable[[CandidateMessage], None]


def _accept_all(_candidate: CandidateMessage) -> bool:
    """Default :class:`ClassifyFn`: pass every candidate through (Haiku replaces it)."""
    return True


def _noop_forward(_candidate: CandidateMessage) -> None:
    """Default :class:`ForwardFn`: do nothing (task 5 replaces with Adjudicator)."""
    return None


class CandidateOutcome(str, Enum):
    """What the shared per-candidate pipeline did with one candidate.

    FORWARDED         classify-positive and not a duplicate → handed to the forward
                      seam (the Adjudicator) with its source message ref (Req 2.4/2.5).
    DUPLICATE         its ``(channel, ts)`` source ref already maps to an Obligation
                      (or was already seen this pass) → skipped (dedup, Req 2.9).
    DROPPED           Haiku classified it as *not* an open loop → not forwarded.
    OUT_OF_SCOPE      its ``channel_id`` is not in the configured watch-channel
                      allow-list → not forwarded (live-detection scoping).
    CLASSIFY_FAILED   Haiku raised → excluded from forwarding and **not** marked
                      processed, so the next sweep re-evaluates it (Req 2.8).
    """

    FORWARDED = "forwarded"
    DUPLICATE = "duplicate"
    DROPPED = "dropped"
    OUT_OF_SCOPE = "out_of_scope"
    CLASSIFY_FAILED = "classify_failed"


def _coerce_str(value: Any) -> str:
    """Coerce a possibly-missing event field to a string (``None`` → "")."""
    return "" if value is None else str(value)


def candidate_from_message_event(event: Mapping[str, Any]) -> CandidateMessage:
    """Turn a Slack ``message`` event into a :class:`CandidateMessage` (Req 2.2).

    Maps the live Events-API message shape onto the same candidate contract the RTS
    sweep produces, so ``on_message_event`` can reuse the identical dedup → classify
    → forward pipeline. Slack message events carry ``channel``, ``ts``, ``user`` and
    ``text``; bot/app authorship is signalled by ``bot_id`` or the ``bot_message``
    subtype. Missing fields degrade to empty strings (high recall: forward a thin
    candidate rather than drop a real one, Req 2.5)."""
    return CandidateMessage(
        channel_id=_coerce_str(event.get("channel")),
        message_ts=_coerce_str(event.get("ts")),
        author_id=_coerce_str(event.get("user")),
        text=_coerce_str(event.get("text")),
        permalink=_coerce_str(event.get("permalink")),
        author_name=event.get("username") or event.get("author_name"),
        channel_name=event.get("channel_name"),
        is_author_bot=bool(event.get("bot_id"))
        or event.get("subtype") == "bot_message",
    )


# --------------------------------------------------------------------------- #
# Sweep result
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SweepResult:
    """The outcome of one :meth:`Watcher.run_sweep` (design.md ``run_sweep -> SweepResult``).

    ``new_candidates`` is the list of NEW (non-duplicate, classify-positive)
    candidates produced by the sweep — empty when RTS returned nothing (Req 2.6) or
    when RTS failed (Req 2.7). ``rts_ok`` records whether the RTS call itself
    succeeded, so a fault test can distinguish "swept, found nothing" from "RTS
    failed". ``duplicates_skipped`` counts candidates dropped by the dedup guard
    (Req 2.9), and ``error`` carries the caught failure message when ``rts_ok`` is
    False.
    """

    new_candidates: list[CandidateMessage] = field(default_factory=list)
    rts_ok: bool = True
    duplicates_skipped: int = 0
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Watcher
# --------------------------------------------------------------------------- #
class Watcher:
    """Perceives open-loop candidates via a periodic RTS sweep (Req 2.1, 2.6, 2.7, 2.9).

    Construct with the shared :class:`ObligationGraph` (for dedup) and an injected
    :class:`RtsClient` port (so the RTS call is mockable). ``classify`` is the
    injectable Haiku classification port (a :class:`HaikuClassifyClient`); its
    default is pass-through, and :func:`build_haiku_classify_client` wires the real
    Haiku-backed one (task 4.3). ``forward`` is the Adjudicator hand-off seam (task
    5); its default is a no-op. ``interval_seconds`` is read from
    :mod:`loop.config` when not supplied and is validated to be ≤ 60s (Req 2.1);
    APScheduler wiring of the cadence is task 17. ``watch_channels`` optionally
    scopes live detection to a set of channel IDs: when non-empty, candidates whose
    ``channel_id`` is outside the allow-list are dropped (``OUT_OF_SCOPE``) before
    classification, which keeps a live demo's detection predictable; ``None`` or an
    empty set means no restriction (watch the whole workspace, backward-compatible).
    """

    def __init__(
        self,
        graph: ObligationGraph,
        rts_client: RtsClient,
        *,
        classify: ClassifyFn = _accept_all,
        forward: ForwardFn = _noop_forward,
        interval_seconds: Optional[int] = None,
        watch_channels: Optional[set[str]] = None,
    ) -> None:
        if interval_seconds is None:
            from loop.config import get_settings

            interval_seconds = get_settings().sweep_interval_seconds
        if interval_seconds <= 0:
            raise ValueError("sweep interval must be a positive number of seconds")
        if interval_seconds > MAX_SWEEP_INTERVAL_SECONDS:
            # Req 2.1: the sweep interval must not exceed 60 seconds.
            raise ValueError(
                f"sweep interval must not exceed {MAX_SWEEP_INTERVAL_SECONDS}s "
                f"(Req 2.1), got {interval_seconds}s"
            )

        self._graph = graph
        self._rts_client = rts_client
        self._classify = classify
        self._forward = forward
        self._interval_seconds = interval_seconds
        # Empty/None allow-list = no restriction (watch the whole workspace).
        self._watch_channels = set(watch_channels) if watch_channels else None

    @property
    def interval_seconds(self) -> int:
        """The validated periodic-sweep interval in seconds (always ≤ 60, Req 2.1)."""
        return self._interval_seconds

    # ------------------------------------------------------------------ #
    # Periodic sweep (task 4.1)
    # ------------------------------------------------------------------ #
    def run_sweep(self) -> SweepResult:
        """Run one RTS sweep and return the NEW (non-duplicate) candidates.

        Flow:
          1. Call the injected RTS port. If it raises (unreachable / timeout /
             error), catch it, complete the sweep with **no** obligations, and
             return ``SweepResult(rts_ok=False, ...)`` so the next scheduled sweep
             can retry (Req 2.7) — the exception never escapes.
          2. Parse the response (:func:`parse_rts_response`). An ``ok=false`` or
             empty response yields an empty candidate list, so the sweep completes
             creating no obligations (Req 2.6).
          3. For each candidate, skip it if its ``(channel_id, message_ts)`` source
             reference already corresponds to an existing Obligation (dedup, Req 2.9).
          4. Apply the ``classify`` seam (the Haiku high-recall classifier; Req 2.3)
             and forward each kept candidate via the ``forward`` seam (the Adjudicator
             hand-off, no-op until task 5). A Haiku failure on a candidate excludes it
             without marking it processed, so the next sweep re-evaluates it (Req 2.8).
             Steps (3)+(4) run through the shared :meth:`_handle_candidate` helper,
             which ``on_message_event`` also uses.

        Returns the :class:`SweepResult` carrying the new candidates.
        """
        # --- (1) RTS call with failure containment (Req 2.7) -----------------
        try:
            response = self._rts_client()
        except Exception as exc:  # noqa: BLE001 — any RTS failure → safe empty sweep.
            logger.warning("RTS sweep failed; retrying next sweep: %s", exc)
            return SweepResult(new_candidates=[], rts_ok=False, error=str(exc))

        # --- (2) Parse; empty / ok=false → no obligations (Req 2.6) ----------
        candidates = parse_rts_response(response)

        # Snapshot existing source refs once so dedup is O(1) per candidate.
        existing_refs = self._existing_source_refs()

        new_candidates: list[CandidateMessage] = []
        duplicates_skipped = 0

        for candidate in candidates:
            outcome = self._handle_candidate(candidate, existing_refs)
            if outcome is CandidateOutcome.DUPLICATE:
                duplicates_skipped += 1
            elif outcome is CandidateOutcome.FORWARDED:
                new_candidates.append(candidate)
            # DROPPED (Haiku said not-a-loop) and CLASSIFY_FAILED (Haiku raised,
            # re-evaluate next sweep, Req 2.8) contribute no new candidate.

        return SweepResult(
            new_candidates=new_candidates,
            rts_ok=True,
            duplicates_skipped=duplicates_skipped,
        )

    # ------------------------------------------------------------------ #
    # Live message-event evaluation (task 4.2)
    # ------------------------------------------------------------------ #
    def on_message_event(self, event: Mapping[str, Any]) -> CandidateOutcome:
        """Evaluate one new Slack message as an open-loop candidate (Req 2.2).

        Turns the live Events-API ``message`` event (channel, ts, author, text) into
        a :class:`CandidateMessage` and runs it through the **same**
        dedup → classify → forward pipeline as :meth:`run_sweep`, via the shared
        :meth:`_handle_candidate` helper. This guarantees the live path and the
        periodic-sweep path behave identically:

          * a message whose ``(channel, ts)`` already maps to an Obligation is
            skipped (dedup, Req 2.9);
          * a Haiku-positive message is forwarded to the Adjudicator with its source
            reference (Req 2.4, 2.5);
          * a Haiku failure excludes the message and leaves it unprocessed, so a
            later sweep that retrieves it re-evaluates it (Req 2.8).

        Returns the :class:`CandidateOutcome` for the message (handy for handlers and
        tests); the per-message graph snapshot keeps the live path stateless.
        """
        candidate = candidate_from_message_event(event)
        # Fresh snapshot of existing source refs per event (mirrors run_sweep's
        # per-sweep snapshot) so live dedup sees obligations written since boot.
        return self._handle_candidate(candidate, self._existing_source_refs())

    # ------------------------------------------------------------------ #
    # Shared per-candidate pipeline (run_sweep + on_message_event, task 4.2/4.3)
    # ------------------------------------------------------------------ #
    def _handle_candidate(
        self, candidate: CandidateMessage, existing_refs: set[tuple[str, str]]
    ) -> CandidateOutcome:
        """Run one candidate through dedup → Haiku classify → forward (Req 2.3–2.9).

        The single place both the periodic sweep and the live message path share, so
        their behaviour cannot drift:

          0. **Scope (live-detection scoping).** If a watch-channel allow-list is
             configured and the candidate's ``channel_id`` is not in it, drop the
             candidate before any classification → ``OUT_OF_SCOPE``. With no
             allow-list this step is a no-op (backward compatible).
          1. **Dedup (Req 2.9).** If the candidate's ``(channel, ts)`` is already in
             ``existing_refs`` (an existing Obligation, or a candidate already kept in
             this same pass), skip it → ``DUPLICATE``.
          2. **Haiku classify (Req 2.3).** Call the injected classify port. If it
             **raises** (Haiku failure), exclude the candidate and DO NOT add it to
             ``existing_refs`` — it is left unprocessed so the next sweep re-evaluates
             it → ``CLASSIFY_FAILED`` (Req 2.8). A ``False`` result drops it →
             ``DROPPED``.
          3. **Forward (Req 2.4, 2.5).** For a positive (including low-certainty)
             classification, forward the candidate to the Adjudicator via the forward
             seam and record its source ref so duplicates later in the same pass are
             skipped → ``FORWARDED``.
        """
        # --- (0) live-detection scoping: drop candidates outside the allow-list ---
        if (
            self._watch_channels is not None
            and candidate.channel_id not in self._watch_channels
        ):
            return CandidateOutcome.OUT_OF_SCOPE

        # --- (1) dedup by source message reference (Req 2.9) -----------------
        if candidate.dedup_key in existing_refs:
            return CandidateOutcome.DUPLICATE

        # --- (2) Haiku classify; a raise = exclude + re-evaluate (Req 2.8) ---
        try:
            keep = self._classify(candidate)
        except Exception as exc:  # noqa: BLE001 — Haiku failure → exclude, retry next sweep.
            logger.warning(
                "Haiku classify failed for %s; re-evaluating next sweep: %s",
                candidate.dedup_key,
                exc,
            )
            return CandidateOutcome.CLASSIFY_FAILED

        if not keep:
            return CandidateOutcome.DROPPED

        # --- (3) forward every positive incl. low-certainty (Req 2.4, 2.5) ---
        self._forward(candidate)
        # Guard against duplicates *within the same pass* too.
        existing_refs.add(candidate.dedup_key)
        return CandidateOutcome.FORWARDED

    # ------------------------------------------------------------------ #
    # Dedup helpers (Req 2.9)
    # ------------------------------------------------------------------ #
    def _existing_source_refs(self) -> set[tuple[str, str]]:
        """Return the set of ``(source_msg_channel, source_msg_ts)`` already in the graph.

        The store has no direct source-reference filter, so we read every Obligation
        (including dismissed ones — a dismissed loop still occupies its source
        message and must not be re-detected) and project the source reference. This
        is the de-dup key the Watcher matches candidates against (Req 2.9).
        """
        existing = self._graph.query(ObligationFilter(include_dismissed=True))
        return {(o.source_msg_channel, o.source_msg_ts) for o in existing}

    def _is_duplicate(self, channel_id: str, message_ts: str) -> bool:
        """True iff a stored Obligation already references this source message (Req 2.9)."""
        return (channel_id, message_ts) in self._existing_source_refs()


# --------------------------------------------------------------------------- #
# Real RTS client wiring (lazy; importing this module never touches the network)
# --------------------------------------------------------------------------- #
def build_rts_client(
    settings: Any | None = None,
    *,
    query: str = DEFAULT_OPEN_LOOP_QUERY,
) -> RtsClient:
    """Wire a real Slack RTS-backed :class:`RtsClient` from config.

    Calls the **verified** Real-Time Search surface: ``assistant.search.context``
    on the **user token** (``SLACK_USER_TOKEN``). This was confirmed against the
    sandbox via ``loop/spikes/rts_probe.py``: ``assistant.search.info`` reported
    ``is_ai_search_enabled: true`` and ``assistant.search.context`` returned
    ``ok: true`` with a ``results`` map (buckets ``messages``/``files``/``channels``/
    ``users``) on the user token — and, unlike the bot token, the user token needs
    **no** per-event ``action_token``, which is what makes the autonomous periodic
    sweep possible (Req 2.1). The bot token returns ``missing_scope`` and would also
    require an action_token, so RTS deliberately runs on the user token here.

    The ``slack_sdk`` import and the network call happen lazily *inside* the returned
    callable, so importing this module never touches the network. Only the verified
    ``query`` argument is sent (additional RTS parameters are intentionally omitted
    until verified against a populated workspace); recall is carried by the broad
    natural-language ``query`` (Req 2.5), and :func:`parse_rts_response` consumes the
    ``results.messages`` bucket.
    """
    if settings is None:
        from loop.config import get_settings

        settings = get_settings()
    # RTS runs on the user token (verified) — not the bot token.
    settings.require("slack_user_token")
    token = settings.slack_user_token

    def _client() -> Mapping[str, Any]:
        from slack_sdk import WebClient

        web = WebClient(token=token)
        # Verified-working call shape (rts_probe.py): query only. Phrasing the query
        # as a natural-language question triggers Slack's semantic (RTS) search.
        response = web.api_call(RTS_API_METHOD, params={"query": query})
        return dict(response.data) if hasattr(response, "data") else dict(response)

    return _client


# --------------------------------------------------------------------------- #
# Real Haiku classifier wiring (lazy; importing this module never touches network)
# --------------------------------------------------------------------------- #
# High-recall instruction for the Haiku binary classifier (Req 2.3, 2.5): when in
# doubt, say YES. The Adjudicator (Opus) is the precision filter downstream.
HAIKU_CLASSIFY_PROMPT = (
    "You are a high-recall first-pass filter for a personal Slack obligation agent.\n"
    "Decide whether the following message could plausibly be an OPEN LOOP — someone "
    "waiting on a reply, blocked on someone, an unanswered question, or a promise to "
    "follow up. Optimize for RECALL: if it is even plausibly an open loop, answer "
    "YES. Only answer NO when it clearly cannot be an open loop.\n\n"
    "Answer with exactly one word: YES or NO.\n\n"
    "Message: {text}"
)


def build_haiku_classify_client(settings: Any | None = None) -> HaikuClassifyClient:
    """Wire a real fast-tier :class:`HaikuClassifyClient` from config (task 4.3).

    Provider-agnostic: routes through :func:`loop.llm.chat` at the **fast tier**,
    which by default is the configured Groq model (``llama-3.1-8b-instant``) and
    falls back to the Anthropic Haiku model when ``llm_provider="anthropic"``. The
    provider SDK and the network call happen lazily *inside* the returned callable
    (via ``loop.llm``), so importing this module — and constructing the client —
    never touches the network or requires a provider SDK installed. The required
    credential for the active provider is validated at call time.

    High recall (Req 2.5): the prompt instructs the model to answer YES on any
    plausible loop, so positives (including low-certainty ones) are kept and
    forwarded. On a model failure the callable **raises**; the Watcher catches that,
    excludes the candidate, and leaves it unprocessed so the next sweep re-evaluates
    it (Req 2.8). The Watcher's logic and tests exercise classification through an
    injected mock port, never this live client.
    """
    if settings is None:
        from loop.config import get_settings

        settings = get_settings()
    # Validate the active provider's credential at build time (provider-agnostic).
    from loop.llm import require_provider_key

    require_provider_key(settings)

    def _classify(candidate: CandidateMessage) -> bool:
        from loop.llm import chat

        text = chat(
            [{"role": "user", "content": HAIKU_CLASSIFY_PROMPT.format(text=candidate.text)}],
            tier="fast",
            settings=settings,
            max_tokens=8,
        )
        # High recall: anything but a clear NO is treated as a potential loop.
        return "no" not in text.strip().lower()[:3]

    return _classify


__all__ = [
    "Watcher",
    "SweepResult",
    "RtsClient",
    "ClassifyFn",
    "HaikuClassifyClient",
    "ForwardFn",
    "CandidateOutcome",
    "candidate_from_message_event",
    "build_rts_client",
    "build_haiku_classify_client",
    "HAIKU_CLASSIFY_PROMPT",
    "DEFAULT_OPEN_LOOP_QUERY",
    "RTS_API_METHOD",
    "MAX_SWEEP_INTERVAL_SECONDS",
]
