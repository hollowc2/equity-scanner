"""Discord webhook posting for the equity-scan report.

One HTTP client serves every message of a report. Discord rate limits (429) are
waited out using the server's `retry_after`, and a connection that never reached
Discord is retried; any other failure raises without exposing the webhook URL,
which is a credential."""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 10.0


def _retry_after_seconds(response: httpx.Response) -> float:
    seconds: object = None
    try:
        seconds = response.json().get("retry_after")
    except (ValueError, AttributeError):
        pass
    if seconds is None:
        seconds = response.headers.get("Retry-After")
    try:
        value = float(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 1.0
    return min(max(value, 0.0), MAX_RETRY_AFTER_SECONDS)


class DiscordNotifier:
    """Sends equity-scan reports to Discord via webhook."""

    def __init__(
        self,
        webhook_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.webhook_url = webhook_url
        self._transport = transport
        self._max_attempts = max_attempts
        self._client: httpx.AsyncClient | None = None

    async def _post(self, content: str) -> None:
        assert self._client is not None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(self.webhook_url, json={"content": content})
            except httpx.ConnectError:
                if attempt == self._max_attempts:
                    raise
                log.warning("discord_connect_retry attempt=%d", attempt)
                await asyncio.sleep(0.5 * attempt)
                continue
            if response.status_code in (200, 204):
                return
            if response.status_code == 429 and attempt < self._max_attempts:
                delay = _retry_after_seconds(response)
                log.warning("discord_rate_limited retry_after=%.2f attempt=%d", delay, attempt)
                await asyncio.sleep(delay)
                continue
            log.error("discord_post_failed status=%s", response.status_code)
            raise RuntimeError(f"Discord rejected notification status={response.status_code}")
        raise AssertionError("retry loop exhausted")

    async def notify_messages(self, messages: list[str]) -> None:
        """Post one or more plain-text messages (e.g. morning equity scan)."""
        pending = [message for message in messages if message.strip()]
        sent = 0
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0), transport=self._transport
            ) as client:
                self._client = client
                for message in pending:
                    await self._post(message)
                    sent += 1
        except Exception as exc:
            # httpx error messages include the request URL, i.e. the webhook secret.
            log.error(
                "discord_error error_type=%s sent=%d total=%d",
                type(exc).__name__,
                sent,
                len(pending),
            )
            raise RuntimeError(
                f"Discord notification failed after {sent} of {len(pending)} messages"
            ) from None
        finally:
            self._client = None
        log.info("discord_messages_sent count=%d", sent)
