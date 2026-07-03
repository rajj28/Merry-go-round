"""Conversational Agent (Front door) — Agent 5.

Natural-language control in the Assistant pane with a visible tool-use trace
(Req 12). See :mod:`loop.conversational.conversational_agent` for the agent that
handles NL queries (task 15.1), routes nudge/delegate/snooze/close commands with
disambiguation and a visible Tool_Use_Trace (task 15.2), and gates send-as-user
commands behind a one-tap confirmation with a 60s timeout (task 15.3).
"""

from loop.conversational.conversational_agent import (
    AssistantReply,
    CommandAction,
    ConversationalAgent,
    ParsedCommand,
    ParsedQuery,
    ReplyKind,
    Selector,
    ToolUseTrace,
    ToolUseTraceEntry,
    default_parser,
)

__all__ = [
    "AssistantReply",
    "CommandAction",
    "ConversationalAgent",
    "ParsedCommand",
    "ParsedQuery",
    "ReplyKind",
    "Selector",
    "ToolUseTrace",
    "ToolUseTraceEntry",
    "default_parser",
]
