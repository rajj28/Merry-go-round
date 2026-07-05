"""The Cycle Breaker — agentic reasoning over a detected deadlock.

When :mod:`loop.graph.chains` finds a deadlock ring (``A owes B, B owes C,
C owes A``) no amount of individual nudging can resolve it: every party is
waiting for someone else to move first. Breaking it is a *decision*, and this
module is the reasoning step that makes it — the same Perceive → Reason → Act
shape as the detection pipeline, applied to graph structure instead of
messages:

    chains.find_cycles (Perceive) → plan_cycle_break (Reason, smart tier)
        → the surfaced deadlock card with a drafted first move (Act)

The smart-tier LLM is asked *which edge should move first* — the smallest
effort that unlocks the most — and to draft the message that gets that edge
moving. Reasoning failures are contained exactly like the Adjudicator's
(Req 3.7 spirit): any raise or malformed answer falls back to a deterministic
heuristic (the **stalest** edge — the one untouched longest — moves first), so
a detected deadlock is always surfaced with an actionable plan, LLM or not.

Injectable port discipline (same as the Adjudicator / Watcher): the LLM is
reached through a narrow callable so tests inject fakes and never hit the
network; :func:`build_cycle_break_client` wires the real provider-agnostic
client lazily via :mod:`loop.llm`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loop.graph.chains import BlockingCycle
from loop.graph.models import Obligation, ObligationId

logger = logging.getLogger(__name__)

# The drafted first-move message is clipped to the same budget as a nudge.
CYCLE_BREAK_DRAFT_MAX_CHARS = 1000

# How the plan's break edge was chosen — shown in the Tool-Use Trace so the
# agentic reasoning stays legible to the user.
SOURCE_LLM = "smart-tier"
SOURCE_HEURISTIC = "stalest-edge heuristic"


@dataclass(frozen=True)
class CycleBreakPlan:
    """The Reason step's output: where to break one deadlock, and how.

    Attributes:
        cycle: the deadlock ring the plan is for.
        break_obligation_id: the edge that should move first (always one of
            ``cycle.edges``).
        rationale: one short sentence of *why this edge* — surfaced verbatim on
            the deadlock card so the agent's reasoning is visible.
        draft_message: the drafted Slack message that gets the chosen edge
            moving (clipped to :data:`CYCLE_BREAK_DRAFT_MAX_CHARS`).
        source: :data:`SOURCE_LLM` when the smart tier produced the plan,
            :data:`SOURCE_HEURISTIC` when the deterministic fallback did.
    """

    cycle: BlockingCycle
    break_obligation_id: ObligationId
    rationale: str
    draft_message: str
    source: str

    @property
    def break_edge(self) -> Obligation:
        """The full obligation chosen as the first move."""
        for edge in self.cycle.edges:
            if edge.obligation_id == self.break_obligation_id:
                return edge
        raise ValueError(
            f"plan's break edge {self.break_obligation_id!r} is not in its cycle"
        )


# The injectable Reason port: given a cycle, return (break_obligation_id,
# rationale, draft_message). MAY raise — plan_cycle_break contains the failure.
CycleBreakClient = Callable[[BlockingCycle], tuple[ObligationId, str, str]]


def stalest_edge(cycle: BlockingCycle) -> Obligation:
    """The deterministic fallback choice: the edge untouched the longest.

    The stalest edge is where the deadlock has been rotting; asking it to move
    first is the neutral, defensible default. Ties break on obligation id so
    the choice is stable across runs (seeded-demo determinism).
    """
    return min(cycle.edges, key=lambda e: (e.last_touch_timestamp, e.obligation_id))


def _heuristic_plan(cycle: BlockingCycle) -> CycleBreakPlan:
    edge = stalest_edge(cycle)
    ring = " → ".join((*cycle.people, cycle.people[0]))
    return CycleBreakPlan(
        cycle=cycle,
        break_obligation_id=edge.obligation_id,
        rationale=(
            f"This edge has been untouched the longest in the {ring} deadlock; "
            "moving it first unblocks the ring with no one waiting on a warmer "
            "conversation."
        ),
        draft_message=(
            f"Hi — flagging a circular block: {ring}. Everyone is waiting on "
            f"someone else, so nothing moves. Could we start with this one: "
            f"{edge.subject_summary}? Happy to hop on a huddle to untangle it."
        )[:CYCLE_BREAK_DRAFT_MAX_CHARS],
        source=SOURCE_HEURISTIC,
    )


def plan_cycle_break(
    cycle: BlockingCycle,
    *,
    client: Optional[CycleBreakClient] = None,
) -> CycleBreakPlan:
    """Produce a break plan for one deadlock, never failing.

    Runs the injected Reason ``client`` when given; any raise, an unknown
    obligation id, or empty text falls back to the deterministic
    :func:`stalest_edge` heuristic so the caller always gets an actionable
    plan (failure containment, same posture as the AdjudicationQueue).
    """
    if client is not None:
        try:
            break_id, rationale, draft = client(cycle)
            valid_ids = {e.obligation_id for e in cycle.edges}
            if break_id in valid_ids and rationale.strip() and draft.strip():
                return CycleBreakPlan(
                    cycle=cycle,
                    break_obligation_id=break_id,
                    rationale=rationale.strip(),
                    draft_message=draft.strip()[:CYCLE_BREAK_DRAFT_MAX_CHARS],
                    source=SOURCE_LLM,
                )
            logger.warning(
                "cycle-break client returned an invalid plan (id %r); "
                "falling back to the stalest-edge heuristic",
                break_id,
            )
        except Exception:  # noqa: BLE001 — reasoning failure must not lose the deadlock.
            logger.exception(
                "cycle-break reasoning failed; falling back to the stalest-edge heuristic"
            )
    return _heuristic_plan(cycle)


def build_cycle_break_client(settings: Any | None = None) -> CycleBreakClient:
    """Wire the real smart-tier Reason port from config.

    Provider-agnostic: routes through :func:`loop.llm.chat` at the **smart
    tier** (this is a judgement call over multiple parties, not a cheap
    filter). The provider SDK and the network call happen lazily inside the
    returned callable; the active provider's credential is validated at build
    time. The returned callable MAY raise — :func:`plan_cycle_break` contains
    that.
    """
    if settings is None:
        from loop.config import get_settings

        settings = get_settings()
    from loop.llm import require_provider_key

    require_provider_key(settings)

    def _client(cycle: BlockingCycle) -> tuple[ObligationId, str, str]:
        import json

        from loop.llm import chat

        lines = [
            f"- obligation_id={e.obligation_id} | {e.owes_person_id} owes "
            f"{e.owed_person_id} | subject: {e.subject_summary} | "
            f"last touched: {e.last_touch_timestamp}"
            for e in cycle.edges
        ]
        ring = " -> ".join((*cycle.people, cycle.people[0]))
        prompt = (
            "A deadlock was detected in a Slack workspace's obligation graph: "
            f"a ring of people all blocked on each other ({ring}). No amount of "
            "individual reminding can fix a ring — one edge has to move first.\n\n"
            "The edges of the ring:\n" + "\n".join(lines) + "\n\n"
            "Decide which single edge should move FIRST: prefer the smallest "
            "effort that unlocks the most, considering the subjects and how "
            "stale each edge is. Then draft a short, warm Slack message (under "
            "1000 characters) that names the circular block plainly and asks "
            "the chosen edge's debtor to make the first move.\n\n"
            'Respond ONLY with JSON: {"break_obligation_id": string, '
            '"rationale": string (one sentence), "draft_message": string}.'
        )
        text = chat(
            [{"role": "user", "content": prompt}],
            tier="smart",
            settings=settings,
            json_mode=True,
            max_tokens=512,
        )
        data = json.loads(text)
        return (
            str(data["break_obligation_id"]),
            str(data.get("rationale", "")),
            str(data.get("draft_message", "")),
        )

    return _client


__all__ = [
    "CycleBreakPlan",
    "CycleBreakClient",
    "CYCLE_BREAK_DRAFT_MAX_CHARS",
    "SOURCE_LLM",
    "SOURCE_HEURISTIC",
    "stalest_edge",
    "plan_cycle_break",
    "build_cycle_break_client",
]
