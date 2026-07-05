"""Provider-agnostic two-tier chat helper for Loop.

Loop's reasoning uses two tiers (design.md → "Two-tier LLM strategy"):

  * **fast / filter tier** — high-recall first pass (the Watcher classifier).
  * **smart / precise tier** — high-precision reasoning (the Adjudicator's
    whose-court judgement and the Action Agent's nudge drafting).

This module hides *which* backend serves those tiers behind a single
:func:`chat` call so the agents stay provider-agnostic. The provider is chosen by
``settings.llm_provider``:

  * ``"groq"`` (default) — a FREE, OpenAI-compatible endpoint. We talk to it with
    the ``openai`` SDK pointed at ``settings.groq_base_url``; the fast tier uses
    ``settings.fast_model`` (e.g. ``llama-3.1-8b-instant``) and the smart tier uses
    ``settings.smart_model`` (e.g. ``llama-3.3-70b-versatile``).
  * ``"anthropic"`` — the original Claude path, preserved for backward-compat: the
    fast tier uses ``settings.haiku_model`` and the smart tier ``settings.opus_model``.

Import-safety: the provider SDKs (``openai`` / ``anthropic``) are imported **lazily
inside** :func:`chat`, so importing :mod:`loop.llm` never needs a network connection
or an installed SDK. The required credential for the active provider is validated
via ``settings.require(...)`` at call time.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

# The two reasoning tiers, named so callers never reference concrete model ids.
FAST_TIER = "fast"
SMART_TIER = "smart"

# Models whose hidden chain-of-thought counts against ``max_tokens`` on Groq.
# A tight caller-side cap starves them mid-thought (json_validate_failed:
# "max completion tokens reached before generating a valid document"), so the
# Groq path pins them to low reasoning effort and guarantees token headroom.
# Only the gpt-oss family accepts reasoning_effort low|medium|high on Groq.
_REASONING_MODEL_PREFIXES = ("openai/gpt-oss",)
_REASONING_MIN_MAX_TOKENS = 2048


def _resolve_settings(settings: Any | None) -> Any:
    if settings is not None:
        return settings
    from loop.config import get_settings

    return get_settings()


def provider_key_field(settings: Any) -> str:
    """Return the settings field name holding the active provider's credential."""
    provider = (getattr(settings, "llm_provider", "groq") or "groq").lower()
    if provider == "groq":
        return "groq_api_key"
    if provider == "anthropic":
        return "anthropic_api_key"
    raise ValueError(f"unknown llm_provider {provider!r}; expected 'groq' or 'anthropic'")


def require_provider_key(settings: Any | None = None) -> None:
    """Validate that the active provider's credential is configured (build-time gate)."""
    settings = _resolve_settings(settings)
    settings.require(provider_key_field(settings))


def chat(
    messages: Sequence[Mapping[str, str]],
    *,
    tier: str,
    settings: Any | None = None,
    json_mode: bool = False,
    max_tokens: int = 512,
) -> str:
    """Run a chat completion at the requested ``tier`` and return the text content.

    Args:
        messages: OpenAI-style chat messages (``[{"role": ..., "content": ...}]``).
        tier: ``"fast"`` (filter tier) or ``"smart"`` (precise tier).
        settings: resolved :class:`loop.config.Settings`; defaults to the cached
            process settings.
        json_mode: when True, ask the model to return a single JSON object (used by
            the Adjudicator's structured judgement).
        max_tokens: response token cap.

    Returns:
        The assistant message content as a string.

    Temperature is pinned to 0 so the live demo reproduces the same output on every
    take. The provider's SDK and the network call are imported/opened lazily here.
    """
    if tier not in (FAST_TIER, SMART_TIER):
        raise ValueError(f"unknown tier {tier!r}; expected 'fast' or 'smart'")

    settings = _resolve_settings(settings)
    provider = (getattr(settings, "llm_provider", "groq") or "groq").lower()

    if provider == "groq":
        return _chat_groq(
            messages, tier=tier, settings=settings, json_mode=json_mode, max_tokens=max_tokens
        )
    if provider == "anthropic":
        return _chat_anthropic(
            messages, tier=tier, settings=settings, json_mode=json_mode, max_tokens=max_tokens
        )
    raise ValueError(f"unknown llm_provider {provider!r}; expected 'groq' or 'anthropic'")


def _chat_groq(
    messages: Sequence[Mapping[str, str]],
    *,
    tier: str,
    settings: Any,
    json_mode: bool,
    max_tokens: int,
) -> str:
    """Groq (OpenAI-compatible) path — uses the ``openai`` SDK against Groq's base_url."""
    settings.require("groq_api_key")
    from openai import OpenAI

    client = OpenAI(base_url=settings.groq_base_url, api_key=settings.groq_api_key)
    model = settings.fast_model if tier == FAST_TIER else settings.smart_model

    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": list(messages),
    }
    if model.startswith(_REASONING_MODEL_PREFIXES):
        kwargs["reasoning_effort"] = "low"
        kwargs["max_tokens"] = max(max_tokens, _REASONING_MIN_MAX_TOKENS)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    completion = client.chat.completions.create(**kwargs)
    return completion.choices[0].message.content or ""


def _chat_anthropic(
    messages: Sequence[Mapping[str, str]],
    *,
    tier: str,
    settings: Any,
    json_mode: bool,
    max_tokens: int,
) -> str:
    """Anthropic path — preserves the original Claude call (haiku=fast, opus=smart)."""
    settings.require("anthropic_api_key")
    from anthropic import Anthropic

    client = Anthropic(api_key=settings.anthropic_api_key)
    model = settings.haiku_model if tier == FAST_TIER else settings.opus_model

    # Anthropic takes a top-level system prompt separate from the message list.
    system: Optional[str] = None
    chat_messages: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            system = content if system is None else f"{system}\n{content}"
        else:
            chat_messages.append({"role": role or "user", "content": content})

    create_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": chat_messages,
    }
    if system is not None:
        create_kwargs["system"] = system

    message = client.messages.create(**create_kwargs)
    return "".join(
        getattr(block, "text", "") for block in getattr(message, "content", [])
    )


__all__ = ["chat", "FAST_TIER", "SMART_TIER", "provider_key_field", "require_provider_key"]
