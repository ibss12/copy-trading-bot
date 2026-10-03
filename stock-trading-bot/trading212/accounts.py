"""Your Trading 212 PRACTICE accounts: one from `.env` plus any you add in the command center.

Each account gets its own folder for its bot ledger, instrument cache and day-start balance, so the
bots of different accounts never mix up whose shares are whose. Keys are stored in
`.cache/trading212/accounts.json` (readable by your user only) and never sent back to the browser.
"""

import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import config

from .client import Trading212Client, validate_base_url
from .ledger import BotLedger

ROOT = config.CACHE_DIR / "trading212"
ENV_ACCOUNT_ID = "default"
ID_RE = re.compile(r"^[a-z0-9]{1,32}$")
MAX_NAME = 40


@dataclass(frozen=True)
class AccountProfile:
    id: str
    name: str
    api_key: str
    api_secret: str
    data_dir: Path
    base_url: str = config.TRADING212_DEMO_URL
    from_env: bool = False

    @property
    def label(self) -> str:
        return f"{self.name} (Trading 212 practice)"

    @property
    def day_start_path(self) -> Path:
        return self.data_dir / "day_start.json"

    def client(self) -> Trading212Client:
        return Trading212Client(self.api_key, self.api_secret, self.base_url, self.data_dir)

    def ledger(self) -> BotLedger:
        return BotLedger(self.data_dir / "bot_ledger.json")

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "from_env": self.from_env}


class AccountStore:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.path = root / "accounts.json"
        self._lock = threading.Lock()

    def env_profile(self) -> AccountProfile | None:
        if not (config.TRADING212_API_KEY and config.TRADING212_API_SECRET):
            return None
        return AccountProfile(
            id=ENV_ACCOUNT_ID,
            name=config.TRADING212_ACCOUNT_NAME,
            api_key=config.TRADING212_API_KEY,
            api_secret=config.TRADING212_API_SECRET,
            base_url=config.TRADING212_BASE_URL,
            data_dir=self.root,
            from_env=True,
        )

    def _read(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text()).get("accounts", [])
        except (OSError, ValueError):
            return []

    def _write(self, accounts: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".accounts")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"accounts": accounts}, fh, indent=1)
        os.replace(tmp, self.path)

    def _profile(self, row: dict) -> AccountProfile:
        return AccountProfile(
            id=row["id"],
            name=row["name"],
            api_key=row["api_key"],
            api_secret=row["api_secret"],
            data_dir=self.root / "accounts" / row["id"],
        )

    def profiles(self) -> list[AccountProfile]:
        env = self.env_profile()
        stored = [self._profile(r) for r in self._read() if ID_RE.match(str(r.get("id", "")))]
        return ([env] if env else []) + stored

    def get(self, account_id: str) -> AccountProfile | None:
        return next((p for p in self.profiles() if p.id == account_id), None)

    def add(self, name: str, api_key: str, api_secret: str) -> AccountProfile:
        name, api_key, api_secret = name.strip()[:MAX_NAME], api_key.strip(), api_secret.strip()
        if not (name and api_key and api_secret):
            raise ValueError("Give the account a name and paste both the API key and the secret")
        validate_base_url(config.TRADING212_DEMO_URL)
        with self._lock:
            rows = self._read()
            if any(r["api_key"] == api_key for r in rows) or api_key == config.TRADING212_API_KEY:
                raise ValueError("That API key is already added")
            if any(p.name.lower() == name.lower() for p in self.profiles()):
                raise ValueError(f"You already have an account called {name!r}")
            row = {"id": uuid.uuid4().hex[:10], "name": name, "api_key": api_key, "api_secret": api_secret}
            self._write([*rows, row])
        return self._profile(row)

    def remove(self, account_id: str) -> None:
        if account_id == ENV_ACCOUNT_ID:
            raise ValueError("This account comes from the .env file; remove TRADING212_API_KEY there instead")
        with self._lock:
            rows = self._read()
            if not any(r["id"] == account_id for r in rows):
                raise KeyError(account_id)
            self._write([r for r in rows if r["id"] != account_id])
