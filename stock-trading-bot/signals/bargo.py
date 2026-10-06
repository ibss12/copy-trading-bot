"""Congressional stock trades (STOCK Act disclosures) via Bargo's free API. No key needed.

https://www.bargo.ai/free-apis/congress - last 3 months of House and Senate filings,
about 30 requests and 100 rows per day per IP without a key.
"""

import logging
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from signals.http import CachedHttp
from signals.models import TraderSignal

logger = logging.getLogger(__name__)

BARGO_URL = "https://www.bargo.ai/free-apis/congress/v1/trades"
BARGO_SOURCE = "Congress (STOCK Act, via bargo.ai)"
CONGRESS_TTL_S = 12 * 3600
PAGE_SIZE = 25
MAX_PAGES = 20
STOCK_ACT_LAG = timedelta(days=45)


def _action(trade_type: str) -> str | None:
    trade_type = trade_type.lower()
    if trade_type.startswith("purchase"):
        return "buy"
    if trade_type.startswith("sale"):
        return "sell"
    return None


def _date(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _window_start(since: date) -> date:
    """Earliest trade date that can still be disclosed on or after `since`, rounded to a Monday
    so request URLs (and their cache entries) stay the same all week."""
    start = since - STOCK_ACT_LAG
    return start - timedelta(days=start.weekday())


def _signal(row: dict, politician: str, since: date) -> TraderSignal | None:
    action = _action(row.get("type") or "")
    symbol = (row.get("ticker") or "").strip().upper()
    disclosed_on = _date(row.get("disclosure_date"))
    if action is None or not symbol or " " in symbol or disclosed_on is None or disclosed_on < since:
        return None
    return TraderSignal(
        trader=row.get("member") or politician,
        source=BARGO_SOURCE,
        symbol=symbol,
        action=action,
        disclosed_on=disclosed_on,
        traded_on=_date(row.get("transaction_date")),
        detail=f"{(row.get('type') or '').capitalize()} {row.get('amount_range') or ''}".strip(),
    )


def bargo_congress_signals(cache_dir: Path, politicians: list[str], since: date) -> list[TraderSignal]:
    http = CachedHttp(
        cache_dir / "bargo",
        headers={"Accept": "application/json", "User-Agent": "stock-trading-bot"},
        min_interval_s=1.0,
    )
    window_start = _window_start(since).isoformat()
    signals = []
    for politician in politicians:
        for page in range(MAX_PAGES):
            query = urlencode({"member": politician, "from": window_start, "limit": PAGE_SIZE, "page": page})
            rows = http.get_json(f"{BARGO_URL}?{query}", ttl_s=CONGRESS_TTL_S, stale_on_error=True).get("trades") or []
            page_signals = [s for s in (_signal(row, politician, since) for row in rows) if s]
            signals += page_signals
            if len(rows) < PAGE_SIZE or not page_signals:
                break
        else:
            logger.warning(
                "Bargo: stopped after %d pages of %s's trades; older ones are skipped", MAX_PAGES, politician
            )
    return signals
