"""Obligation Graph store **interface contract** (task 2.2 — Dev B, day-1 freeze).

This module freezes the abstract read/write contract every Loop agent uses to talk
to the Obligation Graph. It is the authoritative realization of design.md →
"Components and Interfaces" → "Obligation Graph store (shared)" and of
requirements.md Requirement 1 (clauses 1.3, 1.5, 1.7–1.11) plus the Requirement 14
boundary guard (14.2, 14.6).

Scope discipline (read before editing):
  * This file defines **only** the abstract base class :class:`ObligationGraph`, the
    query-shape :class:`ObligationFilter`, and the rejection/error contract
    (:class:`Result`, :class:`Ok`, :class:`Err`, :class:`GraphError`,
    :class:`GraphErrorCode`). It contains **no persistence logic** — the concrete
    SQLite-backed implementation is task 3.1 and lives elsewhere.
  * The frozen value/enum *shapes* (``Obligation``, ``LoopState``, ``GraphConfig`` …)
    come from :mod:`loop.graph.models`; this module never redefines them.
  * Authoritative validation, last-write-wins, persistence, and boundary semantics
    are *described here as the contract* and *enforced* by the concrete store
    (task 3). Subclasses MUST honour the docstring guarantees.

Why a ``Result`` type rather than exceptions on the write paths? Requirement 1
demands that rejected/failed writes **retain the previously stored value** and
**return an error indication to the calling agent** (Req 1.3, 1.5, 1.8). Modelling
that as an explicit ``Result[T]`` keeps the "reject + retain + report" contract
visible in every call site instead of relying on callers to catch the right
exception.

Frozen contract: changing a method signature, the ``Result`` shape, or a
``GraphErrorCode`` value here is an interface break affecting all four owners.
Coordinate before changing it.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Optional, TypeVar, Union

from loop.graph.models import (
    ArtifactType,
    ClosureKind,
    LoopState,
    Obligation,
    ObligationId,
    PersonId,
)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Error / Result contract for the rejection and persist-failure paths
# ---------------------------------------------------------------------------
class GraphErrorCode(str, Enum):
    """Why a write was rejected or failed (the error indication of Req 1.3/1.5/1.8/14.6).

    The string values are stable and safe to log or surface in a Tool_Use_Trace.

      INVALID_LOOP_STATE   upsert carried a Loop_State outside the frozen enum     (Req 1.3)
      INVALID_CONFIDENCE   upsert carried a Confidence_Score outside [0.0, 1.0]    (Req 1.5)
      PERSIST_FAILURE      persistence of an otherwise-valid write failed          (Req 1.8)
      BOUNDARY_VIOLATION   a write/copy targeted a destination outside the
                           workspace boundary and was refused                      (Req 14.2, 14.6)
    """

    INVALID_LOOP_STATE = "invalid_loop_state"
    INVALID_CONFIDENCE = "invalid_confidence"
    PERSIST_FAILURE = "persist_failure"
    BOUNDARY_VIOLATION = "boundary_violation"


@dataclass(frozen=True)
class GraphError:
    """A structured error indication returned on a rejected/failed write.

    ``code`` is the machine-readable reason; ``message`` is a human-readable detail
    suitable for logs or an Assistant_Pane Tool_Use_Trace. ``obligation_id`` names the
    Obligation whose *prior* value was retained (Req 1.3/1.5/1.8), when applicable.
    """

    code: GraphErrorCode
    message: str = ""
    obligation_id: Optional[ObligationId] = None


@dataclass(frozen=True)
class Ok(Generic[T]):
    """Successful outcome carrying the stored/accepted value."""

    value: T


@dataclass(frozen=True)
class Err:
    """Failed/rejected outcome carrying the :class:`GraphError` indication.

    On every ``Err`` the store guarantees the previously stored value is retained
    (Req 1.3, 1.5, 1.8): the caller's attempted write did **not** take effect.
    """

    error: GraphError


# A write either succeeds with a value (``Ok[T]``) or is rejected/failed (``Err``).
Result = Union[Ok[T], Err]


def ok(value: T) -> Ok[T]:
    """Convenience constructor for a successful :class:`Result`."""
    return Ok(value)


def err(
    code: GraphErrorCode,
    message: str = "",
    obligation_id: Optional[ObligationId] = None,
) -> Err:
    """Convenience constructor for a rejected/failed :class:`Result`."""
    return Err(GraphError(code=code, message=message, obligation_id=obligation_id))


def is_ok(result: Result[T]) -> bool:
    """True iff ``result`` is a successful :class:`Ok`."""
    return isinstance(result, Ok)


# ---------------------------------------------------------------------------
# Query shape
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ObligationFilter:
    """The query dimensions agents need against the Obligation Graph (``query``).

    All fields are optional; an empty :class:`ObligationFilter` matches every stored
    Obligation. Set fields are combined with logical AND. The dimensions are drawn
    directly from how design.md's flows read the graph:

      * App Home (Req 6): the two active sections query by ``loop_states`` with
        ``surfaced_only=True``; the Auto-Healed feed queries ``loop_states={healed}``
        with ``closure_kind=AUTONOMOUS`` and ``include_dismissed=False`` (Req 9.1).
      * Daily Digest (Req 11): surfaced ``blocked-on-you`` / ``waiting-on-other``.
      * Conversational (Req 12): "who's blocked on me I haven't touched in 3 days?"
        → ``loop_states={blocked-on-you}``, ``surfaced_only=True``,
        ``min_age_seconds=72*3600``; "what am I waiting on from Dana?" →
        by ``owes_person_id`` / ``owed_person_id``.

    Surfacing/age dimensions:
      * ``surfaced_only`` — when True, restrict to Obligations that satisfy the
        quiet-by-default surfacing predicate (not dismissed, not currently snoozed,
        Confidence_Score ≥ current threshold, active loop state). The store evaluates
        the predicate against its own current threshold and ``now`` (Req 14.1).
      * ``include_dismissed`` — when False (the default) dismissed Obligations are
        excluded everywhere (Req 5.3, 14.5); set True only for diagnostics.
      * ``min_age_seconds`` / ``max_age_seconds`` — inclusive age window measured as
        ``now - last_touch_timestamp`` (drives the "haven't touched in N days"
        queries and the aging chips, Req 6.5).
      * ``now`` — reference instant (ISO 8601 UTC) for the snooze and age
        computations; when None the store uses the current time.
    """

    # by lifecycle state — empty set means "any state"
    loop_states: frozenset[LoopState] = field(default_factory=frozenset)

    # surfacing controls
    surfaced_only: bool = False
    include_dismissed: bool = False

    # by participant / ownership (edge endpoints)
    owner_person_id: Optional[PersonId] = None
    owes_person_id: Optional[PersonId] = None
    owed_person_id: Optional[PersonId] = None

    # by linked work artifact
    artifact_type: Optional[ArtifactType] = None
    artifact_ref: Optional[str] = None

    # by closure provenance (Auto-Healed feed)
    closure_kind: Optional[ClosureKind] = None

    # age window relative to ``now`` (seconds); None bounds are open-ended
    min_age_seconds: Optional[float] = None
    max_age_seconds: Optional[float] = None

    # reference instant for snooze/age evaluation (ISO 8601 UTC); None => current time
    now: Optional[str] = None


# ---------------------------------------------------------------------------
# The shared store contract
# ---------------------------------------------------------------------------
class ObligationGraph(abc.ABC):
    """The single read/write contract for all five agents (design: "shared store").

    Implementations enforce the data invariants centrally so no agent can corrupt
    state. Method docstrings state the guarantee each implementation MUST satisfy;
    the concrete SQLite store (task 3) is the realization.
    """

    @abc.abstractmethod
    def upsert(self, obligation: Obligation) -> Result[Obligation]:
        """Create or update an Obligation, returning the stored value or an error.

        Validation and conflict rules the implementation MUST enforce:
          * Reject a Loop_State outside the frozen enum: keep the previously stored
            Loop_State and return ``Err(INVALID_LOOP_STATE)`` (Req 1.3).
          * Reject a Confidence_Score outside the inclusive ``[0.0, 1.0]`` range: keep
            the previously stored score and return ``Err(INVALID_CONFIDENCE)`` (Req 1.5).
          * Persist within 1 second with read-after-write visibility — any agent
            reading the Obligation after this call returns ``Ok`` sees the update
            (Req 1.7).
          * On persist failure, retain the last successfully stored value and return
            ``Err(PERSIST_FAILURE)`` (Req 1.8).
          * Last-write-wins: when two updates target the same Obligation, retain the
            one with the later ``last_touch_timestamp`` (Req 1.9); on identical
            timestamps, retain the later-*received* write and discard the earlier
            (Req 1.10).

        Returns ``Ok(stored_obligation)`` on success, ``Err(GraphError)`` otherwise.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get(self, obligation_id: ObligationId) -> Optional[Obligation]:
        """Return the stored Obligation for ``obligation_id``, or None if absent.

        Reflects the most recently persisted value (read-after-write, Req 1.7).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def query(self, filter: ObligationFilter) -> list[Obligation]:
        """Return all stored Obligations matching ``filter`` (see :class:`ObligationFilter`).

        An empty filter matches everything; set fields are combined with AND. When
        ``filter.surfaced_only`` is True the result is restricted to Obligations that
        pass the quiet-by-default surfacing predicate against the store's current
        threshold and reference time (Req 14.1). Order is unspecified at this layer —
        callers (App Home, feed) impose their own sort (Req 6.3, 9.3).
        """
        raise NotImplementedError

    @abc.abstractmethod
    def set_threshold(self, value: float) -> Result[float]:
        """Set the global Confidence_Threshold, clamped to inclusive ``[0.0, 1.0]``.

        The Learn loop tunes the threshold by ≤0.05 per feedback event; this setter is
        the authoritative clamp so the stored value can never leave ``[0.0, 1.0]``
        (Req 5.4, 5.5, 5.6). Returns ``Ok(stored_threshold)`` with the value actually
        persisted (after clamping); ``Err(PERSIST_FAILURE)`` if persistence fails, in
        which case the prior threshold is retained.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_threshold(self) -> float:
        """Return the current global Confidence_Threshold (always within ``[0.0, 1.0]``)."""
        raise NotImplementedError

    @abc.abstractmethod
    def reject_external_write(self, dest: object) -> None:
        """Boundary guard: refuse any write/copy of graph data outside the workspace.

        If ``dest`` is a destination outside the user's Slack workspace boundary, the
        implementation MUST reject the attempt, retain the data within the boundary,
        and record an error indication (Req 14.2, 14.6). This method exists so the
        boundary check has one authoritative home; it raises a ``PermissionError`` (or
        a subclass) for an out-of-boundary destination and returns normally for an
        in-boundary one.
        """
        raise NotImplementedError


__all__ = [
    # error / result contract
    "GraphErrorCode",
    "GraphError",
    "Ok",
    "Err",
    "Result",
    "ok",
    "err",
    "is_ok",
    # query shape
    "ObligationFilter",
    # store contract
    "ObligationGraph",
]
