"""Live quotes: Yahoo Finance streaming websocket, with periodic REST refresh as a safety net."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pandas_market_calendars as mcal
import yfinance as yf

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
INDEXES = ["SPY", "QQQ", "DIA"]
SESSIONS = {0: "pre", 1: "regular", 2: "post", 3: "closed"}
REST_REFRESH_SECONDS = 60
DAILY_REFRESH_SECONDS = 30 * 60
STALE_SECONDS = 45


@dataclass
class Quote:
    symbol: str
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    volume: int = 0
    session: str = "closed"
    updated: float = 0.0
    minutes: dict[int, float] = field(default_factory=dict)
    daily_closes: list[float] = field(default_factory=list)
    daily_dates: list[str] = field(default_factory=list)

    @property
    def change(self) -> float:
        return self.price - self.prev_close if self.prev_close else 0.0

    @property
    def change_pct(self) -> float:
        return self.change / self.prev_close * 100 if self.prev_close else 0.0

    def closes_with_live(self) -> list[float]:
        """Daily closes where today's (partial) bar is replaced by the live price."""
        closes = list(self.daily_closes)
        today = datetime.now(NY).date().isoformat()
        if not closes or not self.price:
            return closes
        if self.daily_dates and self.daily_dates[-1] == today:
            closes[-1] = self.price
        elif market_status()["is_open"]:
            closes.append(self.price)
        return closes

    def sma(self, window: int) -> float | None:
        closes = self.closes_with_live()
        if len(closes) < window:
            return None
        return sum(closes[-window:]) / window

    def spark(self, points: int = 78) -> list[float]:
        prices = [self.minutes[k] for k in sorted(self.minutes)]
        if len(prices) <= points:
            return [round(p, 4) for p in prices]
        step = len(prices) / points
        return [round(prices[int(i * step)], 4) for i in range(points - 1)] + [round(prices[-1], 4)]

    def to_dict(self, short_window: int, long_window: int) -> dict:
        short_sma, long_sma = self.sma(short_window), self.sma(long_window)
        trend = None
        if short_sma is not None and long_sma is not None:
            trend = "up" if short_sma > long_sma else "down"
        return {
            "symbol": self.symbol,
            "price": round(self.price, 4),
            "prev_close": round(self.prev_close, 4),
            "change": round(self.change, 4),
            "change_pct": round(self.change_pct, 3),
            "open": round(self.open, 4),
            "day_high": round(self.day_high, 4),
            "day_low": round(self.day_low, 4),
            "volume": self.volume,
            "session": self.session,
            "updated": int(self.updated * 1000),
            "sma_short": round(short_sma, 4) if short_sma else None,
            "sma_long": round(long_sma, 4) if long_sma else None,
            "trend": trend,
            "spark": self.spark(),
        }


_calendar = mcal.get_calendar("NYSE")


def market_status() -> dict:
    now = pd.Timestamp.now(tz="UTC")
    local = now.tz_convert(NY)
    schedule = _calendar.schedule(
        start_date=local.date() - timedelta(days=1), end_date=local.date() + timedelta(days=10)
    )
    today = schedule[schedule.index.date == local.date()]
    market_open = today.iloc[0].market_open if len(today) else None
    market_close = today.iloc[0].market_close if len(today) else None
    is_open = market_open is not None and market_open <= now < market_close
    upcoming = schedule[schedule.market_open > now]
    next_open = upcoming.iloc[0].market_open if len(upcoming) else None
    if is_open:
        session = "regular"
    elif market_open is not None and now < market_open and local.hour >= 4:
        session = "pre"
    elif market_close is not None and now >= market_close and local.hour < 20:
        session = "post"
    else:
        session = "closed"
    return {
        "is_open": bool(is_open),
        "session": session,
        "next_open": next_open.isoformat() if next_open is not None else None,
        "next_close": market_close.isoformat() if is_open else None,
        "now": now.isoformat(),
    }


