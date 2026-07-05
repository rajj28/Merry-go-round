"""Groq reasoning-model handling in :func:`loop.llm.chat`.

The gpt-oss family spends completion tokens on hidden chain-of-thought before
answering; with the callers' tight ``max_tokens=512`` cap, Groq fails JSON mode
with ``json_validate_failed`` ("max completion tokens reached before generating
a valid document") — seen live from the cycle breaker. The Groq path therefore
pins those models to ``reasoning_effort="low"`` and guarantees token headroom,
while leaving non-reasoning models untouched.
"""

from __future__ import annotations

from typing import Any

import pytest

from loop import llm


class _StubSettings:
    llm_provider = "groq"
    groq_base_url = "https://api.groq.example/v1"
    groq_api_key = "gsk-test"
    fast_model = "llama-3.1-8b-instant"
    smart_model = "openai/gpt-oss-120b"

    def require(self, field: str) -> None:  # credential gate: always satisfied
        assert getattr(self, field, None)


class _CapturingClient:
    """Stands in for ``openai.OpenAI``; records the completion kwargs."""

    captured: dict[str, Any] = {}

    def __init__(self, **_: Any) -> None:
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any) -> Any:
        _CapturingClient.captured.update(kwargs)

        class _Msg:
            content = '{"ok": true}'

        class _Choice:
            message = _Msg()

        class _Completion:
            choices = [_Choice()]

        return _Completion()


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import openai

    _CapturingClient.captured.clear()
    monkeypatch.setattr(openai, "OpenAI", _CapturingClient)
    return _CapturingClient.captured


def test_gpt_oss_smart_tier_gets_low_effort_and_token_headroom(
    captured: dict[str, Any],
) -> None:
    llm.chat(
        [{"role": "user", "content": "judge this"}],
        tier=llm.SMART_TIER,
        settings=_StubSettings(),
        json_mode=True,
        max_tokens=512,
    )
    assert captured["model"] == "openai/gpt-oss-120b"
    assert captured["reasoning_effort"] == "low"
    assert captured["max_tokens"] >= 2048
    assert captured["response_format"] == {"type": "json_object"}


def test_generous_caller_cap_is_not_lowered(captured: dict[str, Any]) -> None:
    llm.chat(
        [{"role": "user", "content": "long draft"}],
        tier=llm.SMART_TIER,
        settings=_StubSettings(),
        max_tokens=8192,
    )
    assert captured["max_tokens"] == 8192


def test_non_reasoning_fast_tier_is_untouched(captured: dict[str, Any]) -> None:
    llm.chat(
        [{"role": "user", "content": "classify"}],
        tier=llm.FAST_TIER,
        settings=_StubSettings(),
        max_tokens=8,
    )
    assert captured["model"] == "llama-3.1-8b-instant"
    assert "reasoning_effort" not in captured
    assert captured["max_tokens"] == 8
