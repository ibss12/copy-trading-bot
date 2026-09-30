"""Trading 212 PRACTICE account for the dashboard: balances, positions (bot vs. your shares), orders.

Manual orders placed here are recorded as yours, so the bot never counts them as its own.
"""

import json
import logging
import time
from datetime import datetime

import config
from dashboard.market import NY
from trading212.broker import order_side, order_ticker
from trading212.client import OrderUncertain, Trading212Error, symbol_from_ticker, validate_base_url
from trading212.ledger import split_position
from trading212.practice import bot_ledger, practice_client

logger = logging.getLogger(__name__)

CURRENCY_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "CHF": "CHF ", "PLN": "zł ", "CZK": "Kč ", "RON": "lei "}
HISTORY_REFRESH_SECONDS = 60
DAY_START_PATH = config.CACHE_DIR / "trading212" / "day_start.json"


def _f(value) -> float:
    return float(value) if value not in (None, "") else 0.0


class Trading212Account:
    provider = "trading212"
    label = "Trading 212 practice account"
    refresh_seconds = 15

    def __init__(self):
        self.enabled = bool(config.TRADING212_API_KEY and config.TRADING212_API_SECRET)
        self.error: str | None = None
        self.ledger = bot_ledger()
        self._client = None
        if self.enabled:
            try:
                validate_base_url(config.TRADING212_BASE_URL)
                self._client = practice_client()
            except (ValueError, Trading212Error) as exc:
                self.enabled = False
                self.error = str(exc)
        self._history: list[dict] = []
        self._history_at = 0.0
        self._pending_ids: set[str] = set()

    def _require(self):
        if self._client is None:
            raise RuntimeError(self.error or "Trading 212 practice API key/secret are not configured")
        return self._client

    def _who(self, row: dict) -> str:
        if self.ledger.is_bot_order(row["id"]):
            return "bot"
        return "you"

    def _day_start(self, equity: float) -> float:
        today = datetime.now(NY).date().isoformat()
        try:
            saved = json.loads(DAY_START_PATH.read_text())
        except (OSError, ValueError):
            saved = {}
        if saved.get("date") != today:
            saved = {"date": today, "equity": equity}
            DAY_START_PATH.parent.mkdir(parents=True, exist_ok=True)
            DAY_START_PATH.write_text(json.dumps(saved))
        return float(saved["equity"])

    def _order_dict(self, row: dict, fill: dict | None = None) -> dict:
        quantity = abs(_f(row.get("quantity")))
        filled = abs(_f(row.get("filledQuantity"))) or (abs(_f(fill.get("quantity"))) if fill else 0.0)
        status = str(row.get("status", "")).lower()
        return {
            "id": str(row["id"]),
            "symbol": symbol_from_ticker(order_ticker(row)),
            "side": order_side(row),
            "qty": quantity or filled,
            "filled_qty": filled,
            "filled_avg_price": _f(fill.get("price"))
            if fill
            else (abs(_f(row.get("filledValue"))) / filled if filled else 0),
            "type": str(row.get("type", "")).lower(),
            "status": "filled" if status == "filled" else status,
            "submitted_at": row.get("createdAt"),
            "filled_at": fill.get("filledAt") if fill else None,
            "by": self._who(row),
        }

    def snapshot(self) -> dict:
        base = {"enabled": self.enabled, "provider": self.provider, "label": self.label}
        if self._client is None:
            return {**base, "error": self.error}
        try:
            summary = self._client.account_summary()
            positions = self._client.positions()
            pending = self._client.pending_orders()
            pending_ids = {str(o["id"]) for o in pending}
            finished = self._pending_ids - pending_ids
            self._pending_ids = pending_ids
            if finished or time.time() - self._history_at > HISTORY_REFRESH_SECONDS:
                self._history = self._client.history_orders(limit=25)
                self._history_at = time.time()
        except Exception as exc:
            self.error = str(exc)
            logger.warning("Trading 212 practice account request failed: %s", exc)
            return {**base, "error": self.error}
        self.error = None
        currency = summary.get("currency") or "USD"
        equity = _f(summary.get("totalValue"))
        day_start = self._day_start(equity)
        cash = summary.get("cash") or {}
        bot_counts = self.ledger.bot_shares()
        rows = []
        for p in positions:
            symbol = symbol_from_ticker(order_ticker(p))
            qty = _f(p.get("quantity"))
            bot_qty, your_qty = split_position(qty, bot_counts.get(symbol, 0.0))
            wallet = p.get("walletImpact") or {}
            value, cost = _f(wallet.get("currentValue")), _f(wallet.get("totalCost"))
            rows.append(
                {
                    "symbol": symbol,
                    "qty": qty,
                    "bot_qty": bot_qty,
                    "your_qty": your_qty,
                    "avg_entry_price": _f(p.get("averagePricePaid")),
                    "current_price": _f(p.get("currentPrice")),
                    "market_value": value,
                    "unrealized_pl": _f(wallet.get("unrealizedProfitLoss")),
                    "unrealized_plpc": (_f(wallet.get("unrealizedProfitLoss")) / cost * 100) if cost else 0.0,
                    "intraday_pl": None,
                    "weight_pct": value / equity * 100 if equity else 0.0,
                }
            )
        return {
            **base,
            "error": None,
            "currency": currency,
            "currency_symbol": CURRENCY_SYMBOLS.get(currency, currency + " "),
            "equity": equity,
            "last_equity": day_start,
            "day_pl": equity - day_start,
            "day_pl_pct": (equity / day_start - 1) * 100 if day_start else 0.0,
            "cash": _f(cash.get("availableToTrade")),
            "buying_power": _f(cash.get("availableToTrade")),
            "positions": rows,
            "open_orders": [self._order_dict(o) for o in pending],
            "recent_orders": [self._order_dict(i["order"], i.get("fill")) for i in self._history if i.get("order")],
        }

    def submit_market_order(self, symbol: str, qty: float, side: str) -> dict:
        client = self._require()
        ticker = client.ticker_for(symbol)
        if not ticker:
            raise RuntimeError(f"{symbol} is not available on Trading 212")
        if any(order_ticker(o) == ticker for o in client.pending_orders()):
            raise RuntimeError(f"There's already an open {symbol} order. Wait for it to fill or cancel it first.")
        try:
            row = client.market_order(ticker, qty if side == "buy" else -qty)
        except OrderUncertain as exc:
            raise RuntimeError(
                f"Trading 212 didn't confirm the order ({exc}). Check Open orders before trying again, "
                "so you don't buy twice."
            ) from exc
        self.ledger.record_manual_order(row["id"])
        self._history_at = 0.0
        return self._order_dict(row)

    def cancel_order(self, order_id: str) -> None:
        self._require().cancel_order(int(order_id))
