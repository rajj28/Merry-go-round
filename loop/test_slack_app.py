"""Unit tests for the task 1.1 Slack scaffolding (loop/slack_app.py).

These cover the import-safe, token-free surface: the placeholder view, handler
registration against a fake app, the app_home_opened handler behavior, and the
clear-error path when required tokens are missing. No live Slack connection.
"""

from __future__ import annotations

import pytest

from loop import slack_app


def test_build_home_view_is_single_header_placeholder():
    view = slack_app.build_home_view()
    assert view["type"] == "home"
    assert len(view["blocks"]) == 1
    header = view["blocks"][0]
    assert header["type"] == "header"
    assert header["text"]["text"] == "Loop — coming online"


def test_register_handlers_binds_app_home_opened():
    recorded: dict[str, object] = {}

    class FakeApp:
        def event(self, name):
            def decorator(fn):
                recorded[name] = fn
                return fn

            return decorator

    slack_app.register_handlers(FakeApp())
    assert "app_home_opened" in recorded
    assert recorded["app_home_opened"] is slack_app.handle_app_home_opened


def test_handle_app_home_opened_publishes_view():
    calls: list[dict] = []

    class FakeClient:
        def views_publish(self, **kwargs):
            calls.append(kwargs)

    import logging

    slack_app.handle_app_home_opened(
        {"user": "U123"}, FakeClient(), logging.getLogger("test")
    )
    assert len(calls) == 1
    assert calls[0]["user_id"] == "U123"
    assert calls[0]["view"] == slack_app.build_home_view()


def test_handle_app_home_opened_ignores_missing_user():
    class FakeClient:
        def views_publish(self, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("should not publish without a user")

    import logging

    # No exception, no publish.
    slack_app.handle_app_home_opened({}, FakeClient(), logging.getLogger("test"))


def test_build_app_requires_bot_token(monkeypatch):
    monkeypatch.delenv(slack_app.BOT_TOKEN_ENV, raising=False)
    with pytest.raises(RuntimeError) as exc:
        slack_app.build_app()
    assert slack_app.BOT_TOKEN_ENV in str(exc.value)