def _frame(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        if symbol not in df.columns.get_level_values(0):
            return pd.DataFrame()
        df = df[symbol]
    return df.dropna(how="all")


def _download(symbols: list[str], **kwargs) -> pd.DataFrame:
    return yf.download(symbols, group_by="ticker", progress=False, threads=True, auto_adjust=False, **kwargs)


class LiveMarket:
    """Keeps an in-memory `Quote` per symbol up to date and notifies a callback on every change."""

    def __init__(self, symbols: list[str], on_update: Callable[[str], None]):
        self.quotes: dict[str, Quote] = {}
        self.on_update = on_update
        self.stream_connected = False
        self.last_tick = 0.0
        self._ws: yf.AsyncWebSocket | None = None
        self._tasks: list[asyncio.Task] = []
        self._pending_symbols = [s.upper() for s in dict.fromkeys(symbols + INDEXES)]

    @property
    def symbols(self) -> list[str]:
        return list(self.quotes)

    async def start(self) -> None:
        await self.add_symbols(self._pending_symbols)
        self._tasks = [
            asyncio.create_task(self._stream_loop()),
            asyncio.create_task(self._rest_loop()),
            asyncio.create_task(self._daily_loop()),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def add_symbols(self, symbols: list[str]) -> list[str]:
        new = [s.upper() for s in symbols if s.upper() not in self.quotes]
        if not new:
            return []
        for symbol in new:
            self.quotes[symbol] = Quote(symbol)
        await asyncio.to_thread(self._load_daily, new)
        await asyncio.to_thread(self._load_intraday, new)
        valid = [s for s in new if self.quotes[s].daily_closes]
        for symbol in set(new) - set(valid):
            logger.warning("No Yahoo data for %s - dropping it", symbol)
            del self.quotes[symbol]
        if self._ws is not None and valid:
            try:
                await self._ws.subscribe(valid)
            except Exception as exc:
                logger.warning("Websocket subscribe failed: %s", exc)
        for symbol in valid:
            self.on_update(symbol)
        return valid

    async def remove_symbol(self, symbol: str) -> None:
        if symbol in INDEXES:
            return
        self.quotes.pop(symbol, None)
        if self._ws is not None:
            try:
                await self._ws.unsubscribe([symbol])
            except Exception:
                pass

    # --- data loading ---------------------------------------------------------

    def _load_daily(self, symbols: list[str]) -> None:
        df = _download(symbols, period="1y", interval="1d")
        today = datetime.now(NY).date()
        for symbol in symbols:
            frame = _frame(df, symbol)
            quote = self.quotes.get(symbol)
            if frame.empty or quote is None:
                continue
            closes = frame["Close"].dropna()
            quote.daily_closes = [float(c) for c in closes]
            quote.daily_dates = [ts.date().isoformat() for ts in closes.index]
            before_today = closes[[ts.date() < today for ts in closes.index]]
            if len(before_today):
                quote.prev_close = float(before_today.iloc[-1])
            if not quote.price:
                quote.price = float(closes.iloc[-1])
                quote.updated = time.time()
            last = frame.iloc[-1]
            if frame.index[-1].date() == today:
                quote.open = float(last["Open"])
                quote.day_high = max(quote.day_high, float(last["High"]))
                quote.day_low = float(last["Low"]) if not quote.day_low else min(quote.day_low, float(last["Low"]))
                quote.volume = max(quote.volume, int(last["Volume"] or 0))

    def _load_intraday(self, symbols: list[str]) -> None:
        df = _download(symbols, period="1d", interval="1m", prepost=False)
        now = time.time()
        for symbol in symbols:
            frame = _frame(df, symbol)
            quote = self.quotes.get(symbol)
            if frame.empty or quote is None:
                continue
            closes = frame["Close"].dropna()
            quote.minutes = {int(ts.timestamp() // 60): float(c) for ts, c in closes.items()}
            if frame.index[-1].tz_convert(NY).date() != datetime.now(NY).date():
                continue
            if now - quote.updated > STALE_SECONDS:
                quote.price = float(closes.iloc[-1])
                quote.updated = now
            quote.open = float(frame["Open"].dropna().iloc[0])
            quote.day_high = max(quote.day_high, float(frame["High"].max()))
            low = float(frame["Low"].min())
            quote.day_low = low if not quote.day_low else min(quote.day_low, low)

    # --- background loops -----------------------------------------------------

    async def _stream_loop(self) -> None:
        while True:
            try:
                self._ws = yf.AsyncWebSocket(verbose=False)
                await self._ws.subscribe(self.symbols)
                self.stream_connected = True
                await self._ws.listen(self._on_message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Live stream dropped (%s); reconnecting in 5s", exc)
            self.stream_connected = False
            await asyncio.sleep(5)

    def _on_message(self, message: dict) -> None:
        symbol = message.get("id")
        quote = self.quotes.get(symbol)
        price = message.get("price")
        if quote is None or not price:
            return
        now = time.time()
        quote.price = float(price)
        quote.updated = now
        quote.session = SESSIONS.get(int(message.get("market_hours", 3)), "closed")
        if message.get("day_volume"):
            quote.volume = int(message["day_volume"])
        if message.get("day_high"):
            quote.day_high = float(message["day_high"])
        if message.get("day_low"):
            quote.day_low = float(message["day_low"])
        if quote.session == "regular":
            quote.minutes[int(now // 60)] = quote.price
            quote.day_high = max(quote.day_high, quote.price)
            quote.day_low = min(quote.day_low, quote.price) if quote.day_low else quote.price
        self.last_tick = now
        self.on_update(symbol)

    async def _rest_loop(self) -> None:
        while True:
            await asyncio.sleep(REST_REFRESH_SECONDS)
            try:
                await asyncio.to_thread(self._load_intraday, self.symbols)
                status = market_status()
                for quote in self.quotes.values():
                    if time.time() - quote.updated > STALE_SECONDS:
                        quote.session = status["session"]
                    self.on_update(quote.symbol)
            except Exception as exc:
                logger.warning("Quote refresh failed: %s", exc)

    async def _daily_loop(self) -> None:
        while True:
            await asyncio.sleep(DAILY_REFRESH_SECONDS)
            try:
                await asyncio.to_thread(self._load_daily, self.symbols)
            except Exception as exc:
                logger.warning("Daily history refresh failed: %s", exc)


def chart_data(symbol: str, range_key: str, short_window: int, long_window: int) -> dict:
    """Price series for the chart panel. Daily ranges include the SMA lines the bot trades on."""
    intraday = {"1D": ("1d", "1m"), "5D": ("5d", "5m"), "1M": ("1mo", "30m")}
    daily_days = {"6M": 183, "1Y": 365, "2Y": 730}
    if range_key in intraday:
        period, interval = intraday[range_key]
        frame = _frame(_download([symbol], period=period, interval=interval), symbol)
        closes = frame["Close"].dropna() if not frame.empty else pd.Series(dtype=float)
        return {
            "symbol": symbol,
            "range": range_key,
            "intraday": True,
            "t": [int(ts.timestamp() * 1000) for ts in closes.index],
            "c": [round(float(c), 4) for c in closes],
        }
    if range_key not in daily_days:
        raise ValueError(f"Unknown range {range_key}")
    frame = _frame(_download([symbol], period="5y", interval="1d"), symbol)
    closes = frame["Close"].dropna() if not frame.empty else pd.Series(dtype=float)
    short_sma = closes.rolling(short_window).mean()
    long_sma = closes.rolling(long_window).mean()
    cutoff = pd.Timestamp(datetime.now(NY).date() - timedelta(days=daily_days[range_key]))
    keep = [ts.tz_localize(None) >= cutoff if ts.tzinfo else ts >= cutoff for ts in closes.index]

    def series(values: pd.Series) -> list[float | None]:
        return [None if pd.isna(v) else round(float(v), 4) for v in values[keep]]

    return {
        "symbol": symbol,
        "range": range_key,
        "intraday": False,
        "t": [int(pd.Timestamp(ts).timestamp() * 1000) for ts in closes.index[keep]],
        "c": series(closes),
        "sma_short": series(short_sma),
        "sma_long": series(long_sma),
        "short_window": short_window,
        "long_window": long_window,
    }
