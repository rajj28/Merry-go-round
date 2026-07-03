"""Fault-injection unit tests for seeded-workspace load failure (task 16.5).

These exercise the *real* seed loader (:class:`loop.seed.loader.SeededWorkspace`)
under injected failures and assert the Requirement 15.6 contract:

    IF the seeded input fails to load, THEN THE Loop SHALL record an error
    indication that the Seeded_Workspace did not initialize and SHALL NOT surface
    any Obligation.

Validates: Requirements 15.6

Concretely, for every way the seed load can break, we assert the same three
observable outcomes:

  (a) ``load()`` (or ``reset()``) returns ``False`` — the load did not succeed;
  (b) ``init_error`` is set (non-``None``) — the failure is recorded; and
  (c) ``surfaced_obligations()`` returns an empty list — zero surfaced.

Failures are injected three independent ways so the contract is shown to hold
regardless of *where* the load breaks: an invalid ``order`` permutation, the
fixture accessor raising, and the backing graph rejecting writes.
"""

from __future__ import annotations

import pytest

from loop.seed.fixtures import seed_obligations
from loop.seed.loader import SeededWorkspace

_SEED_COUNT = len(seed_obligations())


def _assert_failed_to_initialize(workspace: SeededWorkspace) -> None:
    """Assert the Req 15.6 failure contract: error recorded + zero surfaced."""
    # (b) the failure is recorded as a human-readable, non-empty indication.
    assert workspace.init_error is not None
    assert isinstance(workspace.init_error, str)
    assert workspace.init_error != ""
    # (c) nothing is surfaced.
    assert workspace.surfaced_obligations() == []
    assert workspace.hero_count() == 0


def test_invalid_order_permutation_records_error_and_surfaces_nothing() -> None:
    """An ``order`` that is not a permutation of the seed indices fails the load.

    Validates: Requirements 15.6.
    """
    workspace = SeededWorkspace(autoload=False)
    # Duplicated/incomplete index list — not a valid permutation.
    bad_order = [0] * _SEED_COUNT

    loaded = workspace.load(order=bad_order)

    # (a) load did not succeed.
    assert loaded is False
    _assert_failed_to_initialize(workspace)


def test_fixture_accessor_raising_records_error_and_surfaces_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the committed fixture pack cannot be read, the load fails cleanly.

    Validates: Requirements 15.6.
    """

    def _boom() -> list:
        raise RuntimeError("seed fixture pack is corrupt")

    # Patch the name as imported into the loader module.
    monkeypatch.setattr("loop.seed.loader.seed_obligations", _boom)

    workspace = SeededWorkspace(autoload=False)
    loaded = workspace.load()

    assert loaded is False
    assert "seed fixture pack is corrupt" in workspace.init_error
    _assert_failed_to_initialize(workspace)


def test_graph_factory_failure_records_error_and_surfaces_nothing() -> None:
    """If the backing graph rejects the seed writes, the load fails cleanly.

    Validates: Requirements 15.6.
    """

    class _BrokenGraph:
        """A graph whose every operation raises, simulating a store fault."""

        def set_threshold(self, *_args, **_kwargs):
            raise RuntimeError("store unavailable")

        def upsert(self, *_args, **_kwargs):
            raise RuntimeError("store unavailable")

        def query(self, *_args, **_kwargs):
            raise RuntimeError("store unavailable")

    workspace = SeededWorkspace(graph_factory=_BrokenGraph, autoload=False)
    loaded = workspace.load()

    assert loaded is False
    _assert_failed_to_initialize(workspace)


def test_autoload_failure_is_recorded_on_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure during ``autoload=True`` construction is recorded, not raised.

    Validates: Requirements 15.6.
    """

    def _boom() -> list:
        raise RuntimeError("seed unavailable at startup")

    monkeypatch.setattr("loop.seed.loader.seed_obligations", _boom)

    # Construction must not raise; the error is surfaced via init_error instead.
    workspace = SeededWorkspace()  # autoload=True by default

    _assert_failed_to_initialize(workspace)


def test_reset_failure_records_error_and_surfaces_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reset that fails leaves the workspace in the failed-to-initialize state.

    Validates: Requirements 15.6.
    """
    # Start from a healthy, loaded workspace.
    workspace = SeededWorkspace()
    assert workspace.init_error is None
    assert workspace.surfaced_obligations() != []

    # Now break the fixture pack and reset.
    def _boom() -> list:
        raise RuntimeError("seed unavailable on reset")

    monkeypatch.setattr("loop.seed.loader.seed_obligations", _boom)
    reset_ok = workspace.reset()

    assert reset_ok is False
    _assert_failed_to_initialize(workspace)
