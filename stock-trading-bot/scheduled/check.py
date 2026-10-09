"""One finite bot check for schedulers such as GitHub Actions.

Each run: settle the bot's earlier orders, read the Trading 212 practice account, apply the same buy/sell
rules as the live strategy, and send what happened to Discord. State (open orders, alerts already sent,
which shares are the bot's) lives under `.cache/`, which the scheduler must keep between runs.
"""

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import yfinance as yf

import config
from dashboard.advice import advise
from notify.discord import DiscordNotifier
from signals.feed import SignalFeed, load_signal_feed
from strategies.rules import Decision, SmaSignal, sma_decision, smart_money_decision
from trading212.accounts import AccountProfile
from trading212.broker import order_side, order_ticker
from trading212.client import OrderUncertain, Trading212Error, symbol_from_ticker, validate_base_url

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
STATE_PATH = config.CACHE_DIR / "scheduled" / "state.json"
UNCERTAIN_TIMEOUT = timedelta(minutes=45)
STALE_ORDER_AGE = timedelta(days=3)
SUMMARY_DELAY = timedelta(minutes=5)
INACTIVE_WARNING_DAYS = 50
GITHUB_INACTIVE_LIMIT_DAYS = 60
MAX_FILING_NOTICES = 8
FILING_KEEP_EXTRA_DAYS = 7

Closes = list[tuple[date, float]]


@dataclass(frozen=True)
class Session:
    open: datetime
    close: datetime


@dataclass
class Account:
    currency: str
    total_ccy: float
    total: float
    cash: float
    held: dict[str, float]
    bot: dict[str, float]


def nyse_session(day: date) -> Session | None:
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return Session(row["market_open"].to_pydatetime(), row["market_close"].to_pydatetime())


def yahoo_closes(symbol: str) -> Closes:
    """Daily closes, oldest first. During market hours the last one is today's live price."""
    df = yf.Ticker(symbol.replace(".", "-")).history(period="1y", interval="1d", auto_adjust=False)
    if df is None or df.empty:
        return []
    return [(ts.date(), float(close)) for ts, close in df["Close"].dropna().items()]


def usd_rate(currency: str) -> float:
    if currency == "USD":
        return 1.0
    return float(yf.Ticker(f"{currency}USD=X").fast_info["last_price"])


def _parse_time(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _money(value: float, currency: str) -> str:
    symbol = {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency)
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}"


def _shares(holdings: dict[str, float]) -> str:
    rows = [f"{s} {q:g}" for s, q in sorted(holdings.items()) if q > 1e-9]
    return ", ".join(rows) if rows else "none"


