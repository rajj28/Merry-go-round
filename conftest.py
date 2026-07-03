"""Pytest + Hypothesis configuration for Loop (workspace root).

Enforces the design's testing-strategy rule (design.md → Testing Strategy):

    "each property test runs a minimum of 100 generated examples"

Enforcement is two-layered so CI fails fast if the bar is ever lowered:

1. Profile-level guard: the active Hypothesis profile must declare
   ``max_examples >= MIN_HYPOTHESIS_EXAMPLES``. A misconfigured profile aborts
   the whole test session in ``pytest_configure``.
2. Per-test guard: every test that uses Hypothesis ``@given`` produces Hypothesis
   statistics; after such a test runs we check the number of examples it executed
   and fail it if it ran fewer than ``MIN_HYPOTHESIS_EXAMPLES``.

The active profile is selected with the ``HYPOTHESIS_PROFILE`` environment
variable (default: ``dev``). CI should set ``HYPOTHESIS_PROFILE=ci``.
"""

from __future__ import annotations

import os

import pytest

# The minimum number of examples every property-based test must run.
MIN_HYPOTHESIS_EXAMPLES = 100


def _register_hypothesis_profiles() -> str:
    """Register Hypothesis profiles and activate the requested one.

    Returns the name of the activated profile. If Hypothesis is not installed,
    returns an empty string and lets the guard no-op — the property tests
    themselves will fail to import without Hypothesis, which is the correct
    signal.
    """
    try:
        from hypothesis import HealthCheck, Phase, settings
    except Exception:  # pragma: no cover - Hypothesis not installed
        return ""

    common = dict(
        max_examples=MIN_HYPOTHESIS_EXAMPLES,
        # Keep the full set of phases so we actually generate examples.
        phases=tuple(Phase),
        # Generation can be slower than Hypothesis's default deadline under load;
        # disabling the per-example deadline avoids flaky timing failures in CI.
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )

    settings.register_profile("dev", **common)
    settings.register_profile("ci", derandomize=True, **common)

    profile = os.environ.get("HYPOTHESIS_PROFILE", "dev")
    settings.load_profile(profile)
    return profile


_ACTIVE_PROFILE = _register_hypothesis_profiles()


def pytest_configure(config: pytest.Config) -> None:
    """Fail the session immediately if the example floor is misconfigured."""
    try:
        from hypothesis import settings
    except Exception:  # pragma: no cover - Hypothesis not installed
        return

    current = settings()
    if current.max_examples < MIN_HYPOTHESIS_EXAMPLES:
        raise pytest.UsageError(
            "Hypothesis profile "
            f"{_ACTIVE_PROFILE!r} sets max_examples={current.max_examples}, "
            f"but property tests must run at least {MIN_HYPOTHESIS_EXAMPLES} "
            "examples (see design.md → Testing Strategy)."
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """Per-test guard: fail any property test that ran < MIN examples."""
    outcome = yield
    report = outcome.get_result()

    if report.when != "call" or report.outcome != "passed":
        return

    stats = getattr(item, "hypothesis_statistics", None)
    if not stats:
        return

    text = "\n".join(stats) if isinstance(stats, (list, tuple)) else str(stats)
    count = _parse_passing_examples(text)
    if count is not None and count < MIN_HYPOTHESIS_EXAMPLES:
        report.outcome = "failed"
        report.longrepr = (
            f"Property test ran only {count} example(s); "
            f"the minimum is {MIN_HYPOTHESIS_EXAMPLES} "
            "(see design.md → Testing Strategy)."
        )


def _parse_passing_examples(statistics_text: str) -> int | None:
    """Extract the count of passing examples from Hypothesis statistics text."""
    import re

    match = re.search(r"(\d+)\s+passing examples", statistics_text)
    if match:
        return int(match.group(1))
    return None
