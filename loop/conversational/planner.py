"""Dynamic orchestration planner for the Conversational Agent (Agent 5).

This module adds a **ReAct-style planning port** the Conversational Agent uses to
decide, *at runtime*, which tools/agents to invoke for a natural-language request —
the "agentic orchestration" showcase (design.md → multi-agent Perceive→Reason→Act
→Verify→Learn loop). Instead of a fixed parse→route path, the agent asks a
:data:`Planner` for an ordered :class:`Plan` of :class:`PlanStep` s, then executes
that plan step-by-step against the **real** collaborators (Obligation Graph,
Verifier, Action Agent), surfacing both the *plan* and its *execution* in the
visible Tool_Use_Trace (Req 12.5).

Design discipline (mirrors the agent's other injectable ports):

  * :data:`Planner` is a small, injectable port — a callable mapping the user's
    message + the :data:`AVAILABLE_TOOLS` catalog to a typed :class:`Plan`. Tests
    inject a deterministic double; production wires :func:`build_llm_planner`.
  * :func:`build_llm_planner` is the default implementation. It calls
    :func:`loop.llm.chat` at the ``"smart"`` tier in ``json_mode`` with a prompt
    that lists the available tools, then parses the model's JSON robustly. The
    ``loop.llm`` import is **lazy inside the closure**, so importing this module
    never needs a network connection or an installed provider SDK.
  * On any planning failure (LLM error, empty/garbage output, unparseable JSON) the
    planner raises :class:`PlannerError`. The Conversational Agent catches it and
    falls back to its deterministic parser path (graceful degradation), recording
    the fallback in the trace — so a bad plan never blocks the user.

The plan is a *value object*: it carries no behaviour and never touches the graph
or the network itself. Executing it (and preserving the send-as-user confirmation
gate, Req 12.6) is the Conversational Agent's job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Tool catalog — the vocabulary the planner may compose into a plan.
# ---------------------------------------------------------------------------
# Tool names are kept as plain string constants so a plan parsed from model JSON
# can be matched without importing the agent (no circular dependency), and so the
# agent can map each step name to a real collaborator invocation.
TOOL_QUERY_GRAPH = "query_graph"
TOOL_VERIFY_PR = "verify_pr"
TOOL_DRAFT_NUDGE = "draft_nudge"
TOOL_DELEGATE = "delegate"
TOOL_SNOOZE = "snooze"
TOOL_CLOSE = "close"


@dataclass(frozen=True)
class ToolSpec:
    """One entry in the catalog of tools the planner may invoke.

    Attributes:
        name: the tool identifier a :class:`PlanStep` references (e.g.
            ``"query_graph"``).
        description: a one-line description shown to the planner LLM.
        args: the argument keys the tool understands (advisory; parsing is lenient).
    """

    name: str
    description: str
    args: tuple[str, ...] = ()


# The catalog handed to the planner. Read-only and shared; ordered for a stable,
# human-readable prompt. Each maps to a real collaborator the agent already owns.
AVAILABLE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        TOOL_QUERY_GRAPH,
        "Read the shared Obligation Graph for loops matching criteria "
        "(loop_state, person, selector oldest/newest, min_age_days).",
        ("loop_state", "person", "selector", "min_age_days"),
    ),
    ToolSpec(
        TOOL_VERIFY_PR,
        "Check real GitHub state for a PR-linked loop via the Verifier (read-only).",
        (),
    ),
    ToolSpec(
        TOOL_DRAFT_NUDGE,
        "Draft a polite nudge to send AS the user (requires one-tap confirm; "
        "nothing is sent until confirmed).",
        (),
    ),
    ToolSpec(
        TOOL_DELEGATE,
        "Hand a loop off to a teammate AS the user (requires one-tap confirm).",
        ("teammate",),
    ),
    ToolSpec(
        TOOL_SNOOZE,
        "Snooze a loop for a duration (autonomous; no send).",
        ("duration_hours", "duration_days"),
    ),
    ToolSpec(
        TOOL_CLOSE,
        "Close a loop, grounded in verified GitHub state (autonomous; no send).",
        (),
    ),
)

# The set of recognized tool names, for fast validation of a parsed plan.
KNOWN_TOOLS: frozenset[str] = frozenset(spec.name for spec in AVAILABLE_TOOLS)


# ---------------------------------------------------------------------------
# Plan value objects (the structured output of the planner port)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanStep:
    """One ordered step in a :class:`Plan`.

    Attributes:
        tool: the tool/agent to invoke (one of :data:`KNOWN_TOOLS` when valid).
        args: the tool's arguments (a free-form mapping; the agent reads leniently).
        rationale: the planner's short reason for this step — surfaced in the trace
            so judges can see *why* the agent chose each tool.
    """

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass(frozen=True)
class Plan:
    """An ordered, typed plan of tool invocations produced by a :data:`Planner`.

    Attributes:
        steps: the ordered steps to execute.
        summary: an optional one-line summary of the overall intent.
    """

    steps: tuple[PlanStep, ...] = ()
    summary: str = ""

    def __bool__(self) -> bool:
        """A plan is truthy iff it has at least one step."""
        return bool(self.steps)

    @property
    def tools(self) -> tuple[str, ...]:
        """The ordered tool names across all steps (handy for tracing/assertions)."""
        return tuple(step.tool for step in self.steps)


# A planner maps the user's message + the tool catalog to a structured plan. It
# raises :class:`PlannerError` when it cannot produce a usable plan (the agent then
# falls back to its deterministic parser path).
Planner = Callable[[str, Sequence[ToolSpec]], Plan]


class PlannerError(RuntimeError):
    """Raised when a planner cannot produce a usable plan (triggers fallback)."""


# ---------------------------------------------------------------------------
# Robust JSON → Plan parsing
# ---------------------------------------------------------------------------
def _coerce_args(raw: Any) -> dict[str, Any]:
    """Coerce a step's ``args`` field into a plain dict (tolerating None/garbage)."""
    if isinstance(raw, Mapping):
        return {str(k): v for k, v in raw.items()}
    return {}


