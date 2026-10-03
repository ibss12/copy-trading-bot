"""Dashboard preferences (watchlist, alert rules), saved to .cache/dashboard_settings.json."""

import json
import uuid
from pathlib import Path

import config

DEFAULTS = {
    "watchlist": config.WATCHLIST,
    "move_alert_pcts": [3.0, 5.0, 10.0],
    "fast_move_pct": 1.5,
    "fast_move_minutes": 5,
    "sma_alerts": True,
    "big_trader_alerts": True,
    "bot_alerts": True,
    "advice_alerts": True,
    "signal_refresh_minutes": 60,
    "price_alerts": [],
    # account id -> strategy for bots that should be running (restarted when the server restarts)
    "running_bots": {},
}
PROTECTED = ("watchlist", "price_alerts", "running_bots")


class Settings:
    def __init__(self, path: Path = config.CACHE_DIR / "dashboard_settings.json"):
        self.path = path
        self.data = dict(DEFAULTS)
        if path.exists():
            self.data.update(json.loads(path.read_text()))

    def __getitem__(self, key: str):
        return self.data[key]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    def update(self, changes: dict) -> None:
        for key, value in changes.items():
            if key in DEFAULTS and key not in PROTECTED:
                self.data[key] = value
        self.save()

    def add_price_alert(self, symbol: str, op: str, price: float) -> dict:
        alert = {"id": uuid.uuid4().hex[:8], "symbol": symbol, "op": op, "price": price, "triggered": False}
        self.data["price_alerts"] = [*self.data["price_alerts"], alert]
        self.save()
        return alert

    def remove_price_alert(self, alert_id: str) -> None:
        self.data["price_alerts"] = [a for a in self.data["price_alerts"] if a["id"] != alert_id]
        self.save()

    def set_watchlist(self, symbols: list[str]) -> None:
        self.data["watchlist"] = list(dict.fromkeys(symbols))
        self.save()

    def set_bot_running(self, account_id: str, strategy: str | None) -> None:
        bots = {k: v for k, v in self.data["running_bots"].items() if k != account_id}
        if strategy:
            bots[account_id] = strategy
        self.data["running_bots"] = bots
        self.save()
