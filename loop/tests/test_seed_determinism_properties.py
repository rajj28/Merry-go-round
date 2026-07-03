"""Property-based test for seeded-workspace determinism (task 16.4 — Property 28).

This exercises the *real* seed loader (:class:`loop.seed.loader.SeededWorkspace`),
which loads the committed fixture pack (:mod:`loop.seed.fixtures`) through the
*real* shared surfacing predicate (``ObligationFilter.surfaced_only`` →
:func:`loop.graph.surfacing.is_surfaced`) into a *real* SQLite-backed Obligation
Graph. No mocking of Loop's own logic.

Property 28 (design.md → "Correctness Properties"):

    For any run against the seeded workspace, the set of surfaced obligations —
    their identities, Loop_States, Confidence_Scores, and display ordering — is
    identical to any other run against the same seeded input, and identical after a
    reset to the initial seeded state.

    Validates: Requirements 15.1, 15.5

Strategy: the demo's determinism guarantee is that pinned (precomputed) outcomes
make the surfaced view independent of the order in which seeded candidates are
processed. So we randomize the **upsert order** (a permutation of the seed
obligation indices) across many examples and assert the surfacing signature —
``(obligation_id, loop_state, confidence_score)`` in display order — is byte-for
-byte identical to a canonical baseline, and that an in-place reset reproduces it.

Each property test runs ≥100 generated examples (enforced by the root
``conftest.py``) and carries the required traceability tag.
"""

from __future__ import annotations

from functools import lru_cache

from hypothesis import given
from hypothesis import strategies as st

from loop.graph.models import LoopState
from loop.seed.fixtures import seed_obligations
from loop.seed.loader import SeededWorkspace, surfacing_signature

# Number of obligations in the committed fixture pack; drives the permutation space.
_SEED_COUNT = len(seed_obligations())

# Deterministic expectations baked into the fixture pack (design points 3 & 4):
#   * exactly three surfaced ``blocked-on-you`` obligations (the hero count, Req 15.2)
#   * nine surfaced active obligations total (3 blocked-on-you + 6 waiting-on-other);
#     the rest of the Northwind org is intentionally non-surfacing (below-threshold,
#     dismissed, snoozed) or healed, proving the quiet-by-default gate does real work.
_EXPECTED_HERO_COUNT = 3
_EXPECTED_SURFACED_TOTAL = 9


@lru_cache(maxsize=1)
def _canonical_signature() -> tuple:
    """The surfacing signature of a default-order load — the run every other run
    must match (computed once)."""
    workspace = SeededWorkspace()
    return surfacing_signature(workspace.surfaced_obligations())


# ---------------------------------------------------------------------------
# Property 28 (task 16.4) — Seeded workspace surfacing is deterministic
# ---------------------------------------------------------------------------
# Feature: loop-obligation-agent, Property 28: Seeded workspace surfacing is deterministic
@given(order=st.permutations(range(_SEED_COUNT)))
def test_property_28_seeded_surfacing_is_deterministic(order: list[int]) -> None:
    """Validates: Requirements 15.1, 15.5.

    For any processing order of the seeded candidates:

      * a freshly loaded workspace surfaces exactly the canonical set, with
        identical identities, Loop_States, Confidence_Scores, and display ordering
        (Req 15.1); and
      * resetting that same workspace (in yet another order) reproduces the same
        surfaced set and the same hero count (Req 15.5).
    """
    baseline = _canonical_signature()

    # --- Req 15.1: a fresh run in an arbitrary order matches the baseline. -----
    workspace = SeededWorkspace(autoload=False)
    assert workspace.load(order=order) is True
    assert workspace.init_error is None

    run_signature = surfacing_signature(workspace.surfaced_obligations())
    assert run_signature == baseline

    # The deterministic demo invariants ride on the same surfaced set.
    assert workspace.hero_count() == _EXPECTED_HERO_COUNT
    assert len(run_signature) == _EXPECTED_SURFACED_TOTAL
    assert all(state != LoopState.HEALED.value for _, state, _ in run_signature)

    # --- Req 15.5: reset to the initial seeded state reproduces the run. -------
    reset_order = list(reversed(order))
    assert workspace.reset(order=reset_order) is True
    assert workspace.init_error is None

    reset_signature = surfacing_signature(workspace.surfaced_obligations())
    assert reset_signature == baseline
    assert workspace.hero_count() == _EXPECTED_HERO_COUNT


# Feature: loop-obligation-agent, Property 28: Seeded workspace surfacing is deterministic
@given(
    order_a=st.permutations(range(_SEED_COUNT)),
    order_b=st.permutations(range(_SEED_COUNT)),
)
def test_property_28_two_independent_runs_agree(
    order_a: list[int], order_b: list[int]
) -> None:
    """Validates: Requirements 15.1.

    Two independent workspaces loaded from the same seeded input in different
    orders surface identical sets (identities, Loop_States, Confidence_Scores, and
    display ordering) — the run-to-run reproducibility guarantee, demonstrated
    without reference to a precomputed baseline.
    """
    ws_a = SeededWorkspace(autoload=False)
    ws_b = SeededWorkspace(autoload=False)
    ws_a.load(order=order_a)
    ws_b.load(order=order_b)

    assert surfacing_signature(ws_a.surfaced_obligations()) == surfacing_signature(
        ws_b.surfaced_obligations()
    )