def _extract_json_object(text: str) -> str:
    """Pull the first complete JSON object out of a model response.

    Tolerates markdown code fences and leading/trailing prose by slicing from the
    first ``{`` to the last ``}``. Raises :class:`PlannerError` when no object-like
    span is present.
    """
    stripped = text.strip()
    # Drop a leading ```json / ``` fence and any trailing fence.
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1] if "\n" in stripped else stripped
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
        stripped = stripped.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise PlannerError("planner response contained no JSON object")
    return stripped[start : end + 1]


def parse_plan(raw: str) -> Plan:
    """Parse a model's JSON response into a :class:`Plan` (robust, lenient).

    Expects an object shaped like::

        {"summary": "...", "steps": [{"tool": "query_graph", "args": {...},
                                      "rationale": "..."}]}

    Unknown keys are ignored and missing optional fields default. A response with no
    parseable steps raises :class:`PlannerError` so the agent can fall back.
    """
    if not raw or not raw.strip():
        raise PlannerError("planner returned an empty response")

    payload = _extract_json_object(raw)
    try:
        data = json.loads(payload)
    except (ValueError, TypeError) as exc:  # malformed JSON
        raise PlannerError(f"planner response was not valid JSON: {exc}") from exc

    if not isinstance(data, Mapping):
        raise PlannerError("planner response was not a JSON object")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
        raise PlannerError("planner response had no 'steps' array")

    steps: list[PlanStep] = []
    for entry in raw_steps:
        if not isinstance(entry, Mapping):
            continue
        tool = entry.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            continue
        steps.append(
            PlanStep(
                tool=tool.strip(),
                args=_coerce_args(entry.get("args")),
                rationale=str(entry.get("rationale", "")),
            )
        )

    if not steps:
        raise PlannerError("planner produced no usable steps")

    summary = data.get("summary")
    return Plan(steps=tuple(steps), summary=summary if isinstance(summary, str) else "")


# ---------------------------------------------------------------------------
# Default LLM planner (provider-agnostic; lazy import; graceful failure)
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You are the planning brain of Loop's conversational assistant. Loop tracks "
    "interpersonal commitments ('loops') in a shared Obligation Graph. Given a "
    "user's request, produce an ORDERED plan of tool calls that fulfils it. "
    "Always read the graph (query_graph) before acting on a loop. Use verify_pr "
    "before closing or nudging a PR-linked loop. Sending as the user (draft_nudge, "
    "delegate) needs explicit confirmation, so plan the draft/prepare step only. "
    "Respond with ONLY a JSON object: "
    '{"summary": str, "steps": [{"tool": str, "args": object, "rationale": str}]}. '
    "Use only the listed tools."
)


def _render_catalog(tools: Sequence[ToolSpec]) -> str:
    """Render the tool catalog as a compact bullet list for the planner prompt."""
    lines = []
    for spec in tools:
        args = f" args: {', '.join(spec.args)}" if spec.args else ""
        lines.append(f"- {spec.name}: {spec.description}{args}")
    return "\n".join(lines)


def build_llm_planner(
    *,
    settings: Any | None = None,
    chat_fn: Callable[..., str] | None = None,
    max_tokens: int = 512,
) -> Planner:
    """Build the default LLM-backed planner (smart tier, JSON mode).

    Args:
        settings: resolved :class:`loop.config.Settings`; defaults to the process
            settings at call time.
        chat_fn: an injectable chat function (defaults to :func:`loop.llm.chat`);
            handy for offline tests of the planner without a network call.
        max_tokens: response cap for the plan JSON.

    Returns:
        A :data:`Planner` callable. On any LLM/parse failure it raises
        :class:`PlannerError`, signalling the agent to fall back to its parser path.

    The ``loop.llm`` import is performed lazily inside the returned closure, so
    importing this module stays network- and SDK-free.
    """

    def planner(message: str, tools: Sequence[ToolSpec]) -> Plan:
        if chat_fn is not None:
            chat = chat_fn
        else:  # lazy import keeps module import network-free
            from loop.llm import chat as chat  # type: ignore[no-redef]

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Available tools:\n{_render_catalog(tools)}\n\n"
                    f"User request: {message!r}\n\n"
                    "Return the JSON plan now."
                ),
            },
        ]
        try:
            raw = chat(
                messages,
                tier="smart",
                settings=settings,
                json_mode=True,
                max_tokens=max_tokens,
            )
        except PlannerError:
            raise
        except Exception as exc:  # network/SDK/provider error → graceful degradation
            raise PlannerError(f"planner LLM call failed: {exc}") from exc

        return parse_plan(raw)

    return planner


__all__ = [
    "TOOL_QUERY_GRAPH",
    "TOOL_VERIFY_PR",
    "TOOL_DRAFT_NUDGE",
    "TOOL_DELEGATE",
    "TOOL_SNOOZE",
    "TOOL_CLOSE",
    "KNOWN_TOOLS",
    "ToolSpec",
    "AVAILABLE_TOOLS",
    "PlanStep",
    "Plan",
    "Planner",
    "PlannerError",
    "parse_plan",
    "build_llm_planner",
]
