"""The bot's buy/sell rules as plain functions, shared by the Lumibot strategies and the scheduled check."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


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
        )


@dataclass(frozen=True)
class Decision:
    action: Literal["buy", "sell"]
    reason: str


@dataclass(frozen=True)
class RiskRules:
    """Exit rules for held positions (checked before the SMA cross) plus an optional entry filter.

    A pct of 0 turns that exit off.
    """

    stop_loss_pct: float = 0.08
    trailing_stop_pct: float = 0.10
    take_profit_pct: float = 0.20
    exit_below_short_sma: bool = True
    require_price_above_long_sma: bool = False

    def allows_entry(self, signal: SmaSignal) -> bool:
        """False when a buy would break the entry filter or be sold again at once by the short-SMA exit."""
        if self.require_price_above_long_sma and signal.price <= signal.long_sma:
            return False
        return not (self.exit_below_short_sma and signal.price < signal.short_sma)


@dataclass(frozen=True)
class Holding:
    """What the bot paid for a position (average entry) and the highest price seen since."""

    entry_price: float
    peak_price: float


def exit_decision(signal: SmaSignal, holding: Holding | None, rules: RiskRules) -> Decision | None:
    """Sell decision for a held position from stop-loss / trailing stop / take-profit / short SMA, else None.

    Without a known `holding` (entry/peak) only the short-SMA exit can fire.
    """
    price = signal.price
    if holding is not None and holding.entry_price > 0:
        entry = holding.entry_price
        change = price / entry - 1
        if rules.stop_loss_pct > 0 and price <= entry * (1 - rules.stop_loss_pct):
            return Decision(
                "sell",
                f"stop-loss: price {price:.2f} is {change:+.1%} vs entry {entry:.2f} (limit -{rules.stop_loss_pct:.0%})",
            )
        peak = holding.peak_price
        if rules.trailing_stop_pct > 0 and peak > 0 and price <= peak * (1 - rules.trailing_stop_pct):
            return Decision(
                "sell",
                f"trailing stop: price {price:.2f} is {price / peak - 1:+.1%} from peak {peak:.2f} "
                f"(limit -{rules.trailing_stop_pct:.0%})",
            )
        if rules.take_profit_pct > 0 and price >= entry * (1 + rules.take_profit_pct):
            return Decision(
                "sell",
                f"take-profit: price {price:.2f} is {change:+.1%} vs entry {entry:.2f} "
                f"(target +{rules.take_profit_pct:.0%})",
            )
    if rules.exit_below_short_sma and price < signal.short_sma:
        return Decision("sell", f"price {price:.2f} fell below short SMA {signal.short_sma:.2f}")
    return None


def sma_decision(
    signal: SmaSignal,
    held: bool,
    on_watchlist: bool,
    *,
    rules: RiskRules,
    holding: Holding | None = None,
) -> Decision | None:
    if held:
        exit_ = exit_decision(signal, holding, rules)
        if exit_:
            return exit_
        if signal.crossed_down:
            return Decision("sell", f"short SMA {signal.short_sma:.2f} crossed below long SMA {signal.long_sma:.2f}")
    elif signal.crossed_up and on_watchlist and rules.allows_entry(signal):
        return Decision("buy", f"short SMA {signal.short_sma:.2f} crossed above long SMA {signal.long_sma:.2f}")
    return None


def smart_money_decision(
    signal: SmaSignal,
    held: bool,
    on_watchlist: bool,
    score: int,
    require_sma_confirmation: bool,
    who: Callable[[str], str],
    *,
    rules: RiskRules,
    holding: Holding | None = None,
) -> Decision | None:
    """`score`: net buys minus sells by followed traders; `who("buy"|"sell")` names them."""
    if held:
        exit_ = exit_decision(signal, holding, rules)
        if exit_:
            return exit_
        if signal.crossed_down:
            return Decision("sell", "SMA bearish crossover")
        if score < 0:
            return Decision("sell", f"followed traders selling: {who('sell')}")
        return None
    if not rules.allows_entry(signal):
        return None
    if score > 0 and (signal.trend_up or not require_sma_confirmation):
        return Decision("buy", f"followed traders buying: {who('buy')}")
    if signal.crossed_up and on_watchlist and score >= 0:
        return Decision("buy", "SMA bullish crossover")
    return None
