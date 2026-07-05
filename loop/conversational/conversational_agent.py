"""The Conversational Agent (Front door) — Agent 5.

Natural-language control of Loop in the Slack Assistant pane, with a **visible
Tool_Use_Trace** after every request (design.md → "Conversational Agent (Front
door)", flow (e) "Conversational query with tool-use trace", Req 12).

Scope realized **here** (tasks 15.1, 15.2, 15.3):

task 15.1 — NL query handling (Req 12.1, 12.2):
  * :meth:`ConversationalAgent.handle_query` parses a natural-language question into
    an :class:`~loop.graph.store.ObligationFilter`, reads the Obligation Graph, and
    replies with the matching surfaced Obligations. The read is an in-memory graph
    query so the reply is produced well within the 5-second budget (Req 12.1). When
    the filter matches zero Obligations it replies with an explicit no-match message
    (Req 12.2). Every reply carries the Tool_Use_Trace (here: ``Obligation Graph
    (query)``).

task 15.2 — NL command routing, disambiguation, and the Tool_Use_Trace
(Req 12.3, 12.4, 12.5, 12.8, 12.9):
  * :meth:`ConversationalAgent.handle_command` parses a nudge/delegate/snooze/close
    command, resolves its target Obligation against the graph, and routes it to the
    :class:`~loop.action.action_agent.ActionAgent` with the identified target and the
    requested action (Req 12.3).
  * If the command maps to more than one candidate Obligation it does **not** route;
    it replies with the candidate set and asks the user to pick one (Req 12.4).
  * Every reply renders a :class:`ToolUseTrace` listing, **in invocation order**,
    each tool and agent invoked to fulfil the request (Req 12.5). This visible trace
    is a core Best-UX / Most-Innovative differentiator, so it is built from the
    *actual* invocations the agent makes (graph query → Verifier (GitHub MCP) →
    Action Agent), never a canned string.
  * A request that cannot be mapped to a supported action gets a reply listing the
    supported actions (Req 12.9).
  * If the Action Agent reports a failure executing a routed command, the agent
    reports the failure and leaves the Obligation in its pre-command state (Req 12.8)
    — which holds naturally because the Action Agent retains the prior value on every
    failure path.

task 15.3 — send-as-user confirmation gate in the pane (Req 12.6, 12.7):
  * A command that sends a message as the user (nudge, delegate) is **never** sent
    immediately. :meth:`handle_command` prepares the draft / handoff prompt (a
    read-only step that does not send) and returns a confirmation-required reply,
    stashing a pending confirmation; the Action Agent is not instructed to send until
    the user taps confirm (Req 12.6).
  * :meth:`confirm` completes the pending send **only** if it is tapped within the
    60-second interaction window; :meth:`decline` and :meth:`check_timeout` cancel it.
    A decline, or 60 seconds of silence, cancels the command, leaves the Obligation
    unchanged, and tells the user no message was sent (Req 12.7).

Autonomous commands (snooze, close) do not send as the user, so they route straight
to the Action Agent with no confirmation gate (design "Autonomy Boundaries", Req 13).

Dynamic orchestration (additive — the "agentic orchestration" showcase):
  * The agent accepts an injectable :data:`~loop.conversational.planner.Planner` port.
    When wired, the unified :meth:`ConversationalAgent.handle` entry point asks the
    planner — a ReAct-style reasoner over the :data:`~loop.conversational.planner.
    AVAILABLE_TOOLS` catalog — for an ordered :class:`~loop.conversational.planner.
    Plan` of tool/agent invocations *for this specific request*, then executes that
    plan step-by-step against the real collaborators (Obligation Graph, Verifier,
    Action Agent). The Tool_Use_Trace now records the agent's **plan** interleaved
    with its **execution**, so judges see the reasoning, not a fixed route (Req 12.5).
  * The confirmation gate and ReplyKinds are preserved end-to-end: a planned
    send-as-user step (draft_nudge, delegate) still prepares only and awaits a
    one-tap confirm (Req 12.6).
  * Graceful degradation: if the planner errors or returns an unusable plan, the
    agent falls back to the deterministic parser path and records the fallback in
    the trace — so dynamic orchestration never blocks the user. The planner defaults
    to ``None``, so the original parser-based behaviour (and every existing test) is
    unchanged unless a planner is explicitly wired.

Design discipline: natural-language *understanding* is isolated behind an injectable
parser port (mirroring the Action Agent's injectable the LLM / Slack ports), so the
routing, disambiguation, trace, and confirmation logic tested here never depends on a
live LLM. A deterministic rule-based :func:`default_parser` handles the demo phrases.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Optional, Union

from loop.action.action_agent import ActionAgent, NudgeDraft
from loop.graph.models import (
    LoopState,
    Obligation,
    PersonId,
    UserId,
    utc_now_iso,
)
from loop.graph.store import ObligationFilter, ObligationGraph

from loop.conversational.planner import (
    TOOL_CLOSE,
    TOOL_DELEGATE,
    TOOL_DRAFT_NUDGE,
    TOOL_QUERY_GRAPH,
    TOOL_SNOOZE,
    TOOL_VERIFY_PR,
    AVAILABLE_TOOLS,
    KNOWN_TOOLS,
    Plan,
    PlanStep,
    Planner,
)

logger = logging.getLogger(__name__)

# The in-pane interaction window for a one-tap confirmation (Req 12.7). This is the
# tighter conversational timeout; the broader send-as-user confirmation timeout
# (Req 13.2) is 24h and lives with the Action Agent / scheduler wiring (task 17).
CONFIRM_TIMEOUT = timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Supported commands (the vocabulary the front door understands)
# ---------------------------------------------------------------------------
class CommandAction(str, Enum):
    """The four natural-language commands routed to the Action Agent (Req 12.3)."""

    NUDGE = "nudge"
    DELEGATE = "delegate"
    SNOOZE = "snooze"
    CLOSE = "close"


# The two commands that send a message *as the user* and therefore require a one-tap
# confirmation before the Action Agent is instructed to send (Req 12.6, 13.2).
SEND_AS_USER_ACTIONS: frozenset[CommandAction] = frozenset(
    {CommandAction.NUDGE, CommandAction.DELEGATE}
)

# The set replied with when a request cannot be mapped to a supported action
# (Req 12.9). Ordered for a stable, human-readable reply.
SUPPORTED_ACTIONS: tuple[str, ...] = (
    CommandAction.NUDGE.value,
    CommandAction.DELEGATE.value,
    CommandAction.SNOOZE.value,
    CommandAction.CLOSE.value,
)


class Selector(str, Enum):
    """A superlative that narrows a candidate set down to exactly one Obligation.

    ``oldest``/``newest`` are resolved against ``last_touch_timestamp`` (oldest =
    least-recently touched). When a command carries a selector it picks a single
    target, so "nudge the oldest one" never triggers disambiguation (Req 12.4).
    """

    OLDEST = "oldest"
    NEWEST = "newest"


# ---------------------------------------------------------------------------
# Parsed-intent value objects (the output of the NL parser port)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ParsedQuery:
    """A natural-language *question* parsed into graph filter criteria (Req 12.1)."""

    filter: ObligationFilter


@dataclass(frozen=True)
class ParsedCommand:
    """A natural-language *command* parsed into an action + target criteria (Req 12.3).

    Attributes:
        action: which Action Agent capability to route to.
        filter: the criteria that select the candidate target Obligation(s).
        selector: an optional superlative ("oldest"/"newest") that, when present,
            collapses the candidate set to a single target (so no disambiguation).
        teammate_id: the delegation recipient (delegate only, Req 10.6).
        duration: the requested snooze duration (snooze only); ``None`` defers to the
            Action Agent's 24h default (Req 13.3).
    """

    action: CommandAction
    filter: ObligationFilter
    selector: Optional[Selector] = None
    teammate_id: Optional[PersonId] = None
    duration: Optional[timedelta] = None


# A parser maps raw text to a query, a command, or ``None`` (unmappable → Req 12.9).
ParsedRequest = Union[ParsedQuery, ParsedCommand, None]
NLParser = Callable[[str], ParsedRequest]


# ---------------------------------------------------------------------------
# Tool_Use_Trace (Req 12.5) — the visible record of what the agent did
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolUseTraceEntry:
    """One tool/agent invocation in the Tool_Use_Trace (Req 12.5).

    Attributes:
        tool: the tool or agent invoked (e.g. ``"Obligation Graph"``,
            ``"Verifier (GitHub MCP)"``, ``"Action Agent"``).
        detail: an optional sub-detail (e.g. the operation: ``"query"``,
            ``"draft_polite_nudge"``) shown in parentheses.
    """

    tool: str
    detail: Optional[str] = None

    def render(self) -> str:
        """Render this entry as ``Tool (detail)`` or just ``Tool``."""
        return f"{self.tool} ({self.detail})" if self.detail else self.tool


# Canonical tool/agent names so every trace reads consistently across requests.
TRACE_GRAPH = "Obligation Graph"
TRACE_VERIFIER = "Verifier (GitHub MCP)"
TRACE_ACTION_AGENT = "Action Agent"
# The dynamic-orchestration planner appears in the trace too, so the visible record
# shows the agent's *plan* (and any graceful fallback) alongside its execution.
TRACE_PLANNER = "Planner"


@dataclass
class ToolUseTrace:
    """An ordered, human-readable record of every tool/agent invoked (Req 12.5).

    Entries are appended **as the agent invokes each tool**, so the trace reflects
    the real invocation order rather than a pre-written list. :meth:`render` joins
    them with arrows, matching the design's ``A → B → C`` presentation.
    """

    entries: list[ToolUseTraceEntry] = field(default_factory=list)

    def add(self, tool: str, detail: Optional[str] = None) -> None:
        """Append an invocation to the trace, in invocation order."""
        self.entries.append(ToolUseTraceEntry(tool=tool, detail=detail))

    def render(self) -> str:
        """Render the whole trace as ``A → B → C`` (empty string when nothing ran)."""
        return " → ".join(entry.render() for entry in self.entries)


# ---------------------------------------------------------------------------
# Assistant reply
# ---------------------------------------------------------------------------
class ReplyKind(str, Enum):
    """The shape of an :class:`AssistantReply`, for unambiguous handling/testing."""

    QUERY_RESULT = "query_result"        # query matched ≥1 obligation (Req 12.1)
    NO_MATCH = "no_match"                # query/command matched zero (Req 12.2)
    DISAMBIGUATION = "disambiguation"    # >1 candidate; pick one (Req 12.4)
    CLARIFICATION = "clarification"      # command under-specified (e.g. no teammate)
    CONFIRM_REQUIRED = "confirm_required"  # send-as-user; awaiting tap (Req 12.6)
    ACTION_DONE = "action_done"          # routed command succeeded (Req 12.3)
    ACTION_FAILED = "action_failed"      # Action Agent reported failure (Req 12.8)
    CANCELLED = "cancelled"              # declined / 60s timeout (Req 12.7)
    UNSUPPORTED = "unsupported"          # unmappable request (Req 12.9)


@dataclass(frozen=True)
class AssistantReply:
    """A single reply rendered into the Assistant pane.

    Every reply carries a :class:`ToolUseTrace` (Req 12.5). The other fields are
    populated per :class:`ReplyKind`:

    Attributes:
        kind: the reply shape.
        text: the human-readable message shown to the user.
        trace: the Tool_Use_Trace for the request (Req 12.5).
        obligations: the matching set for a query result (Req 12.1).
        candidates: the candidate set when disambiguation is required (Req 12.4).
        supported_actions: the supported actions for an unmappable request (Req 12.9).
        requires_confirmation: True for a send-as-user command awaiting a one-tap
            confirm (Req 12.6).
        obligation: the single target Obligation an action acted on / would act on.
        error: the failure detail when ``kind`` is :attr:`ReplyKind.ACTION_FAILED`.
    """

    kind: ReplyKind
    text: str
    trace: ToolUseTrace
    obligations: tuple[Obligation, ...] = ()
    candidates: tuple[Obligation, ...] = ()
    supported_actions: tuple[str, ...] = ()
    requires_confirmation: bool = False
    obligation: Optional[Obligation] = None
    error: Optional[str] = None

    @property
    def trace_text(self) -> str:
        """The rendered Tool_Use_Trace string (Req 12.5)."""
        return self.trace.render()


# ---------------------------------------------------------------------------
# Pending one-tap confirmation (the send-as-user gate state — Req 12.6, 12.7)
# ---------------------------------------------------------------------------
@dataclass
class _PendingConfirmation:
    """A send-as-user command awaiting the user's one-tap confirm (Req 12.6).

    Held per-user in-process between :meth:`ConversationalAgent.handle_command` and
    the user's :meth:`confirm`/:meth:`decline`. ``created_at`` anchors the 60-second
    interaction timeout (Req 12.7). No message has been sent while this exists.
    """

    action: CommandAction
    obligation: Obligation
    created_at: str
    nudge_draft: Optional[NudgeDraft] = None
    teammate_id: Optional[PersonId] = None


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO 8601 timestamp into a tz-aware UTC ``datetime``.

    Mirrors the coercion used across the store, surfacing predicate, and Action
    Agent so the 60s confirmation arithmetic here is consistent with the rest of the
    system.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Default rule-based NL parser (deterministic; no LLM)
# ---------------------------------------------------------------------------
# Verb → action mapping for command detection (first match wins, longest phrases
# first so "hand off" beats a bare word).
_COMMAND_VERBS: tuple[tuple[str, CommandAction], ...] = (
    ("nudge", CommandAction.NUDGE),
    ("remind", CommandAction.NUDGE),
    ("ping", CommandAction.NUDGE),
    ("hand off", CommandAction.DELEGATE),
    ("handoff", CommandAction.DELEGATE),
    ("delegate", CommandAction.DELEGATE),
    ("reassign", CommandAction.DELEGATE),
    ("snooze", CommandAction.SNOOZE),
    ("mute", CommandAction.SNOOZE),
    ("close", CommandAction.CLOSE),
    ("resolve", CommandAction.CLOSE),
    ("mark done", CommandAction.CLOSE),
    ("mark as done", CommandAction.CLOSE),
)

_DURATION_UNITS: dict[str, timedelta] = {
    "hour": timedelta(hours=1),
    "hours": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "hrs": timedelta(hours=1),
    "day": timedelta(days=1),
    "days": timedelta(days=1),
    "week": timedelta(weeks=1),
    "weeks": timedelta(weeks=1),
}

_ACTIVE_STATES: frozenset[LoopState] = frozenset(
    {LoopState.BLOCKED_ON_YOU, LoopState.WAITING_ON_OTHER}
)
_BLOCKED_ONLY: frozenset[LoopState] = frozenset({LoopState.BLOCKED_ON_YOU})
_WAITING_ONLY: frozenset[LoopState] = frozenset({LoopState.WAITING_ON_OTHER})

# Plan-step ``person`` tokens that refer to the asker rather than a teammate.
# The caller's endpoint is pinned by ``_personalize``, so these add nothing and
# must never leak into an endpoint filter as literal strings.
_SELF_PERSON_TOKENS: frozenset[str] = frozenset(
    {"me", "myself", "i", "self", "user", "the user", "you", "the tracked user"}
)

# Shape of a literal Slack member id (real ids like U0BFZDE289W and the U_NAME
# style used across the test workspace). A plan-step person that neither
# resolves via the person port nor matches this shape is discarded.
_SLACK_ID_RE = re.compile(r"^[UW][A-Z0-9_]{3,}$")

# Words in the *user's message* that genuinely ask for an oldest/newest pick.
# A plan-step selector is honored only when one of these is present.
_SELECTOR_CUES: tuple[str, ...] = (
    "oldest",
    "newest",
    "latest",
    "most recent",
    "first",
    "stalest",
    "last",
)


def _detect_loop_states(text: str) -> frozenset[LoopState]:
    """Infer which active loop state(s) a phrase refers to (default: both).

    "blocked on me" / "blocking me" → ``blocked-on-you`` (the user owes); "waiting
    on" / "waiting for" → ``waiting-on-other`` (someone owes the user). When neither
    cue is present both active states are in scope.
    """
    if "blocked on me" in text or "blocking me" in text or "blocked on you" in text:
        return frozenset({LoopState.BLOCKED_ON_YOU})
    if "waiting on" in text or "waiting for" in text or "waiting from" in text:
        return frozenset({LoopState.WAITING_ON_OTHER})
    return _ACTIVE_STATES


def _detect_min_age_seconds(text: str) -> Optional[float]:
    """Parse a "haven't touched in N days/hours" age floor into seconds.

    Drives queries like "who's blocked on me that I haven't touched in 3 days?"
    (design flow (e)). Returns ``None`` when no age phrase is present.
    """
    match = re.search(r"(\d+)\s*(day|days|hour|hours|week|weeks)\b", text)
    if not match:
        return None
    quantity = int(match.group(1))
    unit = _DURATION_UNITS.get(match.group(2))
    if unit is None:
        return None
    return (unit * quantity).total_seconds()


def _detect_duration(text: str) -> Optional[timedelta]:
    """Parse a snooze "for N hours/days/weeks" duration; ``None`` defers to default."""
    match = re.search(r"for\s+(\d+)\s*(hour|hours|hr|hrs|day|days|week|weeks)\b", text)
    if not match:
        return None
    return _DURATION_UNITS[match.group(2)] * int(match.group(1))


def _detect_selector(text: str) -> Optional[Selector]:
    """Parse an "oldest"/"newest" (a.k.a. first/latest) superlative selector."""
    if "oldest" in text or "first" in text or "stalest" in text:
        return Selector.OLDEST
    if "newest" in text or "latest" in text or "most recent" in text:
        return Selector.NEWEST
    return None


def _detect_person(text: str) -> Optional[str]:
    """Extract a "from/on/to <token>" person token, if present.

    Returns the raw token **with its original case preserved** (a Slack id like
    ``U_DANA`` or a bare name) — the keyword match is case-insensitive but the token
    is captured verbatim so Slack ids survive. Mapping a name to a Slack id is the
    caller's job via an injected resolver; by default the token is used as-is so
    Slack ids work out of the box.
    """
    match = re.search(r"\b(?:from|to)\s+([A-Za-z0-9_]+)", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def default_parser(text: str) -> ParsedRequest:
    """A deterministic, LLM-free parser for the demo phrases (Req 12.1, 12.3, 12.9).

    Detects a command verb first (nudge/delegate/snooze/close); if none is present
    and the text reads like a question about loops, parses a query; otherwise returns
    ``None`` so the agent replies with the supported actions (Req 12.9).

    This is intentionally simple and explicit: routing, disambiguation, the trace,
    and the confirmation gate are the logic under test, and they are exercised with
    injected intents independent of this parser's NL coverage.
    """
    lowered = text.strip().lower()
    if not lowered:
        return None

    # --- command detection ---------------------------------------------------
    action: Optional[CommandAction] = None
    for phrase, mapped in _COMMAND_VERBS:
        if phrase in lowered:
            action = mapped
            break

    if action is not None:
        selector = _detect_selector(lowered)
        person = _detect_person(text)
        loop_states = _detect_loop_states(lowered)
        # A command targets active loops only, surfaced (quiet by default — Req 14.1).
        filt = ObligationFilter(
            loop_states=loop_states,
            surfaced_only=True,
        )
        teammate = person if action is CommandAction.DELEGATE else None
        duration = _detect_duration(lowered) if action is CommandAction.SNOOZE else None
        return ParsedCommand(
            action=action,
            filter=filt,
            selector=selector,
            teammate_id=teammate,
            duration=duration,
        )

    # --- query detection -----------------------------------------------------
    query_cues = (
        "who",
        "what",
        "which",
        "show",
        "list",
        "blocked",
        "waiting",
        "loops",
    )
    if any(cue in lowered for cue in query_cues):
        loop_states = _detect_loop_states(lowered)
        person = _detect_person(text)
        min_age = _detect_min_age_seconds(lowered)
        # "from X": for waiting-on-other the other party owes (owes_person_id = X);
        # for blocked-on-you the other party is owed (owed_person_id = X).
        owes = person if loop_states == frozenset({LoopState.WAITING_ON_OTHER}) else None
        owed = person if loop_states == frozenset({LoopState.BLOCKED_ON_YOU}) else None
        return ParsedQuery(
            filter=ObligationFilter(
                loop_states=loop_states,
                surfaced_only=True,
                owes_person_id=owes,
                owed_person_id=owed,
                min_age_seconds=min_age,
            )
        )

    # --- unmappable ----------------------------------------------------------
    return None


# ---------------------------------------------------------------------------
# The Conversational Agent
# ---------------------------------------------------------------------------
def _summarize(obligation: Obligation) -> str:
    """A one-line obligation summary for list/candidate replies."""
    return f"{obligation.obligation_id}: {obligation.subject_summary}"


def _sort_oldest_first(obligations: list[Obligation]) -> list[Obligation]:
    """Sort obligations by ``last_touch_timestamp`` ascending (oldest first).

    Matches the App Home ordering (Req 6.4) so the front door presents loops the same
    way, and makes "oldest"/"newest" selection deterministic.
    """
    return sorted(obligations, key=lambda o: _parse_iso_utc(o.last_touch_timestamp))


class ConversationalAgent:
    """Agent 5 — the natural-language front door over the Assistant pane (Req 12).

    The agent owns no state about the graph itself; it reads through the shared
    :class:`~loop.graph.store.ObligationGraph` and routes commands through the shared
    :class:`~loop.action.action_agent.ActionAgent`, so it can never diverge from the
    single source of truth. Its only state is the per-user pending confirmation for a
    send-as-user command (Req 12.6, 12.7).

    Args:
        graph: the shared Obligation Graph store (read for queries / target
            resolution).
        action: the Action Agent every command is routed to (Req 12.3).
        parser: an injectable NL parser port mapping text → :class:`ParsedRequest`.
            Defaults to the deterministic :func:`default_parser`; tests inject a stub
            to drive routing/disambiguation/trace logic independently of NL coverage.
        now: injectable ISO 8601 UTC clock; defaults to
            :func:`loop.graph.models.utc_now_iso`. Injected so the 60s confirmation
            timeout is deterministic in tests (Req 12.7).
        resolve_person: optional name→Slack-id resolver applied to a parsed person
            token; defaults to identity (the token is used as-is, so Slack ids work).
        planner: optional dynamic-orchestration port (see
            :mod:`loop.conversational.planner`). When ``None`` (the default) the agent
            uses the deterministic parser path, preserving the original behaviour and
            every existing test. When wired (e.g. with
            :func:`~loop.conversational.planner.build_llm_planner`) the unified
            :meth:`handle` entry point plans which tools to invoke at runtime and
            executes that plan step-by-step, surfacing the plan in the trace; any
            planning failure falls back to the parser path (graceful degradation).
    """

    def __init__(
        self,
        graph: ObligationGraph,
        action: ActionAgent,
        *,
        parser: NLParser = default_parser,
        now: Callable[[], str] = utc_now_iso,
        resolve_person: Optional[Callable[[str], Optional[PersonId]]] = None,
        planner: Optional[Planner] = None,
        user_id: Optional[UserId] = None,
    ) -> None:
        self._graph = graph
        self._action = action
        self._parser = parser
        self._now = now
        self._resolve_person = resolve_person or (lambda token: token)
        self._planner = planner
        # The tracked user, when known. With third-party loops in the graph
        # (edges between two *other* people, feeding the workspace map and
        # deadlock intelligence), every personal query/command must scope to
        # loops the user is a party to — "what am I waiting on?" must never
        # list someone else's loop. ``None`` (the default) keeps the original
        # unscoped behaviour for tests and single-user graphs.
        self._user_id = user_id
        # Per-user pending send-as-user confirmation (Req 12.6). At most one per user.
        self._pending: dict[UserId, _PendingConfirmation] = {}

    def _scoped(
        self, rows: list[Obligation], user: Optional[UserId] = None
    ) -> list[Obligation]:
        """Restrict query results to loops ``user`` is a party to.

        Scoping is armed by constructing the agent with a ``user_id`` (personal
        mode); the *target* is whoever is asking — every workspace member gets
        their own answers — falling back to the configured user. No-op when no
        ``user_id`` was configured. Applied post-query because a single
        :class:`ObligationFilter` cannot express the per-state endpoint OR
        ("owes me" for waiting, "I owe" for blocked) across a mixed-state
        query.
        """
        if self._user_id is None:
            return rows
        target = user or self._user_id
        return [
            o
            for o in rows
            if target in (o.owes_person_id, o.owed_person_id)
        ]

    def _personalize(
        self, filt: ObligationFilter, user: Optional[UserId] = None
    ) -> ObligationFilter:
        """Re-anchor a direction filter to the asking member's edge endpoints.

        Loop_State labels are stored relative to the tracked user, so for any
        other caller "blocked on me" (I owe) and "waiting on others" (someone
        owes me) cannot be answered from the label — exactly like the App Home
        selectors, the state filter widens to both open states and the caller is
        pinned to the owing / owed endpoint. Armed only in personal mode; a
        mixed-state filter carries no direction claim and passes through.
        """
        if self._user_id is None:
            return filt
        target = user or self._user_id
        if filt.loop_states == _BLOCKED_ONLY:
            return replace(
                filt,
                loop_states=_ACTIVE_STATES,
                owes_person_id=filt.owes_person_id or target,
            )
        if filt.loop_states == _WAITING_ONLY:
            return replace(
                filt,
                loop_states=_ACTIVE_STATES,
                owed_person_id=filt.owed_person_id or target,
            )
        return filt

    # ------------------------------------------------------------------
    # Unified entry point — dispatch query vs command vs unmappable
    # ------------------------------------------------------------------
    def handle(self, user: UserId, text: str) -> AssistantReply:
        """Parse ``text`` and dispatch to query/command handling (Req 12.1, 12.3, 12.9).

        A convenience front door for the Slack layer (task 17): commands route to
        :meth:`handle_command`, questions to :meth:`handle_query`, and anything the
        parser cannot map gets the supported-actions reply (Req 12.9).

        When a :class:`~loop.conversational.planner.Planner` is wired (additive,
        opt-in), this entry point instead asks the planner which tools to invoke and
        executes that plan dynamically (:meth:`_handle_with_plan`); on any planning
        failure it transparently falls back to the parser path below.
        """
        if self._planner is not None:
            return self._handle_with_plan(user, text)
        parsed = self._parser(text)
        return self._dispatch_parsed(user, parsed)

    def _dispatch_parsed(
        self,
        user: UserId,
        parsed: ParsedRequest,
        trace: Optional[ToolUseTrace] = None,
    ) -> AssistantReply:
        """Dispatch a parsed request to query/command/unsupported handling.

        An optional ``trace`` lets the dynamic-orchestration fallback path thread an
        already-started trace (carrying the planner/fallback entries) through the
        deterministic parser path so the visible record stays continuous (Req 12.5).
        """
        if isinstance(parsed, ParsedCommand):
            return self._route_command(user, parsed, trace)
        if isinstance(parsed, ParsedQuery):
            return self._run_query(parsed, trace, user=user)
        return self._unsupported_reply(trace)

    # ------------------------------------------------------------------
    # Dynamic orchestration — plan, then execute the plan (additive, opt-in)
    # ------------------------------------------------------------------
    def _handle_with_plan(self, user: UserId, text: str) -> AssistantReply:
        """Plan which tools to invoke at runtime, then execute the plan (Req 12.5).

        Asks the injected :class:`~loop.conversational.planner.Planner` for an ordered
        :class:`~loop.conversational.planner.Plan`, records the plan in the trace, and
        executes it step-by-step against the real collaborators. Any planning failure
        (LLM error, empty/garbage output) degrades gracefully to the deterministic
        parser path, with the fallback recorded in the trace.
        """
        trace = ToolUseTrace()
        try:
            plan = self._planner(text, AVAILABLE_TOOLS)  # type: ignore[misc]
        except Exception as exc:  # PlannerError or any provider/SDK error
            return self._fallback_to_parser(user, text, trace, reason=str(exc))

        if (
            not isinstance(plan, Plan)
            or not plan.steps
            or not any(step.tool in KNOWN_TOOLS for step in plan.steps)
        ):
            return self._fallback_to_parser(
                user, text, trace, reason="planner returned an unusable plan"
            )

        summary = f": {plan.summary}" if plan.summary else ""
        trace.add(TRACE_PLANNER, f"plan {len(plan.steps)} step(s){summary}")
        logger.info(
            "assistant plan for %s: %s",
            user,
            [(s.tool, s.args) for s in plan.steps],
        )
        return self._execute_plan(user, text, plan, trace)

    def _fallback_to_parser(
        self, user: UserId, text: str, trace: ToolUseTrace, *, reason: str
    ) -> AssistantReply:
        """Record a planner fallback and answer via the deterministic parser path.

        Keeps the front door responsive when the planner cannot help: the visible
        trace shows the fallback, then the parser-path tool invocations (Req 12.5).
        """
        short = reason if len(reason) <= 80 else reason[:77] + "..."
        trace.add(TRACE_PLANNER, f"fallback to parser ({short})")
        parsed = self._parser(text)
        return self._dispatch_parsed(user, parsed, trace)

    def _execute_plan(
        self, user: UserId, text: str, plan: Plan, trace: ToolUseTrace
    ) -> AssistantReply:
        """Execute an ordered plan against the real collaborators (Req 12.3-12.8).

        Each step is first recorded as a planned entry (``Planner (step N: ...)``)
        and then executed, with the execution's own tool/agent invocations appended
        in order — so the trace shows the agent's *plan* interleaved with its
        *execution*. Read steps (``query_graph``/``verify_pr``) resolve and verify the
        target; the first terminal action step (``snooze``/``close``/``draft_nudge``/
        ``delegate``) produces the reply, preserving disambiguation (Req 12.4) and the
        send-as-user confirmation gate (Req 12.6).
        """
        parsed = self._parser(text)
        base_filter = self._base_filter(parsed)
        candidates: list[Obligation] = []
        queried = False
        verifier_recorded = False

        for index, step in enumerate(plan.steps, start=1):
            tool = step.tool
            detail = f"step {index}: {tool}"
            if step.rationale:
                detail = f"{detail} — {step.rationale}"
            trace.add(TRACE_PLANNER, detail)

            if tool == TOOL_QUERY_GRAPH:
                filt = self._personalize(self._filter_from_step(step, base_filter), user)
                trace.add(TRACE_GRAPH, "query")
                candidates = _sort_oldest_first(
                    self._scoped(self._graph.query(self._apply_now(filt)), user)
                )
                candidates = self._apply_selector(
                    candidates, self._grounded_selector(step, text)
                )
                queried = True

            elif tool == TOOL_VERIFY_PR:
                target = candidates[0] if len(candidates) == 1 else None
                if target is not None and target.artifact_ref and target.artifact_type is not None:
                    trace.add(TRACE_VERIFIER, "verify_pr")
                    verifier_recorded = True
                # Otherwise there is no single PR-linked target yet: the step is
                # recorded as planned, but there is nothing concrete to verify.

            elif tool in (TOOL_SNOOZE, TOOL_CLOSE, TOOL_DRAFT_NUDGE, TOOL_DELEGATE):
                # Resolve a single target before acting (implicit query if needed).
                if not queried:
                    trace.add(TRACE_GRAPH, "query")
                    candidates = _sort_oldest_first(
                        self._scoped(
                            self._graph.query(
                                self._apply_now(self._personalize(base_filter, user))
                            ),
                            user,
                        )
                    )
                    queried = True
                # Let a per-step selector collapse an ambiguous set.
                selector = self._grounded_selector(step, text)
                if selector is not None:
                    candidates = self._apply_selector(candidates, selector)

                if not candidates:
                    return AssistantReply(
                        kind=ReplyKind.NO_MATCH,
                        text=f"No matching loop found to {tool.replace('_', ' ')}.",
                        trace=trace,
                    )
                if len(candidates) > 1:
                    lines = "\n".join(f"• {_summarize(o)}" for o in candidates)
                    return AssistantReply(
                        kind=ReplyKind.DISAMBIGUATION,
                        text=(
                            f"That maps to {len(candidates)} loops. Which one should "
                            f"I act on?\n{lines}"
                        ),
                        trace=trace,
                        candidates=tuple(candidates),
                    )
                return self._execute_action_step(
                    user, tool, step, candidates[0], parsed, trace, verifier_recorded
                )

            # Unknown tool names are tolerated: recorded above, then skipped.

        # No terminal action ran — the plan was a pure read. Answer from candidates.
        if not queried:
            trace.add(TRACE_GRAPH, "query")
            candidates = _sort_oldest_first(
                self._scoped(
                    self._graph.query(
                        self._apply_now(self._personalize(base_filter, user))
                    ),
                    user,
                )
            )
        return self._query_reply_from(candidates, trace)

    def _execute_action_step(
        self,
        user: UserId,
        tool: str,
        step: PlanStep,
        target: Obligation,
        parsed: ParsedRequest,
        trace: ToolUseTrace,
        verifier_recorded: bool,
    ) -> AssistantReply:
        """Run a single terminal action step, reusing the existing routing helpers."""
        fallback_cmd = parsed if isinstance(parsed, ParsedCommand) else None

        if tool == TOOL_SNOOZE:
            duration = self._duration_from_step(step)
            if duration is None and fallback_cmd is not None:
                duration = fallback_cmd.duration
            return self._route_snooze(target, duration, trace)

        if tool == TOOL_CLOSE:
            # Verified close: only add the Verifier entry if a verify_pr step didn't
            # already record it, and only when the target is actually PR-linked.
            if not verifier_recorded and target.artifact_ref and target.artifact_type is not None:
                trace.add(TRACE_VERIFIER, "verify_pr")
            trace.add(TRACE_ACTION_AGENT, "auto_close")
            result = self._action.auto_close(target)
            if not result.closed:
                return AssistantReply(
                    kind=ReplyKind.ACTION_FAILED,
                    text=(
                        "Couldn't close this loop — the linked work isn't verified as "
                        f"done ({result.verification.value}). The loop is unchanged."
                    ),
                    trace=trace,
                    obligation=result.obligation,
                    error=f"verification={result.verification.value}",
                )
            return AssistantReply(
                kind=ReplyKind.ACTION_DONE,
                text="Closed the loop — the linked PR is merged.",
                trace=trace,
                obligation=result.obligation,
            )

        if tool == TOOL_DRAFT_NUDGE:
            nudge_cmd = ParsedCommand(
                action=CommandAction.NUDGE,
                filter=ObligationFilter(loop_states=_ACTIVE_STATES, surfaced_only=True),
            )
            return self._prepare_send_as_user(user, nudge_cmd, target, trace)

        # TOOL_DELEGATE
        teammate = self._teammate_from_step(step)
        if teammate is None and fallback_cmd is not None:
            teammate = fallback_cmd.teammate_id
        delegate_cmd = ParsedCommand(
            action=CommandAction.DELEGATE,
            filter=ObligationFilter(loop_states=_ACTIVE_STATES, surfaced_only=True),
            teammate_id=teammate,
        )
        return self._prepare_send_as_user(user, delegate_cmd, target, trace)

    # --- plan-step argument helpers ----------------------------------------
    def _base_filter(self, parsed: ParsedRequest) -> ObligationFilter:
        """The default target/query filter for a plan, taken from the parser's read.

        Reusing the parser's understanding of the message gives the planner a
        reliable default scope (active, surfaced loops) without depending on the LLM
        emitting precise filter arguments.
        """
        if isinstance(parsed, (ParsedCommand, ParsedQuery)):
            return parsed.filter
        return ObligationFilter(loop_states=_ACTIVE_STATES, surfaced_only=True)

    def _filter_from_step(self, step: PlanStep, base: ObligationFilter) -> ObligationFilter:
        """Build a query filter from a step's args, deferring to ``base`` when absent."""
        args = step.args or {}
        loop_state = args.get("loop_state") or args.get("state")
        person = args.get("person") or args.get("from") or args.get("to")
        # A self-referential person ("me", "the user") is the asker, whose endpoint
        # _personalize pins — as a literal string it would match no edge at all.
        if isinstance(person, str) and person.strip().lower() in _SELF_PERSON_TOKENS:
            person = None
        min_age_days = args.get("min_age_days") or args.get("min_age")

        states = base.loop_states
        if isinstance(loop_state, str):
            lowered = loop_state.lower()
            if "block" in lowered:
                states = frozenset({LoopState.BLOCKED_ON_YOU})
            elif "wait" in lowered:
                states = frozenset({LoopState.WAITING_ON_OTHER})

        min_age_seconds = base.min_age_seconds
        if isinstance(min_age_days, (int, float)):
            min_age_seconds = float(min_age_days) * 86400.0

        owes = base.owes_person_id
        owed = base.owed_person_id
        if isinstance(person, str) and person.strip():
            raw = person.strip()
            resolved = self._resolve_person(raw)
            token: Optional[str] = None
            if resolved and resolved != raw:
                token = resolved
            elif _SLACK_ID_RE.match(raw):
                token = raw
            # An unresolvable name would make the endpoint filter match nothing
            # at all — dropping it degrades to "all matching loops", never to a
            # silently empty answer.
            if token is not None:
                if states == _WAITING_ONLY:
                    owes = token
                elif states == _BLOCKED_ONLY:
                    owed = token

        return ObligationFilter(
            loop_states=states,
            surfaced_only=base.surfaced_only,
            include_dismissed=base.include_dismissed,
            owner_person_id=base.owner_person_id,
            owes_person_id=owes,
            owed_person_id=owed,
            artifact_type=base.artifact_type,
            artifact_ref=base.artifact_ref,
            closure_kind=base.closure_kind,
            min_age_seconds=min_age_seconds,
            max_age_seconds=base.max_age_seconds,
        )

    @classmethod
    def _grounded_selector(cls, step: PlanStep, text: str) -> Optional[Selector]:
        """A step's selector, honored only when the user's own words ask for one.

        Planners sometimes over-specify — emitting ``selector="newest"`` for a
        plain listing question — which silently collapses the answer to a single
        loop, or worse, aims an action at the wrong one. The user's message is
        the ground truth: no oldest/newest cue in it, no selector.
        """
        selector = cls._selector_from_step(step)
        if selector is None:
            return None
        lowered = text.lower()
        if any(cue in lowered for cue in _SELECTOR_CUES):
            return selector
        return None

    @staticmethod
    def _selector_from_step(step: PlanStep) -> Optional[Selector]:
        """Read an oldest/newest selector from a step's args, if present."""
        raw = (step.args or {}).get("selector")
        if not isinstance(raw, str):
            return None
        lowered = raw.lower()
        if lowered in ("oldest", "first", "stalest"):
            return Selector.OLDEST
        if lowered in ("newest", "latest", "most_recent", "most recent"):
            return Selector.NEWEST
        return None

    @staticmethod
    def _apply_selector(
        candidates: list[Obligation], selector: Optional[Selector]
    ) -> list[Obligation]:
        """Collapse a candidate list to a single target per an oldest/newest selector."""
        if not candidates or selector is None:
            return candidates
        return [candidates[0]] if selector is Selector.OLDEST else [candidates[-1]]

    @staticmethod
    def _duration_from_step(step: PlanStep) -> Optional[timedelta]:
        """Parse a snooze duration from a step's args (hours/days), else ``None``."""
        args = step.args or {}
        hours = args.get("duration_hours") or args.get("hours")
        days = args.get("duration_days") or args.get("days")
        if isinstance(hours, (int, float)) and hours > 0:
            return timedelta(hours=float(hours))
        if isinstance(days, (int, float)) and days > 0:
            return timedelta(days=float(days))
        return None

    def _teammate_from_step(self, step: PlanStep) -> Optional[PersonId]:
        """Read a delegation recipient from a step's args, applying the resolver."""
        args = step.args or {}
        raw = args.get("teammate") or args.get("to") or args.get("recipient")
        if isinstance(raw, str) and raw.strip():
            return self._resolve_person(raw.strip()) or raw.strip()
        return None

    def _query_reply_from(
        self, matches: list[Obligation], trace: ToolUseTrace
    ) -> AssistantReply:
        """Build a query-result / no-match reply from an already-resolved set."""
        if not matches:
            return AssistantReply(
                kind=ReplyKind.NO_MATCH,
                text="No matching loops found.",
                trace=trace,
            )
        lines = "\n".join(f"• {_summarize(o)}" for o in matches)
        noun = "loop" if len(matches) == 1 else "loops"
        return AssistantReply(
            kind=ReplyKind.QUERY_RESULT,
            text=f"Found {len(matches)} matching {noun}:\n{lines}",
            trace=trace,
            obligations=tuple(matches),
        )

    # ------------------------------------------------------------------
    # task 15.1 — NL query handling (Req 12.1, 12.2)
    # ------------------------------------------------------------------
    def handle_query(self, user: UserId, text: str) -> AssistantReply:
        """Answer a natural-language query about open loops (Req 12.1, 12.2).

        Parses ``text`` into filter criteria, reads the Obligation Graph, and replies
        with the matching surfaced Obligations (sorted oldest→newest) within the 5s
        budget — the read is an in-memory query (Req 12.1). A zero-match query gets an
        explicit no-match reply (Req 12.2). The reply renders the Tool_Use_Trace
        (Req 12.5).
        """
        parsed = self._parser(text)
        if isinstance(parsed, ParsedQuery):
            return self._run_query(parsed, user=user)
        if isinstance(parsed, ParsedCommand):
            # Text understood as a command answered through the query door: still
            # answer with the obligations its target criteria match.
            return self._run_query(ParsedQuery(filter=parsed.filter), user=user)
        return self._unsupported_reply()

    def _run_query(
        self,
        parsed: ParsedQuery,
        trace: Optional[ToolUseTrace] = None,
        user: Optional[UserId] = None,
    ) -> AssistantReply:
        """Execute a parsed query and build the reply + trace (Req 12.1, 12.2, 12.5)."""
        trace = trace if trace is not None else ToolUseTrace()

        filt = self._apply_now(self._personalize(parsed.filter, user))
        trace.add(TRACE_GRAPH, "query")
        matches = _sort_oldest_first(self._scoped(self._graph.query(filt), user))

        # Req 12.2: zero matches → explicit no-match message.
        if not matches:
            return AssistantReply(
                kind=ReplyKind.NO_MATCH,
                text="No matching loops found.",
                trace=trace,
            )

        # Req 12.1: respond with the matching obligations.
        lines = "\n".join(f"• {_summarize(o)}" for o in matches)
        noun = "loop" if len(matches) == 1 else "loops"
        return AssistantReply(
            kind=ReplyKind.QUERY_RESULT,
            text=f"Found {len(matches)} matching {noun}:\n{lines}",
            trace=trace,
            obligations=tuple(matches),
        )

    # ------------------------------------------------------------------
    # task 15.2 — NL command routing, disambiguation, trace (Req 12.3-12.5, 12.8, 12.9)
    # ------------------------------------------------------------------
    def handle_command(self, user: UserId, text: str) -> AssistantReply:
        """Route a natural-language command to the Action Agent (Req 12.3-12.9).

        Parses ``text`` into a command; an unmappable request gets the supported-
        actions reply (Req 12.9). A mappable command is resolved to its target and
        routed via :meth:`_route_command`.
        """
        parsed = self._parser(text)
        if not isinstance(parsed, ParsedCommand):
            return self._unsupported_reply()
        return self._route_command(user, parsed)

    def _route_command(
        self, user: UserId, parsed: ParsedCommand, trace: Optional[ToolUseTrace] = None
    ) -> AssistantReply:
        """Resolve the target, disambiguate, and route (or gate) the command."""
        trace = trace if trace is not None else ToolUseTrace()

        # Resolve the candidate target Obligation(s) from the graph.
        trace.add(TRACE_GRAPH, "query")
        candidates = self._resolve_candidates(parsed, user)

        # Zero candidates: nothing to act on (Req 12.2-style no-match for a command).
        if not candidates:
            return AssistantReply(
                kind=ReplyKind.NO_MATCH,
                text=f"No matching loop found to {parsed.action.value}.",
                trace=trace,
            )

        # Req 12.4: more than one candidate → ask the user to pick one; do NOT route.
        if len(candidates) > 1:
            lines = "\n".join(f"• {_summarize(o)}" for o in candidates)
            return AssistantReply(
                kind=ReplyKind.DISAMBIGUATION,
                text=(
                    f"That maps to {len(candidates)} loops. Which one should I "
                    f"{parsed.action.value}?\n{lines}"
                ),
                trace=trace,
                candidates=tuple(candidates),
            )

        target = candidates[0]

        # Send-as-user commands are gated on a one-tap confirm (Req 12.6) — handled
        # by preparing (not sending) and stashing a pending confirmation.
        if parsed.action in SEND_AS_USER_ACTIONS:
            return self._prepare_send_as_user(user, parsed, target, trace)

        # Autonomous commands (snooze, close) route straight through (Req 13.1).
        if parsed.action is CommandAction.SNOOZE:
            return self._route_snooze(target, parsed.duration, trace)
        return self._route_close(target, trace)

    def _resolve_candidates(
        self, parsed: ParsedCommand, user: Optional[UserId] = None
    ) -> list[Obligation]:
        """Resolve a command's target candidate set, applying any selector.

        Applies the parsed filter, sorts oldest→newest, then — when a selector is
        present — collapses to the single oldest/newest Obligation so an explicit
        "the oldest one" never disambiguates (Req 12.4).
        """
        filt = self._apply_now(self._personalize(parsed.filter, user))
        matches = _sort_oldest_first(self._scoped(self._graph.query(filt), user))
        if not matches:
            return []
        if parsed.selector is Selector.OLDEST:
            return [matches[0]]
        if parsed.selector is Selector.NEWEST:
            return [matches[-1]]
        return matches

    # ------------------------------------------------------------------
    # task 15.3 — send-as-user confirmation gate (Req 12.6, 12.7)
    # ------------------------------------------------------------------
    def _prepare_send_as_user(
        self,
        user: UserId,
        parsed: ParsedCommand,
        target: Obligation,
        trace: ToolUseTrace,
    ) -> AssistantReply:
        """Prepare a send-as-user command and await a one-tap confirm (Req 12.6).

        Crucially, **nothing is sent here**: for a nudge the agent only *drafts*
        (a read-only Action Agent step that does not post), and for a delegate it
        only builds the handoff prompt. A pending confirmation is stashed and a
        confirmation-required reply is returned; the Action Agent is not instructed
        to send until :meth:`confirm` is tapped within 60s (Req 12.6, 12.7).
        """
        if parsed.action is CommandAction.NUDGE:
            # Draft (does NOT send) — Req 7.2 / 12.6: never post before confirm.
            trace.add(TRACE_ACTION_AGENT, "draft_polite_nudge")
            draft_result = self._action.draft_polite_nudge(target)
            if not draft_result.drafted or draft_result.draft is None:
                # The Action Agent reported a drafting failure (Req 12.8): report it,
                # leave the obligation in its pre-command state (no write occurred).
                return AssistantReply(
                    kind=ReplyKind.ACTION_FAILED,
                    text=draft_result.message or "Drafting the nudge failed; nothing was sent.",
                    trace=trace,
                    obligation=target,
                    error=draft_result.error,
                )
            self._pending[user] = _PendingConfirmation(
                action=CommandAction.NUDGE,
                obligation=target,
                created_at=self._now(),
                nudge_draft=draft_result.draft,
            )
            return AssistantReply(
                kind=ReplyKind.CONFIRM_REQUIRED,
                text=(
                    "Here's the nudge I'll send as you — tap Confirm within 60s to "
                    f"send it:\n{draft_result.draft.text}"
                ),
                trace=trace,
                requires_confirmation=True,
                obligation=target,
            )

        # DELEGATE: build the handoff prompt (no send) and await confirm.
        teammate = parsed.teammate_id
        if not teammate:
            # Under-specified delegation: ask who to hand it to before routing.
            return AssistantReply(
                kind=ReplyKind.CLARIFICATION,
                text="Who should I delegate this loop to?",
                trace=trace,
                obligation=target,
            )
        trace.add(TRACE_ACTION_AGENT, "delegation_prompt")
        prompt = self._action.delegation_prompt(target, teammate)
        self._pending[user] = _PendingConfirmation(
            action=CommandAction.DELEGATE,
            obligation=target,
            created_at=self._now(),
            teammate_id=teammate,
        )
        return AssistantReply(
            kind=ReplyKind.CONFIRM_REQUIRED,
            text=f"{prompt}\n(Tap Confirm within 60s.)",
            trace=trace,
            requires_confirmation=True,
            obligation=target,
        )

    def confirm(self, user: UserId) -> AssistantReply:
        """Complete a pending send-as-user command after a one-tap confirm (Req 12.6).

        If the confirm arrives within the 60-second window the command is routed to
        the Action Agent to actually send (Req 12.3); a confirm that arrives after
        60s is treated as a timeout and cancelled with no change (Req 12.7). If there
        is no pending confirmation the call is a no-op reply.
        """
        pending = self._pending.get(user)
        trace = ToolUseTrace()
        if pending is None:
            return AssistantReply(
                kind=ReplyKind.CANCELLED,
                text="There's nothing waiting to confirm.",
                trace=trace,
            )

        # Req 12.7: a confirm later than the 60s window is too late → cancel, no change.
        if self._is_expired(pending, self._now()):
            self._pending.pop(user, None)
            return self._timeout_reply(pending, trace)

        self._pending.pop(user, None)
        if pending.action is CommandAction.NUDGE:
            return self._send_nudge(pending, trace)
        return self._send_delegate(pending, trace)

    def decline(self, user: UserId) -> AssistantReply:
        """Cancel a pending send-as-user command on an explicit decline (Req 12.7).

        Leaves the affected Obligation unchanged and tells the user nothing was sent.
        """
        pending = self._pending.pop(user, None)
        trace = ToolUseTrace()
        if pending is None:
            return AssistantReply(
                kind=ReplyKind.CANCELLED,
                text="There's nothing waiting to confirm.",
                trace=trace,
            )
        return AssistantReply(
            kind=ReplyKind.CANCELLED,
            text="Cancelled — no message was sent and nothing changed.",
            trace=trace,
            obligation=pending.obligation,
        )

    def check_timeout(self, user: UserId) -> Optional[AssistantReply]:
        """Cancel a pending confirmation that has gone 60s without a response (Req 12.7).

        Intended to be polled by the Slack/scheduler layer (task 17). Returns a
        cancellation reply when a pending confirmation has expired (and clears it), or
        ``None`` when there is nothing pending or it is still within the window.
        """
        pending = self._pending.get(user)
        if pending is None:
            return None
        if not self._is_expired(pending, self._now()):
            return None
        self._pending.pop(user, None)
        return self._timeout_reply(pending, ToolUseTrace())

    def has_pending_confirmation(self, user: UserId) -> bool:
        """True iff a send-as-user command is awaiting this user's confirmation."""
        return user in self._pending

    # ------------------------------------------------------------------
    # Action routing helpers (Req 12.3, 12.8)
    # ------------------------------------------------------------------
    def _send_nudge(
        self, pending: _PendingConfirmation, trace: ToolUseTrace
    ) -> AssistantReply:
        """Instruct the Action Agent to send a confirmed nudge (Req 12.3, 12.8)."""
        draft = pending.nudge_draft
        assert draft is not None  # invariant: a nudge pending always carries a draft

        # The Action Agent invokes the Verifier (GitHub MCP) for a PR-referencing
        # nudge before sending; reflect that real invocation in the trace (Req 12.5).
        if draft.obligation.artifact_ref and draft.obligation.artifact_type is not None:
            trace.add(TRACE_VERIFIER, "verify_pr")
        trace.add(TRACE_ACTION_AGENT, "send_polite_nudge")

        result = self._action.send_polite_nudge(draft, confirmed=True)

        if result.cancelled:
            # The Verifier reported the loop already resolved (Req 7.5): nothing sent.
            return AssistantReply(
                kind=ReplyKind.ACTION_DONE,
                text=result.message or "This loop is already resolved — nudge cancelled.",
                trace=trace,
                obligation=result.obligation,
            )
        if not result.sent:
            # Req 12.8: the Action Agent reported a send failure — report it and leave
            # the obligation in its pre-command state (the Action Agent kept the draft
            # and did not touch the timestamp, Req 7.7).
            return AssistantReply(
                kind=ReplyKind.ACTION_FAILED,
                text="The nudge could not be sent. The loop is unchanged.",
                trace=trace,
                obligation=result.obligation,
                error=result.error,
            )
        return AssistantReply(
            kind=ReplyKind.ACTION_DONE,
            text="Sent the nudge as you.",
            trace=trace,
            obligation=result.obligation,
        )

    def _send_delegate(
        self, pending: _PendingConfirmation, trace: ToolUseTrace
    ) -> AssistantReply:
        """Instruct the Action Agent to send a confirmed delegation (Req 12.3, 12.8)."""
        teammate = pending.teammate_id
        assert teammate is not None  # invariant: a delegate pending carries a teammate

        trace.add(TRACE_ACTION_AGENT, "delegate")
        result = self._action.delegate(pending.obligation, teammate, confirmed=True)

        if not result.delegated:
            # Req 12.8: the Action Agent reported a failure — report it and leave the
            # obligation in its pre-command state (original owner/timestamp retained,
            # Req 10.5).
            return AssistantReply(
                kind=ReplyKind.ACTION_FAILED,
                text=f"The delegation to {teammate} could not be completed. The loop is unchanged.",
                trace=trace,
                obligation=result.obligation,
                error=result.error,
            )
        return AssistantReply(
            kind=ReplyKind.ACTION_DONE,
            text=f"Delegated the loop to {teammate} as you.",
            trace=trace,
            obligation=result.obligation,
        )

    def _route_snooze(
        self, target: Obligation, duration: Optional[timedelta], trace: ToolUseTrace
    ) -> AssistantReply:
        """Route a snooze command to the Action Agent (Req 12.3) — no send, no confirm."""
        trace.add(TRACE_ACTION_AGENT, "snooze")
        result = self._action.snooze(target, duration)
        if not result.snoozed:
            # Req 12.8: persist failure — report it; the store retained the prior value.
            return AssistantReply(
                kind=ReplyKind.ACTION_FAILED,
                text="Could not snooze the loop. It is unchanged.",
                trace=trace,
                obligation=target,
            )
        return AssistantReply(
            kind=ReplyKind.ACTION_DONE,
            text=f"Snoozed until {result.snoozed_until}.",
            trace=trace,
            obligation=result.obligation,
        )

    def _route_close(self, target: Obligation, trace: ToolUseTrace) -> AssistantReply:
        """Route a close command to the Action Agent's verified auto-close (Req 12.3).

        Closing is grounded in real GitHub state via the Verifier (the Action Agent's
        ``auto_close`` calls the Verifier first), so the trace records the Verifier
        (GitHub MCP) invocation before the Action Agent (Req 12.5). The loop is closed
        only on a verified merge; otherwise it is reported as not closed and left in
        its pre-command state (Req 8.2-8.4, 12.8).
        """
        trace.add(TRACE_VERIFIER, "verify_pr")
        trace.add(TRACE_ACTION_AGENT, "auto_close")
        result = self._action.auto_close(target)
        if not result.closed:
            return AssistantReply(
                kind=ReplyKind.ACTION_FAILED,
                text=(
                    "Couldn't close this loop — the linked work isn't verified as done "
                    f"({result.verification.value}). The loop is unchanged."
                ),
                trace=trace,
                obligation=result.obligation,
                error=f"verification={result.verification.value}",
            )
        return AssistantReply(
            kind=ReplyKind.ACTION_DONE,
            text="Closed the loop — the linked PR is merged.",
            trace=trace,
            obligation=result.obligation,
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def _apply_now(self, filt: ObligationFilter) -> ObligationFilter:
        """Stamp the agent's clock onto a filter's ``now`` for snooze/age evaluation.

        Keeps surfacing/age comparisons anchored to the same instant the rest of the
        reply uses, and lets tests pin time deterministically.
        """
        if filt.now is not None:
            return filt
        return ObligationFilter(
            loop_states=filt.loop_states,
            surfaced_only=filt.surfaced_only,
            include_dismissed=filt.include_dismissed,
            owner_person_id=filt.owner_person_id,
            owes_person_id=filt.owes_person_id,
            owed_person_id=filt.owed_person_id,
            artifact_type=filt.artifact_type,
            artifact_ref=filt.artifact_ref,
            closure_kind=filt.closure_kind,
            min_age_seconds=filt.min_age_seconds,
            max_age_seconds=filt.max_age_seconds,
            now=self._now(),
        )

    def _is_expired(self, pending: _PendingConfirmation, now_iso: str) -> bool:
        """True iff ``pending`` is older than the 60s confirmation window (Req 12.7)."""
        elapsed = _parse_iso_utc(now_iso) - _parse_iso_utc(pending.created_at)
        return elapsed >= CONFIRM_TIMEOUT

    def _timeout_reply(
        self, pending: _PendingConfirmation, trace: ToolUseTrace
    ) -> AssistantReply:
        """Build the cancellation reply for a 60s-elapsed confirmation (Req 12.7)."""
        return AssistantReply(
            kind=ReplyKind.CANCELLED,
            text="No response within 60 seconds — cancelled. No message was sent.",
            trace=trace,
            obligation=pending.obligation,
        )

    def _unsupported_reply(self, trace: Optional[ToolUseTrace] = None) -> AssistantReply:
        """Build the supported-actions reply for an unmappable request (Req 12.9)."""
        actions = ", ".join(SUPPORTED_ACTIONS)
        return AssistantReply(
            kind=ReplyKind.UNSUPPORTED,
            text=(
                "I couldn't map that to a supported action. I can: "
                f"{actions}. You can also ask about your open loops."
            ),
            trace=trace if trace is not None else ToolUseTrace(),
            supported_actions=SUPPORTED_ACTIONS,
        )


__all__ = [
    "CommandAction",
    "SEND_AS_USER_ACTIONS",
    "SUPPORTED_ACTIONS",
    "Selector",
    "ParsedQuery",
    "ParsedCommand",
    "ParsedRequest",
    "NLParser",
    "ToolUseTraceEntry",
    "ToolUseTrace",
    "TRACE_GRAPH",
    "TRACE_VERIFIER",
    "TRACE_ACTION_AGENT",
    "TRACE_PLANNER",
    "ReplyKind",
    "AssistantReply",
    "ConversationalAgent",
    "default_parser",
    "CONFIRM_TIMEOUT",
]
