"""Remembers which shares the bot bought, so it never sells shares you bought yourself.

Trading 212 only reports one combined position per stock, so the bot keeps its own count on disk.
The bot's share of a position is `min(bot count, total shares held)`; everything else is yours.
Shared between the bot process and the dashboard via a file lock.
"""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

EMPTY = {"bot_shares": {}, "bot_orders": [], "manual_orders": [], "applied_orders": []}
MAX_IDS = 2000


class BotLedger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = path.with_suffix(".lock")

    @contextmanager
    def _locked(self, write: bool):
        with open(self._lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            try:
                data = self._read()
                yield data
                if write:
                    self._write(data)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict:
        if not self.path.exists():
            return json.loads(json.dumps(EMPTY))
        data = json.loads(self.path.read_text() or "{}")
        for key, value in EMPTY.items():
            data.setdefault(key, type(value)())
        return data

    def _write(self, data: dict) -> None:
        for key in ("bot_orders", "manual_orders", "applied_orders"):
            data[key] = data[key][-MAX_IDS:]
        data["bot_shares"] = {s: q for s, q in data["bot_shares"].items() if q > 1e-9}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ reads
    def snapshot(self) -> dict:
        with self._locked(write=False) as data:
            return data

    def bot_shares(self) -> dict[str, float]:
        return dict(self.snapshot()["bot_shares"])

    def is_bot_order(self, order_id) -> bool:
        return str(order_id) in self.snapshot()["bot_orders"]

    def is_manual_order(self, order_id) -> bool:
        return str(order_id) in self.snapshot()["manual_orders"]

    # ------------------------------------------------------------------ writes
    def record_bot_order(self, order_id) -> None:
        with self._locked(write=True) as data:
            if str(order_id) not in data["bot_orders"]:
                data["bot_orders"].append(str(order_id))

    def record_manual_order(self, order_id) -> None:
        with self._locked(write=True) as data:
            if str(order_id) not in data["manual_orders"]:
                data["manual_orders"].append(str(order_id))

    def apply_fill(self, order_id, symbol: str, signed_quantity: float) -> bool:
        """Add a bot buy (+) or sell (-) fill once. Returns False if this order was already counted."""
        with self._locked(write=True) as data:
            if str(order_id) in data["applied_orders"]:
                return False
            data["applied_orders"].append(str(order_id))
            current = float(data["bot_shares"].get(symbol, 0.0))
            data["bot_shares"][symbol] = max(0.0, round(current + signed_quantity, 8))
            return True

    def reconcile(self, held: dict[str, float]) -> dict[str, float]:
        """Shrink the bot's count when you sold more than your own shares. Returns the bot's shares."""
        with self._locked(write=True) as data:
            for symbol, count in list(data["bot_shares"].items()):
                total = float(held.get(symbol, 0.0))
                if count > total:
                    data["bot_shares"][symbol] = max(0.0, total)
            return {s: q for s, q in data["bot_shares"].items() if q > 1e-9}


def split_position(total: float, bot_count: float) -> tuple[float, float]:
    """(bot shares, your shares) for a combined position."""
    bot = max(0.0, min(total, bot_count))
    return bot, max(0.0, total - bot)
