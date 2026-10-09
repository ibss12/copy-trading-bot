"""Turns live quotes, big-trader filings, bot output and account changes into user-facing alerts."""

import itertools
import re
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime

from dashboard.market import NY, Quote
from dashboard.settings import Settings
from signals.models import TraderSignal

MAX_ALERTS = 300
FAST_MOVE_COOLDOWN_SECONDS = 15 * 60
BOT_PATTERNS = [
    (re.compile(r"\b(BUY \S+ [A-Z.\-]+ @ .*)"), "success", "Bot bought"),
    (re.compile(r"\b(SELL \S+ [A-Z.\-]+: .*)"), "warning", "Bot sold"),
    (re.compile(r"(Daily loss .*|Daily loss limit hit.*)"), "danger", "Daily loss limit"),
    (re.compile(r"\b(ALERT [A-Z.\-]+ .*)"), "info", "Bot price alert"),
]


class AlertEngine:
    def __init__(self, settings: Settings, emit: Callable[[dict], None], short_window: int, long_window: int):
        self.settings = settings
        self.emit = emit
        self._short = short_window
        self._long = long_window
        self.history: deque[dict] = deque(maxlen=MAX_ALERTS)
        self._ids = itertools.count(1)
        self._day_moves: set[tuple[str, str, float, int]] = set()
        self._trend: dict[str, str] = {}
        self._fast_sent: dict[str, float] = {}
        self._seen_signals: set[str] = set()
        self._seen_orders: dict[str, set[str]] = {}
        self._advice: dict[str, str] = {}

    def raise_alert(
        self,
        level: str,
        category: str,
        title: str,
        message: str,
        symbol: str | None = None,
        account: str | None = None,
    ) -> dict:
        alert = {
            "id": next(self._ids),
            "ts": int(time.time() * 1000),
            "level": level,
            "category": category,
            "symbol": symbol,
            "title": title,
            "message": message,
            "account": account,
        }
        self.history.appendleft(alert)
        self.emit(alert)
        return alert

    # --- live prices ------------------------------------------------------------

    def check_quote(self, quote: Quote) -> None:
        if not quote.price:
            return
        self._check_price_alerts(quote)
        if quote.symbol not in self.settings["watchlist"]:
            return
        self._check_day_move(quote)
        self._check_fast_move(quote)
        self._check_trend(quote)

    def _check_price_alerts(self, quote: Quote) -> None:
        changed = False
        for rule in self.settings["price_alerts"]:
            if rule["symbol"] != quote.symbol or rule["triggered"]:
                continue
            hit = quote.price >= rule["price"] if rule["op"] == "above" else quote.price <= rule["price"]
            if hit:
                rule["triggered"] = True
                changed = True
                self.raise_alert(
                    "warning",
                    "price",
                    f"{quote.symbol} is {rule['op']} ${rule['price']:,.2f}",
                    f"{quote.symbol} is trading at ${quote.price:,.2f} (your alert: {rule['op']} ${rule['price']:,.2f}).",
                    quote.symbol,
                )
        if changed:
            self.settings.save()

    def _check_day_move(self, quote: Quote) -> None:
        if not quote.prev_close:
            return
        move = quote.change_pct
        today = datetime.now(NY).date().isoformat()
        direction = 1 if move > 0 else -1
        crossed = [t for t in sorted(self.settings["move_alert_pcts"]) if abs(move) >= t]
        if not crossed:
            return
        key = (today, quote.symbol, crossed[-1], direction)
        if key in self._day_moves:
            return
        self._day_moves.add(key)
        word = "up" if direction > 0 else "down"
        self.raise_alert(
            "success" if direction > 0 else "danger",
            "move",
            f"{quote.symbol} {word} {abs(move):.1f}% today",
            f"{quote.symbol} is at ${quote.price:,.2f}, {move:+.2f}% from yesterday's close of ${quote.prev_close:,.2f}.",
            quote.symbol,
        )

    def _check_fast_move(self, quote: Quote) -> None:
        if quote.session != "regular" or not quote.minutes:
            return
        minutes = int(self.settings["fast_move_minutes"])
        threshold = float(self.settings["fast_move_pct"])
        now_minute = int(time.time() // 60)
        earlier = [m for m in quote.minutes if now_minute - minutes - 1 <= m <= now_minute - minutes]
        if not earlier:
            return
        base = quote.minutes[max(earlier)]
        move = (quote.price / base - 1) * 100
        if abs(move) < threshold or time.time() - self._fast_sent.get(quote.symbol, 0) < FAST_MOVE_COOLDOWN_SECONDS:
            return
        self._fast_sent[quote.symbol] = time.time()
        word = "jumped" if move > 0 else "dropped"
        self.raise_alert(
            "success" if move > 0 else "danger",
            "move",
            f"{quote.symbol} {word} {abs(move):.1f}% in {minutes} min",
            f"{quote.symbol} moved from ${base:,.2f} to ${quote.price:,.2f} in the last {minutes} minutes.",
            quote.symbol,
        )

    def _check_trend(self, quote: Quote) -> None:
        short_sma, long_sma = quote.sma(self._short), quote.sma(self._long)
        if short_sma is None or long_sma is None:
            return
        trend = "up" if short_sma > long_sma else "down"
        previous = self._trend.get(quote.symbol)
        self._trend[quote.symbol] = trend
        if previous is None or previous == trend or not self.settings["sma_alerts"]:
            return
        if trend == "up":
            self.raise_alert(
                "success",
                "sma",
                f"{quote.symbol}: buy signal (golden cross)",
                f"The {self._short}-day average (${short_sma:,.2f}) moved above the {self._long}-day average "
                f"(${long_sma:,.2f}). This is the crossover the bot buys on.",
                quote.symbol,
            )
        else:
            self.raise_alert(
                "danger",
                "sma",
                f"{quote.symbol}: sell signal (death cross)",
                f"The {self._short}-day average (${short_sma:,.2f}) fell below the {self._long}-day average "
                f"(${long_sma:,.2f}). This is the crossover the bot sells on.",
                quote.symbol,
            )

    # --- big traders ------------------------------------------------------------

    def check_signals(self, signals: list[TraderSignal], alert_since: str) -> None:
        """Alert on filings not seen before. On the first load, only filings disclosed since `alert_since`."""
        for signal in signals:
            key = signal.describe()
            if key in self._seen_signals:
                continue
            self._seen_signals.add(key)
            if signal.disclosed_on.isoformat() < alert_since or not self.settings["big_trader_alerts"]:
                continue
            verb = "bought" if signal.action == "buy" else "sold"
            self.raise_alert(
                "success" if signal.action == "buy" else "warning",
                "big_trader",
                f"{signal.trader} {verb} {signal.symbol}",
                f"{signal.detail} ({signal.source}, disclosed {signal.disclosed_on})."
                + (f" Why: {signal.reason}" if signal.reason else "")
                + (" Info only: the bot doesn't trade on this one." if signal.notify_only else ""),
                signal.symbol,
            )

    # --- bot + account ------------------------------------------------------------

    def check_bot_line(self, line: str, account: str | None = None, account_name: str = "") -> None:
        if not self.settings["bot_alerts"]:
            return
        for pattern, level, title in BOT_PATTERNS:
            match = pattern.search(line)
            if match:
                text = match.group(1).strip()
                tokens = text.split()
                symbol = {"BUY": 2, "SELL": 2, "ALERT": 1}.get(tokens[0])
                self.raise_alert(
                    level,
                    "bot",
                    f"{title} ({account_name})" if account_name else title,
                    text,
                    tokens[symbol].rstrip(":") if symbol else None,
                    account,
                )
                return

    def check_account(self, snapshot: dict) -> None:
        orders = snapshot.get("recent_orders")
        if orders is None:
            return
        filled = {o["id"]: o for o in orders if o["status"] == "filled"}
        label = snapshot.get("label", "paper account")
        account = snapshot.get("id")
        seen = self._seen_orders.get(account)
        if seen is None:
            self._seen_orders[account] = set(filled)
            return
        for order_id, order in filled.items():
            if order_id in seen:
                continue
            seen.add(order_id)
            who = {"bot": "Bot's order filled", "you": "Your order filled"}.get(order.get("by"), "Order filled")
            self.raise_alert(
                "success" if order["side"] == "buy" else "warning",
                "bot" if order.get("by") == "bot" else "order",
                f"{who}: {order['side'].upper()} {order['filled_qty']:g} {order['symbol']}",
                f"{order['side'].capitalize()} {order['filled_qty']:g} {order['symbol']} "
                f"at ${order['filled_avg_price']:,.2f} ({label}).",
                order["symbol"],
                account,
            )

    # --- buy/sell advice ----------------------------------------------------------

    def check_advice(self, advice: dict) -> None:
        """Alert when a stock's call changes to BUY or SELL (the first call per stock only sets a baseline)."""
        symbol, action = advice["symbol"], advice["action"]
        previous = self._advice.get(symbol)
        self._advice[symbol] = action
        if previous is None or previous == action or action not in ("buy", "sell"):
            return
        if not self.settings["advice_alerts"]:
            return
        self.raise_alert(
            "success" if action == "buy" else "danger",
            "advice",
            f"{symbol}: {advice['headline']}",
            " ".join(advice["reasons"]),
            symbol,
        )

    def advice_summary(self, advice: list[dict]) -> None:
        for item in advice:
            self._advice[item["symbol"]] = item["action"]
        buys = [a["symbol"] for a in advice if a["action"] == "buy"]
        sells = [a["symbol"] for a in advice if a["action"] == "sell"]
        if not self.settings["advice_alerts"] or not (buys or sells):
            return
        parts = ([f"BUY {', '.join(buys)}"] if buys else []) + ([f"SELL {', '.join(sells)}"] if sells else [])
        self.raise_alert(
            "info",
            "advice",
            "Today's calls: " + " · ".join(parts),
            "Based on the SMA trend and what the big traders you follow disclosed. Open a stock to see why.",
        )
