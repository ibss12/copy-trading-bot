"""Send bot notifications to a Discord channel through a webhook (no app or Discord bot needed).

Webhook API: https://docs.discord.com/developers/resources/webhook#execute-webhook
"""

import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

MAX_EMBEDS_PER_MESSAGE = 10
MAX_TITLE = 256
MAX_DESCRIPTION = 4000
MAX_RETRIES = 3
COLORS = {"success": 0x2ECC71, "danger": 0xE74C3C, "warning": 0xF1C40F, "info": 0x3498DB}


@dataclass(frozen=True)
class Notice:
    level: str
    title: str
    body: str = ""


class DiscordNotifier:
    """Collects notices during a check and sends them in as few webhook calls as possible.

    Without a webhook URL the notices are only printed (if `echo`), so the bot still works on your own computer.
    """

    def __init__(self, webhook_url: str, username: str = "Stock bot", timeout_s: float = 15, echo: bool = True):
        self.webhook_url = webhook_url.strip()
        self.echo = echo
        self.username = username
        self.timeout_s = timeout_s
        self.notices: list[Notice] = []

    def add(self, level: str, title: str, body: str = "") -> None:
        self.notices.append(Notice(level, title, body))

    @staticmethod
    def embed(notice: Notice) -> dict:
        embed = {"title": notice.title[:MAX_TITLE], "color": COLORS.get(notice.level, COLORS["info"])}
        if notice.body:
            embed["description"] = notice.body[:MAX_DESCRIPTION]
        return embed

    def flush(self) -> int:
        """Send everything collected so far. Returns how many notices were delivered."""
        notices, self.notices = self.notices, []
        if not notices:
            return 0
        if not self.webhook_url:
            if not self.echo:
                return 0
            for notice in notices:
                print(f"[{notice.level}] {notice.title}" + (f"\n    {notice.body}" if notice.body else ""))
            return 0
        sent = 0
        for start in range(0, len(notices), MAX_EMBEDS_PER_MESSAGE):
            batch = notices[start : start + MAX_EMBEDS_PER_MESSAGE]
            payload = {
                "username": self.username,
                "embeds": [self.embed(n) for n in batch],
                "allowed_mentions": {"parse": []},
            }
            if self._post(payload):
                sent += len(batch)
        return sent

    def _post(self, payload: dict) -> bool:
        for _ in range(MAX_RETRIES):
            try:
                resp = requests.post(self.webhook_url, params={"wait": "true"}, json=payload, timeout=self.timeout_s)
            except requests.RequestException as exc:
                logger.warning("Discord message not sent (%s)", exc.__class__.__name__)
                return False
            if resp.status_code == 429:
                try:
                    wait = float(resp.json().get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                time.sleep(min(max(wait, 0.5), 30))
                continue
            if resp.status_code >= 400:
                logger.warning("Discord refused the message (HTTP %s); check DISCORD_WEBHOOK_URL", resp.status_code)
                return False
            return True
        logger.warning("Discord kept rate-limiting; message dropped")
        return False
