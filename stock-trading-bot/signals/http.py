import hashlib
import json
import time
from pathlib import Path

import requests


class CachedHttp:
    """Small polite HTTP client with an on-disk cache and a per-client rate limit."""

    def __init__(self, cache_dir: Path, headers: dict[str, str], min_interval_s: float, timeout_s: float = 30):
        self.cache_dir = cache_dir
        self.headers = headers
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self._last_request = 0.0
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / hashlib.sha256(key.encode()).hexdigest()

    def _read_cache(self, key: str, ttl_s: float | None) -> str | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        if ttl_s is not None and time.time() - path.stat().st_mtime > ttl_s:
            return None
        return path.read_text()

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get_text(self, url: str, ttl_s: float | None = None) -> str:
        """GET `url`. `ttl_s=None` caches forever (use for immutable documents)."""
        cached = self._read_cache(url, ttl_s)
        if cached is not None:
            return cached
        self._throttle()
        response = requests.get(url, headers=self.headers, timeout=self.timeout_s)
        response.raise_for_status()
        self._cache_path(url).write_text(response.text)
        return response.text

    def get_json(self, url: str, ttl_s: float | None = None):
        return json.loads(self.get_text(url, ttl_s))

    def post_json(self, url: str, payload) -> object:
        self._throttle()
        response = requests.post(url, headers=self.headers, json=payload, timeout=self.timeout_s)
        response.raise_for_status()
        return response.json()
