"""Scaffolding smoke tests.

These verify the project skeleton imports cleanly and that the configuration
module loads. They intentionally test no feature logic — feature tests arrive
with their owning tasks (Sections 3-18). Property tests will live alongside the
components they validate and carry the traceability tag required by the design.
"""

from __future__ import annotations

import importlib

import pytest

SUBPACKAGES = [
    "watcher",
    "adjudicator",
    "verifier",
    "action",
    "conversational",
    "graph",
    "seed",
]


@pytest.mark.parametrize("subpackage", SUBPACKAGES)
def test_subpackages_import_cleanly(subpackage: str) -> None:
    module = importlib.import_module(f"loop.{subpackage}")
    assert module is not None


def test_config_module_loads_settings() -> None:
    from loop.config import Settings, load_settings

    settings = load_settings()
    assert isinstance(settings, Settings)
    # Defaults are present and sane scaffolding values.
    assert settings.sweep_interval_seconds <= 60  # Req 2.1 ceiling
    assert settings.database_path


def test_settings_repr_redacts_secrets() -> None:
    from loop.config import Settings

    s = Settings(slack_bot_token="xoxb-super-secret")
    assert "xoxb-super-secret" not in repr(s)
    assert "***" in repr(s)


def test_require_raises_for_missing_settings() -> None:
    from loop.config import Settings

    with pytest.raises(RuntimeError):
        Settings().require("slack_bot_token")
