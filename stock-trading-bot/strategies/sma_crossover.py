"""Short/long simple-moving-average crossover on daily bars, with risk controls."""

import math
from dataclasses import dataclass
from datetime import date

from lumibot.entities import Order
from lumibot.strategies.strategy import Strategy


@dataclass(frozen=True)
class SmaSignal:
    price: float
    previous_close: float
    short_sma: float
    long_sma: float
    crossed_up: bool
    crossed_down: bool

    @property
    def trend_up(self) -> bool:
        return self.short_sma > self.long_sma


class SmaCrossover(Strategy):
    """Buy when the short SMA crosses above the long SMA, sell when it crosses below.

    Risk controls:
      * `max_position_pct`: max fraction of portfolio value per stock.
      * `max_open_positions`: cap on the number of stocks held at once.
      * `max_daily_loss_pct`: once the portfolio is down this much versus the previous close,
        no new buys are placed for the rest of the session (optionally liquidates everything).
        A loss detected at the close halts buying for the next session, so the guard also
        works with daily (1D) cadences where no intraday iteration happens.
    """

    parameters = {
        "symbols": ["AAPL", "MSFT"],
        "sma_short_window": 20,
        "sma_long_window": 50,
        "max_position_pct": 0.10,
        "max_open_positions": 8,
        "max_daily_loss_pct": 0.03,
        "liquidate_on_daily_loss": False,
        "price_alert_pcts": [5.0, 10.0],
        "sleeptime": "1D",
    }

    # --- Lifecycle hooks -------------------------------------------------------

    def initialize(self):
        self.sleeptime = self.parameters["sleeptime"]
        self.symbols = [s.upper() for s in self.parameters["symbols"]]
        self.short_window = int(self.parameters["sma_short_window"])
        self.long_window = int(self.parameters["sma_long_window"])
        self.trading_day: date | None = None
        self.day_start_value = 0.0
        self.last_close_value = 0.0
        self.halted_for_day = False
        self.halt_next_day = False
        self.alerts_sent: set[tuple[str, float]] = set()

    def before_market_opens(self):
        self._start_new_day()

    def after_market_closes(self):
        self._check_daily_loss(at_close=True)
        self.last_close_value = float(self.get_portfolio_value())

    def on_trading_iteration(self):
        if self.get_datetime().date() != self.trading_day:
            self._start_new_day()
        self._check_daily_loss()

        self.iteration_cash = float(self.get_cash())
        self.iteration_new_positions = 0
        self.pending_orders = self._pending_orders()
        for symbol in self.universe():
            signal = self.sma_signal(symbol)
            if signal is None:
                continue
            self._price_alert(symbol, signal)
            self.decide(symbol, signal)

    # --- Decision logic (overridden by subclasses) ----------------------------

    def universe(self) -> list[str]:
        held = [p.asset.symbol for p in self.get_positions() if p.quantity > 0]
        return list(dict.fromkeys(self.symbols + held))

    def decide(self, symbol: str, signal: SmaSignal) -> None:
        if self.held_quantity(symbol) > 0:
            if signal.crossed_down:
                self.sell(symbol, f"short SMA {signal.short_sma:.2f} crossed below long SMA {signal.long_sma:.2f}")
        elif signal.crossed_up and symbol in self.symbols:
            self.buy(
                symbol, signal.price, f"short SMA {signal.short_sma:.2f} crossed above long SMA {signal.long_sma:.2f}"
            )

    # --- Helpers ---------------------------------------------------------------

    def sma_signal(self, symbol: str) -> SmaSignal | None:
        try:
            bars = self.get_historical_prices(symbol, self.long_window + 2, "day")
        except Exception as exc:  # unknown/delisted ticker - skip it, keep trading the rest
            self.log_message(f"No price data for {symbol}: {exc}", color="yellow")
            return None
        if bars is None or len(bars.df) < self.long_window + 1:
            return None
        closes = bars.df["close"].astype(float)
        short = closes.rolling(self.short_window).mean()
        long = closes.rolling(self.long_window).mean()
        return SmaSignal(
            price=float(closes.iloc[-1]),
            previous_close=float(closes.iloc[-2]),
            short_sma=float(short.iloc[-1]),
            long_sma=float(long.iloc[-1]),
            crossed_up=short.iloc[-2] <= long.iloc[-2] and short.iloc[-1] > long.iloc[-1],
            crossed_down=short.iloc[-2] >= long.iloc[-2] and short.iloc[-1] < long.iloc[-1],
        )

    def held_quantity(self, symbol: str) -> float:
        position = self.get_position(symbol)
        return float(position.quantity) if position else 0.0

    def buy(self, symbol: str, price: float, reason: str) -> None:
        if self.halted_for_day:
            self.log_message(f"Skip BUY {symbol}: daily loss limit reached", color="yellow")
            return
        if symbol in self.pending_orders:
            self.log_message(f"Skip BUY {symbol}: {self.pending_orders[symbol]} order still open", color="yellow")
            return
        held = {p.asset.symbol for p in self.get_positions() if p.quantity > 0}
        pending_buys = {s for s, side in self.pending_orders.items() if side == "buy" and s not in held}
        open_positions = len(held) + len(pending_buys) + self.iteration_new_positions
        if open_positions >= int(self.parameters["max_open_positions"]):
            self.log_message(f"Skip BUY {symbol}: max open positions reached", color="yellow")
            return
        budget = float(self.get_portfolio_value()) * float(self.parameters["max_position_pct"])
        budget = min(budget - self.held_quantity(symbol) * price, self.iteration_cash)
        quantity = math.floor(budget / price) if price > 0 else 0
        if quantity < 1:
            self.log_message(f"Skip BUY {symbol}: not enough cash/position budget", color="yellow")
            return
        self.submit_order(self.create_order(symbol, quantity, "buy"))
        self.pending_orders[symbol] = "buy"
        self.iteration_cash -= quantity * price
        self.iteration_new_positions += 1
        self.log_message(f"BUY {quantity} {symbol} @ ~{price:.2f}: {reason}", color="green")

    def sell(self, symbol: str, reason: str) -> None:
        quantity = self.held_quantity(symbol)
        if quantity <= 0:
            return
        if symbol in self.pending_orders:
            self.log_message(f"Skip SELL {symbol}: {self.pending_orders[symbol]} order still open", color="yellow")
            return
        self.submit_order(self.create_order(symbol, quantity, "sell"))
        self.pending_orders[symbol] = "sell"
        self.log_message(f"SELL {quantity:g} {symbol}: {reason}", color="red")

    def _pending_orders(self) -> dict[str, str]:
        """Symbol -> side ("buy"/"sell") of orders submitted but not yet filled or cancelled."""
        pending = {}
        for order in self.get_orders(statuses=Order.ACTIVE_STATUSES):
            pending[order.asset.symbol] = "buy" if order.is_buy_order() else "sell"
        return pending

    def _start_new_day(self) -> None:
        self.trading_day = self.get_datetime().date()
        self.day_start_value = self.last_close_value or float(self.get_portfolio_value())
        self.halted_for_day = self.halt_next_day
        self.halt_next_day = False
        self.alerts_sent.clear()
        if self.halted_for_day:
            self.log_message("Daily loss limit hit at the last close: no new buys this session", color="red")

    def _check_daily_loss(self, at_close: bool = False) -> None:
        limit = float(self.parameters["max_daily_loss_pct"])
        if limit <= 0 or self.day_start_value <= 0 or (self.halted_for_day and not at_close):
            return
        loss = 1 - float(self.get_portfolio_value()) / self.day_start_value
        if loss < limit:
            return
        if at_close:
            self.halt_next_day = True
            self.log_message(f"Daily loss {loss:.2%} hit limit {limit:.2%}: no new buys next session", color="red")
        else:
            self.halted_for_day = True
            self.log_message(f"Daily loss {loss:.2%} hit limit {limit:.2%}: no new buys today", color="red")
        if self.parameters["liquidate_on_daily_loss"]:
            self.sell_all()

    def _price_alert(self, symbol: str, signal: SmaSignal) -> None:
        last = self.get_last_price(symbol)
        last = float(last) if last else signal.price
        reference = signal.previous_close if last == signal.price else signal.price
        move = (last / reference - 1) * 100 if reference else 0.0
        crossed = [t for t in sorted(self.parameters["price_alert_pcts"]) if abs(move) >= t]
        if crossed and (symbol, crossed[-1]) not in self.alerts_sent:
            self.alerts_sent.add((symbol, crossed[-1]))
            direction = "UP" if move > 0 else "DOWN"
            self.log_message(f"ALERT {symbol} {direction} {move:+.1f}% today (> {crossed[-1]:g}%)", color="blue")
