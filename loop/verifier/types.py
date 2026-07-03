"""Authoritative Verifier result contract (task 2.4 — Dev C, day-1 interface freeze).

This module defines the **frozen day-1 contract** that the Verifier (task 8.1) and the
Action Agent (auto-close — task 10, Polite-Nudge gate — task 12) return and consume.
It is the safety interlock for the entire auto-close / nudge feature: nothing speaks as
the user, and nothing auto-closes a loop, except on the result values defined here.

Two enums make up the contract:

  - ``VerificationResult ∈ {RESOLVED, UNRESOLVED, UNVERIFIED}`` — the three-valued
    outcome the Verifier reports for a PR-referencing obligation:
      * RESOLVED   — the referenced PR is merged (Req 4.3).
      * UNRESOLVED — the PR is reachable but not merged: still open, or closed
                     without merging (Req 4.4, 8.7).
      * UNVERIFIED — transport failure: GitHub MCP unreachable / timed out / errored
                     after at most 3 attempts (Req 4.5). This is the value that owns
                     the safety gate: while UNVERIFIED, auto-close is forbidden
                     (Req 4.6, 8.6).

  - ``VerifyPurpose ∈ {AUTO_CLOSE, NUDGE}`` — why the Verifier is being called, which
    selects the timeout budget the Verifier (task 8.1) applies:
      * NUDGE      — gating a Polite_Nudge; 10-second timeout (Req 4.2).
      * AUTO_CLOSE — evaluating an autonomous closure; resolve within 30 seconds
                     (Req 8.1).

Relationship to ``mcp_contract.VerificationIntent``
---------------------------------------------------
Task 1.3 created ``loop.verifier.mcp_contract`` with a ``VerificationIntent`` enum used
**only** as the mapping target for a *successfully retrieved* PR status. That intent enum
can only ever produce RESOLVED or UNRESOLVED — it never yields UNVERIFIED, because
UNVERIFIED is a transport-failure value owned by the Verifier's call logic, not derivable
from a response body. ``VerificationResult`` defined here is the AUTHORITATIVE enum; the
``from_intent`` adapter below lines the two up so the contract-mapping layer feeds cleanly
into the authoritative result type.

This contract is frozen for day 1 so all four owners (Verifier, Action Agent, App Home,
seed) can build against stable values without coordination churn.
"""

from __future__ import annotations

from enum import Enum

from loop.verifier.mcp_contract import VerificationIntent


class VerificationResult(str, Enum):
    """Authoritative three-valued Verifier outcome (Req 4.3, 4.4, 4.5).

    The Verifier (task 8.1) returns this; the Action Agent consumes it as the safety
    interlock for auto-close (task 10) and the Polite_Nudge gate (task 12):

      RESOLVED    PR is merged                                     (Req 4.3)
      UNRESOLVED  PR reachable but not merged (open / closed)      (Req 4.4, 8.7)
      UNVERIFIED  transport failure after <=3 attempts;            (Req 4.5)
                  while UNVERIFIED, auto-close is forbidden        (Req 4.6, 8.6)
    """

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    UNVERIFIED = "unverified"


class VerifyPurpose(str, Enum):
    """Why the Verifier is invoked — selects the applicable timeout budget.

    AUTO_CLOSE  evaluating an autonomous closure; resolve within 30s   (Req 8.1)
    NUDGE       gating a Polite_Nudge before send; 10s timeout         (Req 4.2)
    """

    AUTO_CLOSE = "auto_close"
    NUDGE = "nudge"


def from_intent(intent: VerificationIntent) -> VerificationResult:
    """Adapt a ``mcp_contract.VerificationIntent`` to the authoritative ``VerificationResult``.

    The contract-mapping layer (``mcp_contract.to_verification_intent``) classifies a
    *successfully retrieved* PR status as RESOLVED or UNRESOLVED only — it never produces
    UNVERIFIED, which is the transport-failure value owned by the Verifier (Req 4.5, 8.6).
    This adapter therefore maps:

        VerificationIntent.RESOLVED   -> VerificationResult.RESOLVED    (Req 4.3)
        VerificationIntent.UNRESOLVED -> VerificationResult.UNRESOLVED  (Req 4.4, 8.7)
        VerificationIntent.UNVERIFIED -> VerificationResult.UNVERIFIED  (Req 4.5; defensive)

    The UNVERIFIED branch is defensive: the contract mapping does not emit it today, but
    keeping the adapter total means the two enums stay aligned if that ever changes.
    """
    return VerificationResult(intent.value)
