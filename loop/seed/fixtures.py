"""Committed seed fixture pack for the deterministic demo workspace (Req 15).

This module is the *data half* of the Seeded Deterministic Demo Workspace
(design.md → "Seeded Deterministic Demo Workspace"). It holds the **precomputed
adjudication results** — the obligations exactly as the Adjudicator would have
produced them — so demo mode can resolve seeded candidates from these pinned
results instead of a live Opus call (design point 2). Loading this pack
initializes the Obligation Graph to a known, reproducible state.

Production-scale demo org ("Northwind")
---------------------------------------
The pack models a believable mid-size SaaS company so the signature reveal lands
against real noise: ~15 people across ~10 channels and ~25 obligations. The
*surfaced* set stays deliberately small and sharp — **exactly three** surfaced
``blocked-on-you`` loops (the iconic hero count) — while a large spread of
waiting-on-other loops, auto-healed history, and **non-surfacing negatives**
(below-threshold asks, dismissed items, a snoozed loop, a manually-closed loop)
surrounds it. The negatives are the *precision proof*: they must stay hidden on
every run, which is exactly what "quiet by default" means. The contrast — Loop
scanning a noisy org and surfacing only the three that are genuinely on you — is
the whole story.

The pack is engineered to satisfy the Requirement 15 acceptance criteria:

  * **Exactly three surfaced ``blocked-on-you`` obligations** at/above the seeded
    Confidence_Threshold and not dismissed, so the App Home hero banner reads a
    deterministic ``3`` (Req 15.2).
  * **One seeded mergeable PR reference** on a ``blocked-on-you`` obligation, the
    target of the GitHub-verified auto-close beat (Req 15.3).
  * **All four demo beats covered** by at least one obligation: the blocking-count
    reveal, a Polite Nudge target, a GitHub-verified auto-close, and an
    Auto-Healed feed entry (Req 15.4).
  * Below-threshold, dismissed, snoozed, and manually-closed obligations are
    included so the surfacing gate (quiet-by-default) does real filtering work —
    they must stay hidden on every run, which is part of what "deterministic
    surfacing" means.

Purity discipline: every accessor returns **fresh** value objects on each call so
callers (the loader, tests, repeated runs) can never mutate shared fixture state.
This is essential to the determinism guarantee — two runs must start from byte-for
-byte identical inputs.
"""

from __future__ import annotations

from dataclasses import dataclass

from loop.graph.models import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    ArtifactType,
    ClosureKind,
    LoopState,
    Obligation,
    Person,
)

# The Confidence_Threshold the seeded workspace starts at. Using the model's
# documented default keeps the seed aligned with a freshly-initialized graph; the
# fixture confidences below are chosen relative to this gate (Req 14.1).
SEED_THRESHOLD: float = DEFAULT_CONFIDENCE_THRESHOLD  # 0.5

# The seeded mergeable GitHub PR reference driving the auto-close beat (Req 15.3).
# Points at the real demo PR so the live GitHub-MCP auto-close beat (Demo Beat 3)
# fires against actual GitHub state.
SEED_PR_REF: str = "rajj28/loop-demo#2"

# The obligation that references the seeded mergeable PR — the target of the
# GitHub-verified auto-close beat (Req 15.3). Pinned here so the seed module and the
# demo-control API agree on a single source of truth for "which loop the PR heals".
SEED_PR_OBLIGATION_ID: str = "OBL_B2"


# ---------------------------------------------------------------------------
# People (graph nodes) — the Northwind org.
#
# A single Personas table is the source of truth for the people, their display
# names, and their demo avatar URLs, so :func:`seed_people`, :func:`seed_names`,
# and :func:`seed_avatars` can never drift apart. Avatar URLs point at a real,
# reachable image service (pravatar) so the premium avatar accessories actually
# render in the live judged dashboard — not just placeholder boxes.
# ---------------------------------------------------------------------------
_USER = "U_USER"
_ALICE = "U_ALICE"
_BOB = "U_BOB"
_CAROL = "U_CAROL"
_DANA = "U_DANA"
_ERIN = "U_ERIN"
_FRANK = "U_FRANK"
_GREG = "U_GREG"
_HANK = "U_HANK"
_IVY = "U_IVY"
_JACK = "U_JACK"
_KARA = "U_KARA"
_LEO = "U_LEO"
_MIA = "U_MIA"
_NINA = "U_NINA"

