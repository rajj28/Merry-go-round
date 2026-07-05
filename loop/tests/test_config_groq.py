"""Config tests for the provider-agnostic Groq LLM settings.

These verify that the new two-tier LLM settings load from the environment with the
expected env-var names (LLM_PROVIDER / GROK_API / model overrides) and that the
Groq API key is treated as a secret (redacted in ``repr``).
"""

from __future__ import annotations

import pytest


def test_groq_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from loop.config import load_settings

    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROK_API", "gsk-live-key")
    monkeypatch.setenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("LOOP_FAST_MODEL", "llama-3.1-8b-instant")
    monkeypatch.setenv("LOOP_SMART_MODEL", "llama-3.3-70b-versatile")

    settings = load_settings()

    assert settings.llm_provider == "groq"
    assert settings.groq_api_key == "gsk-live-key"
    assert settings.groq_base_url == "https://api.groq.com/openai/v1"
    assert settings.fast_model == "llama-3.1-8b-instant"
    assert settings.smart_model == "llama-3.3-70b-versatile"


def test_grok_api_falls_back_to_groq_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from loop.config import load_settings

    # Empty (but present) GROK_API so the on-disk .env cannot repopulate it; the
    # loader should then fall back to GROQ_API_KEY.
    monkeypatch.setenv("GROK_API", "")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-fallback")

    settings = load_settings()
    assert settings.groq_api_key == "gsk-fallback"


def test_grok_api_takes_precedence_over_groq_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from loop.config import load_settings

    monkeypatch.setenv("GROK_API", "gsk-primary")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-secondary")

    settings = load_settings()
    assert settings.groq_api_key == "gsk-primary"


def test_groq_defaults_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import loop.config
    from loop.config import load_settings

    # Neutralize the on-disk .env: delenv alone is not enough, because
    # load_settings() re-reads it and repopulates whatever it defines.
    monkeypatch.setattr(loop.config, "_load_env_file", lambda: None)
    for name in ("LLM_PROVIDER", "GROQ_BASE_URL", "LOOP_FAST_MODEL", "LOOP_SMART_MODEL"):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()
    assert settings.llm_provider == "groq"
    assert settings.groq_base_url == "https://api.groq.com/openai/v1"
    assert settings.fast_model == "llama-3.1-8b-instant"
    assert settings.smart_model == "llama-3.3-70b-versatile"


def test_user_tokens_parse_and_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    from loop.config import load_settings

    monkeypatch.setenv(
        "LOOP_USER_TOKENS", "U0AAA:xoxp-alpha, U0BBB:xoxp-beta ,junk, :xoxp-x, U0CCC:"
    )
    settings = load_settings()
    assert settings.user_tokens == (("U0AAA", "xoxp-alpha"), ("U0BBB", "xoxp-beta"))
    assert "xoxp-alpha" not in repr(settings)


def test_groq_api_key_redacted_in_repr() -> None:
    from loop.config import Settings

    s = Settings(groq_api_key="gsk-super-secret")
    assert "gsk-super-secret" not in repr(s)
    assert "***" in repr(s)
