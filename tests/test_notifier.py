"""Tests for the Discord webhook notifier."""

from __future__ import annotations

from unittest.mock import patch

import httpx
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


def _sender(handler, **kwargs) -> DiscordNotifier:
    return DiscordNotifier(
        "https://example.invalid/private-webhook",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(notifier_module.asyncio, "sleep", fake_sleep)
    return slept


@pytest.mark.parametrize("status", [200, 204, 400, 500])
async def test_notification_http_failure_propagates_without_logging_webhook(caplog, status):
    sender = _sender(lambda request: httpx.Response(status))
    if status in (200, 204):
        await sender.notify_messages(["report"])
    else:
        with pytest.raises(RuntimeError, match="Discord notification failed"):
            await sender.notify_messages(["report"])
    assert "private-webhook" not in caplog.text


async def test_notification_transport_error_does_not_expose_url(caplog):
    def handler(request):
        raise httpx.ReadError("failed URL " + str(request.url))

    with pytest.raises(RuntimeError, match="Discord notification failed") as failure:
        await _sender(handler).notify_messages(["report"])
    assert "private-webhook" not in caplog.text
    assert "private-webhook" not in str(failure.value)


async def test_rate_limit_waits_for_retry_after_then_posts(no_sleep):
    responses = iter(
        [httpx.Response(429, json={"retry_after": 1.5}), httpx.Response(204), httpx.Response(204)]
    )
    bodies: list[bytes] = []

    def handler(request):
        bodies.append(request.content)
        return next(responses)

    await _sender(handler).notify_messages(["one", "two"])

    assert no_sleep == [1.5]
    assert len(bodies) == 3
    assert bodies[0] == bodies[1]


async def test_persistent_rate_limit_reports_partial_delivery():
    responses = iter([httpx.Response(204)] + [httpx.Response(429, json={"retry_after": 0})] * 3)
    with pytest.raises(RuntimeError, match="after 1 of 2 messages"):
        await _sender(lambda request: next(responses)).notify_messages(["one", "two"])


async def test_connect_error_is_retried():
    attempts = iter([httpx.ConnectError("down"), httpx.Response(204)])

    def handler(request):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    await _sender(handler).notify_messages(["one"])


async def test_one_client_serves_every_message(monkeypatch):
    created = 0
    real_client = httpx.AsyncClient

    def counting_client(**kwargs):
        nonlocal created
        created += 1
        return real_client(**kwargs)

    monkeypatch.setattr(notifier_module.httpx, "AsyncClient", counting_client)
    await _sender(lambda request: httpx.Response(204)).notify_messages(["a", "b", "c"])
    assert created == 1
