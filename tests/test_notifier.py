"""Tests for the ported Discord notifier subset (_post/notify_messages only —
see notifier.py's module docstring). Mirrors ButterflyGuy's own DiscordNotifier
tests: patch `_post` rather than mocking aiohttp internals directly."""

from __future__ import annotations

from unittest.mock import patch

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
