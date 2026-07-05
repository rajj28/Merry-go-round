"""End-to-end scripted demo-beat acceptance tests (task 18.1 — the deliverable).

These tests drive the **real, fully-wired** :class:`loop.app.LoopApp` through the
four demo beats against a deterministic seeded workspace. Only the three external
network boundaries are mocked — exactly the seams the production composition root
(:func:`loop.app.build_loop_app`) wires lazily:

  * the GitHub MCP PR-status client (the Verifier's injected port),
  * the the LLM nudge-drafting port (the Action Agent's injected port),
  * the Slack "send as user" port (the Action Agent's injected port).

Everything else is the genuine production code: the embedded SQLite Obligation
Graph, the Action Agent (draft / send / auto-close), the Verifier's three-valued
grounding, the App Home Block Kit builder, and the App-Home publish wiring on
``LoopApp.open_home``. Because the seeded fixture pack / loader (task 16) is not yet
built, the deterministic seed state is constructed here directly via the store —
the obligations, confidence scores, states and PR references the four beats need.

The four beats (requirements 15.2, 15.3, 15.4, 7.1, 7.3, 9.3):

  * Beat 1 — App Home hero banner reports a count of three ``blocked-on-you``
    obligations at/above the Confidence_Threshold (Req 15.2, 6.1).
  * Beat 2 — a one-tap Polite Nudge drafts (≤1000 chars, referencing the subject)
    and, only on confirm, is sent as the user in the source channel (Req 7.1, 7.3).
  * Beat 3 — the seeded PR transitioning to merged drives the Verifier to RESOLVED
    and auto-closes the obligation within 2s → healed + autonomous + closure
    metadata + a new Auto-Healed feed entry (Req 15.3).
  * Beat 4 — the Auto-Healed feed shows the new entries newest→oldest by closure
    timestamp (Req 9.3).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from loop.action.action_agent import AUTO_CLOSE_REASON, ActionAgent
from loop.action.app_home import (
    HEALED_SECTION_TITLE,
    auto_healed_rows,
    build_app_home_view,
    hero_count,
    slack_date,
)
from loop.adjudicator.adjudicator import Adjudicator, Direction, SmartAdjudication
from loop.app import LoopApp
from loop.config import Settings
from loop.conversational.conversational_agent import ConversationalAgent
from loop.graph.models import (
    ArtifactType,
    ClosureKind,
    DEFAULT_CONFIDENCE_THRESHOLD,
    LoopState,
    Obligation,
)
from loop.graph.sqlite_store import IN_MEMORY, SqliteObligationGraph
from loop.graph.store import is_ok
from loop.learn.feedback import LearnEngine
from loop.pipeline import AdjudicationQueue
from loop.verifier.types import VerificationResult
from loop.verifier.verifier import Verifier
from loop.watcher.watcher import Watcher


# The tracked user and a couple of counterparties in the seeded workspace.
USER = "U_USER"
ALEX = "U_ALEX"
BREE = "U_BREE"
CHEN = "U_CHEN"


# --------------------------------------------------------------------------- #
# External-boundary doubles (the only things mocked — see module docstring)
# --------------------------------------------------------------------------- #
class FakeGitHubMcp:
    """Mutable GitHub MCP PR-status client (the Verifier's external boundary).

    Returns an *open* PR payload for every pull number until :meth:`merge` flips it
    to *merged*. The payload shape is exactly what
    :meth:`PullRequestStatus.from_mcp_response` reads, so the real Verifier maps it
    to UNRESOLVED (open) / RESOLVED (merged) with no special-casing.
    """

    def __init__(self) -> None:
        self._merged: set[int] = set()

    def merge(self, pull_number: int) -> None:
        self._merged.add(pull_number)

    def __call__(self, owner, repo, pull_number, *, timeout):  # noqa: ANN001
        if pull_number in self._merged:
            return {
                "number": pull_number,
                "state": "closed",
                "merged": True,
                "merged_at": "2025-01-08T12:30:00Z",
            }
        return {
            "number": pull_number,
            "state": "open",
            "merged": False,
            "merged_at": None,
        }


def _draft(obligation: Obligation) -> str:
    """Deterministic LLM drafting double (the Action Agent's external boundary).

    Mirrors a real nudge: references the subject and the source message timestamp,
    well under the 1000-char limit so the Action Agent's clip is a no-op here.
    """
    return (
        f"Hi! Just a friendly nudge about \"{obligation.subject_summary}\" "
        f"(from your message on {obligation.source_msg_ts}). "
        "Whenever you get a moment — thanks!"
    )


def _smart_user_owes(_c, *, user_id):  # noqa: ANN001
    """Adjudicator reasoning double — not exercised by these beats but required to
    construct the real detection pipeline wiring."""
    return SmartAdjudication(
        is_loop=True,
        involves_user=True,
        direction=Direction.USER_OWES,
        confidence=0.95,
        subject_summary="seeded",
    )


# --------------------------------------------------------------------------- #
# Deterministic seeded workspace (stands in for task 16's loader)
# --------------------------------------------------------------------------- #
# A fixed reference instant so aging/ordering are reproducible across runs.
SEED_NOW = datetime(2025, 1, 10, 9, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _blocked(
    oid: str,
    *,
    owed: str,
    subject: str,
    channel: str,
    last_touch: datetime,
    confidence: float = 0.9,
    artifact_ref: str | None = None,
) -> Obligation:
    """A ``blocked-on-you`` obligation: the user owes ``owed`` a response."""
    return Obligation(
        obligation_id=oid,
        owes_person_id=USER,
        owed_person_id=owed,
        owner_person_id=USER,
        loop_state=LoopState.BLOCKED_ON_YOU,
        confidence_score=confidence,
        last_touch_timestamp=_iso(last_touch),
        source_msg_channel=channel,
        source_msg_ts="1700000000.000100",
        subject_summary=subject,
        artifact_type=ArtifactType.GITHUB_PR if artifact_ref else None,
        artifact_ref=artifact_ref,
    )


def _seed_graph(graph: SqliteObligationGraph) -> None:
    """Initialize the graph to the known demo state.

    Exactly three ``blocked-on-you`` obligations at/above the default threshold and
    not dismissed (Req 15.2). Two reference seeded GitHub PRs (for the auto-close /
    feed beats), one is a plain message nudge target (Beat 2). A below-threshold and
    a dismissed obligation are included to prove the surfacing gate keeps the hero
    count at exactly three (Req 6.1, 6.3). One ``waiting-on-other`` rounds out the
    workspace.
    """
    seeds = [
        # --- the three surfaced blocked-on-you obligations (the hero count) ---
        _blocked(
            "OBL_REVIEW",
            owed=ALEX,
            subject="Review the obligation-graph PR",
            channel="C_ENG",
            last_touch=SEED_NOW - timedelta(days=4),  # overdue, oldest
            confidence=0.97,
            artifact_ref="acme/throwaway#42",
        ),
        _blocked(
            "OBL_DECK",
            owed=BREE,
            subject="Send the Q1 planning deck",
            channel="C_DECK",
            last_touch=SEED_NOW - timedelta(days=2),
            confidence=0.92,
        ),
        _blocked(
            "OBL_BUDGET",
            owed=CHEN,
            subject="Approve the travel budget PR",
            channel="C_OPS",
            last_touch=SEED_NOW - timedelta(hours=3),  # freshest
            confidence=0.88,
            artifact_ref="acme/throwaway#43",
        ),
        # --- below threshold: must NOT count toward the hero banner (Req 6.1) ---
        _blocked(
            "OBL_NOISE",
            owed=ALEX,
            subject="Maybe-a-loop low confidence",
            channel="C_RANDOM",
            last_touch=SEED_NOW - timedelta(hours=1),
            confidence=0.10,
        ),
        # --- dismissed: must NOT count toward the hero banner (Req 6.3) ---
        Obligation(
            obligation_id="OBL_DISMISSED",
            owes_person_id=USER,
            owed_person_id=BREE,
            owner_person_id=USER,
            loop_state=LoopState.BLOCKED_ON_YOU,
            confidence_score=0.95,
            last_touch_timestamp=_iso(SEED_NOW - timedelta(hours=5)),
            source_msg_channel="C_OLD",
            source_msg_ts="1700000000.000200",
            subject_summary="Already handled, dismissed",
            dismissed=True,
        ),
        # --- a waiting-on-other, for completeness of the workspace ---
        Obligation(
            obligation_id="OBL_WAITING",
            owes_person_id=CHEN,
            owed_person_id=USER,
            owner_person_id=CHEN,
            loop_state=LoopState.WAITING_ON_OTHER,
            confidence_score=0.9,
            last_touch_timestamp=_iso(SEED_NOW - timedelta(days=1)),
            source_msg_channel="C_ENG",
            source_msg_ts="1700000000.000300",
            subject_summary="Chen owes you the launch checklist",
        ),
    ]
    assert graph.get_threshold() == DEFAULT_CONFIDENCE_THRESHOLD
    for obligation in seeds:
        assert is_ok(graph.upsert(obligation)), f"failed to seed {obligation.obligation_id}"


class SeededLoop:
    """A fully-wired :class:`LoopApp` over the deterministic seed + boundary doubles."""

    def __init__(self) -> None:
        self.clock = {"t": SEED_NOW}
        self.sends: list[tuple[str, str]] = []
        self.github = FakeGitHubMcp()

        def now() -> str:
            return self.clock["t"].isoformat()

        def send_as_user(channel, text):  # noqa: ANN001
            self.sends.append((channel, text))

        graph = SqliteObligationGraph(IN_MEMORY)
        _seed_graph(graph)

        verifier = Verifier(self.github)
        action = ActionAgent(
            graph,
            verifier,
            now=now,
            slack_send_as_user=send_as_user,
            draft=_draft,
        )
        adjudicator = Adjudicator(_smart_user_owes, graph)
        queue = AdjudicationQueue(adjudicator, USER)
        watcher = Watcher(
            graph,
            rts_client=lambda: {"ok": False},
            forward=queue.enqueue,
            interval_seconds=30,
        )
        conversational = ConversationalAgent(graph, action)
        learn = LearnEngine(graph)

        self.now = now
        self.app = LoopApp(
            graph=graph,
            watcher=watcher,
            adjudicator=adjudicator,
            verifier=verifier,
            action=action,
            conversational=conversational,
            learn=learn,
            queue=queue,
            user_id=USER,
            # Pin section rendering so these assertions don't depend on ambient
            # .env LOOP_UI_STYLE/LOOP_HOME_STYLE; the section-text helpers below
            # read section blocks (the cards path is covered by its own tests,
            # and the rich data_table feed by test_app_home_rich_blocks.py).
            settings=Settings(
                home_style="sections", assistant_style="sections", rich_blocks=False
            ),
            now=now,
        )

    def advance(self, delta: timedelta) -> None:
        self.clock["t"] = self.clock["t"] + delta


class _CapturingClient:
    """Captures ``views_publish`` calls so we can inspect the published App Home."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    def views_publish(self, **kwargs):  # noqa: ANN001
        self.published.append(kwargs)

    def chat_postMessage(self, **kwargs):  # noqa: ANN001
        pass


class _RichRejectingClient(_CapturingClient):
    """Rejects the first ``views_publish`` (a workspace refusing rich blocks)."""

    def views_publish(self, **kwargs):  # noqa: ANN001
        super().views_publish(**kwargs)
        if len(self.published) == 1:
            raise RuntimeError("invalid_blocks: data_table")


def test_rich_view_falls_back_to_classic_when_publish_is_rejected(
    loop: SeededLoop,
) -> None:
    """Failure-safe publish: a workspace that rejects the newest blocks gets
    the classic layout on an immediate second publish — the Home never dies."""
    import dataclasses

    loop.app._settings = dataclasses.replace(loop.app._settings, rich_blocks=True)
    client = _RichRejectingClient()
    loop.app.open_home(USER, client)

    assert len(client.published) == 2
    rich_view = str(client.published[0]["view"])
    classic_view = str(client.published[1]["view"])
    # The seeded workspace has open loops, so rich mode renders the native
    # aging chart; the fallback republish must carry none of the rich blocks.
    assert "data_visualization" in rich_view
    for rich_only in ("data_visualization", "data_table", "'container'"):
        assert rich_only not in classic_view


class _PartialRichClient(_CapturingClient):
    """Rejects rich publishes the way Slack really does: an ``invalid_arguments``
    response naming each unsupported block type, until the view stops carrying
    the refused types."""

    REFUSED = ("data_visualization", "container")

    def views_publish(self, **kwargs):  # noqa: ANN001
        super().views_publish(**kwargs)
        text = str(kwargs["view"])
        offending = [t for t in self.REFUSED if f"'{t}'" in text]
        if offending:
            exc = RuntimeError("The request to the Slack API failed: invalid_arguments")
            exc.response = {  # duck-typed SlackResponse.data shape
                "ok": False,
                "error": "invalid_arguments",
                "response_metadata": {
                    "messages": [
                        f"[ERROR] unsupported type: {t} [json-pointer:/view/blocks/5/type]"
                        for t in offending
                    ]
                },
            }
            raise exc


def test_rich_publish_adapts_to_the_workspace_instead_of_going_classic(
    loop: SeededLoop,
) -> None:
    """Capability discovery: when Slack's rejection names the refused types,
    the immediate republish drops exactly those — not the whole rich layer —
    and the app remembers, so the next refresh publishes clean on the first try."""
    import dataclasses

    loop.app._settings = dataclasses.replace(loop.app._settings, rich_blocks=True)
    client = _PartialRichClient()
    loop.app.open_home(USER, client)

    assert len(client.published) == 2  # one rejection, one adapted publish
    retry_view = str(client.published[1]["view"])
    for refused in _PartialRichClient.REFUSED:
        assert f"'{refused}'" not in retry_view
    assert loop.app._rich_unsupported == set(_PartialRichClient.REFUSED)

    # The learned capability set persists: a later refresh needs no retry.
    second = _PartialRichClient()
    loop.app.open_home(USER, second)
    assert len(second.published) == 1


class TestUnsupportedBlockTypeParsing:
    def test_reads_a_dict_response(self) -> None:
        from loop.app import _unsupported_block_types

        exc = RuntimeError("invalid_arguments")
        exc.response = {
            "response_metadata": {
                "messages": [
                    "[ERROR] unsupported type: data_visualization"
                    " [json-pointer:/view/blocks/5/type]",
                    "[ERROR] unsupported type: container"
                    " [json-pointer:/view/blocks/9/type]",
                    "[ERROR] failed to match all allowed schemas"
                    " [json-pointer:/view]",
                ]
            }
        }
        assert _unsupported_block_types(exc) == {"data_visualization", "container"}

    def test_reads_a_slack_response_like_object_via_data(self) -> None:
        from loop.app import _unsupported_block_types

        class _Resp:
            data = {
                "response_metadata": {
                    "messages": [
                        "[ERROR] unsupported type: data_table"
                        " [json-pointer:/view/blocks/1/type]"
                    ]
                }
            }

        exc = RuntimeError("invalid_arguments")
        exc.response = _Resp()
        assert _unsupported_block_types(exc) == {"data_table"}

    def test_yields_nothing_for_shapeless_errors(self) -> None:
        from loop.app import _unsupported_block_types

        assert _unsupported_block_types(RuntimeError("boom")) == set()
        exc = RuntimeError("no metadata")
        exc.response = {"ok": False, "error": "invalid_blocks"}
        assert _unsupported_block_types(exc) == set()


@pytest.fixture()
def loop() -> SeededLoop:
    return SeededLoop()


# --------------------------------------------------------------------------- #
# Block Kit view helpers
# --------------------------------------------------------------------------- #
def _hero_header_text(view: dict) -> str:
    """The hero banner header text (the first header block of the home view)."""
    for block in view["blocks"]:
        if block.get("type") == "header":
            return block["text"]["text"]
    raise AssertionError("no header block in the App Home view")


def _healed_section_row_texts(view: dict) -> list[str]:
    """The Auto-Healed rows under the section, each as headline + timeline subline.

    Each feed row renders a mrkdwn ``section`` headline immediately followed by a
    ``context`` timeline subline (the native closure time). We merge each headline
    with its subline so the closure time is observable on the row.
    """
    blocks = view["blocks"]
    start = next(
        i
        for i, b in enumerate(blocks)
        if b.get("type") == "header" and b["text"]["text"] == HEALED_SECTION_TITLE
    )
    rows: list[str] = []
    # Exclude the trailing footer divider + footer context from the span.
    for block in blocks[start + 1 : len(blocks) - 2]:
        if block.get("type") == "header":
            break  # reached the next section
        if block.get("type") == "section":
            rows.append(block["text"]["text"])
        elif block.get("type") == "context" and rows and block.get("elements"):
            rows[-1] = rows[-1] + "\n" + block["elements"][0]["text"]
    return rows


# =========================================================================== #
# Beat 1 — the "blocking N people" reveal (Req 15.2, 6.1)
# =========================================================================== #
def test_beat1_hero_banner_reports_three_blocked_on_you(loop: SeededLoop) -> None:
    client = _CapturingClient()

    # Drive the REAL publish wiring (LoopApp.open_home → build_app_home_view).
    loop.app.open_home(USER, client)

    assert len(client.published) == 1
    view = client.published[0]["view"]
    assert view["type"] == "home"

    # The hero count is exactly three surfaced blocked-on-you obligations: the
    # below-threshold and dismissed seeds are excluded by the surfacing gate.
    assert hero_count(loop.app.graph, loop.now()) == 3

    hero = _hero_header_text(view)
    assert "3 people are blocked on you" in hero, hero


# =========================================================================== #
# Beat 2 — one-tap Polite Nudge: draft, then send-as-user only on confirm
# (Req 7.1, 7.3)
# =========================================================================== #
def test_beat2_polite_nudge_drafts_then_sends_on_confirm(loop: SeededLoop) -> None:
    # One tap on "Nudge" drafts the message and gates the send behind confirmation.
    draft_result = loop.app.handle_nudge_click("OBL_DECK")
    assert draft_result.requires_confirmation is True
    assert loop.app.confirmations.has_pending(USER)
    # Nothing has been sent as the user yet (Req 7.2 / 13.2).
    assert loop.sends == []

    # The drafted text references the subject and is within the 1000-char cap (Req 7.1).
    pending = loop.app.confirmations.get(USER)
    assert pending is not None and pending.nudge_draft is not None
    assert "Send the Q1 planning deck" in pending.nudge_draft.text
    assert 0 < len(pending.nudge_draft.text) <= 1000

    # The one-tap confirm sends it as the user, in the source channel (Req 7.3).
    confirm = loop.app.handle_confirm_send(USER)
    assert confirm.sent is True
    assert len(loop.sends) == 1
    channel, text = loop.sends[0]
    assert channel == "C_DECK"  # the obligation's source channel
    assert "Send the Q1 planning deck" in text
    assert not loop.app.confirmations.has_pending(USER)


def test_beat2_decline_sends_nothing(loop: SeededLoop) -> None:
    loop.app.handle_nudge_click("OBL_DECK")
    declined = loop.app.handle_decline_send(USER)
    assert declined.cancelled is True
    assert loop.sends == []
    assert not loop.app.confirmations.has_pending(USER)


# =========================================================================== #
# Beat 3 — seeded PR merge → auto-close within 2s (healed + autonomous + feed)
# (Req 15.3)
# =========================================================================== #
def test_beat3_seeded_pr_merge_auto_closes_within_2s(loop: SeededLoop) -> None:
    obligation = loop.app.graph.get("OBL_REVIEW")
    assert obligation.loop_state is LoopState.BLOCKED_ON_YOU

    # Before the merge the seeded PR is open → Verifier UNRESOLVED → no closure.
    pre = loop.app.action.auto_close(obligation)
    assert pre.closed is False
    assert pre.verification is VerificationResult.UNRESOLVED
    assert loop.app.graph.get("OBL_REVIEW").loop_state is LoopState.BLOCKED_ON_YOU

    # The seeded PR transitions to merged; auto-close must complete within 2s.
    loop.github.merge(42)
    closure_at = SEED_NOW + timedelta(minutes=5)
    loop.clock["t"] = closure_at

    started = time.perf_counter()
    result = loop.app.action.auto_close(loop.app.graph.get("OBL_REVIEW"))
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"auto-close took {elapsed:.3f}s (>2s)"
    assert result.closed is True
    assert result.verification is VerificationResult.RESOLVED

    # Healed + autonomous + closure metadata, observable from the graph (Req 8.2, 8.5).
    stored = loop.app.graph.get("OBL_REVIEW")
    assert stored.loop_state is LoopState.HEALED
    assert stored.closure_kind is ClosureKind.AUTONOMOUS
    assert stored.closure_reason == AUTO_CLOSE_REASON
    assert stored.closure_timestamp == closure_at.isoformat()
    assert stored.artifact_ref == "acme/throwaway#42"  # source artifact retained

    # A new Auto-Healed feed entry appears (Req 9.1) and the hero count drops to two.
    feed = auto_healed_rows(loop.app.graph, loop.now(), USER)
    assert any(o.obligation_id == "OBL_REVIEW" for o in feed)
    assert hero_count(loop.app.graph, loop.now()) == 2

    # And it is rendered in the published App Home's Auto-Healed section.
    client = _CapturingClient()
    loop.app.open_home(USER, client)
    healed_rows = _healed_section_row_texts(client.published[0]["view"])
    assert any(AUTO_CLOSE_REASON in row for row in healed_rows)


# =========================================================================== #
# Beat 4 — Auto-Healed feed ordering: newest→oldest by closure timestamp (Req 9.3)
# =========================================================================== #
def test_beat4_auto_healed_feed_orders_newest_first(loop: SeededLoop) -> None:
    # Merge and auto-close PR#42 first (earlier closure time)...
    loop.github.merge(42)
    loop.clock["t"] = SEED_NOW + timedelta(minutes=5)
    first = loop.app.action.auto_close(loop.app.graph.get("OBL_REVIEW"))
    assert first.closed is True

    # ...then PR#43 later (later closure time).
    loop.github.merge(43)
    loop.clock["t"] = SEED_NOW + timedelta(minutes=20)
    second = loop.app.action.auto_close(loop.app.graph.get("OBL_BUDGET"))
    assert second.closed is True

    # The real feed selector orders the entries newest→oldest by closure timestamp.
    feed = auto_healed_rows(loop.app.graph, loop.now(), USER)
    feed_ids = [o.obligation_id for o in feed]
    assert feed_ids == ["OBL_BUDGET", "OBL_REVIEW"], feed_ids

    # Closure timestamps are strictly descending (the ordering invariant, Req 9.3).
    timestamps = [o.closure_timestamp for o in feed]
    assert timestamps == sorted(timestamps, reverse=True)

    # The published App Home renders the same newest-first order.
    client = _CapturingClient()
    loop.app.open_home(USER, client)
    rows = _healed_section_row_texts(client.published[0]["view"])
    # OBL_BUDGET's later closure timestamp must appear before OBL_REVIEW's.
    budget_ts = loop.app.graph.get("OBL_BUDGET").closure_timestamp
    review_ts = loop.app.graph.get("OBL_REVIEW").closure_timestamp
    budget_row = next(i for i, r in enumerate(rows) if slack_date(budget_ts) in r)
    review_row = next(i for i, r in enumerate(rows) if slack_date(review_ts) in r)
    assert budget_row < review_row
