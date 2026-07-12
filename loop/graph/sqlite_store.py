"""SQLite-backed Obligation Graph store (task 3 — Dev B).

This is the concrete realization of the :class:`loop.graph.store.ObligationGraph`
interface frozen in task 2.2, persisting the SQLModel entities frozen in task 2.1
(:mod:`loop.graph.models`) into an embedded SQLite database. It is the single
read/write home for every Loop agent's view of the obligation graph
(design.md → "Components and Interfaces" → "Obligation Graph store (shared)").

Task scope realized **here**:
  * task 3.1 — ``upsert`` with authoritative validation — reject a Loop_State
    outside the frozen enum (``Err(INVALID_LOOP_STATE)``) and a Confidence_Score
    outside the inclusive ``[0.0, 1.0]`` range (``Err(INVALID_CONFIDENCE)``), in
    both cases **retaining the previously stored value** (Req 1.2, 1.3, 1.4, 1.5);
    plus ``get`` and ``query`` (with the full :class:`ObligationFilter` shape).
  * task 3.2 — last-write-wins conflict resolution by ``last_touch_timestamp``
    (Req 1.9, 1.10), implemented in the :meth:`_resolve_write` hook.
  * task 3.3 — sub-second persistence with read-after-write visibility (Req 1.7),
    persist-failure handling that retains the last good value and returns
    ``Err(PERSIST_FAILURE)`` (Req 1.8), and the authoritative
    ``reject_external_write`` boundary guard (Req 1.11, 14.2, 14.6).
  * task 3.4 — ``set_threshold`` / ``get_threshold`` persisted via
    :class:`GraphConfig` (Req 5.6), clamped to inclusive ``[0.0, 1.0]`` and durable
    across store instances sharing the same database file.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from loop.config import get_settings
from loop.graph.models import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    FeedbackEvent,
    GraphConfig,
    Obligation,
    ObligationId,
    clamp_confidence,
    is_valid_confidence,
    is_valid_loop_state,
)
from loop.graph.store import (
    GraphError,
    GraphErrorCode,
    ObligationFilter,
    ObligationGraph,
    Result,
    err,
    ok,
)
from loop.graph.surfacing import is_surfaced

# Sentinel database path that selects an in-memory SQLite database (tests).
IN_MEMORY = ":memory:"

# Stable single-row key for the global GraphConfig (the threshold lives here).
# The Tier 0 spine tracks a single User, so one fixed-key row is authoritative;
# task 3.4 persists the Confidence_Threshold against it (Req 5.6).
GRAPH_CONFIG_KEY = "__loop_user__"

logger = logging.getLogger(__name__)


def _engine_url(database_path: str) -> str:
    """Translate a configured database path into a SQLAlchemy SQLite URL."""
    if database_path == IN_MEMORY:
        # A pure in-memory database. ``StaticPool`` (configured by the caller)
        # keeps a single shared connection so the schema and data survive across
        # successive sessions within one process.
        return "sqlite://"
    return f"sqlite:///{database_path}"


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a tz-aware UTC ``datetime``.

    Mirrors the coercion the surfacing predicate uses: a bare trailing ``Z`` is
    normalized to ``+00:00`` and naive values are assumed to be UTC, so age and
    ordering comparisons are consistent regardless of input form.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SqliteObligationGraph(ObligationGraph):
    """SQLite/SQLModel implementation of the shared Obligation Graph contract.

    Args:
        database_path: SQLite path. Defaults to the configured
            ``Settings.database_path`` (a local file). Pass ``":memory:"`` for an
            isolated in-process database (used by tests).
    """

    def __init__(self, database_path: Optional[str] = None) -> None:
        if database_path is None:
            database_path = get_settings().database_path

        self._database_path = database_path

        if database_path == IN_MEMORY:
            # Share one connection across sessions so the in-memory schema/data
            # persist for the lifetime of this store instance.
            self._engine = create_engine(
                _engine_url(database_path),
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            self._engine = create_engine(_engine_url(database_path))

        # Materialize the schema from the frozen SQLModel metadata (task 2.1).
        SQLModel.metadata.create_all(self._engine)
        self._migrate_additive_columns()

        # Recorded boundary-violation error indications (Req 14.6). Every refused
        # out-of-boundary destination appends a structured record here so the
        # rejection is observable, not just raised-and-lost.
        self._boundary_violations: list[GraphError] = []

    def _migrate_additive_columns(self) -> None:
        """Add columns that postdate an existing database file (additive only).

        ``create_all`` never alters an existing table, so a database created before
        the meeting-loop fields shipped would be missing ``kind`` / ``due_at``. Each
        missing column is added with the model's default so old rows keep behaving
        as classic reply loops.
        """
        from sqlalchemy import text

        additions = {
            "kind": "ALTER TABLE obligation ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'REPLY'",
            "due_at": "ALTER TABLE obligation ADD COLUMN due_at VARCHAR",
        }
        try:
            with self._engine.connect() as conn:
                existing = {
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info(obligation)"))
                }
                for column, ddl in additions.items():
                    if column not in existing:
                        conn.execute(text(ddl))
                conn.commit()
        except Exception:  # noqa: BLE001 — a failed migration surfaces on first write.
            logger.exception("additive column migration failed")

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def upsert(self, obligation: Obligation) -> Result[Obligation]:
        """Create or update an Obligation after validating the frozen invariants.

        Validation order matches the interface contract: Loop_State enum first
        (Req 1.3), then Confidence_Score range (Req 1.5). On either rejection the
        previously stored value is left untouched (no write occurs) and an ``Err``
        carrying the obligation id is returned.

        On valid input the row is created or updated under the last-write-wins
        ordering rule (:meth:`_resolve_write`, Req 1.9/1.10) and committed. The
        SQLite commit gives read-after-write visibility: any agent calling
        :meth:`get`/:meth:`query` after this returns ``Ok`` observes the persisted
        value (Req 1.7). If the commit fails, the transaction is rolled back so the
        last successfully stored value is retained and ``Err(PERSIST_FAILURE)`` is
        returned (Req 1.8).
        """
        # --- Req 1.2 / 1.3: Loop_State must be a value of the frozen enum. -----
        if not is_valid_loop_state(obligation.loop_state):
            return err(
                GraphErrorCode.INVALID_LOOP_STATE,
                message=f"invalid loop_state: {obligation.loop_state!r}",
                obligation_id=obligation.obligation_id,
            )

        # --- Req 1.4 / 1.5: Confidence_Score must be within [0.0, 1.0]. --------
        if not is_valid_confidence(obligation.confidence_score):
            return err(
                GraphErrorCode.INVALID_CONFIDENCE,
                message=f"confidence_score out of range: {obligation.confidence_score!r}",
                obligation_id=obligation.obligation_id,
            )

        # Copy the caller's value object so we never attach their instance to our
        # session (keeps the input a pure value object and the return detached).
        incoming = Obligation(**obligation.model_dump())

        try:
            with Session(self._engine) as session:
                existing = session.get(Obligation, incoming.obligation_id)
                if existing is None:
                    session.add(incoming)
                elif self._resolve_write(existing, incoming):
                    for key, value in incoming.model_dump().items():
                        setattr(existing, key, value)
                    session.add(existing)
                # else: the stored write wins by last-write-wins (Req 1.9/1.10) —
                # leave the row as-is and report success with the retained value.
                self._commit(session)

                stored = session.get(Obligation, incoming.obligation_id)
                # Detach a copy so callers can read fields after the session closes.
                return ok(Obligation(**stored.model_dump()))
        except Exception as exc:  # noqa: BLE001 — any persist failure maps to Err.
            # Req 1.8: the commit failed, so nothing was persisted (the session
            # context manager rolls back on exception). The last successfully
            # stored value is therefore retained intact.
            logger.warning(
                "persist failure on upsert of %s: %s",
                incoming.obligation_id,
                exc,
            )
            return err(
                GraphErrorCode.PERSIST_FAILURE,
                message=f"failed to persist obligation: {exc}",
                obligation_id=incoming.obligation_id,
            )

    def _commit(self, session: Session) -> None:
        """Commit the active session.

        Isolated as a single seam so the persist step has exactly one home: it is
        where read-after-write durability is established (Req 1.7) and the single
        point that fault-injection tests override to exercise the persist-failure
        path (Req 1.8).
        """
        session.commit()

    def _resolve_write(self, existing: Obligation, incoming: Obligation) -> bool:
        """Decide whether ``incoming`` should overwrite ``existing`` (last-write-wins).

        Ordering rule (Req 1.9, 1.10):
          * Retain the update with the **later** ``last_touch_timestamp`` — so an
            ``incoming`` write strictly newer than ``existing`` wins (returns True),
            and a strictly older one is discarded (returns False).
          * On an **identical** ``last_touch_timestamp`` the later-*received* write
            wins. ``upsert`` calls are serialized, so ``incoming`` is by definition
            the later-received write at a tie and is accepted (returns True).

        Timestamps are parsed as ISO 8601 UTC via :func:`_parse_iso_utc` so values
        written with different offsets or a trailing ``Z`` compare correctly. If a
        stored or incoming timestamp cannot be parsed, the rule cannot be evaluated;
        we conservatively accept the later-received (``incoming``) write so a
        malformed prior value can never permanently pin the row.
        """
        try:
            existing_ts = _parse_iso_utc(existing.last_touch_timestamp)
            incoming_ts = _parse_iso_utc(incoming.last_touch_timestamp)
        except (ValueError, TypeError):
            return True

        if incoming_ts > existing_ts:
            return True   # incoming is strictly newer — it wins (Req 1.9)
        if incoming_ts < existing_ts:
            return False  # incoming is strictly older — discard it (Req 1.9)
        return True       # identical timestamps — later-received wins (Req 1.10)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get(self, obligation_id: ObligationId) -> Optional[Obligation]:
        """Return the stored Obligation for ``obligation_id`` or None if absent."""
        with Session(self._engine) as session:
            stored = session.get(Obligation, obligation_id)
            if stored is None:
                return None
            return Obligation(**stored.model_dump())

    def query(self, filter: ObligationFilter) -> list[Obligation]:
        """Return all stored Obligations matching ``filter`` (AND of set fields).

        Filtering is performed in Python over the full row set, which is more than
        adequate at the graph sizes Loop operates on and keeps the surfacing and
        age semantics identical to the shared predicate. An empty filter matches
        everything; result order is unspecified at this layer (Req 6.3/9.3 sorting
        is the caller's job).
        """
        with Session(self._engine) as session:
            rows = session.exec(select(Obligation)).all()
            results = [Obligation(**row.model_dump()) for row in rows]

        # --- lifecycle state ------------------------------------------------
        if filter.loop_states:
            results = [o for o in results if o.loop_state in filter.loop_states]

        # --- dismissed exclusion (default) ----------------------------------
        if not filter.include_dismissed:
            results = [o for o in results if not o.dismissed]

        # --- participant / ownership ----------------------------------------
        if filter.owner_person_id is not None:
            results = [o for o in results if o.owner_person_id == filter.owner_person_id]
        if filter.owes_person_id is not None:
            results = [o for o in results if o.owes_person_id == filter.owes_person_id]
        if filter.owed_person_id is not None:
            results = [o for o in results if o.owed_person_id == filter.owed_person_id]

        # --- linked work artifact -------------------------------------------
        if filter.artifact_type is not None:
            results = [o for o in results if o.artifact_type == filter.artifact_type]
        if filter.artifact_ref is not None:
            results = [o for o in results if o.artifact_ref == filter.artifact_ref]

        # --- closure provenance (Auto-Healed feed) --------------------------
        if filter.closure_kind is not None:
            results = [o for o in results if o.closure_kind == filter.closure_kind]

        # Reference instant for snooze/age evaluation.
        now_iso = filter.now if filter.now is not None else _utc_now_iso()

        # --- age window relative to ``now`` ---------------------------------
        if filter.min_age_seconds is not None or filter.max_age_seconds is not None:
            now_dt = _parse_iso_utc(now_iso)

            def _within_age(o: Obligation) -> bool:
                age = (now_dt - _parse_iso_utc(o.last_touch_timestamp)).total_seconds()
                if filter.min_age_seconds is not None and age < filter.min_age_seconds:
                    return False
                if filter.max_age_seconds is not None and age > filter.max_age_seconds:
                    return False
                return True

            results = [o for o in results if _within_age(o)]

        # --- quiet-by-default surfacing gate --------------------------------
        if filter.surfaced_only:
            threshold = self.get_threshold()
            results = [o for o in results if is_surfaced(o, threshold, now_iso)]

        return results

    # ------------------------------------------------------------------
    # Threshold accessors (persisted via GraphConfig — task 3.4)
    # ------------------------------------------------------------------
    def set_threshold(self, value: float) -> Result[float]:
        """Set the global Confidence_Threshold, clamped to inclusive ``[0.0, 1.0]``.

        The value is clamped first (this setter is the authoritative clamp, so the
        stored threshold can never leave the inclusive range — Req 5.6) and then
        persisted to the single-row :class:`GraphConfig`, replacing any prior value.
        Persisting durably means the threshold survives across store instances that
        share the same database file.

        On persist failure the transaction is rolled back, the prior threshold is
        retained, and ``Err(PERSIST_FAILURE)`` is returned (mirrors the upsert
        persist-failure contract, Req 1.8). On success returns ``Ok(stored_value)``
        carrying the actually-persisted (clamped) threshold.
        """
        clamped = clamp_confidence(value)
        try:
            with Session(self._engine) as session:
                config = session.get(GraphConfig, GRAPH_CONFIG_KEY)
                if config is None:
                    config = GraphConfig(
                        user_id=GRAPH_CONFIG_KEY,
                        confidence_threshold=clamped,
                    )
                else:
                    config.confidence_threshold = clamped
                session.add(config)
                self._commit(session)
            return ok(clamped)
        except Exception as exc:  # noqa: BLE001 — any persist failure maps to Err.
            logger.warning("persist failure on set_threshold(%s): %s", value, exc)
            return err(
                GraphErrorCode.PERSIST_FAILURE,
                message=f"failed to persist threshold: {exc}",
            )

    def get_threshold(self) -> float:
        """Return the current global Confidence_Threshold (always within ``[0.0, 1.0]``).

        Reads the persisted single-row :class:`GraphConfig`; before any
        :meth:`set_threshold`, the default starting gate
        (:data:`DEFAULT_CONFIDENCE_THRESHOLD`) applies. Because the value is read
        from storage, a threshold tuned by one store instance is visible to every
        other instance backed by the same database (Req 5.6).
        """
        with Session(self._engine) as session:
            config = session.get(GraphConfig, GRAPH_CONFIG_KEY)
            if config is None:
                return DEFAULT_CONFIDENCE_THRESHOLD
            return config.confidence_threshold

    # ------------------------------------------------------------------
    # Feedback persistence (Learn loop — task 6)
    # ------------------------------------------------------------------
    def add_feedback(self, event: FeedbackEvent) -> Result[FeedbackEvent]:
        """Append a Confirm/Dismiss :class:`FeedbackEvent` to the graph.

        Feedback events are the immutable record the Learn loop consumes (Req 5.1,
        5.2). This is an append: each call inserts one new row keyed by
        ``event_id``. Persistence mirrors the :meth:`upsert` contract — on a commit
        failure the transaction is rolled back so nothing is persisted and
        ``Err(PERSIST_FAILURE)`` is returned (Req 5.7); on success the stored event
        is returned detached from the session.
        """
        incoming = FeedbackEvent(**event.model_dump())
        try:
            with Session(self._engine) as session:
                session.add(incoming)
                self._commit(session)
                stored = session.get(FeedbackEvent, incoming.event_id)
                return ok(FeedbackEvent(**stored.model_dump()))
        except Exception as exc:  # noqa: BLE001 — any persist failure maps to Err.
            logger.warning(
                "persist failure on add_feedback for %s: %s",
                incoming.obligation_id,
                exc,
            )
            return err(
                GraphErrorCode.PERSIST_FAILURE,
                message=f"failed to persist feedback event: {exc}",
                obligation_id=incoming.obligation_id,
            )

    def get_feedback(self, obligation_id: ObligationId) -> list[FeedbackEvent]:
        """Return every :class:`FeedbackEvent` recorded against ``obligation_id``.

        Order is unspecified at this layer; callers that need chronological order
        sort by ``created_at`` themselves. Returns an empty list when no feedback
        has been recorded for the obligation.
        """
        with Session(self._engine) as session:
            rows = session.exec(
                select(FeedbackEvent).where(
                    FeedbackEvent.obligation_id == obligation_id
                )
            ).all()
            return [FeedbackEvent(**row.model_dump()) for row in rows]

    # ------------------------------------------------------------------
    # Workspace boundary guard (Req 1.11, 14.2, 14.6)
    # ------------------------------------------------------------------
    def _is_in_boundary(self, dest: object) -> bool:
        """Return whether ``dest`` is inside the user's Slack workspace boundary.

        The workspace boundary is the embedded store itself. The only in-boundary
        destinations are therefore:
          * ``None`` — no external sink; nothing leaves the boundary, and
          * the store's own configured database path (incl. the in-memory sentinel).

        Every other destination — an external URL, a filepath other than the
        workspace store, a socket/stream, or any other object — is treated as
        out-of-boundary and refused (Req 1.11, 14.2, 14.6).
        """
        if dest is None:
            return True
        if isinstance(dest, str):
            return dest.strip() == self._database_path
        return False

    def reject_external_write(self, dest: object) -> None:
        """Boundary guard: refuse any write/copy of graph data outside the workspace.

        If ``dest`` is out-of-boundary (see :meth:`_is_in_boundary`), the attempt is
        refused: the data is retained within the boundary (nothing is exported),
        a structured :class:`GraphError` error indication is recorded
        (see :attr:`boundary_violations`, Req 14.6), and a ``PermissionError`` is
        raised so the calling agent cannot proceed. An in-boundary destination
        returns normally.
        """
        if self._is_in_boundary(dest):
            return

        error = GraphError(
            code=GraphErrorCode.BOUNDARY_VIOLATION,
            message=f"refused out-of-boundary write/copy to {dest!r}",
        )
        self._boundary_violations.append(error)
        logger.warning("%s", error.message)
        raise PermissionError(error.message)

    @property
    def boundary_violations(self) -> list[GraphError]:
        """The recorded out-of-boundary write attempts (Req 14.6 error indications)."""
        return list(self._boundary_violations)


def _utc_now_iso() -> str:
    """Current time as an ISO 8601 UTC string (local helper to avoid import cycles)."""
    return datetime.now(timezone.utc).isoformat()


__all__ = ["SqliteObligationGraph", "IN_MEMORY"]
