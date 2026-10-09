"""FastAPI app behind the browser command center: live quotes + alerts over Server-Sent Events."""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from dashboard.account import PaperAccount
from dashboard.advice import advise
from dashboard.alerts import AlertEngine
from dashboard.auth import COOKIE, LOOPBACK, SESSION_DAYS, Auth
from dashboard.bot import BotRunner
from dashboard.market import INDEXES, LiveMarket, chart_data, market_status
from dashboard.push import PushNotifier
from dashboard.settings import Settings
from dashboard.trading212_account import Trading212Account
from signals import load_signal_feed
from trading212.accounts import AccountProfile, AccountStore
from trading212.client import Trading212Error

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SYMBOL_RE = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")
FIRST_LOAD_ALERT_DAYS = 3
DISPLAY_DAYS = 180
PUBLIC_PATHS = {"/login", "/api/login", "/manifest.webmanifest", "/sw.js", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/icons/", "/static/login")


class AccountView:
    """One trading account in the command center: its live snapshot and its own bot."""

    def __init__(self, hub: "Hub", account):
        self.hub = hub
        self.account = account
        self.id: str = account.id
        self.name: str = account.name
        self.state: dict = self._base()
        self.task: asyncio.Task | None = None
        self.bot = BotRunner(
            self.id,
            account.label,
            lambda: self.account.enabled,
            self._on_bot_line,
            lambda: hub.publish("bot", self.bot.status()),
            self._on_bot_crash,
        )

    def _base(self) -> dict:
        a = self.account
        return {"enabled": a.enabled, "provider": a.provider, "label": a.label, "error": a.error}

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "from_env": self.id in ("default", "alpaca"),
            "state": self.state,
            "bot": self.bot.status(),
            "bot_log": list(self.bot.lines),
        }

    def _on_bot_line(self, line: str) -> None:
        self.hub.alerts.check_bot_line(line, self.id, self.name if len(self.hub.views) > 1 else "")
        self.hub.publish("bot_log", {"account": self.id, "line": line})

    def _on_bot_crash(self, message: str, restarting: bool) -> None:
        self.hub.alerts.raise_alert("danger", "bot", "Bot stopped unexpectedly", message, None, self.id)

    def start(self) -> None:
        if self.account.enabled and self.task is None:
            self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        await self.bot.stop()

    async def _loop(self) -> None:
        while True:
            snapshot = await asyncio.to_thread(self.account.snapshot)
            self.state = {**snapshot, "id": self.id, "name": self.name}
            self.hub.alerts.check_account(self.state)
            self.hub.publish("account", {"id": self.id, "state": self.state})
            await asyncio.sleep(self.account.refresh_seconds)


