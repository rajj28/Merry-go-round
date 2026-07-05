"""RTS → Watcher contract (Task 1.2 spike output, consumed by Task 4.1).

This module is the **frozen interface** between the Slack Real-Time Search (RTS)
API and the Watcher (Perceive). It defines the shape the Watcher consumes so that
Task 4.1 (`run_sweep` / `_classify`) can be built against a stable contract while
the live RTS round-trip is verified separately (see `loop/spikes/rts_spike.py`).

Design references:
- design.md → "Watcher (Perceive)" + "Integrations → Slack Real-Time Search (RTS)"
- requirements.md → Requirement 2.1 (periodic sweep retrieves open-loop candidates)

------------------------------------------------------------------------------
ASSUMED RTS RESPONSE SCHEMA
------------------------------------------------------------------------------
RTS is exposed through the Slack Web API method ``assistant.search.context``
(POST https://slack.com/api/assistant.search.context). This is the documented
Real-Time Search API surface — there is no separate "rts.search" endpoint.

A successful response has this shape (fields the Watcher relies on are marked *):

    {
      "ok": true,
      "results": {
        "messages": [
          {
            "author_name":    "Jennifer Hynes",
            "author_user_id": "U0123456",      # * Slack user id of the author
            "team_id":        "T0123456",
            "channel_id":     "C0123456",       # * channel the message lives in
            "channel_name":   "proj-gizmo",
            "message_ts":     "123456.7890",    # * message timestamp (dedup key)
            "content":        "Hey team ...",   # * raw message text
            "is_author_bot":  false,            #   used to drop bot chatter
            "permalink":      "https://.../p123456789",  # * link back to source
            "blocks":         [ ... ],
            "context_messages": { "before": [...], "after": [...] }
          }
        ],
        "files":    [ ... ],
        "channels": [ ... ]
      },
      "response_metadata": { "next_cursor": "..." }   # "" when no more pages
    }

The de-duplication key the Watcher uses (Req 2.9) is the pair
``(channel_id, message_ts)`` — captured here as ``CandidateMessage.dedup_key``.

NOTE / ASSUMPTION: the live response was not exercised against a real workspace
in this spike (no entitled token available — see SETUP_RTS.md). The schema above
is taken from the official ``assistant.search.context`` reference. If the live
shape differs, update ONLY this module and Task 4.1 inherits the fix for free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class CandidateMessage:
    """One open-loop candidate the Watcher will hand to fast-tier for classification.

    Fields map 1:1 onto a single entry of ``results.messages`` from RTS.
    """

    channel_id: str
    message_ts: str
    author_id: str
    text: str
    permalink: str
    # Convenience extras RTS returns; not required by the contract but cheap to
    # carry and useful for high-recall filtering (skip bots) and nicer UI later.
    author_name: str | None = None
    channel_name: str | None = None
    is_author_bot: bool = False

    @property
    def dedup_key(self) -> tuple[str, str]:
        """Watcher de-dup key against the Obligation Graph (Req 2.9)."""
        return (self.channel_id, self.message_ts)


def _coerce_str(value: Any) -> str:
    """RTS values should be strings; coerce defensively so a spike never crashes."""
    return "" if value is None else str(value)


def parse_candidate(raw: Mapping[str, Any]) -> CandidateMessage:
    """Parse a single ``results.messages[i]`` entry into a CandidateMessage.

    Missing keys degrade to empty strings rather than raising — high recall means
    we would rather forward a thin candidate than drop a real one (Req 2.5).
    """
    return CandidateMessage(
        channel_id=_coerce_str(raw.get("channel_id")),
        message_ts=_coerce_str(raw.get("message_ts")),
        author_id=_coerce_str(raw.get("author_user_id")),
        text=_coerce_str(raw.get("content")),
        permalink=_coerce_str(raw.get("permalink")),
        author_name=raw.get("author_name"),
        channel_name=raw.get("channel_name"),
        is_author_bot=bool(raw.get("is_author_bot", False)),
    )


def parse_rts_response(response: Mapping[str, Any]) -> list[CandidateMessage]:
    """Normalize a full ``assistant.search.context`` response → candidate list.

    Tolerant by design:
    - ``ok: false`` or a missing ``results`` block yields an empty list (the
      Watcher treats an empty sweep as "no obligations", Req 2.6/2.7).
    - Only the ``messages`` content type is consumed; files/channels are ignored.
    """
    if not response or not response.get("ok", False):
        return []

    results = response.get("results") or {}
    messages: Sequence[Mapping[str, Any]] = results.get("messages") or []
    return [parse_candidate(m) for m in messages]


def next_cursor(response: Mapping[str, Any]) -> str:
    """Return the pagination cursor, or "" when there are no further pages."""
    meta = response.get("response_metadata") or {}
    return _coerce_str(meta.get("next_cursor"))


__all__ = [
    "CandidateMessage",
    "parse_candidate",
    "parse_rts_response",
    "next_cursor",
]