class ScheduledCheck:
    def __init__(
        self,
        notifier: DiscordNotifier,
        profile: AccountProfile | None,
        now: datetime | None = None,
        state_path: Path = STATE_PATH,
        session_for: Callable[[date], Session | None] = nyse_session,
        closes_for: Callable[[str], Closes] = yahoo_closes,
        feed_loader: Callable[[list[str], date], SignalFeed] = load_signal_feed,
        fx: Callable[[str], float] = usd_rate,
        repo_pushed_at: str = "",
    ):
        self.notifier = notifier
        self.profile = profile
        self.now = now or datetime.now(timezone.utc)
        self.today = self.now.astimezone(NEW_YORK).date()
        self.state_path = state_path
        self.session_for = session_for
        self.closes_for = closes_for
        self.feed_loader = feed_loader
        self.fx = fx
        self.repo_pushed_at = repo_pushed_at
        self.client = None
        self.ledger = None
        if profile is not None:
            validate_base_url(profile.base_url)
            self.client = profile.client()
            self.ledger = profile.ledger()
        self.state: dict = {}
        self.pending: list[dict] = []

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True))
        tmp.replace(self.state_path)

    def _start_day(self) -> None:
        state = self.state
        for key in ("open_orders", "uncertain", "trades_today"):
            state.setdefault(key, [])
        for key in ("advice", "errors", "moves"):
            state.setdefault(key, {})
        if state.get("day") == self.today.isoformat():
            return
        state.update(
            day=self.today.isoformat(),
            day_start_value=state.get("last_close_value"),
            moves={},
            trades_today=[],
            errors={},
        )

    def _error_once(self, title: str, body: str = "") -> None:
        """Same problem is only sent once a day, so a broken key doesn't ping you every 15 minutes."""
        logger.warning("%s %s", title, body)
        if self.state["errors"].get(title) != self.today.isoformat():
            self.state["errors"][title] = self.today.isoformat()
            self.notifier.add("danger", title, body)

    @property
    def account_label(self) -> str:
        return f"{self.profile.name} (practice)" if self.profile else ""

    # ------------------------------------------------------------------ main
    def run(self) -> None:
        self.state = self._load_state()
        self._start_day()
        try:
            self._run()
        except Exception as exc:
            self._error_once("Bot check failed", f"{exc.__class__.__name__}: {exc}")
            raise
        finally:
            self._save_state()
            self.notifier.flush()

    def _run(self) -> None:
        session = self.session_for(self.today)
        market_open = bool(session and session.open <= self.now < session.close)
        after_close = bool(session and self.now >= session.close + SUMMARY_DELAY)
        first_run = not self.state.get("welcomed")
        if first_run:
            self._welcome()
            self.state["welcomed"] = True

        account = self._read_account() if self.client else None

        smart = config.STRATEGY == "smart_money"
        feed = self._load_feed() if smart else None
        scores = feed.net_scores(self.today, config.SIGNAL_LOOKBACK_DAYS) if feed else {}
        if feed:
            self._notify_filings(feed)

        if not (market_open or (after_close and self.state.get("summary_day") != self.today.isoformat())):
            logger.info("US market closed: nothing to trade this run")
            return

        universe = list(config.WATCHLIST)
        if account:
            universe += [s for s, q in account.bot.items() if q > 0]
        if smart and config.FOLLOW_EXPANDS_WATCHLIST:
            universe += [s for s, score in scores.items() if score > 0]
        universe = list(dict.fromkeys(universe))
        closes: dict[str, Closes] = {}
        for symbol in universe:
            try:
                closes[symbol] = self.closes_for(symbol)
            except Exception as exc:
                logger.warning("No price data for %s: %s", symbol, exc)

        advice = self._advice(closes, scores, feed, account)
        if market_open:
            self._price_moves(closes)
            if account:
                self._trade(account, closes, scores, feed)
        elif after_close:
            self._summary(account, advice)

    # ------------------------------------------------------------------ messages
    def _welcome(self) -> None:
        lines = [f"Watching: {', '.join(config.WATCHLIST)}."]
        if self.profile:
            lines.append(
                f"Trading your Trading 212 **practice** account '{self.profile.name}'. "
                "The bot only ever sells shares it bought itself; your own trades are left alone."
            )
        else:
            lines.append(
                "No Trading 212 practice key yet, so the bot will NOT trade. "
                "You'll still get buy/sell advice and big-trader alerts."
            )
        lines.append(
            "Checks run about every 15 minutes while the US market is open (9:30am-4pm New York time), "
            "plus a summary after the close."
        )
        self.notifier.add("info", "Stock bot is connected", "\n".join(lines))

    def _load_feed(self) -> SignalFeed | None:
        since = self.today - timedelta(days=config.SIGNAL_LOOKBACK_DAYS + FILING_KEEP_EXTRA_DAYS)
        try:
            return self.feed_loader(list(config.WATCHLIST), since)
        except Exception as exc:
            self._error_once("Couldn't load big-trader filings", str(exc))
            return None

    def _notify_filings(self, feed: SignalFeed) -> None:
        active = feed.active(self.today, config.SIGNAL_LOOKBACK_DAYS)
        seen: dict[str, str] | None = self.state.get("seen_signals")
        fresh = [s for s in active if seen is not None and s.describe() not in seen]
        if seen is None:
            if active:
                recent = "\n".join(f"- {s.trader} {s.action} {s.symbol} ({s.disclosed_on})" for s in active[-5:])
                self.notifier.add(
                    "info",
                    f"Following {len(active)} recent big-trader filings",
                    f"Most recent:\n{recent}\nFrom now on you'll get a message for each new one.",
                )
            seen = {}
        for signal in fresh[:MAX_FILING_NOTICES]:
            verb = "bought" if signal.action == "buy" else "sold"
            self.notifier.add(
                "success" if signal.action == "buy" else "warning",
                f"{signal.trader} {verb} {signal.symbol}",
                f"{signal.detail} ({signal.source}, disclosed {signal.disclosed_on}). "
                "Public filings come out days to months after the trade."
                + (f"\nWhy: {signal.reason}" if signal.reason else "")
                + ("\nFor your info only: the bot doesn't trade on this one." if signal.notify_only else ""),
            )
        if len(fresh) > MAX_FILING_NOTICES:
            self.notifier.add("info", f"...and {len(fresh) - MAX_FILING_NOTICES} more new big-trader filings")
        keep_after = (self.today - timedelta(days=config.SIGNAL_LOOKBACK_DAYS + FILING_KEEP_EXTRA_DAYS)).isoformat()
        seen.update({s.describe(): s.disclosed_on.isoformat() for s in active})
        self.state["seen_signals"] = {k: d for k, d in seen.items() if d >= keep_after}

    def _advice(
        self, closes: dict[str, Closes], scores: dict[str, int], feed: SignalFeed | None, account: Account | None
    ) -> dict[str, dict]:
        """BUY/SELL/HOLD/WATCH per watchlist stock. Messages only when a stock turns BUY or SELL."""
        result = {}
        for symbol in config.WATCHLIST:
            prices = [c for _, c in closes.get(symbol, [])]
            traders = [
                {"trader": s.trader, "action": s.action, "disclosed_on": s.disclosed_on.isoformat()}
                for s in (feed.reasons(symbol, self.today, config.SIGNAL_LOOKBACK_DAYS) if feed else [])
            ]
            held = account.held.get(symbol, 0.0) if account else 0.0
            advice = advise(
                symbol, prices, config.SMA_SHORT_WINDOW, config.SMA_LONG_WINDOW, scores.get(symbol, 0), traders, held
            )
            if advice is None:
                continue
            result[symbol] = advice
            previous = self.state["advice"].get(symbol)
            self.state["advice"][symbol] = advice["action"]
            if previous is None or previous == advice["action"] or advice["action"] not in ("buy", "sell"):
                continue
            self.notifier.add(
                "success" if advice["action"] == "buy" else "danger",
                f"{symbol} is now a {advice['action'].upper()}",
                f"{advice['headline']}\n" + "\n".join(f"- {r}" for r in advice["reasons"]),
            )
        return result

    def _price_moves(self, closes: dict[str, Closes]) -> None:
        levels = sorted(config.PRICE_ALERT_PCTS)
        for symbol in config.WATCHLIST:
            series = closes.get(symbol, [])
            if len(series) < 2 or series[-1][0] != self.today or series[-2][1] <= 0:
                continue
            move = (series[-1][1] / series[-2][1] - 1) * 100
            hit = [lvl for lvl in levels if abs(move) >= lvl]
            already = float(self.state["moves"].get(symbol, 0))
            if not hit or hit[-1] <= already:
                continue
            self.state["moves"][symbol] = hit[-1]
            self.notifier.add(
                "success" if move > 0 else "danger",
                f"{symbol} {'up' if move > 0 else 'down'} {abs(move):.1f}% today",
                f"Now ${series[-1][1]:,.2f} (yesterday's close ${series[-2][1]:,.2f}).",
            )

    def _summary(self, account: Account | None, advice: dict[str, dict]) -> None:
        self.state["summary_day"] = self.today.isoformat()
        lines = []
        if account:
            self.state["last_close_value"] = account.total
            start = self.state.get("day_start_value")
            change = ""
            if start:
                pct = (account.total / start - 1) * 100
                change = f" ({pct:+.2f}% today)"
            yours = {s: q - account.bot.get(s, 0.0) for s, q in account.held.items()}
            lines += [
                f"Account {self.account_label}: {_money(account.total_ccy, account.currency)}{change}",
                f"Bot's shares: {_shares(account.bot)}",
                f"Your own shares: {_shares(yours)}",
            ]
            trades = self.state.get("trades_today") or []
            lines.append(
                "Bot trades today:\n" + "\n".join(f"- {t}" for t in trades) if trades else "No bot trades today."
            )
            if self.state.get("halted_day") == self.today.isoformat():
                lines.append("Daily loss limit was hit, so the bot stopped buying today.")
        for action in ("buy", "sell"):
            names = [s for s, a in advice.items() if a["action"] == action]
            if names:
                lines.append(f"{action.upper()} right now: {', '.join(names)}")
        warning = self._inactivity_warning()
        if warning:
            lines.append(warning)
        weekday = self.now.astimezone(NEW_YORK).strftime("%a %d %b")
        self.notifier.add("info", f"Daily summary: {weekday}", "\n".join(lines) or "Nothing to report.")

    def _inactivity_warning(self) -> str:
        pushed = _parse_time(self.repo_pushed_at)
        if pushed is None:
            return ""
        idle = (self.now - pushed).days
        if idle < INACTIVE_WARNING_DAYS:
            return ""
        left = max(GITHUB_INACTIVE_LIMIT_DAYS - idle, 0)
        return (
            f"Heads up: GitHub pauses scheduled bots after 60 days without changes to the repo (about {left} days "
            "left). Make any small edit to the repo (for example the README) to keep it running."
        )

    # ------------------------------------------------------------------ account + orders
    def _read_account(self) -> Account | None:
        try:
            self.pending = self.client.pending_orders()
            self._settle_orders()
            summary = self.client.account_summary()
            currency = summary.get("currency") or "USD"
            fx = self.fx(currency)
            held: dict[str, float] = {}
            for row in self.client.positions():
                symbol = symbol_from_ticker(order_ticker(row))
                held[symbol] = held.get(symbol, 0.0) + float(row.get("quantity") or 0)
        except (Trading212Error, OSError, ValueError) as exc:
            self._error_once("Couldn't reach your Trading 212 practice account", str(exc))
            return None
        total_ccy = float(summary.get("totalValue") or 0)
        return Account(
            currency=currency,
            total_ccy=total_ccy,
            total=total_ccy * fx,
            cash=float((summary.get("cash") or {}).get("availableToTrade") or 0) * fx,
            held=held,
            bot=self.ledger.reconcile(held),
        )

    def _settle_orders(self) -> None:
        pending_ids = {str(row["id"]) for row in self.pending}
        known = self.ledger.snapshot()
        taken = set(known["bot_orders"]) | set(known["manual_orders"])
        unsure = []
        for entry in self.state["uncertain"]:
            found = self._find_uncertain(entry, taken)
            words = f"{entry['side'].upper()} {entry['qty']:g} {entry['symbol']}"
            if found:
                taken.add(found)
                self.ledger.record_bot_order(found)
                self.state["open_orders"].append({**entry, "id": found})
                self.notifier.add("info", f"The bot's {words} order did go through")
            elif self.now - _parse_time(entry["placed_at"]) > UNCERTAIN_TIMEOUT:
                self.notifier.add("warning", f"The bot's {words} order was not placed", "It was not resent.")
            else:
                unsure.append(entry)
        self.state["uncertain"] = unsure

        still_open = []
        for entry in self.state["open_orders"]:
            if entry["id"] in pending_ids:
                still_open.append(entry)
                continue
            final = self.client.final_order_state(entry["ticker"], entry["id"])
            if final is None:
                if self.now - _parse_time(entry["placed_at"]) < STALE_ORDER_AGE:
                    still_open.append(entry)
                continue
            self._order_finished(entry, *final)
        self.state["open_orders"] = still_open

    def _find_uncertain(self, entry: dict, taken: set[str]) -> str | None:
        placed = _parse_time(entry["placed_at"]) - timedelta(minutes=2)

        def matches(row: dict) -> bool:
            created = _parse_time(str(row.get("createdAt") or ""))
            return (
                order_ticker(row) == entry["ticker"]
                and str(row["id"]) not in taken
                and row.get("initiatedFrom") == "API"
                and order_side(row) == entry["side"]
                and (created is None or created >= placed)
            )

        rows = [row for row in self.pending if matches(row)]
        if not rows:
            rows = [
                i["order"] for i in self.client.history_orders(ticker=entry["ticker"], limit=10) if matches(i["order"])
            ]
        return str(rows[0]["id"]) if rows else None

    def _order_finished(self, entry: dict, status: str, filled: float, price: float) -> None:
        symbol, side = entry["symbol"], entry["side"]
        if filled > 0:
            if self.ledger.apply_fill(entry["id"], symbol, filled if side == "buy" else -filled):
                verb = "bought" if side == "buy" else "sold"
                text = f"{verb} {filled:g} {symbol} at ${price:,.2f}"
                self.state["trades_today"].append(text)
                self.notifier.add(
                    "success" if side == "buy" else "danger",
                    f"Filled: bot {text}",
                    f"Why: {entry.get('reason', '')}\nAccount: {self.account_label}",
                )
        elif status == "CANCELLED":
            self.notifier.add("warning", f"The bot's {side.upper()} {symbol} order was cancelled")
        else:
            self.notifier.add("danger", f"Trading 212 did not fill the bot's {side.upper()} {symbol} order", status)

    # ------------------------------------------------------------------ trading
    def _halted(self, account: Account) -> bool:
        today = self.today.isoformat()
        if self.state.get("halted_day") == today:
            return True
        start = self.state.get("day_start_value")
        if not start:
            self.state["day_start_value"] = start = account.total
        limit = config.MAX_DAILY_LOSS_PCT
        if limit <= 0 or start <= 0 or account.total > start * (1 - limit):
            return False
        self.state["halted_day"] = today
        loss = (1 - account.total / start) * 100
        self.notifier.add(
            "danger",
            f"Daily loss limit hit ({loss:.1f}% down today)",
            "The bot won't buy anything else today."
            + (" It is selling the shares it bought." if config.LIQUIDATE_ON_DAILY_LOSS else ""),
        )
        if config.LIQUIDATE_ON_DAILY_LOSS:
            for symbol, qty in account.bot.items():
                if qty > 0:
                    self._submit(symbol, qty, "sell", 0.0, "daily loss limit reached")
        return True

    def _decide(self, symbol: str, signal: SmaSignal, held: bool, scores: dict, feed: SignalFeed | None):
        on_watchlist = symbol in config.WATCHLIST
        if config.STRATEGY != "smart_money":
            return sma_decision(signal, held, on_watchlist)

        def who(action: str) -> str:
            reasons = feed.reasons(symbol, self.today, config.SIGNAL_LOOKBACK_DAYS) if feed else []
            return "; ".join(f"{s.trader} {s.detail}" for s in reasons if s.action == action)

        return smart_money_decision(
            signal, held, on_watchlist, scores.get(symbol, 0), config.REQUIRE_SMA_CONFIRMATION, who
        )

    def _trade(self, account: Account, closes: dict[str, Closes], scores: dict, feed: SignalFeed | None) -> None:
        halted = self._halted(account)
        busy = {
            symbol_from_ticker(order_ticker(row)): order_side(row)
            for row in self.pending
            if self.ledger.is_bot_order(row["id"])
        }
        busy.update({e["symbol"]: e["side"] for e in self.state["open_orders"] + self.state["uncertain"]})
        held = {s for s, q in account.bot.items() if q > 0}
        cash = account.cash
        new_positions = 0
        for symbol, series in closes.items():
            signal = SmaSignal.from_closes([c for _, c in series], config.SMA_SHORT_WINDOW, config.SMA_LONG_WINDOW)
            if signal is None:
                continue
            decision: Decision | None = self._decide(symbol, signal, symbol in held, scores, feed)
            if decision is None:
                continue
            if symbol in busy:
                logger.info("Skip %s %s: an order is still open", decision.action, symbol)
                continue
            if decision.action == "sell":
                if self._submit(symbol, account.bot.get(symbol, 0.0), "sell", signal.price, decision.reason):
                    busy[symbol] = "sell"
                continue
            if halted:
                logger.info("Skip buy %s: daily loss limit reached", symbol)
                continue
            pending_buys = {s for s, side in busy.items() if side == "buy" and s not in held}
            if len(held) + len(pending_buys) + new_positions >= config.MAX_OPEN_POSITIONS:
                logger.info("Skip buy %s: max open positions reached", symbol)
                continue
            budget = min(account.total * config.MAX_POSITION_PCT - account.bot.get(symbol, 0.0) * signal.price, cash)
            qty = math.floor(budget / signal.price) if signal.price > 0 else 0
            if qty < 1:
                logger.info("Skip buy %s: not enough cash/position budget", symbol)
                continue
            if self._submit(symbol, qty, "buy", signal.price, decision.reason):
                busy[symbol] = "buy"
                cash -= qty * signal.price
                new_positions += 1

    def _submit(self, symbol: str, qty: float, side: str, price: float, reason: str) -> bool:
        """Place one market order. Never retried: Trading 212 could fill a resend twice."""
        if qty <= 0:
            return False
        ticker = self.client.ticker_for(symbol)
        if not ticker:
            self._error_once(f"{symbol} isn't available on Trading 212", "The bot skipped it.")
            return False
        words = f"{side.upper()} {qty:g} {symbol}"
        entry = {
            "symbol": symbol,
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "reason": reason,
            "placed_at": self.now.isoformat(),
        }
        try:
            row = self.client.market_order(ticker, qty if side == "buy" else -qty)
        except OrderUncertain as exc:
            self.state["uncertain"].append(entry)
            self.notifier.add(
                "warning",
                f"Not sure the bot's {words} order went through",
                f"{exc}. The bot will NOT resend it; it checks your account again next run.",
            )
            return True
        except Trading212Error as exc:
            self.notifier.add("danger", f"Trading 212 refused the bot's {words} order", str(exc))
            return False
        order_id = str(row["id"])
        self.ledger.record_bot_order(order_id)
        self.state["open_orders"].append({**entry, "id": order_id})
        price_text = f" at about ${price:,.2f} each" if price > 0 else ""
        self.notifier.add(
            "success" if side == "buy" else "danger",
            f"Bot {'BUYING' if side == 'buy' else 'SELLING'} {qty:g} {symbol}",
            f"Market order sent{price_text}.\nWhy: {reason}\nAccount: {self.account_label}",
        )
        logger.info("%s order placed", words)
        return True