# The tracked User's id, exported so the demo-control API (and the Auto-Healed feed,
# which needs the tracked user to pick the *resolved person*) share one source of
# truth for "who the User is" in the seeded workspace.
SEED_USER_ID: str = _USER

# Avatar image URL template (real, reachable photos, stable per ``img`` index) so
# the App Home / Assistant cards render premium human avatars in the live demo.
_AVATAR_URL = "https://i.pravatar.cc/150?img={img}"


@dataclass(frozen=True)
class Persona:
    """One seeded person: graph node identity + display name + role + demo avatar."""

    person_id: str
    display_name: str
    role: str
    avatar_img: int
    is_user: bool = False

    @property
    def avatar_url(self) -> str:
        return _AVATAR_URL.format(img=self.avatar_img)


# The roster. Roles span Engineering, Design, Product, GTM, and People so the demo
# reads like a real company rather than a handful of test accounts.
PERSONAS: tuple[Persona, ...] = (
    Persona(_USER, "You", "Staff Engineer", 0, is_user=True),
    Persona(_ALICE, "Alice Nguyen", "Product Designer", 47),
    Persona(_BOB, "Bob Martinez", "Backend Engineer", 12),
    Persona(_CAROL, "Carol Davis", "Product Manager", 45),
    Persona(_DANA, "Dana Cole", "Design Lead", 31),
    Persona(_ERIN, "Erin Walsh", "DevOps Engineer", 5),
    Persona(_FRANK, "Frank Hill", "Finance", 51),
    Persona(_GREG, "Greg Park", "Frontend Engineer", 13),
    Persona(_HANK, "Hank Osei", "Engineering Manager", 15),
    Persona(_IVY, "Ivy Chen", "Senior PM", 32),
    Persona(_JACK, "Jack Reed", "Marketing", 11),
    Persona(_KARA, "Kara Lopez", "Account Executive", 49),
    Persona(_LEO, "Leo Faye", "Customer Success", 14),
    Persona(_MIA, "Mia Roy", "People Ops", 26),
    Persona(_NINA, "Nina Bauer", "Data Analyst", 27),
)

# ---------------------------------------------------------------------------
# Channels — the workspace surface the loops were detected in. Modeled as a
# ``channel_id → human #name`` map so a row's source channel reads like a real
# place (the App Home renders the id as a native <#…> mention; this map is the
# story-level reference + the live-sandbox seeder's channel plan).
# ---------------------------------------------------------------------------
SEED_CHANNELS: dict[str, str] = {
    "C_ENG_BACKEND": "#eng-backend",
    "C_ENG_FRONTEND": "#eng-frontend",
    "C_DESIGN": "#design",
    "C_PRODUCT": "#product",
    "C_LAUNCH": "#launch-q3",
    "C_INCIDENTS": "#incidents",
    "C_SALES": "#sales",
    "C_CUSTOMER": "#customer-success",
    "C_PEOPLE": "#people-ops",
    "C_GENERAL": "#general",
}


def seed_people() -> list[Person]:
    """Return fresh :class:`Person` nodes for the seeded workspace.

    Exactly one person (``U_USER``) is the tracked User. The rest are the
    counterparties on the seeded open loops, drawn from the :data:`PERSONAS` roster.
    """
    return [
        Person(person_id=p.person_id, display_name=p.display_name, is_user=p.is_user)
        for p in PERSONAS
    ]


