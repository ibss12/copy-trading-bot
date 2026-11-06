"""The bot's buy/sell rules as plain functions, shared by the Lumibot strategies and the scheduled check."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

RSI_PERIOD = 14


def rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """Wilder's RSI of the latest close (oldest first). None without `period + 1` closes."""
    if period <= 0 or len(closes) < period + 1:
        return None
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(c, 0.0) for c in changes[:period]) / period
    loss = sum(max(-c, 0.0) for c in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


@dataclass(frozen=True)
class SmaSignal:
    price: float
    previous_close: float
    short_sma: float
    long_sma: float
    crossed_up: bool
    crossed_down: bool
    rsi: float | None = None

    @property
    def trend_up(self) -> bool:
        return self.short_sma > self.long_sma

    @classmethod
    def from_closes(cls, closes: list[float], short_window: int, long_window: int) -> "SmaSignal | None":
        """Signal from daily closes (oldest first, latest last). None without enough history."""
        if len(closes) < long_window + 1:
            return None

        def sma(window: int, end: int) -> float:
            return sum(closes[end - window : end]) / window

        n = len(closes)
        short_now, long_now = sma(short_window, n), sma(long_window, n)
        short_prev, long_prev = sma(short_window, n - 1), sma(long_window, n - 1)
        return cls(
            price=float(closes[-1]),
            previous_close=float(closes[-2]),
            short_sma=short_now,
            long_sma=long_now,
            crossed_up=short_prev <= long_prev and short_now > long_now,
            crossed_down=short_prev >= long_prev and short_now < long_now,
            rsi=rsi([float(c) for c in closes]),
        )


@dataclass(frozen=True)
class Decision:
    action: Literal["buy", "sell"]
    reason: str


@dataclass(frozen=True)
class PositionState:
    """What the bot paid for a holding and the highest price seen since."""

    entry_price: float
    peak_price: float

    def update(self, price: float) -> "PositionState":
        return PositionState(self.entry_price, max(self.peak_price, price))


@dataclass(frozen=True)
class TradeRules:
    """Exit stack and entry filters. A pct of None (or 0) turns that rule off."""

    stop_loss_pct: float | None = 0.10
    trailing_stop_pct: float | None = 0.12
    take_profit_pct: float | None = 0.30
    exit_below_short_sma: bool = True
    require_price_above_long_sma: bool = False
    rsi_max_for_entry: float | None = None

    @classmethod
    def from_parameters(cls, parameters: dict) -> "TradeRules":
        defaults = cls()
        return cls(**{name: parameters.get(name, getattr(defaults, name)) for name in cls.__dataclass_fields__})


NO_RULES = TradeRules(None, None, None, False, False, None)


def exit_decision(signal: SmaSignal, position: PositionState | None, rules: TradeRules) -> Decision | None:
    """Stop-loss, trailing stop, take-profit and below-short-SMA exits for a held stock."""
    price = signal.price
    if position is not None:
        entry, peak = position.entry_price, max(position.peak_price, price)
        if rules.stop_loss_pct and entry > 0 and price <= entry * (1 - rules.stop_loss_pct):
            return Decision(
                "sell",
                f"stop-loss: {price:.2f} is {price / entry - 1:+.1%} from entry {entry:.2f} "
                f"(limit -{rules.stop_loss_pct:.0%})",
            )
        if rules.trailing_stop_pct and peak > 0 and price <= peak * (1 - rules.trailing_stop_pct):
            return Decision(
                "sell",
                f"trailing stop: {price:.2f} is {price / peak - 1:+.1%} from peak {peak:.2f} "
                f"(limit -{rules.trailing_stop_pct:.0%})",
            )
        if rules.take_profit_pct and entry > 0 and price >= entry * (1 + rules.take_profit_pct):
            return Decision(
                "sell",
                f"take-profit: {price:.2f} is {price / entry - 1:+.1%} from entry {entry:.2f} "
                f"(target +{rules.take_profit_pct:.0%})",
            )
    if rules.exit_below_short_sma and price < signal.short_sma:
        return Decision("sell", f"below short SMA: {price:.2f} < {signal.short_sma:.2f}")
    return None


def entry_blocked(signal: SmaSignal, rules: TradeRules) -> str | None:
    """Why a buy is not allowed right now, or None if the entry filters pass."""
    if rules.require_price_above_long_sma and signal.price <= signal.long_sma:
        return f"price {signal.price:.2f} not above long SMA {signal.long_sma:.2f}"
    if rules.rsi_max_for_entry is not None and signal.rsi is not None and signal.rsi > rules.rsi_max_for_entry:
        return f"RSI {signal.rsi:.0f} above {rules.rsi_max_for_entry:g}"
    return None


def _buy(signal: SmaSignal, rules: TradeRules, reason: str) -> Decision | None:
    return None if entry_blocked(signal, rules) else Decision("buy", reason)


def sma_decision(
    signal: SmaSignal,
    held: bool,
    on_watchlist: bool,
    position: PositionState | None = None,
    rules: TradeRules = NO_RULES,
) -> Decision | None:
    if held:
        if exit := exit_decision(signal, position, rules):
            return exit
        if signal.crossed_down:
            return Decision("sell", f"short SMA {signal.short_sma:.2f} crossed below long SMA {signal.long_sma:.2f}")
    elif signal.crossed_up and on_watchlist:
        return _buy(signal, rules, f"short SMA {signal.short_sma:.2f} crossed above long SMA {signal.long_sma:.2f}")
    return None


def smart_money_decision(
    signal: SmaSignal,
    held: bool,
    on_watchlist: bool,
    score: int,
    require_sma_confirmation: bool,
    who: Callable[[str], str],
    position: PositionState | None = None,
    rules: TradeRules = NO_RULES,
) -> Decision | None:
    """`score`: net buys minus sells by followed traders; `who("buy"|"sell")` names them."""
    if held:
        if exit := exit_decision(signal, position, rules):
            return exit
        if signal.crossed_down:
            return Decision("sell", "SMA bearish crossover")
        if score < 0:
            return Decision("sell", f"followed traders selling: {who('sell')}")
        return None
    if score > 0 and (signal.trend_up or not require_sma_confirmation):
        return _buy(signal, rules, f"followed traders buying: {who('buy')}")
    if signal.crossed_up and on_watchlist and score >= 0:
        return _buy(signal, rules, "SMA bullish crossover")
    return None
