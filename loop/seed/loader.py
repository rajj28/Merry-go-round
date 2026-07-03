"""Seed loader + demo-mode determinism + reset (Req 15.1, 15.5, 15.6).

This is the *behavior half* of the Seeded Deterministic Demo Workspace: it takes
the committed fixture pack (:mod:`loop.seed.fixtures`) and initializes an
Obligation Graph to a known state, so the four demo beats reproduce identically
every run.

Determinism contract realized here (design.md → "Seeded Deterministic Demo
Workspace"):

  * **Order-independent load** (Req 15.1) — :meth:`SeededWorkspace.load` upserts the
    precomputed seed obligations, optionally in a caller-supplied ``order``. Because
    each seeded obligation has a distinct id and a fixed precomputed value, the final
    graph state — and therefore the surfaced set, its loop_states, confidence_scores,
    and display ordering — is identical regardless of the order in which the seeded
    candidates are processed. (This is exactly the demo-mode guarantee: outcomes are
    pinned by the fixture, not produced by a live, order-sensitive model call.)
  * **Reset** (Req 15.5) — :meth:`reset` rebuilds the graph from the same fixture
    pack, reproducing the same surfaced obligations and the same hero count as the
    initial run.
  * **Load-failure handling** (Req 15.6) — if loading the seed raises, the workspace
    records an init error and surfaces **no** obligations.

The surfaced view is computed through the *real* shared surfacing predicate
(:func:`loop.graph.surfacing.is_surfaced`, via ``ObligationFilter.surfaced_only``)
so this loader can never drift from what App Home / the digest / the assistant show.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from loop.graph.models import Obligation
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import ObligationFilter, ObligationGraph, is_ok
from loop.seed.fixtures import SEED_THRESHOLD, seed_obligations

# A fixed reference instant for surfacing/age evaluation in the seeded workspace.
# It sits after every seeded ``last_touch_timestamp`` so all seeded obligations are
# fully "aged in"; using a constant (rather than the wall clock) keeps the surfaced
# view byte-for-byte reproducible across runs (Req 15.1).
SEED_NOW: str = "2025-01-06T00:00:00+00:00"

# Factory type for building the backing graph; overridable for tests.
GraphFactory = Callable[[], ObligationGraph]


class SeedLoadError(RuntimeError):
    """Raised internally when the seed fixture pack cannot be loaded."""


def _default_graph_factory() -> ObligationGraph:
    """Build a fresh in-memory Obligation Graph for the seeded workspace."""
    return SqliteObligationGraph(IN_MEMORY)


def _display_sort_key(obligation: Obligation) -> tuple[str, str]:
    """Canonical display ordering key: ``last_touch_timestamp`` oldest→newest,
    with ``obligation_id`` as a deterministic tie-break.

    Matches the App Home section ordering rule (oldest→newest by last touch,
    Req 6.3/6.4) and guarantees a *total* order so the surfaced sequence is
    identical on every run (Req 15.1).
    """
    return (obligation.last_touch_timestamp, obligation.obligation_id)


class SeededWorkspace:
    """A deterministic, reloadable seeded Obligation Graph for the demo.

    Owning graph construction lets :meth:`reset` rebuild cleanly from the committed
    fixture pack. Construct with ``autoload=True`` (the default) to load immediately,
    or call :meth:`load` explicitly.

    Args:
        graph_factory: builds the backing graph; defaults to a fresh in-memory store.
        threshold: the seeded Confidence_Threshold (defaults to ``SEED_THRESHOLD``).
        autoload: when True, load the seed during construction.
    """

    def __init__(
        self,
        graph_factory: Optional[GraphFactory] = None,
        *,
        threshold: float = SEED_THRESHOLD,
        autoload: bool = True,
    ) -> None:
        self._graph_factory = graph_factory or _default_graph_factory
        self._threshold = threshold
        self.graph: ObligationGraph = self._graph_factory()
        self.init_error: Optional[str] = None
        if autoload:
            self.load()

    # ------------------------------------------------------------------
    # Loading / resetting
    # ------------------------------------------------------------------
    def load(self, order: Optional[Sequence[int]] = None) -> bool:
        """Load the committed fixture pack into a fresh graph (Req 15.1).

        Always starts from a brand-new graph so a reload is a true reset to the
        initial seeded state (Req 15.5). The seeded obligations are upserted in
        ``order`` when supplied (a permutation of indices into
        :func:`seed_obligations`), otherwise in fixture order; the resulting state
        is identical either way because outcomes are pinned per id.

        On any failure the workspace records an init error and ends up surfacing no
        obligations (Req 15.6).

        Args:
            order: optional permutation of indices controlling upsert order.

        Returns:
            ``True`` if the seed loaded cleanly, ``False`` if an init error occurred.
        """
        self.graph = self._graph_factory()
        self.init_error = None
        try:
            obligations = seed_obligations()

            indices: Sequence[int]
            if order is None:
                indices = range(len(obligations))
            else:
                if sorted(order) != list(range(len(obligations))):
                    raise SeedLoadError(
                        "order must be a permutation of the seed obligation indices"
                    )
                indices = order

            threshold_result = self.graph.set_threshold(self._threshold)
            if not is_ok(threshold_result):
                raise SeedLoadError("failed to persist seed threshold")

            for i in indices:
                result = self.graph.upsert(obligations[i])
                if not is_ok(result):
                    raise SeedLoadError(
                        f"failed to load seed obligation {obligations[i].obligation_id}"
                    )
            return True
        except Exception as exc:  # noqa: BLE001 — any load failure → init error.
            # Req 15.6: record the init error and ensure nothing is surfaced by
            # discarding any partially-loaded graph.
            self.init_error = f"seeded workspace failed to initialize: {exc}"
            self.graph = self._graph_factory()
            return False

    def reset(self, order: Optional[Sequence[int]] = None) -> bool:
        """Reset to the initial seeded state (Req 15.5).

        Equivalent to a fresh :meth:`load`; reproduces the same surfaced obligations
        and the same hero count as the initial run.
        """
        return self.load(order)

    # ------------------------------------------------------------------
    # Surfaced view (through the real shared surfacing predicate)
    # ------------------------------------------------------------------
    def surfaced_obligations(self, now: str = SEED_NOW) -> list[Obligation]:
        """Return the surfaced active obligations in canonical display order.

        Uses the real quiet-by-default surfacing gate (``surfaced_only`` →
        :func:`loop.graph.surfacing.is_surfaced`) against the seeded threshold and a
        fixed reference instant, then applies the App-Home display ordering. When the
        seed failed to load this is empty (Req 15.6).
        """
        if self.init_error is not None:
            return []
        surfaced = self.graph.query(
            ObligationFilter(surfaced_only=True, now=now)
        )
        return sorted(surfaced, key=_display_sort_key)

    def hero_count(self, now: str = SEED_NOW) -> int:
        """Count of surfaced ``blocked-on-you`` obligations (the hero banner, Req 15.2)."""
        from loop.graph.models import LoopState

        return sum(
            1
            for o in self.surfaced_obligations(now)
            if o.loop_state == LoopState.BLOCKED_ON_YOU
        )


def surfacing_signature(obligations: Sequence[Obligation]) -> tuple:
    """Build a comparable signature of a surfaced sequence (Req 15.1 / Property 28).

    Captures exactly what Property 28 requires to be identical across runs: each
    obligation's **identity**, **Loop_State**, **Confidence_Score**, and the
    **display ordering** (the tuple is order-sensitive).
    """
    return tuple(
        (
            o.obligation_id,
            o.loop_state.value,
            o.confidence_score,
        )
        for o in obligations
    )


__all__ = [
    "SEED_NOW",
    "SeedLoadError",
    "SeededWorkspace",
    "surfacing_signature",
    "seed_obligations_rebased",
    "load_into_graph",
]


# ---------------------------------------------------------------------------
# Live demo seeding — load the seeded org into a real (running-app) graph, with
# timestamps rebased so the carefully-tuned relative ages (fresh / warning /
# overdue) stay correct against the *live* clock instead of the pinned Jan-2025
# reference instant. This is what makes the polished Northwind org fully
# *interactive* in the running app (nudge / quick-nudge / PR-merge auto-heal),
# rather than only a static published dashboard.
# ---------------------------------------------------------------------------
def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp into a tz-aware UTC datetime (accepts trailing Z)."""
    text = ts.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _coerce_now(now: Optional[object]) -> datetime:
    """Coerce ``now`` (datetime | ISO string | None) into a tz-aware UTC datetime."""
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    return _parse_iso(str(now))