class Hub:
    """Owns all live components and fans events out to every connected browser tab."""

    def __init__(self):
        self.settings = Settings()
        self.auth = Auth()
        self.push = PushNotifier()
        self.subscribers: set[asyncio.Queue] = set()
        self.dirty: set[str] = set()
        self.market = LiveMarket(self.settings["watchlist"], self.dirty.add)
        self.alerts = AlertEngine(self.settings, self._on_alert, config.SMA_SHORT_WINDOW, config.SMA_LONG_WINDOW)
        self.store = AccountStore() if config.BROKER == "trading212" else None
        self.views: dict[str, AccountView] = {}
        if self.store is None:
            self._add_view(PaperAccount(config.ALPACA_API_KEY, config.ALPACA_API_SECRET))
        else:
            for profile in self.store.profiles():
                self._add_view(Trading212Account(profile))
        self.advice_armed = False
        self.started = False
        self.signals_state: dict = {"loading": True, "signals": [], "scores": {}, "updated": None, "error": None}
        self._tasks: list[asyncio.Task] = []
        self._chart_cache: dict[tuple[str, str], tuple[float, dict]] = {}

    def _add_view(self, account) -> AccountView:
        view = AccountView(self, account)
        self.views[view.id] = view
        return view

    def _on_alert(self, alert: dict) -> None:
        self.publish("alert", alert)
        self.push.notify(alert)

    def view(self, account_id: str | None) -> AccountView:
        if not self.views:
            raise HTTPException(400, "Add a Trading 212 practice account first")
        if account_id is None:
            return next(iter(self.views.values()))
        view = self.views.get(account_id)
        if view is None:
            raise HTTPException(404, "Unknown account")
        return view

    async def start(self) -> None:
        await self.market.start()
        self.started = True
        for view in self.views.values():
            view.start()
        self._tasks = [
            asyncio.create_task(self._flush_loop()),
            asyncio.create_task(self._status_loop()),
            asyncio.create_task(self._signals_loop()),
        ]
        await self.resume_bots()

    async def resume_bots(self) -> None:
        for account_id, strategy in dict(self.settings["running_bots"]).items():
            view = self.views.get(account_id)
            if view is None or not view.account.enabled:
                continue
            try:
                await view.bot.start(self.settings["watchlist"], strategy)
            except (RuntimeError, OSError) as exc:
                logger.warning("Couldn't resume the bot for %s: %s", view.name, exc)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for view in self.views.values():
            await view.stop()
        await self.market.stop()

    def add_account(self, profile: AccountProfile) -> AccountView:
        view = self._add_view(Trading212Account(profile))
        if self.started:
            view.start()
        self.publish("accounts", self.accounts_public())
        return view

    async def remove_account(self, account_id: str) -> None:
        view = self.views.pop(account_id)
        await view.stop()
        self.settings.set_bot_running(account_id, None)
        self.publish("accounts", self.accounts_public())

    def accounts_public(self) -> list[dict]:
        return [v.public() for v in self.views.values()]

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
        held = sum(
            p["qty"] for v in self.views.values() for p in v.state.get("positions") or [] if p["symbol"] == symbol
        )
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
            "accounts": self.accounts_public(),
            "signals": self.signals_state,
            "alerts": list(self.alerts.history),
            "settings": self.settings.data,
            "config": {
                "strategy": config.STRATEGY,
                "sma_short": config.SMA_SHORT_WINDOW,
                "sma_long": config.SMA_LONG_WINDOW,
                "max_position_pct": config.MAX_POSITION_PCT,
                "max_daily_loss_pct": config.MAX_DAILY_LOSS_PCT,
                "broker": config.BROKER,
                "auth": self.auth.enabled,
                "push_key": self.push.public_key,
                "self_update": config.SELF_UPDATE,
                "follows": [f.label for f in config.FOLLOW_FUNDS + config.FOLLOW_INSIDERS] + config.FOLLOW_POLITICIANS,
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
                    "reason": s.reason,
                    "notify_only": s.notify_only,
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


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    # Without a password the command center only answers this computer (not other devices or a proxy).
    if not hub.auth.enabled and (_client_ip(request) not in LOOPBACK or "x-forwarded-for" in request.headers):
        return JSONResponse(
            {
                "detail": "Set DASHBOARD_PASSWORD in stock-trading-bot/.env to open the command center from other devices"
            },
            status_code=403,
        )
    if hub.auth.enabled and not _is_public(path) and not hub.auth.valid(request.cookies.get(COOKIE)):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Please sign in"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    # A custom header can't be sent cross-site without a CORS preflight, so other websites
    # open in your browser can't place orders or start the bot through this server.
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
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(STATIC_DIR / "icons" / "icon-192.png", media_type="image/png")


# --- sign in ---------------------------------------------------------------------


@app.get("/login")
async def login_page(request: Request):
    if not hub.auth.enabled or hub.auth.valid(request.cookies.get(COOKIE)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-cache"})


class LoginIn(BaseModel):
    password: str = Field(max_length=200)


@app.post("/api/login")
async def login(body: LoginIn, request: Request):
    if not hub.auth.enabled:
        return {"ok": True}
    client = _client_ip(request)
    if hub.auth.locked_out(client):
        raise HTTPException(429, "Too many wrong passwords. Try again in 15 minutes.")
    if not hub.auth.check_password(client, body.password):
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong password")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        COOKIE,
        hub.auth.new_token(),
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/api/logout")
async def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE)
    return response


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
    account: str | None = None


@app.post("/api/orders")
async def place_order(body: OrderIn):
    symbol = _symbol(body.symbol)
    view = hub.view(body.account)
    try:
        order = await asyncio.to_thread(view.account.submit_market_order, symbol, body.qty, body.side)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    hub.alerts.raise_alert(
        "info",
        "order",
        f"Your order sent: {body.side.upper()} {body.qty:g} {symbol}",
        f"Market order sent to your {view.account.label}. The bot won't count these shares as its own.",
        symbol,
        view.id,
    )
    return order


@app.delete("/api/orders/{order_id}")
async def cancel_order(order_id: str, account: str | None = None):
    view = hub.view(account)
    try:
        await asyncio.to_thread(view.account.cancel_order, order_id)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


class BotStartIn(BaseModel):
    strategy: str = Field(config.STRATEGY, pattern="^(sma|smart_money)$")
    account: str | None = None


class BotStopIn(BaseModel):
    account: str | None = None


@app.post("/api/bot/start")
async def start_bot(body: BotStartIn):
    view = hub.view(body.account)
    try:
        await view.bot.start(hub.settings["watchlist"], body.strategy)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    hub.settings.set_bot_running(view.id, body.strategy)
    return view.bot.status()


@app.post("/api/bot/stop")
async def stop_bot(body: BotStopIn | None = None):
    view = hub.view(body.account if body else None)
    hub.settings.set_bot_running(view.id, None)
    await view.bot.stop()
    return view.bot.status()


# --- accounts ----------------------------------------------------------------------


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    api_key: str = Field(min_length=1, max_length=400)
    api_secret: str = Field(min_length=1, max_length=400)


@app.post("/api/accounts")
async def add_account(body: AccountIn):
    if hub.store is None:
        raise HTTPException(400, "Extra accounts are for Trading 212 practice accounts (set BROKER=trading212)")
    probe = AccountProfile(
        "probe", body.name, body.api_key.strip(), body.api_secret.strip(), config.CACHE_DIR / "trading212"
    )
    try:
        summary = await asyncio.to_thread(probe.client().account_summary)
    except Trading212Error as exc:
        raise HTTPException(
            400,
            f"Trading 212 didn't accept that key ({exc}). Make sure you created it on your PRACTICE account "
            "and copied both the key and the secret.",
        ) from exc
    try:
        profile = hub.store.add(body.name, body.api_key, body.api_secret)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    view = hub.add_account(profile)
    hub.alerts.raise_alert(
        "success",
        "system",
        f"Added {profile.name}",
        f"Connected your Trading 212 practice account ({summary.get('currency') or 'USD'}).",
        None,
        view.id,
    )
    return view.public()


@app.delete("/api/accounts/{account_id}")
async def remove_account(account_id: str):
    if hub.store is None or account_id not in hub.views:
        raise HTTPException(404, "Unknown account")
    try:
        hub.store.remove(account_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    await hub.remove_account(account_id)
    return {"ok": True}


# --- push notifications --------------------------------------------------------------


class PushSubIn(BaseModel):
    subscription: dict
    accounts: list[str] | None = None
    device: str = Field("", max_length=60)


class EndpointIn(BaseModel):
    endpoint: str = Field(max_length=1000)


@app.post("/api/push/subscribe")
async def push_subscribe(body: PushSubIn):
    try:
        row = hub.push.subscribe(body.subscription, body.accounts, body.device)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"accounts": row["accounts"], "device": row["device"]}


@app.post("/api/push/device")
async def push_device(body: EndpointIn):
    row = hub.push.device(body.endpoint)
    return {"subscribed": row is not None, "accounts": row["accounts"] if row else None}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(body: EndpointIn):
    hub.push.unsubscribe(body.endpoint)
    return {"ok": True}


@app.post("/api/push/test")
async def push_test(body: EndpointIn):
    if hub.push.device(body.endpoint) is None:
        raise HTTPException(404, "This device isn't signed up for notifications")
    hub.push.notify(
        {
            "id": "test",
            "level": "success",
            "title": "Notifications are working",
            "message": "You'll get these for bot trades, buy/sell calls and big-trader moves, even with the app closed.",
        },
        only_endpoint=body.endpoint,
    )
    return {"ok": True}


# --- self update (cloud install) -------------------------------------------------------


def _update_code() -> str:
    repo = config.BASE_DIR.parent
    pull = subprocess.run(["git", "pull", "--ff-only"], cwd=repo, capture_output=True, text=True, timeout=180)
    if pull.returncode != 0:
        raise RuntimeError((pull.stderr or pull.stdout).strip()[-500:])
    install = subprocess.run(
        ["uv", "pip", "install", "--python", sys.executable, "-r", str(config.BASE_DIR / "requirements.txt")],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if install.returncode != 0:
        raise RuntimeError((install.stderr or install.stdout).strip()[-500:])
    return pull.stdout.strip().splitlines()[-1] if pull.stdout.strip() else "Updated"


@app.post("/api/system/update")
async def update_app():
    if not config.SELF_UPDATE:
        raise HTTPException(400, "Updating from the app is only available on the cloud install")
    try:
        summary = await asyncio.to_thread(_update_code)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(400, f"Update failed: {exc}") from exc
    # The service manager starts the app again; running bots are resumed.
    asyncio.get_running_loop().call_later(1.5, os.kill, os.getpid(), signal.SIGTERM)
    return {"ok": True, "summary": summary}


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
