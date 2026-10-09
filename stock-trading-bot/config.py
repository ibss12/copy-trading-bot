"""Central configuration, loaded from environment variables / `.env`."""

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / ".cache"

load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _str(name)
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = _str(name)
    return int(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _list(name: str, default: str) -> list[str]:
    return [item.strip() for item in _str(name, default).split(",") if item.strip()]


def _date(name: str, default: str) -> date:
    return date.fromisoformat(_str(name, default))


@dataclass(frozen=True)
class FollowedFiler:
    label: str
    cik: int


def _filers(name: str, default: str) -> list[FollowedFiler]:
    filers = []
    for item in _list(name, default):
        label, _, cik = item.rpartition(":")
        if not label or not cik.strip().isdigit():
            raise ValueError(f"{name}: expected 'Label:CIK', got {item!r}")
        filers.append(FollowedFiler(label=label.strip(), cik=int(cik)))
    return filers


# --- Alpaca (paper only) -----------------------------------------------------
ALPACA_API_KEY = _str("ALPACA_API_KEY")
ALPACA_API_SECRET = _str("ALPACA_API_SECRET")

# PAPER is hard-coded: this project never places real-money orders.
ALPACA_CONFIG = {
    "API_KEY": ALPACA_API_KEY,
    "API_SECRET": ALPACA_API_SECRET,
    "PAPER": True,
}

# --- Trading 212 (practice/demo account only) --------------------------------
TRADING212_API_KEY = _str("TRADING212_API_KEY")
TRADING212_API_SECRET = _str("TRADING212_API_SECRET")
TRADING212_DEMO_URL = "https://demo.trading212.com/api/v0"
# Only the demo server (or a local test server) is accepted; see trading212/client.py.
TRADING212_BASE_URL = _str("TRADING212_BASE_URL", TRADING212_DEMO_URL)
# Name shown for the .env account. More practice accounts can be added in the command center.
TRADING212_ACCOUNT_NAME = _str("TRADING212_ACCOUNT_NAME", "Practice account")
# Which account a bot process trades (set by the command center; "default" = the .env keys).
TRADING212_ACCOUNT = _str("TRADING212_ACCOUNT", "default")

# Where the bot places its orders: "alpaca" (paper) or "trading212" (practice account).
BROKER = _str("BROKER", "alpaca").lower()

# --- Command center access ----------------------------------------------------
# Password for the command center. Required whenever it can be reached from another device.
# Scheduled mode (run_scheduled.py): bot messages go to this Discord channel webhook.
DISCORD_WEBHOOK_URL = _str("DISCORD_WEBHOOK_URL")

DASHBOARD_PASSWORD = _str("DASHBOARD_PASSWORD")
# Public https address when hosted (e.g. https://1-2-3-4.sslip.io). Used as the push contact.
PUBLIC_URL = _str("PUBLIC_URL").rstrip("/")
# Lets the command center update itself with `git pull` (set by the cloud install script).
SELF_UPDATE = _bool("STOCKBOT_SELF_UPDATE", False)

# --- What to trade -----------------------------------------------------------
WATCHLIST = [symbol.upper() for symbol in _list("WATCHLIST", "AAPL,TSLA,MSFT,NVDA,AMZN,GOOGL,META")]
STRATEGY = _str("STRATEGY", "smart_money").lower()

# --- SMA crossover -----------------------------------------------------------
SMA_SHORT_WINDOW = _int("SMA_SHORT_WINDOW", 20)
SMA_LONG_WINDOW = _int("SMA_LONG_WINDOW", 50)
LIVE_SLEEPTIME = _str("LIVE_SLEEPTIME", "30M")

# --- Risk controls -----------------------------------------------------------
MAX_POSITION_PCT = _float("MAX_POSITION_PCT", 0.10)
MAX_OPEN_POSITIONS = _int("MAX_OPEN_POSITIONS", 8)
MAX_DAILY_LOSS_PCT = _float("MAX_DAILY_LOSS_PCT", 0.03)
LIQUIDATE_ON_DAILY_LOSS = _bool("LIQUIDATE_ON_DAILY_LOSS", False)
PRICE_ALERT_PCTS = [float(pct) for pct in _list("PRICE_ALERT_PCTS", "5,10")]

# --- Smart money -------------------------------------------------------------
FOLLOW_FUNDS = _filers(
    "FOLLOW_FUNDS",
    "Warren Buffett (Berkshire Hathaway):1067983,"
    "Leopold Aschenbrenner (Situational Awareness):2045724,"
    "Bill Ackman (Pershing Square):1336528",
)
FOLLOW_INSIDERS = _filers("FOLLOW_INSIDERS", "Donald J. Trump:947033,Donald Trump Jr.:2016181")
TRACK_CEO_TRADES = _bool("TRACK_CEO_TRADES", True)
CEO_TITLES = _list("CEO_TITLES", "CEO,Chief Executive")
# Also announce trades by other top executives of watchlist companies (never traded on).
NOTIFY_OTHER_INSIDERS = _bool("NOTIFY_OTHER_INSIDERS", True)
INSIDER_ALERT_TITLES = _list(
    "INSIDER_ALERT_TITLES",
    "CEO,Chief Executive,CFO,Chief Financial,COO,Chief Operating,President,Chair,Chairman,Chairwoman,Chairperson",
)
INSIDER_ALERT_MIN_SELL_USD = _float("INSIDER_ALERT_MIN_SELL_USD", 500_000)
IGNORE_PLANNED_SALES = _bool("IGNORE_PLANNED_SALES", True)

QUIVER_API_KEY = _str("QUIVER_API_KEY")
BARGO_API_KEY = _str("BARGO_API_KEY")
FOLLOW_POLITICIANS = _list("FOLLOW_POLITICIANS", "Nancy Pelosi")

OPENFIGI_API_KEY = _str("OPENFIGI_API_KEY")
SEC_USER_AGENT = _str("SEC_USER_AGENT", "stock-trading-bot contact@example.com")

SIGNAL_LOOKBACK_DAYS = _int("SIGNAL_LOOKBACK_DAYS", 45)
FOLLOW_EXPANDS_WATCHLIST = _bool("FOLLOW_EXPANDS_WATCHLIST", True)
REQUIRE_SMA_CONFIRMATION = _bool("REQUIRE_SMA_CONFIRMATION", True)

# --- Backtest ----------------------------------------------------------------
BACKTEST_START = _date("BACKTEST_START", "2024-01-01")
BACKTEST_END = _date("BACKTEST_END", "2025-12-31")
BACKTEST_BUDGET = _float("BACKTEST_BUDGET", 100_000)


def strategy_parameters() -> dict:
    """Parameters passed to the lumibot Strategy (available as `self.parameters`)."""
    return {
        "symbols": WATCHLIST,
        "sma_short_window": SMA_SHORT_WINDOW,
        "sma_long_window": SMA_LONG_WINDOW,
        "max_position_pct": MAX_POSITION_PCT,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
        "liquidate_on_daily_loss": LIQUIDATE_ON_DAILY_LOSS,
        "price_alert_pcts": PRICE_ALERT_PCTS,
        "signal_lookback_days": SIGNAL_LOOKBACK_DAYS,
        "follow_expands_watchlist": FOLLOW_EXPANDS_WATCHLIST,
        "require_sma_confirmation": REQUIRE_SMA_CONFIRMATION,
    }


def validate() -> None:
    if SMA_SHORT_WINDOW <= 0 or SMA_LONG_WINDOW <= SMA_SHORT_WINDOW:
        raise ValueError("Require 0 < SMA_SHORT_WINDOW < SMA_LONG_WINDOW")
    if not 0 < MAX_POSITION_PCT <= 1:
        raise ValueError("MAX_POSITION_PCT must be in (0, 1]")
    if not 0 <= MAX_DAILY_LOSS_PCT < 1:
        raise ValueError("MAX_DAILY_LOSS_PCT must be in [0, 1)")
    if MAX_OPEN_POSITIONS <= 0:
        raise ValueError("MAX_OPEN_POSITIONS must be positive")
    if STRATEGY not in ("sma", "smart_money"):
        raise ValueError("STRATEGY must be 'sma' or 'smart_money'")
    if not WATCHLIST:
        raise ValueError("WATCHLIST is empty")
    if BROKER not in ("alpaca", "trading212"):
        raise ValueError("BROKER must be 'alpaca' or 'trading212'")


def broker_keys_configured() -> bool:
    if BROKER == "trading212":
        from trading212.accounts import AccountStore

        return bool(AccountStore().profiles())
    return bool(ALPACA_API_KEY and ALPACA_API_SECRET)
