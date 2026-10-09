from dataclasses import dataclass
from datetime import date
from typing import Literal

Action = Literal["buy", "sell"]


@dataclass(frozen=True)
class TraderSignal:
    """One disclosed buy/sell by someone we follow.

    `disclosed_on` is when the trade became public; strategies must only act on a
    signal once the (backtest) clock has reached this date to avoid look-ahead bias.
    `notify_only` signals are shown and announced but never affect trading.
    `reason` is extra context for people (not part of `describe()`, which keys deduplication).
    """

    trader: str
    source: str
    symbol: str
    action: Action
    disclosed_on: date
    traded_on: date | None
    detail: str
    notify_only: bool = False
    reason: str = ""

    def describe(self) -> str:
        traded = f"traded {self.traded_on}, " if self.traded_on else ""
        return (
            f"{self.trader} {self.action.upper()} {self.symbol} "
            f"({self.source}; {traded}disclosed {self.disclosed_on}) - {self.detail}"
        )
