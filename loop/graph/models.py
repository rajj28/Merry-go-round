"""Loop Obligation-Graph data model — the **day-1 frozen interface contract**.

This module is the *single source of truth* for the shapes every Loop agent reads
and writes (task 2.1). All four owners (Detection, Graph & Learn, Verify & Act,
Surfaces) import the types defined here; do not redefine these shapes elsewhere.

It is the authoritative realization of design.md → "Data Models" (the ER diagram
and the "Field semantics and invariants" section) and of requirements.md
Requirements 1.1, 1.2, 1.4, 1.6.

Scope discipline (read before editing):
  * These are **SQLModel** classes so they *double* as the persistence schema for
    the SQLite-backed store (task 3.1). They are nonetheless pure, DB-free value
    objects: importing this module and constructing any model requires **no engine
    and no connection**.
  * Authoritative *validation*, *last-write-wins*, and *persistence* semantics live
    in the store (task 3, Requirements 1.3/1.5/1.7-1.10). This module only freezes
    the field/enum contract and provides a few **lightweight, non-enforcing**
    validation *helpers*. It deliberately does NOT reject bad values on
    construction — the store owns the "reject + retain prior value + return error"
    behaviour so it can satisfy Req 1.3/1.5 precisely.

Frozen contract: changing a field name, enum value, or primary key here is an
interface break that affects every owner. Coordinate before changing it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel

# ---------------------------------------------------------------------------
# Identifier type aliases
#
# All ids are Slack-native or app-generated strings. These aliases exist purely
# for readability at call sites and in signatures; they are plain ``str`` so they
# stay JSON/DB friendly and need no special handling.
# ---------------------------------------------------------------------------
PersonId = str       # a Slack user id, e.g. "U0123456"
UserId = str         # the tracked User's Slack id (a PersonId where is_user is True)
ObligationId = str   # app-generated obligation primary key
EventId = str        # app-generated feedback-event primary key


# ---------------------------------------------------------------------------
# Enumerated value types (frozen vocabularies)
# ---------------------------------------------------------------------------
class LoopState(str, Enum):
    """Lifecycle state of an Obligation (Req 1.2).

    Exactly one of these three values is ever valid. The string values are the
    on-the-wire / on-disk representation and MUST remain exactly as written —
    they appear verbatim in requirements, Block Kit copy, and the seeded demo.
    """

    BLOCKED_ON_YOU = "blocked-on-you"        # ball in the User's court (User owes)
    WAITING_ON_OTHER = "waiting-on-other"    # ball in the other party's court
    HEALED = "healed"                        # resolved / closed


class ClosureKind(str, Enum):
    """How an Obligation reached the ``healed`` state.

    Only ``autonomous`` closures (a Verifier-confirmed merge) appear in the
    Auto-Healed feed; ``manual`` closures are excluded (Req 9.1).
    """

    AUTONOMOUS = "autonomous"   # Loop closed it after verifying real artifact state
    MANUAL = "manual"           # a user action closed it


class ArtifactType(str, Enum):
    """The kind of external work artifact an Obligation references.

    Tier 0/1 grounds only GitHub pull requests via the GitHub MCP (Req 4, 8).
    ``artifact_type`` is null when an Obligation references no artifact.
    """

    GITHUB_PR = "github_pr"


class FeedbackPolarity(str, Enum):
    """Direction of a Confirm/Dismiss feedback event consumed by Learn (Req 5)."""

    POSITIVE = "positive"   # user confirmed a surfaced Obligation
    NEGATIVE = "negative"   # user dismissed a surfaced Obligation


# Convenience frozensets for callers that need to validate raw strings without
# importing the enums (e.g. the store's reject path, task 3).
LOOP_STATE_VALUES: frozenset[str] = frozenset(s.value for s in LoopState)
CLOSURE_KIND_VALUES: frozenset[str] = frozenset(s.value for s in ClosureKind)
ARTIFACT_TYPE_VALUES: frozenset[str] = frozenset(s.value for s in ArtifactType)
FEEDBACK_POLARITY_VALUES: frozenset[str] = frozenset(s.value for s in FeedbackPolarity)

# Confidence / threshold are floats in the inclusive range [0.0, 1.0] (Req 1.4).
CONFIDENCE_MIN: float = 0.0
CONFIDENCE_MAX: float = 1.0

# Default starting surfacing gate before the Learn loop tunes it (Req 5).
DEFAULT_CONFIDENCE_THRESHOLD: float = 0.5


# ---------------------------------------------------------------------------
# Entities (people as nodes, open loops as directed edges)
# ---------------------------------------------------------------------------
class Person(SQLModel, table=True):
    """A node in the Obligation Graph: a tracked person (Req 1.1).

    Exactly one Person in a workspace has ``is_user=True`` — the tracked User
    whose obligations Loop maintains.
    """

    __tablename__ = "person"

    person_id: PersonId = Field(primary_key=True, description="Slack user id")
    display_name: str = Field(description="Human-readable name for UI")
    is_user: bool = Field(default=False, description="True only for the tracked User")


class Obligation(SQLModel, table=True):
    """A directed edge in the Obligation Graph: one open loop between two people.

    Direction always points from the party who **owes** a response
    (``owes_person_id``) to the party who is **owed** one (``owed_person_id``),
    per Req 1.1. ``blocked-on-you`` means ``owes_person_id == user``;
    ``waiting-on-other`` means ``owed_person_id == user`` (design field semantics).

    Field groups:
      * Identity / edge:   obligation_id, owes_person_id, owed_person_id, owner_person_id
      * Judgement:         loop_state, confidence_score
      * Provenance:        last_touch_timestamp, source_msg_channel, source_msg_ts,
                           subject_summary  (Req 1.6 source-message reference)
      * Surfacing control: dismissed, snoozed_until
      * Artifact link:     artifact_type, artifact_ref
      * Closure metadata:  closure_kind, closure_timestamp, closure_reason

    Optional fields carry defaults so a freshly-detected Obligation is trivial to
    construct; the closure_* fields and snoozed_until stay null until the relevant
    lifecycle event occurs.
    """

    __tablename__ = "obligation"

    # --- identity / edge ----------------------------------------------------
    obligation_id: ObligationId = Field(primary_key=True)
    owes_person_id: PersonId = Field(
        foreign_key="person.person_id",
        description="Edge source — who must respond",
    )
    owed_person_id: PersonId = Field(
        foreign_key="person.person_id",
        description="Edge target — who is waiting",
    )
    owner_person_id: PersonId = Field(
        foreign_key="person.person_id",
        description="Current owner; delegation can reassign (Req 10.4)",
    )

    # --- judgement ----------------------------------------------------------
    loop_state: LoopState = Field(description="blocked-on-you | waiting-on-other | healed")
    confidence_score: float = Field(
        description="Adjudicator certainty, inclusive [0.0, 1.0] (Req 1.4)",
    )

    # --- provenance ---------------------------------------------------------
    last_touch_timestamp: str = Field(
        description="ISO 8601 UTC; drives aging chips and last-write-wins (Req 1.9)",
    )
    source_msg_channel: str = Field(description="Slack channel id of the source message")
    source_msg_ts: str = Field(description="Slack ts of the source message")
    subject_summary: str = Field(description="Claude summary of the loop")

    # --- surfacing control --------------------------------------------------
    dismissed: bool = Field(
        default=False,
        description="True => never surfaced anywhere (Req 5.3, 14.5)",
    )
    snoozed_until: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC; unsurfaced while now < snoozed_until, else null (Req 13.3)",
    )

    # --- artifact link ------------------------------------------------------
    artifact_type: Optional[ArtifactType] = Field(
        default=None,
        description="github_pr or null",
    )
    artifact_ref: Optional[str] = Field(
        default=None,
        description="e.g. 'owner/repo#number', or null",
    )

    # --- closure metadata ---------------------------------------------------
    closure_kind: Optional[ClosureKind] = Field(
        default=None,
        description="autonomous | manual | null (Req 9.1)",
    )
    closure_timestamp: Optional[str] = Field(
        default=None,
        description="ISO 8601 UTC closure time, or null (Req 8.5)",
    )
    closure_reason: Optional[str] = Field(
        default=None,
        description="e.g. 'PR merged', or null",
    )


class FeedbackEvent(SQLModel, table=True):
    """A single Confirm/Dismiss feedback record consumed by the Learn loop (Req 5)."""

    __tablename__ = "feedback_event"

    event_id: EventId = Field(primary_key=True)
    obligation_id: ObligationId = Field(foreign_key="obligation.obligation_id")
    polarity: FeedbackPolarity = Field(description="positive | negative")
    created_at: str = Field(description="ISO 8601 UTC")


class GraphConfig(SQLModel, table=True):
    """Per-user graph configuration — currently the global surfacing gate.

    ``confidence_threshold`` is the quiet-by-default gate (Req 14.1), tuned by the
    Learn loop within the inclusive bounds [0.0, 1.0] (Req 5.4, 5.5). Authoritative
    clamping lives in the store's ``set_threshold`` (task 3.4); the default here is
    only the starting value.
    """

    __tablename__ = "graph_config"

    user_id: UserId = Field(primary_key=True)
    confidence_threshold: float = Field(
        default=DEFAULT_CONFIDENCE_THRESHOLD,
        description="Surfacing gate, inclusive [0.0, 1.0], tuned by Learn",
    )


# ---------------------------------------------------------------------------
# Lightweight, NON-enforcing validation helpers
#
# These are pure predicates/utilities for callers (store, agents, tests). They do
# NOT mutate or reject anything — the store owns the authoritative
# "reject + retain prior value + return error" semantics (Req 1.3/1.5, task 3).
# ---------------------------------------------------------------------------
def is_valid_loop_state(value: object) -> bool:
    """True iff ``value`` is a valid Loop_State (enum member or its string value)."""
    if isinstance(value, LoopState):
        return True
    return isinstance(value, str) and value in LOOP_STATE_VALUES


def is_valid_confidence(score: object) -> bool:
    """True iff ``score`` is a real number in the inclusive range [0.0, 1.0]."""
    return (
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and CONFIDENCE_MIN <= float(score) <= CONFIDENCE_MAX
    )


def clamp_confidence(score: float) -> float:
    """Clamp ``score`` into the inclusive range [0.0, 1.0]."""
    return max(CONFIDENCE_MIN, min(CONFIDENCE_MAX, float(score)))


def utc_now_iso() -> str:
    """Return the current time as an ISO 8601 UTC string (the timestamp format
    every ``*_timestamp`` / ``*_at`` field uses)."""
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    # id aliases
    "PersonId",
    "UserId",
    "ObligationId",
    "EventId",
    # enums
    "LoopState",
    "ClosureKind",
    "ArtifactType",
    "FeedbackPolarity",
    # value sets / constants
    "LOOP_STATE_VALUES",
    "CLOSURE_KIND_VALUES",
    "ARTIFACT_TYPE_VALUES",
    "FEEDBACK_POLARITY_VALUES",
    "CONFIDENCE_MIN",
    "CONFIDENCE_MAX",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    # entities
    "Person",
    "Obligation",
    "FeedbackEvent",
    "GraphConfig",
    # helpers
    "is_valid_loop_state",
    "is_valid_confidence",
    "clamp_confidence",
    "utc_now_iso",
]
