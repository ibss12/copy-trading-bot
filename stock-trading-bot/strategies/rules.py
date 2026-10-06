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


def sma_decision(signal: SmaSignal, held: bool, on_watchlist: bool) -> Decision | None:
    if held:
        if signal.crossed_down:
            return Decision("sell", f"short SMA {signal.short_sma:.2f} crossed below long SMA {signal.long_sma:.2f}")
    elif signal.crossed_up and on_watchlist:
        return Decision("buy", f"short SMA {signal.short_sma:.2f} crossed above long SMA {signal.long_sma:.2f}")
    return None


def smart_money_decision(
    signal: SmaSignal,
    held: bool,
    on_watchlist: bool,
    score: int,
    require_sma_confirmation: bool,
    who: Callable[[str], str],
) -> Decision | None:
    """`score`: net buys minus sells by followed traders; `who("buy"|"sell")` names them."""
    if held:
        if signal.crossed_down:
            return Decision("sell", "SMA bearish crossover")
        if score < 0:
            return Decision("sell", f"followed traders selling: {who('sell')}")
        return None
    if score > 0 and (signal.trend_up or not require_sma_confirmation):
        return Decision("buy", f"followed traders buying: {who('buy')}")
    if signal.crossed_up and on_watchlist and score >= 0:
        return Decision("buy", "SMA bullish crossover")
    return None