def seed_names() -> dict[str, str]:
    """Return a fresh ``person_id → display name`` map for the seeded org.

    The card layout leads with the person's name (title) and keeps the full ask in
    the wrapping body, so this map is what makes the dashboard read "Alice Nguyen"
    rather than a truncated sentence.
    """
    return {p.person_id: p.display_name for p in PERSONAS if not p.is_user}


def seed_avatars() -> dict[str, str]:
    """Return a fresh ``person_id → https avatar URL`` map for the seeded org.

    Points at a real, reachable image service so the premium avatar accessories
    render in the live judged dashboard. The tracked User is omitted (their own
    avatar never appears on a counterparty row).
    """
    return {p.person_id: p.avatar_url for p in PERSONAS if not p.is_user}


# Snooze horizon for the snoozed negative: comfortably after SEED_NOW
# (loader.SEED_NOW == 2025-01-06T00:00:00Z) so the loop stays hidden on every run.
_SNOOZE_UNTIL = "2025-01-08T00:00:00+00:00"


def seed_obligations() -> list[Obligation]:
    """Return the precomputed seed obligations as **fresh** value objects.

    The list mixes surfaced, hidden, and healed obligations on purpose so the
    surfacing predicate does real work. Against ``SEED_THRESHOLD`` (0.5) the
    surfaced active set is deterministically:

      * three ``blocked-on-you`` — ``OBL_B1``, ``OBL_B2`` (PR), ``OBL_B3`` (Req 15.2)
      * six ``waiting-on-other`` — ``OBL_W1``…``OBL_W6``

    while the below-threshold (``OBL_LOW``/``OBL_LOW2``/``OBL_LOW3``), dismissed
    (``OBL_DISMISSED``/``OBL_DISMISSED2``), and snoozed (``OBL_SNOOZED``)
    obligations stay hidden, and the autonomously-healed loops (``OBL_HEALED``,
    ``OBL_H2``…``OBL_H5``) live only in the Auto-Healed feed. ``OBL_MANUAL_HEALED``
    is healed but *manually* closed, so it is excluded from that feed too (Req 9.1).

    Distinct ``last_touch_timestamp`` values give a total order so display
    ordering (oldest→newest) is unambiguous and reproducible.
    """
    return [
        # =================================================================
        # SURFACED — blocked-on-you ×3 (the iconic hero count, Req 15.2).
        # =================================================================
        # --- Beat 1 (blocking-count reveal) + Beat 2 (Polite Nudge target) ----
        Obligation(
            obligation_id="OBL_B1",
            owes_person_id=_USER,
            owed_person_id=_ALICE,
            owner_person_id=_USER,
            loop_state=LoopState.BLOCKED_ON_YOU,
            confidence_score=0.92,
            last_touch_timestamp="2025-01-02T09:00:00+00:00",
            source_msg_channel="C_DESIGN",
            source_msg_ts="1735808400.000100",
            subject_summary="Alice is waiting on your review of the onboarding design doc.",
        ),
        # --- Beat 3 (GitHub-verified auto-close): references the mergeable PR --
        Obligation(
            obligation_id="OBL_B2",
            owes_person_id=_USER,
            owed_person_id=_BOB,
            owner_person_id=_USER,
            loop_state=LoopState.BLOCKED_ON_YOU,
            confidence_score=0.80,
            last_touch_timestamp="2025-01-03T11:30:00+00:00",
            source_msg_channel="C_ENG_BACKEND",
            source_msg_ts="1735903800.000200",
            subject_summary="Bob is blocked on you merging the widgets PR.",
            artifact_type=ArtifactType.GITHUB_PR,
            artifact_ref=SEED_PR_REF,
        ),
        # --- third surfaced blocked-on-you (completes the deterministic 3) ----
        Obligation(
            obligation_id="OBL_B3",
            owes_person_id=_USER,
            owed_person_id=_CAROL,
            owner_person_id=_USER,
            loop_state=LoopState.BLOCKED_ON_YOU,
            confidence_score=0.65,
            last_touch_timestamp="2025-01-04T14:15:00+00:00",
            source_msg_channel="C_LAUNCH",
            source_msg_ts="1735999300.000300",
            subject_summary="Carol needs your sign-off on the Q3 launch checklist.",
        ),
        # =================================================================
        # SURFACED — waiting-on-other ×6 (the ball is in their court).
        # =================================================================
        Obligation(
            obligation_id="OBL_W1",
            owes_person_id=_DANA,
            owed_person_id=_USER,
            owner_person_id=_DANA,
            loop_state=LoopState.WAITING_ON_OTHER,
            confidence_score=0.75,
            last_touch_timestamp="2025-01-02T16:45:00+00:00",
            source_msg_channel="C_DESIGN",
            source_msg_ts="1735836300.000400",
            subject_summary="You are waiting on Dana for the updated mocks.",
        ),
        Obligation(
            obligation_id="OBL_W2",
            owes_person_id=_ERIN,
            owed_person_id=_USER,
            owner_person_id=_ERIN,
            loop_state=LoopState.WAITING_ON_OTHER,
            confidence_score=0.55,
            last_touch_timestamp="2025-01-03T08:20:00+00:00",
            source_msg_channel="C_ENG_BACKEND",
            source_msg_ts="1735892400.000500",
            subject_summary="You are waiting on Erin to confirm the deploy window.",
        ),
        Obligation(
            obligation_id="OBL_W3",
            owes_person_id=_GREG,
            owed_person_id=_USER,
            owner_person_id=_GREG,
            loop_state=LoopState.WAITING_ON_OTHER,
            confidence_score=0.70,
            last_touch_timestamp="2025-01-01T10:00:00+00:00",
            source_msg_channel="C_ENG_FRONTEND",
            source_msg_ts="1735725600.000600",
            subject_summary="You are waiting on Greg for the frontend bug-fix estimate.",
        ),
        Obligation(
            obligation_id="OBL_W4",
            owes_person_id=_KARA,
            owed_person_id=_USER,
            owner_person_id=_KARA,
            loop_state=LoopState.WAITING_ON_OTHER,
            confidence_score=0.68,
            last_touch_timestamp="2025-01-04T09:30:00+00:00",
            source_msg_channel="C_SALES",
            source_msg_ts="1735983000.000700",
            subject_summary="You are waiting on Kara for the enterprise contract numbers.",
        ),
        Obligation(
            obligation_id="OBL_W5",
            owes_person_id=_NINA,
            owed_person_id=_USER,
            owner_person_id=_NINA,
            loop_state=LoopState.WAITING_ON_OTHER,
            confidence_score=0.60,
            last_touch_timestamp="2025-01-03T15:00:00+00:00",
            source_msg_channel="C_PRODUCT",
            source_msg_ts="1735916400.000800",
            subject_summary="You are waiting on Nina for the churn dashboard data pull.",
        ),
        Obligation(
            obligation_id="OBL_W6",
            owes_person_id=_MIA,
            owed_person_id=_USER,
            owner_person_id=_MIA,
            loop_state=LoopState.WAITING_ON_OTHER,
            confidence_score=0.52,
            last_touch_timestamp="2025-01-04T18:00:00+00:00",
            source_msg_channel="C_PEOPLE",
            source_msg_ts="1736013600.000900",
            subject_summary="You are waiting on Mia to approve the new offer letter.",
        ),
        # =================================================================
        # AUTO-HEALED feed ×5 — autonomously closed (Verifier-confirmed merges).
        # These live only in the Auto-Healed feed (Req 9.1), newest→oldest.
        # =================================================================
        Obligation(
            obligation_id="OBL_HEALED",
            owes_person_id=_USER,
            owed_person_id=_BOB,
            owner_person_id=_USER,
            loop_state=LoopState.HEALED,
            confidence_score=0.88,
            last_touch_timestamp="2025-01-01T18:00:00+00:00",
            source_msg_channel="C_ENG_BACKEND",
            source_msg_ts="1735754400.001000",
            subject_summary="Earlier PR auto-closed after it merged.",
            artifact_type=ArtifactType.GITHUB_PR,
            artifact_ref="acme/widgets#7",
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_timestamp="2025-01-01T18:05:00+00:00",
            closure_reason="PR merged",
        ),
        Obligation(
            obligation_id="OBL_H2",
            owes_person_id=_USER,
            owed_person_id=_GREG,
            owner_person_id=_USER,
            loop_state=LoopState.HEALED,
            confidence_score=0.83,
            last_touch_timestamp="2025-01-05T08:30:00+00:00",
            source_msg_channel="C_ENG_FRONTEND",
            source_msg_ts="1736065800.001100",
            subject_summary="Login-page PR auto-closed after it merged.",
            artifact_type=ArtifactType.GITHUB_PR,
            artifact_ref="acme/web#142",
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_timestamp="2025-01-05T09:00:00+00:00",
            closure_reason="PR merged",
        ),
        Obligation(
            obligation_id="OBL_H3",
            owes_person_id=_USER,
            owed_person_id=_ERIN,
            owner_person_id=_USER,
            loop_state=LoopState.HEALED,
            confidence_score=0.79,
            last_touch_timestamp="2025-01-04T11:30:00+00:00",
            source_msg_channel="C_INCIDENTS",
            source_msg_ts="1735990200.001200",
            subject_summary="CI-pipeline fix PR auto-closed after it merged.",
            artifact_type=ArtifactType.GITHUB_PR,
            artifact_ref="acme/infra#88",
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_timestamp="2025-01-04T12:00:00+00:00",
            closure_reason="PR merged",
        ),
        Obligation(
            obligation_id="OBL_H4",
            owes_person_id=_USER,
            owed_person_id=_BOB,
            owner_person_id=_USER,
            loop_state=LoopState.HEALED,
            confidence_score=0.81,
            last_touch_timestamp="2025-01-03T19:30:00+00:00",
            source_msg_channel="C_ENG_BACKEND",
            source_msg_ts="1735932600.001300",
            subject_summary="Hot-fix PR auto-closed after it merged.",
            artifact_type=ArtifactType.GITHUB_PR,
            artifact_ref="acme/api#311",
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_timestamp="2025-01-03T20:00:00+00:00",
            closure_reason="PR merged",
        ),
        Obligation(
            obligation_id="OBL_H5",
            owes_person_id=_USER,
            owed_person_id=_GREG,
            owner_person_id=_USER,
            loop_state=LoopState.HEALED,
            confidence_score=0.77,
            last_touch_timestamp="2025-01-05T16:00:00+00:00",
            source_msg_channel="C_ENG_FRONTEND",
            source_msg_ts="1736092800.001400",
            subject_summary="Dashboard refactor PR auto-closed after it merged.",
            artifact_type=ArtifactType.GITHUB_PR,
            artifact_ref="acme/web#150",
            closure_kind=ClosureKind.AUTONOMOUS,
            closure_timestamp="2025-01-05T16:30:00+00:00",
            closure_reason="PR merged",
        ),
        # =================================================================
        # NEGATIVES — must stay hidden everywhere (the precision proof).
        # =================================================================
        # --- below-threshold ×3: quiet by default (confidence < 0.5) ----------
        Obligation(
            obligation_id="OBL_LOW",
            owes_person_id=_USER,
            owed_person_id=_FRANK,
            owner_person_id=_USER,
            loop_state=LoopState.BLOCKED_ON_YOU,
            confidence_score=0.30,  # < SEED_THRESHOLD → never surfaced
            last_touch_timestamp="2025-01-05T10:00:00+00:00",
            source_msg_channel="C_GENERAL",
            source_msg_ts="1736071200.001500",
            subject_summary="Possible ask from Frank — low confidence.",
        ),
        Obligation(
            obligation_id="OBL_LOW2",
            owes_person_id=_USER,
            owed_person_id=_JACK,
            owner_person_id=_USER,
            loop_state=LoopState.BLOCKED_ON_YOU,
            confidence_score=0.35,  # < SEED_THRESHOLD → never surfaced
            last_touch_timestamp="2025-01-05T13:00:00+00:00",
            source_msg_channel="C_GENERAL",
            source_msg_ts="1736082000.001600",
            subject_summary="Jack mentioned a blog draft — likely not a real ask.",
        ),
        Obligation(
            obligation_id="OBL_LOW3",
            owes_person_id=_NINA,
            owed_person_id=_USER,
            owner_person_id=_NINA,
            loop_state=LoopState.WAITING_ON_OTHER,
            confidence_score=0.40,  # < SEED_THRESHOLD → never surfaced
            last_touch_timestamp="2025-01-05T14:30:00+00:00",
            source_msg_channel="C_PRODUCT",
            source_msg_ts="1736087400.001700",
            subject_summary="Casual 'maybe later' from Nina — low confidence.",
        ),
        # --- dismissed ×2: high confidence, but the user said "not a loop" ----
        Obligation(
            obligation_id="OBL_DISMISSED",
            owes_person_id=_USER,
            owed_person_id=_ALICE,
            owner_person_id=_USER,
            loop_state=LoopState.BLOCKED_ON_YOU,
            confidence_score=0.95,  # high, but dismissed → never surfaced
            last_touch_timestamp="2025-01-01T12:00:00+00:00",
            source_msg_channel="C_DESIGN",
            source_msg_ts="1735732800.001800",
            subject_summary="Already handled — dismissed by the user.",
            dismissed=True,
        ),
        Obligation(
            obligation_id="OBL_DISMISSED2",
            owes_person_id=_USER,
            owed_person_id=_HANK,
            owner_person_id=_USER,
            loop_state=LoopState.BLOCKED_ON_YOU,
            confidence_score=0.90,  # high, but dismissed → never surfaced
            last_touch_timestamp="2025-01-02T12:00:00+00:00",
            source_msg_channel="C_ENG_BACKEND",
            source_msg_ts="1735819200.001900",
            subject_summary="Hank's 1:1 prep — dismissed, handled offline.",
            dismissed=True,
        ),
        # --- snoozed ×1: high confidence, hidden until the snooze elapses ------
        Obligation(
            obligation_id="OBL_SNOOZED",
            owes_person_id=_USER,
            owed_person_id=_LEO,
            owner_person_id=_USER,
            loop_state=LoopState.BLOCKED_ON_YOU,
            confidence_score=0.85,  # high, but snoozed past SEED_NOW → hidden
            last_touch_timestamp="2025-01-04T20:00:00+00:00",
            source_msg_channel="C_CUSTOMER",
            source_msg_ts="1736020800.002000",
            subject_summary="Leo's customer escalation — snoozed until tomorrow.",
            snoozed_until=_SNOOZE_UNTIL,
        ),
        # --- manually-closed ×1: healed, but NOT in the auto-healed feed -------
        Obligation(
            obligation_id="OBL_MANUAL_HEALED",
            owes_person_id=_USER,
            owed_person_id=_IVY,
            owner_person_id=_USER,
            loop_state=LoopState.HEALED,
            confidence_score=0.86,
            last_touch_timestamp="2025-01-02T15:00:00+00:00",
            source_msg_channel="C_PRODUCT",
            source_msg_ts="1735830000.002100",
            subject_summary="Spec sign-off Ivy needed — closed manually by you.",
            closure_kind=ClosureKind.MANUAL,
            closure_timestamp="2025-01-02T15:30:00+00:00",
            closure_reason="Closed manually",
        ),
    ]


__all__ = [
    "SEED_THRESHOLD",
    "SEED_PR_REF",
    "SEED_PR_OBLIGATION_ID",
    "SEED_USER_ID",
    "SEED_CHANNELS",
    "Persona",
    "PERSONAS",
    "seed_people",
    "seed_names",
    "seed_avatars",
    "seed_obligations",
]
