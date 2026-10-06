"""Congressional stock trades (STOCK Act disclosures) via Bargo's free API. No key needed.

https://www.bargo.ai/free-apis/congress - last 3 months of House and Senate filings,
about 30 requests and 100 rows per day per IP without a key.
"""

from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from signals.http import CachedHttp
from signals.models import TraderSignal

BARGO_URL = "https://www.bargo.ai/free-apis/congress/v1/trades"
BARGO_SOURCE = "Congress (STOCK Act, via bargo.ai)"
CONGRESS_TTL_S = 6 * 3600
PAGE_SIZE = 25
MAX_PAGES = 3


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


def bargo_congress_signals(cache_dir: Path, politicians: list[str], since: date) -> list[TraderSignal]:
    http = CachedHttp(
        cache_dir / "bargo",
        headers={"Accept": "application/json", "User-Agent": "stock-trading-bot"},
        min_interval_s=1.0,
    )
    signals = []
    for politician in politicians:
        for page in range(MAX_PAGES):
            query = urlencode({"member": politician, "limit": PAGE_SIZE, "page": page})
            rows = http.get_json(f"{BARGO_URL}?{query}", ttl_s=CONGRESS_TTL_S, stale_on_error=True).get("trades") or []
            for row in rows:
                action = _action(row.get("type") or "")
                symbol = (row.get("ticker") or "").strip().upper()
                disclosed_on = _date(row.get("disclosure_date"))
                if action is None or not symbol or " " in symbol or disclosed_on is None or disclosed_on < since:
                    continue
                signals.append(
                    TraderSignal(
                        trader=row.get("member") or politician,
                        source=BARGO_SOURCE,
                        symbol=symbol,
                        action=action,
                        disclosed_on=disclosed_on,
                        traded_on=_date(row.get("transaction_date")),
                        detail=f"{row.get('type', '').capitalize()} {row.get('amount_range') or ''}".strip(),
                    )
                )
            if len(rows) < PAGE_SIZE:
                break
    return signals
