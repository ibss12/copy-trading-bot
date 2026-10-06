"""SMA crossover + copy the publicly disclosed trades of people you follow."""

from datetime import date, timedelta

from signals import SignalFeed, load_signal_feed
from strategies.rules import smart_money_decision
from strategies.sma_crossover import SmaCrossover, SmaSignal


class SmartMoneySma(SmaCrossover):
    """Adds "smart money" signals (13F funds, Form 4 insiders/CEOs, Congress) on top of SMA.

    * Buy when people you follow are net buyers of a stock (optionally only if the SMA trend
      agrees), or on a plain SMA bullish cross of a watchlist stock nobody is selling.
    * Sell a holding on an SMA bearish cross or when people you follow are net sellers.
    """

    parameters = {
        **SmaCrossover.parameters,
        "signal_lookback_days": 45,
        "follow_expands_watchlist": True,
        "require_sma_confirmation": True,
        "signal_history_start": None,
    }

    def initialize(self):
        super().initialize()
        self.lookback_days = int(self.parameters["signal_lookback_days"])
        history_start = self.parameters["signal_history_start"]
        since = (
            date.fromisoformat(history_start)
            if history_start
            else self.get_datetime().date() - timedelta(days=self.lookback_days)
        )
        self._load_feed(since)
        self.scores: dict[str, int] = {}

    def before_market_opens(self):
        super().before_market_opens()
        if not self.is_backtesting:
            self._load_feed(self.get_datetime().date() - timedelta(days=self.lookback_days))

    def on_trading_iteration(self):
        self.scores = self.feed.net_scores(self.get_datetime().date(), self.lookback_days)
        super().on_trading_iteration()

    def universe(self) -> list[str]:
        symbols = super().universe()
        if self.parameters["follow_expands_watchlist"]:
            symbols += [s for s, score in self.scores.items() if score > 0]
        return list(dict.fromkeys(symbols))

    def decide(self, symbol: str, signal: SmaSignal) -> None:
        decision = smart_money_decision(
            signal,
            held=self.held_quantity(symbol) > 0,
            on_watchlist=symbol in self.symbols,
            score=self.scores.get(symbol, 0),
            require_sma_confirmation=bool(self.parameters["require_sma_confirmation"]),
            who=lambda action: self._who(symbol, action),
        )
        self.act(symbol, signal, decision)

    def _load_feed(self, since: date) -> None:
        self.log_message(f"Loading smart-money disclosures since {since}...")
        self.feed: SignalFeed = load_signal_feed(self.symbols, since)
        self.log_message(f"Loaded {len(self.feed.signals)} smart-money signals")

    def _who(self, symbol: str, action: str) -> str:
        reasons = self.feed.reasons(symbol, self.get_datetime().date(), self.lookback_days)
        return "; ".join(f"{s.trader} {s.detail}" for s in reasons if s.action == action)
