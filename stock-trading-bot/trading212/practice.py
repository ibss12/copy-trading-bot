"""Build the Trading 212 practice-account client and bot ledger from config."""

import config

from .client import Trading212Client
from .ledger import BotLedger

LEDGER_PATH = config.CACHE_DIR / "trading212" / "bot_ledger.json"


def practice_client() -> Trading212Client:
    return Trading212Client(
        config.TRADING212_API_KEY,
        config.TRADING212_API_SECRET,
        config.TRADING212_BASE_URL,
        config.CACHE_DIR / "trading212",
    )


def bot_ledger() -> BotLedger:
    return BotLedger(LEDGER_PATH)
