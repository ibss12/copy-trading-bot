"""FastAPI app behind the browser command center: live quotes + alerts over Server-Sent Events."""

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from dashboard.account import PaperAccount
from dashboard.advice import advise
from dashboard.alerts import AlertEngine
from dashboard.bot import BotRunner
from dashboard.market import INDEXES, LiveMarket, chart_data, market_status
from dashboard.settings import Settings
from dashboard.trading212_account import Trading212Account
from signals import load_signal_feed

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SYMBOL_RE = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")
FIRST_LOAD_ALERT_DAYS = 3
DISPLAY_DAYS = 180


class Hub:
    """Owns all live components and fans events out to every connected browser tab."""

    def __init__(self):
        self.settings = Settings()
        self.subscribers: set[asyncio.Queue] = set()
        self.dirty: set[str] = set()
        self.market = LiveMarket(self.settings["watchlist"], self.dirty.add)
        self.account = (
            Trading212Account()
            if config.BROKER == "trading212"
            else PaperAccount(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
        )
        self.alerts = AlertEngine(
            self.settings, lambda a: self.publish("alert", a), config.SMA_SHORT_WINDOW, config.SMA_LONG_WINDOW
        )
        self.bot = BotRunner(self._on_bot_line, lambda: self.publish("bot", self.bot.status()))
        self.account_state: dict = {
            "enabled": self.account.enabled,
            "provider": self.account.provider,
            "label": self.account.label,
            "error": self.account.error,
        }
        self.advice_armed = False
        self.signals_state: dict = {"loading": True, "signals": [], "scores": {}, "updated": None, "error": None}
        self._tasks: list[asyncio.Task] = []
        self._chart_cache: dict[tuple[str, str], tuple[float, dict]] = {}

    async def start(self) -> None:
        await self.market.start()
        self._tasks = [
            asyncio.create_task(self._flush_loop()),
            asyncio.create_task(self._account_loop()),
            asyncio.create_task(self._status_loop()),
            asyncio.create_task(self._signals_loop()),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.bot.stop()
        await self.market.stop()

    def publish(self, event: str, data) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait((event, data))
            except asyncio.QueueFull:
                pass

    def quote_dict(self, symbol: str) -> dict:
        data = self.market.quotes[symbol].to_dict(config.SMA_SHORT_WINDOW, config.SMA_LONG_WINDOW)
        data["advice"] = self.advice(symbol)
        return data

    def advice(self, symbol: str) -> dict | None:
        """BUY/SELL/HOLD/WATCH for a stock. None until the big-trader filings have loaded."""
        quote = self.market.quotes.get(symbol)
        if quote is None or self.signals_state.get("updated") is None:
            return None
        cutoff = (date.today() - timedelta(days=config.SIGNAL_LOOKBACK_DAYS)).isoformat()
        traders = [
            s for s in self.signals_state.get("signals", []) if s["symbol"] == symbol and s["disclosed_on"] >= cutoff
        ]
        held = sum(p["qty"] for p in self.account_state.get("positions") or [] if p["symbol"] == symbol)
        return advise(
            symbol,
            quote.closes_with_live(),
            config.SMA_SHORT_WINDOW,
            config.SMA_LONG_WINDOW,
            int(self.signals_state.get("scores", {}).get(symbol, 0)),
            traders,
            held,
        )

    def state(self) -> dict:
        return {
            "watchlist": [s for s in self.settings["watchlist"] if s in self.market.quotes],
            "indexes": INDEXES,
            "quotes": {s: self.quote_dict(s) for s in self.market.quotes},
            "market": self.market_info(),
            "account": self.account_state,
            "signals": self.signals_state,
            "alerts": list(self.alerts.history),
            "bot": self.bot.status(),
            "bot_log": list(self.bot.lines),
            "settings": self.settings.data,
            "config": {
                "strategy": config.STRATEGY,
                "sma_short": config.SMA_SHORT_WINDOW,
                "sma_long": config.SMA_LONG_WINDOW,
                "max_position_pct": config.MAX_POSITION_PCT,
                "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
                "broker": config.BROKER,
                "broker_label": self.account.label,
                "quiver_enabled": bool(config.QUIVER_API_KEY),
                "follows": [f.label for f in config.FOLLOW_FUNDS + config.FOLLOW_INSIDERS]
                + (config.FOLLOW_POLITICIANS if config.QUIVER_API_KEY else []),
            },
        }

    def market_info(self) -> dict:
        return {**market_status(), "stream_connected": self.market.stream_connected}

    async def chart(self, symbol: str, range_key: str) -> dict:
        key = (symbol, range_key)
        loop = asyncio.get_running_loop()
        cached = self._chart_cache.get(key)
        if cached and loop.time() - cached[0] < 30:
            return cached[1]
        data = await asyncio.to_thread(chart_data, symbol, range_key, config.SMA_SHORT_WINDOW, config.SMA_LONG_WINDOW)
        self._chart_cache[key] = (loop.time(), data)
        return data

    def _on_bot_line(self, line: str) -> None:
        self.alerts.check_bot_line(line)
        self.publish("bot_log", line)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            if not self.dirty:
                continue
            symbols = [s for s in self.dirty if s in self.market.quotes]
            self.dirty.clear()
            for symbol in symbols:
                self.alerts.check_quote(self.market.quotes[symbol])
            quotes = [self.quote_dict(s) for s in symbols]
            if self.advice_armed:
                for q in quotes:
                    if q["advice"] and q["symbol"] in self.settings["watchlist"]:
                        self.alerts.check_advice(q["advice"])
            self.publish("quotes", quotes)

    async def _account_loop(self) -> None:
        if not self.account.enabled:
            return
        while True:
            self.account_state = await asyncio.to_thread(self.account.snapshot)
            self.alerts.check_account(self.account_state)
            self.publish("account", self.account_state)
            await asyncio.sleep(self.account.refresh_seconds)

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            self.publish("market", self.market_info())

    async def _signals_loop(self) -> None:
        first = True
        while True:
            await self.refresh_signals(first)
            first = False
            await asyncio.sleep(max(5, int(self.settings["signal_refresh_minutes"])) * 60)

    async def refresh_signals(self, first: bool = False) -> None:
        self.signals_state = {**self.signals_state, "loading": True}
        self.publish("signals", self.signals_state)
        today = date.today()
        lookback = config.SIGNAL_LOOKBACK_DAYS
        display_days = max(DISPLAY_DAYS, lookback)
        try:
            feed = await asyncio.to_thread(
                load_signal_feed, self.settings["watchlist"], today - timedelta(days=display_days)
            )
        except Exception as exc:
            logger.warning("Big-trader refresh failed: %s", exc)
            self.signals_state = {**self.signals_state, "loading": False, "error": str(exc)}
            self.publish("signals", self.signals_state)
            return
        active = feed.active(today, display_days)
        alert_since = (today - timedelta(days=FIRST_LOAD_ALERT_DAYS)).isoformat() if first else "0000"
        self.alerts.check_signals(active, alert_since)
        self.signals_state = {
            "loading": False,
            "error": None,
            "updated": today.isoformat(),
            "lookback_days": lookback,
            "display_days": display_days,
            "scores": feed.net_scores(today, lookback),
            "signals": [
                {
                    "trader": s.trader,
                    "source": s.source,
                    "symbol": s.symbol,
                    "action": s.action,
                    "disclosed_on": s.disclosed_on.isoformat(),
                    "traded_on": s.traded_on.isoformat() if s.traded_on else None,
                    "detail": s.detail,
                }
                for s in reversed(active)
            ],
        }
        self.publish("signals", self.signals_state)
        watched = [s for s in self.settings["watchlist"] if s in self.market.quotes]
        advice = [a for a in (self.advice(s) for s in watched) if a]
        if not self.advice_armed:
            self.advice_armed = True
            self.alerts.advice_summary(advice)
        self.publish("quotes", [self.quote_dict(s) for s in watched])


hub = Hub()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await hub.start()
    yield
    await hub.stop()


app = FastAPI(title="Stock Bot Command Center", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def require_app_header(request: Request, call_next):
    # A custom header can't be sent cross-site without a CORS preflight, so other websites
    # open in your browser can't place orders or start the bot through this local server.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-command-center") != "1":
        return JSONResponse({"detail": "Missing X-Command-Center header"}, status_code=403)
    return await call_next(request)


def _symbol(raw: str) -> str:
    symbol = raw.strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise HTTPException(400, f"'{raw}' is not a valid ticker symbol")
    return symbol


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def get_state():
    return hub.state()


@app.get("/api/stream")
async def stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    hub.subscribers.add(queue)

    async def events():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    event, data = await asyncio.wait_for(queue.get(), 15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
        finally:
            hub.subscribers.discard(queue)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.get("/api/chart/{symbol}")
async def get_chart(symbol: str, range: str = "1D"):
    try:
        return await hub.chart(_symbol(symbol), range)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class SymbolIn(BaseModel):
    symbol: str


@app.post("/api/watchlist")
async def add_to_watchlist(body: SymbolIn):
    symbol = _symbol(body.symbol)
    if symbol not in hub.market.quotes:
        added = await hub.market.add_symbols([symbol])
        if not added:
            raise HTTPException(404, f"No market data found for {symbol}")
    if symbol not in hub.settings["watchlist"]:
        hub.settings.set_watchlist([*hub.settings["watchlist"], symbol])
    hub.publish("watchlist", hub.settings["watchlist"])
    return {"watchlist": hub.settings["watchlist"], "quote": hub.quote_dict(symbol)}


@app.delete("/api/watchlist/{symbol}")
async def remove_from_watchlist(symbol: str):
    symbol = _symbol(symbol)
    hub.settings.set_watchlist([s for s in hub.settings["watchlist"] if s != symbol])
    watched_by_alert = any(a["symbol"] == symbol for a in hub.settings["price_alerts"])
    if not watched_by_alert:
        await hub.market.remove_symbol(symbol)
    hub.publish("watchlist", hub.settings["watchlist"])
    return {"watchlist": hub.settings["watchlist"]}


class SettingsIn(BaseModel):
    move_alert_pcts: list[float] | None = None
    fast_move_pct: float | None = Field(None, gt=0)
    fast_move_minutes: int | None = Field(None, ge=1, le=60)
    sma_alerts: bool | None = None
    big_trader_alerts: bool | None = None
    bot_alerts: bool | None = None
    advice_alerts: bool | None = None
    signal_refresh_minutes: int | None = Field(None, ge=5)


@app.put("/api/settings")
async def update_settings(body: SettingsIn):
    changes = body.model_dump(exclude_none=True)
    if "move_alert_pcts" in changes:
        changes["move_alert_pcts"] = sorted({abs(p) for p in changes["move_alert_pcts"] if p})
    hub.settings.update(changes)
    hub.publish("settings", hub.settings.data)
    return hub.settings.data


class PriceAlertIn(BaseModel):
    symbol: str
    op: str = Field(pattern="^(above|below)$")
    price: float = Field(gt=0)


@app.post("/api/price-alerts")
async def add_price_alert(body: PriceAlertIn):
    symbol = _symbol(body.symbol)
    if symbol not in hub.market.quotes and not await hub.market.add_symbols([symbol]):
        raise HTTPException(404, f"No market data found for {symbol}")
    rule = hub.settings.add_price_alert(symbol, body.op, body.price)
    hub.publish("settings", hub.settings.data)
    return rule


@app.delete("/api/price-alerts/{alert_id}")
async def delete_price_alert(alert_id: str):
    hub.settings.remove_price_alert(alert_id)
    hub.publish("settings", hub.settings.data)
    return {"ok": True}


class OrderIn(BaseModel):
    symbol: str
    qty: float = Field(gt=0)
    side: str = Field(pattern="^(buy|sell)$")


@app.post("/api/orders")
async def place_order(body: OrderIn):
    symbol = _symbol(body.symbol)
    try:
        order = await asyncio.to_thread(hub.account.submit_market_order, symbol, body.qty, body.side)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    hub.alerts.raise_alert(
        "info",
        "order",
        f"Your order sent: {body.side.upper()} {body.qty:g} {symbol}",
        f"Market order sent to your {hub.account.label}. The bot won't count these shares as its own.",
        symbol,
    )
    return order


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str):
    try:
        await asyncio.to_thread(hub.account.cancel_order, order_id)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


class BotStartIn(BaseModel):
    strategy: str = Field(config.STRATEGY, pattern="^(sma|smart_money)$")


@app.post("/api/bot/start")
async def start_bot(body: BotStartIn):
    try:
        await hub.bot.start(hub.settings["watchlist"], body.strategy)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return hub.bot.status()


@app.post("/api/bot/stop")
async def stop_bot():
    await hub.bot.stop()
    return hub.bot.status()


@app.post("/api/signals/refresh")
async def refresh_signals():
    asyncio.create_task(hub.refresh_signals())
    return {"ok": True}


@app.post("/api/alerts/test")
async def test_alert():
    return hub.alerts.raise_alert(
        "info",
        "system",
        "Test alert",
        "Pop-up alerts are working. You'll see these for price moves, signals and trades.",
    )
