"""Detection work queue — connects the Watcher to the Adjudicator (task 17.1).

design.md → "Deployment shape": *"The Adjudicator and Verifier run as internal
services invoked from the work queue."* This module is that work queue: the thin,
in-process seam that decouples the Watcher (Perceive) from the Adjudicator
(Reason) so the Watcher can forward a high-recall stream of candidates without
blocking on smart-tier latency.

Shape (monolith-with-workers, Req 2.1 path):

    Watcher.run_sweep / on_message_event
        -> forward(candidate)            # the Watcher's ForwardFn seam
        -> AdjudicationQueue.enqueue      # this module (thread-safe hand-off)
        -> worker drains the queue
        -> Adjudicator.adjudicate(candidate, user_id=...)   # writes the graph

The queue exposes two complementary drive modes so the same wiring serves both
the live app and the tests:

  * :meth:`AdjudicationQueue.start_worker` / :meth:`stop_worker` — a background
    daemon thread that drains continuously (the live app, task 17).
  * :meth:`AdjudicationQueue.drain` — drains synchronously and returns the
    :class:`~loop.adjudicator.adjudicator.AdjudicationResult` for every candidate
    processed (deterministic for tests and the seeded demo).

The queue never lets an Adjudicator failure escape the worker loop: a smart-tier error
is already absorbed by the Adjudicator (Req 3.7), and any unexpected exception is
caught and logged here so one bad candidate can never kill the worker that drains
the rest. This keeps the autonomous sensing→adjudication path (Req 13.1) running
unattended.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from loop.adjudicator.adjudicator import AdjudicationResult, Adjudicator
from loop.graph.models import UserId
from loop.watcher.rts_contract import CandidateMessage

logger = logging.getLogger(__name__)

# Sentinel pushed onto the queue to unblock and stop the worker thread.
_STOP = object()


@dataclass(frozen=True)
class _QueuedCandidate:
    """One candidate awaiting adjudication, tagged with the tracked user it is for."""

    candidate: CandidateMessage
    user_id: UserId


class AdjudicationQueue:
    """Thread-safe work queue draining Watcher candidates into the Adjudicator.

    Construct with the :class:`Adjudicator` and the tracked ``user_id`` whose court
    every candidate is adjudicated against. Pass :meth:`enqueue` as the Watcher's
    ``forward`` seam so a forwarded candidate lands here instead of being adjudicated
    inline on the sweep thread.

    Args:
        adjudicator: the Reason stage candidates are routed to (Req 3.1).
        user_id: the tracked User's Slack id, supplied to ``adjudicate`` so direction
            (blocked-on-you vs waiting-on-other) is resolved against the right person
            (Req 3.2).
        on_result: optional callback invoked with each :class:`AdjudicationResult`
            after a candidate is adjudicated — the seam the app uses to refresh the
            App Home when a new obligation is written. Exceptions it raises are caught
            and logged so the worker keeps draining.
    """

    def __init__(
        self,
        adjudicator: Adjudicator,
        user_id: UserId,
        *,
        on_result: Optional[Callable[[AdjudicationResult], None]] = None,
    ) -> None:
        self._adjudicator = adjudicator
        self._user_id = user_id
        self._on_result = on_result
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    # Producer side — the Watcher's forward seam
    # ------------------------------------------------------------------
    def enqueue(self, candidate: CandidateMessage) -> None:
        """Hand a Watcher-forwarded candidate to the queue (the Watcher ``ForwardFn``).

        Non-blocking: the candidate is queued and the Watcher sweep returns
        immediately, so smart-tier latency never slows perception (Req 2.1).
        """
        self._queue.put(_QueuedCandidate(candidate=candidate, user_id=self._user_id))

    # ------------------------------------------------------------------
    # Consumer side — synchronous drain (tests / seeded demo)
    # ------------------------------------------------------------------
    def drain(self) -> list[AdjudicationResult]:
        """Adjudicate every currently-queued candidate and return the results.

        Drains until the queue is empty (does not block waiting for more), so it is
        deterministic for tests and the seeded demo. Each candidate is run through
        :meth:`_adjudicate_one`, whose failures are contained, so a drain always
        completes and reports a result per processed candidate.
        """
        results: list[AdjudicationResult] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _STOP:
                continue
            assert isinstance(item, _QueuedCandidate)
            result = self._adjudicate_one(item)
            if result is not None:
                results.append(result)
        return results

    # ------------------------------------------------------------------
    # Consumer side — background worker (the live app, task 17)
    # ------------------------------------------------------------------
    def start_worker(self) -> None:
        """Start the background daemon thread that drains the queue continuously.

        Idempotent: calling it while a worker is already running is a no-op.
        """
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(
            target=self._run, name="loop-adjudication-worker", daemon=True
        )
        self._worker.start()

    def stop_worker(self, *, timeout: float = 5.0) -> None:
        """Signal the worker to stop and join it (best-effort within ``timeout``)."""
        if not self._running:
            return
        self._running = False
        self._queue.put(_STOP)
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None

    def _run(self) -> None:
        """Worker loop: block for the next candidate and adjudicate it (Req 13.1)."""
        while self._running:
            item = self._queue.get()
            if item is _STOP:
                break
            if not isinstance(item, _QueuedCandidate):  # defensive
                continue
            self._adjudicate_one(item)

    # ------------------------------------------------------------------
    # Shared adjudication step with failure containment
    # ------------------------------------------------------------------
    def _adjudicate_one(self, item: _QueuedCandidate) -> Optional[AdjudicationResult]:
        """Adjudicate one queued candidate, never letting a failure escape.

        The Adjudicator already absorbs a smart-tier error into an ``ERROR`` result
        (Req 3.7); this guard additionally contains any *unexpected* exception so one
        bad candidate cannot kill the worker draining the rest.
        """
        try:
            result = self._adjudicator.adjudicate(
                item.candidate, user_id=item.user_id
            )
        except Exception:  # noqa: BLE001 — keep the worker alive; log and move on.
            logger.exception(
                "adjudication crashed for candidate %s", item.candidate.dedup_key
            )
            return None

        _log_adjudication(item.candidate, result)

        if self._on_result is not None:
            try:
                self._on_result(result)
            except Exception:  # noqa: BLE001 — a UI refresh failure must not stop draining.
                logger.exception("on_result callback failed after adjudication")
        return result


def _log_adjudication(
    candidate: CandidateMessage, result: AdjudicationResult
) -> None:
    """Log every adjudication outcome so a dropped candidate is never silent.

    CREATED/UPDATED log at INFO with the written edge; DISCARDED logs the reason;
    ERROR logs whichever error indication the result carries. Message text is never
    logged — only the source reference and the structured judgement.
    """
    from loop.adjudicator.adjudicator import AdjudicationOutcome

    key = candidate.dedup_key
    if result.outcome in (AdjudicationOutcome.CREATED, AdjudicationOutcome.UPDATED):
        o = result.obligation
        logger.info(
            "adjudicated %s: %s %s -> %s (%s, conf=%.2f, surfacing=%s)",
            key,
            result.outcome.value,
            o.owes_person_id if o else "?",
            o.owed_person_id if o else "?",
            o.loop_state.value if o else "?",
            o.confidence_score if o else -1.0,
            result.surfacing_eligible,
        )
    elif result.outcome is AdjudicationOutcome.DISCARDED:
        logger.info("adjudicated %s: discarded (%s)", key, result.discard_reason)
    else:  # ERROR
        logger.warning(
            "adjudicated %s: error (%s)",
            key,
            result.error_message or result.error,
        )


__all__ = ["AdjudicationQueue"]