def seed_obligations_rebased(now: Optional[object] = None) -> list[Obligation]:
    """Return the seed obligations with every timestamp shifted by ``now - SEED_NOW``.

    The fixture timestamps are tuned relative to :data:`SEED_NOW` (2025-01-06) so the
    surfaced spread reads fresh/warning/overdue at that instant. Loading them verbatim
    into a live graph evaluated at the real clock would make every loop appear
    months overdue. Rebasing preserves the *relative* spacing (and therefore the aging
    chips, ordering, and the still-future snooze on ``OBL_SNOOZED``) while anchoring it
    to ``now`` — so the live dashboard looks exactly like the deterministic preview.
    """
    offset = _coerce_now(now) - _parse_iso(SEED_NOW)

    def _shift(ts: Optional[str]) -> Optional[str]:
        return (_parse_iso(ts) + offset).isoformat() if ts else ts

    rebased: list[Obligation] = []
    for o in seed_obligations():
        updates: dict[str, object] = {"last_touch_timestamp": _shift(o.last_touch_timestamp)}
        if o.closure_timestamp:
            updates["closure_timestamp"] = _shift(o.closure_timestamp)
        if o.snoozed_until:
            updates["snoozed_until"] = _shift(o.snoozed_until)
        rebased.append(o.model_copy(update=updates))
    return rebased


def load_into_graph(
    graph: ObligationGraph,
    *,
    now: Optional[object] = None,
    threshold: float = SEED_THRESHOLD,
) -> int:
    """Upsert the rebased seeded org into an existing (live) graph; return the count.

    Sets the seeded Confidence_Threshold and upserts every rebased seed obligation
    (idempotent — fixed obligation ids mean re-seeding on each boot overwrites in
    place, giving a clean demo state every run). People are intentionally not written
    (the store does not require them; display names/avatars come from the seed maps).
    """
    graph.set_threshold(threshold)
    count = 0
    for o in seed_obligations_rebased(now):
        if is_ok(graph.upsert(o)):
            count += 1
    return count
