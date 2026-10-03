"""Minimal Trading 212 public API client, restricted to the practice (demo) server.

Docs: https://docs.trading212.com/api
"""

import json
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

DEMO_HOST = "demo.trading212.com"
LOCAL_HOSTS = ("127.0.0.1", "localhost")

# Minimum seconds between calls per endpoint (Trading 212 publishes these limits per account).
RATE_LIMITS = {
    "summary": 5.0,
    "positions": 1.0,
    "orders": 5.0,
    "order": 1.0,
    "market": 1.2,
    "cancel": 1.0,
    "history": 3.0,
    "instruments": 50.0,
}


class Trading212Error(Exception):
    """The API rejected the request (bad input, not enough funds, auth, ...). Nothing was placed."""


class OrderUncertain(Trading212Error):
    """A market order request failed in a way where it may or may not have been placed."""


def validate_base_url(url: str) -> str:
    """Only allow the demo server (or a local test double). Never the real-money server."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host == DEMO_HOST and parsed.scheme == "https":
        return url.rstrip("/")
    if host in LOCAL_HOSTS and parsed.scheme in ("http", "https"):
        return url.rstrip("/")
    raise ValueError(
        f"Refusing Trading 212 URL {url!r}: this bot only trades on the practice server https://{DEMO_HOST}"
    )


class Trading212Client:
    def __init__(self, api_key: str, api_secret: str, base_url: str, cache_dir: Path, timeout_s: float = 15):
        if not (api_key and api_secret):
            raise Trading212Error("Trading 212 practice API key/secret are not configured")
        self.base_url = validate_base_url(base_url)
        self.cache_dir = cache_dir
        self.timeout_s = timeout_s
        self._session = requests.Session()
        self._session.auth = (api_key, api_secret)
        self._session.headers["Accept"] = "application/json"
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()
        self._instruments: list[dict] | None = None

    # ------------------------------------------------------------------ plumbing
    def _throttle(self, bucket: str) -> None:
        with self._lock:
            wait = self._last_call.get(bucket, 0) + RATE_LIMITS[bucket] - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call[bucket] = time.monotonic()

    @staticmethod
    def _error_text(resp: requests.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return resp.text[:300] or resp.reason
        if isinstance(body, dict):
            return str(body.get("message") or body.get("code") or body.get("type") or body)[:300]
        return str(body)[:300]

    def _get(self, bucket: str, path: str, params: dict | None = None):
        for attempt in range(2):
            self._throttle(bucket)
            resp = self._session.get(self.base_url + path, params=params, timeout=self.timeout_s)
            if resp.status_code == 429 and attempt == 0:
                reset = resp.headers.get("x-ratelimit-reset")
                time.sleep(min(max(float(reset) - time.time(), 1.0), 30.0) if reset else RATE_LIMITS[bucket])
                continue
            if resp.status_code in (401, 403):
                raise Trading212Error("Trading 212 rejected the API key/secret (check they are practice-account keys)")
            if resp.status_code >= 400:
                raise Trading212Error(f"Trading 212 {resp.status_code}: {self._error_text(resp)}")
            return resp.json()
        raise Trading212Error("Trading 212 rate limit hit, try again shortly")

    # ------------------------------------------------------------------ account
    def account_summary(self) -> dict:
        return self._get("summary", "/equity/account/summary")

    def positions(self) -> list[dict]:
        return self._get("positions", "/equity/positions")

    def pending_orders(self) -> list[dict]:
        return self._get("orders", "/equity/orders")

    def order(self, order_id: int) -> dict | None:
        try:
            return self._get("order", f"/equity/orders/{order_id}")
        except Trading212Error as exc:
            if "404" in str(exc):
                return None
            raise

    def history_orders(self, ticker: str | None = None, limit: int = 50) -> list[dict]:
        params = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self._get("history", "/equity/history/orders", params).get("items", [])

    # ------------------------------------------------------------------ trading
    def market_order(self, ticker: str, quantity: float) -> dict:
        """Positive quantity buys, negative sells. Never retried: the endpoint is not idempotent."""
        self._throttle("market")
        try:
            resp = self._session.post(
                self.base_url + "/equity/orders/market",
                json={"ticker": ticker, "quantity": quantity, "extendedHours": False},
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            raise OrderUncertain(f"No answer from Trading 212 ({exc.__class__.__name__})") from exc
        if resp.status_code >= 500 or resp.status_code == 408:
            raise OrderUncertain(f"Trading 212 {resp.status_code}: {self._error_text(resp)}")
        if resp.status_code >= 400:
            raise Trading212Error(f"Trading 212 refused the order ({resp.status_code}): {self._error_text(resp)}")
        return resp.json()

    def cancel_order(self, order_id: int) -> None:
        self._throttle("cancel")
        resp = self._session.delete(f"{self.base_url}/equity/orders/{order_id}", timeout=self.timeout_s)
        if resp.status_code >= 400:
            raise Trading212Error(f"Trading 212 couldn't cancel ({resp.status_code}): {self._error_text(resp)}")

    # ------------------------------------------------------------------ instruments
    def instruments(self) -> list[dict]:
        if self._instruments is not None:
            return self._instruments
        path = self.cache_dir / "trading212_instruments.json"
        if path.exists() and time.time() - path.stat().st_mtime < 24 * 3600:
            self._instruments = json.loads(path.read_text())
            return self._instruments
        data = self._get("instruments", "/equity/metadata/instruments")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        self._instruments = data
        return data

    def ticker_for(self, symbol: str) -> str | None:
        """Plain US symbol (AAPL) -> Trading 212 ticker (AAPL_US_EQ)."""
        symbol = symbol.upper()
        items = self.instruments()
        direct = f"{symbol.replace('.', '_')}_US_EQ"
        for item in items:
            if item.get("ticker") == direct:
                return direct
        for item in items:
            if (
                (item.get("shortName") or "").upper() == symbol
                and item.get("currencyCode") == "USD"
                and item.get("type") in ("STOCK", "ETF")
            ):
                return item["ticker"]
        return None


def symbol_from_ticker(ticker: str) -> str:
    """Trading 212 ticker (AAPL_US_EQ, BRK_B_US_EQ) -> plain symbol (AAPL, BRK.B)."""
    if ticker.endswith("_US_EQ"):
        return ticker[: -len("_US_EQ")].replace("_", ".")
    return ticker.split("_")[0]
