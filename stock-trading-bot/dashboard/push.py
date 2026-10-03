"""Phone/desktop push notifications (Web Push), delivered even when the command center is closed.

Each device that turns on notifications is stored with the accounts it wants alerts for.
"""

import json
import logging
import os
import queue
import tempfile
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02, b64urlencode
from pywebpush import WebPushException, webpush

import config

logger = logging.getLogger(__name__)

PUSH_DIR = config.CACHE_DIR / "push"
TTL_SECONDS = 6 * 3600
MAX_SUBSCRIPTIONS = 50
GONE = (404, 410)


class PushNotifier:
    def __init__(self, folder: Path = PUSH_DIR, contact: str = ""):
        self.folder = folder
        self.key_path = folder / "vapid_private.pem"
        self.subs_path = folder / "subscriptions.json"
        self.contact = contact or config.PUBLIC_URL or "mailto:stock-trading-bot@users.noreply.github.com"
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._vapid = self._load_key()
        self.public_key = b64urlencode(
            self._vapid.public_key.public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )
        threading.Thread(target=self._worker, name="push", daemon=True).start()

    def _load_key(self) -> Vapid02:
        self.folder.mkdir(parents=True, exist_ok=True)
        if not self.key_path.exists():
            vapid = Vapid02()
            vapid.generate_keys()
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(vapid.private_pem())
        return Vapid02.from_file(str(self.key_path))

    # ------------------------------------------------------------------ devices
    def subscriptions(self) -> list[dict]:
        try:
            return json.loads(self.subs_path.read_text())
        except (OSError, ValueError):
            return []

    def _save(self, subs: list[dict]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.folder, prefix=".subs")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(subs, fh, indent=1)
        os.replace(tmp, self.subs_path)

    def subscribe(self, subscription: dict, accounts: list[str] | None, device: str) -> dict:
        endpoint = str(subscription.get("endpoint", ""))
        keys = subscription.get("keys") or {}
        if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("That isn't a valid push subscription")
        row = {
            "endpoint": endpoint,
            "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
            "accounts": accounts,
            "device": device[:60],
            "created": int(time.time()),
        }
        with self._lock:
            subs = [s for s in self.subscriptions() if s["endpoint"] != endpoint]
            self._save([*subs, row][-MAX_SUBSCRIPTIONS:])
        return row

    def unsubscribe(self, endpoint: str) -> None:
        with self._lock:
            self._save([s for s in self.subscriptions() if s["endpoint"] != endpoint])

    def device(self, endpoint: str) -> dict | None:
        return next((s for s in self.subscriptions() if s["endpoint"] == endpoint), None)

    # ------------------------------------------------------------------ sending
    @staticmethod
    def wants(sub: dict, account: str | None) -> bool:
        accounts = sub.get("accounts")
        return account is None or accounts is None or account in accounts

    def notify(self, alert: dict, only_endpoint: str | None = None) -> None:
        payload = {
            "title": alert["title"],
            "body": alert.get("message") or "",
            "tag": f"cc-{alert.get('id', int(time.time()))}",
            "level": alert.get("level", "info"),
            "url": "/" + (f"?symbol={alert['symbol']}" if alert.get("symbol") else ""),
        }
        targets = [
            s
            for s in self.subscriptions()
            if (s["endpoint"] == only_endpoint if only_endpoint else self.wants(s, alert.get("account")))
        ]
        for sub in targets:
            try:
                self._queue.put_nowait((sub, payload))
            except queue.Full:
                logger.warning("Push queue full; dropping a notification")

    def _worker(self) -> None:
        while True:
            sub, payload = self._queue.get()
            try:
                self.send(sub, payload)
            except Exception:
                logger.exception("Push notification failed")

    def send(self, sub: dict, payload: dict) -> None:
        try:
            webpush(
                {"endpoint": sub["endpoint"], "keys": sub["keys"]},
                json.dumps(payload),
                vapid_private_key=self._vapid,
                vapid_claims={"sub": self.contact},
                ttl=TTL_SECONDS,
                timeout=15,
                headers={"Urgency": "high" if payload["level"] in ("success", "danger", "warning") else "normal"},
            )
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in GONE:
                logger.info("Push device %s unsubscribed; removing it", sub.get("device") or "?")
                self.unsubscribe(sub["endpoint"])
            else:
                logger.warning("Push to %s failed: %s", sub.get("device") or "device", exc)
