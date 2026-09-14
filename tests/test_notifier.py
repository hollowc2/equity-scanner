"""Tests for the ported Discord notifier subset (_post/notify_messages only —
see notifier.py's module docstring). Mirrors ButterflyGuy's own DiscordNotifier
tests: patch `_post` rather than mocking aiohttp internals directly."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from equity_scanner import notifier as notifier_module
from equity_scanner.notifier import DiscordNotifier


async def test_notify_messages_posts_each_nonblank_message():
    notifier = DiscordNotifier("https://discord.example/webhook")
    posted: list[str] = []

    async def capture(content: str, **_kwargs) -> None:
        posted.append(content)

    with patch.object(notifier, "_post", side_effect=capture):
        await notifier.notify_messages(["first message", "  ", "", "second message"])

    assert posted == ["first message", "second message"]


async def test_notify_messages_skips_all_blank_messages():
    notifier = DiscordNotifier("https://discord.example/webhook")

    async def capture(content: str, **_kwargs) -> None:
        raise AssertionError("should not post a blank message")

    with patch.object(notifier, "_post", side_effect=capture):
        await notifier.notify_messages(["", "   ", "\n"])


@pytest.mark.parametrize("status", [200, 204, 429, 500])
async def test_notification_http_failure_propagates_without_logging_webhook(
    monkeypatch, caplog, status
):
    class Response:
        async def __aenter__(self):
            self.status = status
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            assert isinstance(url, str)
            return Response()

    monkeypatch.setattr(notifier_module.aiohttp, "ClientSession", Session)
    sender = DiscordNotifier("https://example.invalid/private-webhook")
    if status in (200, 204):
        await sender.notify_messages(["report"])
    else:
        with pytest.raises(RuntimeError, match="Discord notification failed"):
            await sender.notify_messages(["report"])
    assert "private-webhook" not in caplog.text


async def test_notification_transport_error_does_not_expose_url(monkeypatch, caplog):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            raise ValueError("failed URL " + url)

    monkeypatch.setattr(notifier_module.aiohttp, "ClientSession", Session)
    sender = DiscordNotifier("https://example.invalid/private-webhook")
    with pytest.raises(RuntimeError, match="Discord notification failed") as failure:
        await sender.notify_messages(["report"])
    assert "private-webhook" not in caplog.text
    assert "private-webhook" not in str(failure.value)
