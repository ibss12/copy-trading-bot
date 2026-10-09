import logging
from collections import defaultdict
from datetime import date, timedelta

import config
from signals.bargo import bargo_congress_signals
from signals.models import TraderSignal
from signals.quiver import congress_signals
from signals.sec_edgar import SecEdgarClient

logger = logging.getLogger(__name__)

MAX_FORM4_FILINGS_PER_FILER = 150


class SignalFeed:
    """Time-indexed collection of disclosed trades by the people you follow."""

    def __init__(self, signals: list[TraderSignal]):
        self.signals = sorted(signals, key=lambda s: s.disclosed_on)

    def active(self, as_of: date, lookback_days: int) -> list[TraderSignal]:
        """Signals already public on `as_of` and disclosed within the last `lookback_days`."""
        start = as_of - timedelta(days=lookback_days)
        return [s for s in self.signals if start < s.disclosed_on <= as_of]

    def net_scores(self, as_of: date, lookback_days: int) -> dict[str, int]:
        """Per symbol: (# people whose latest move is a buy) - (# whose latest move is a sell)."""
        latest: dict[tuple[str, str], TraderSignal] = {}
        for signal in self.tradable(as_of, lookback_days):
            latest[(signal.trader, signal.symbol)] = signal
        scores: dict[str, int] = defaultdict(int)
        for signal in latest.values():
            scores[signal.symbol] += 1 if signal.action == "buy" else -1
        return dict(scores)

    def tradable(self, as_of: date, lookback_days: int) -> list[TraderSignal]:
        return [s for s in self.active(as_of, lookback_days) if not s.notify_only]

    def reasons(self, symbol: str, as_of: date, lookback_days: int) -> list[TraderSignal]:
        return [s for s in self.tradable(as_of, lookback_days) if s.symbol == symbol]


def _collect(name: str, fetch) -> list[TraderSignal]:
    try:
        signals = fetch()
    except Exception as exc:  # one broken/unreachable source must not stop the bot
        logger.warning("Smart-money source '%s' failed: %s", name, exc)
        return []
    logger.info("Smart-money source '%s': %d signals", name, len(signals))
    return signals


def load_signal_feed(watchlist: list[str], since: date) -> SignalFeed:
    """Fetch every configured source (SEC 13F, SEC Form 4, Congress) from `since` onward."""
    sec = SecEdgarClient(config.CACHE_DIR, config.SEC_USER_AGENT, config.OPENFIGI_API_KEY)
    signals: list[TraderSignal] = []

    for fund in config.FOLLOW_FUNDS:
        signals += _collect(
            fund.label,
            lambda fund=fund: sec.thirteenf_signals(fund.cik, fund.label, since),
        )

    for insider in config.FOLLOW_INSIDERS:
        signals += _collect(
            insider.label,
            lambda insider=insider: sec.form4_signals(
                insider.cik,
                insider.label,
                since,
                MAX_FORM4_FILINGS_PER_FILER,
                ignore_planned_sales=config.IGNORE_PLANNED_SALES,
            ),
        )

    if config.TRACK_CEO_TRADES:
        issuer_ciks = _collect_issuer_ciks(sec)
        for symbol in watchlist:
            cik = issuer_ciks.get(symbol)
            if cik is None:
                continue
            signals += _collect(
                f"{symbol} CEO",
                lambda cik=cik: sec.form4_signals(
                    cik,
                    None,
                    since,
                    MAX_FORM4_FILINGS_PER_FILER,
                    officer_titles=config.CEO_TITLES,
                    ignore_planned_sales=config.IGNORE_PLANNED_SALES,
                    other_insiders_notify_only=config.NOTIFY_OTHER_INSIDERS,
                ),
            )

    if config.FOLLOW_POLITICIANS:
        if config.QUIVER_API_KEY:
            signals += _collect(
                "Congress",
                lambda: congress_signals(config.CACHE_DIR, config.QUIVER_API_KEY, config.FOLLOW_POLITICIANS, since),
            )
        else:
            signals += _collect(
                "Congress",
                lambda: bargo_congress_signals(
                    config.CACHE_DIR, config.FOLLOW_POLITICIANS, since, config.BARGO_API_KEY
                ),
            )

    return SignalFeed(signals)


def _collect_issuer_ciks(sec: SecEdgarClient) -> dict[str, int]:
    try:
        return sec.issuer_ciks()
    except Exception as exc:
        logger.warning("Could not load SEC ticker->CIK map, skipping CEO trades: %s", exc)
        return {}
