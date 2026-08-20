"""Discord webhook posting, ported from ButterflyGuy's services/notifier.py —
_only_ `_post` and `notify_messages`. The rest of ButterflyGuy's DiscordNotifier
(notify_entry/notify_exit/notify_eod_chart/notify_daily_summary/
notify_consecutive_loss_warning) is coupled to butterfly-options-trade
notifications and doesn't apply here."""

from __future__ import annotations

import json
import logging

import aiohttp

log = logging.getLogger(__name__)


class DiscordNotifier:
    """Sends equity-scan reports to Discord via webhook."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    async def _post(
        self,
        content: str,
        *,
        image_png: bytes | None = None,
        image_name: str = "chart.png",
    ) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                if image_png:
                    payload = {
                        "content": content,
                        "embeds": [{"image": {"url": f"attachment://{image_name}"}}],
                    }
                    form = aiohttp.FormData()
                    form.add_field(
                        "payload_json",
                        json.dumps(payload),
                        content_type="application/json",
                    )
                    form.add_field(
                        "file",
                        image_png,
                        filename=image_name,
                        content_type="image/png",
                    )
                    request = session.post(
                        self.webhook_url,
                        data=form,
                        timeout=aiohttp.ClientTimeout(total=15),
                    )
                else:
                    request = session.post(
                        self.webhook_url,
                        json={"content": content},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                async with request as resp:
                    if resp.status not in (200, 204):
                        log.warning("discord_post_failed status=%s", resp.status)
        except Exception as exc:
            log.error("discord_error error=%s", exc)

    async def notify_messages(self, messages: list[str]) -> None:
        """Post one or more plain-text messages (e.g. morning equity scan)."""
        for message in messages:
            if message.strip():
                await self._post(message)
