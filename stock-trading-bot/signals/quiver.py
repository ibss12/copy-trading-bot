"""Congressional stock trades (STOCK Act disclosures) via Quiver Quantitative.

Requires a Quiver API key: https://www.quiverquant.com/pricing
"""

from datetime import date
from pathlib import Path
from urllib.parse import quote

from signals.http import CachedHttp
from signals.models import TraderSignal

QUIVER_URL = "https://api.quiverquant.com/beta/bulk/congresstrading"
CONGRESS_TTL_S = 6 * 3600


def _first(row: dict, *keys: str) -> str:
    for key in keys:
        if row.get(key):
            return str(row[key])
    return ""


def congress_signals(cache_dir: Path, api_key: str, politicians: list[str], since: date) -> list[TraderSignal]:
    http = CachedHttp(
        cache_dir / "quiver",
        headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
        min_interval_s=1.0,
    )
    signals = []
    for politician in politicians:
        rows = http.get_json(f"{QUIVER_URL}?representative={quote(politician)}", ttl_s=CONGRESS_TTL_S)
        for row in rows:
            transaction = _first(row, "Transaction").lower()
            action = "buy" if "purchase" in transaction else "sell" if "sale" in transaction else None
            symbol = _first(row, "Ticker").upper()
            disclosed_raw = _first(row, "ReportDate", "Filed")[:10]
            if action is None or not symbol or not disclosed_raw:
                continue
            disclosed_on = date.fromisoformat(disclosed_raw)
            if disclosed_on < since:
                continue
            traded_raw = _first(row, "TransactionDate", "Traded")[:10]
            amount = _first(row, "Range", "Trade_Size_USD", "Amount")
            signals.append(
                TraderSignal(
                    trader=_first(row, "Representative", "Name") or politician,
                    source="Congress (STOCK Act)",
                    symbol=symbol,
                    action=action,
                    disclosed_on=disclosed_on,
                    traded_on=date.fromisoformat(traded_raw) if traded_raw else None,
                    detail=f"{_first(row, 'Transaction')} {amount}".strip(),
                )
            )
    return signals
