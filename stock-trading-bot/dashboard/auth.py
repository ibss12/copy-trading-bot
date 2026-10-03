"""Password login for the command center, so only you can open it when it's on the internet."""

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

import config

COOKIE = "cc_session"
SESSION_DAYS = 90
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _load_secret(path: Path) -> bytes:
    try:
        return bytes.fromhex(path.read_text().strip())
    except (OSError, ValueError):
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secret.hex())
        return secret


class Auth:
    def __init__(
        self, password: str = config.DASHBOARD_PASSWORD, secret_path: Path = config.CACHE_DIR / "session_secret"
    ):
        self.password = password
        self._secret = _load_secret(secret_path) if password else b""
        # Changing the password signs everyone out.
        self._password_tag = hashlib.sha256(password.encode()).hexdigest()[:16] if password else ""
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def _sign(self, expires: int) -> str:
        msg = f"{expires}:{self._password_tag}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def new_token(self) -> str:
        expires = int(time.time()) + SESSION_DAYS * 86400
        return f"{expires}.{self._sign(expires)}"

    def valid(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token or "." not in token:
            return False
        raw_expires, sig = token.split(".", 1)
        if not raw_expires.isdigit() or int(raw_expires) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(int(raw_expires)))

    def locked_out(self, client: str) -> bool:
        attempts = self._failures[client]
        while attempts and attempts[0] < time.time() - LOCKOUT_SECONDS:
            attempts.popleft()
        return len(attempts) >= MAX_FAILURES

    def check_password(self, client: str, password: str) -> bool:
        if self.locked_out(client):
            return False
        ok = hmac.compare_digest(password.encode(), self.password.encode())
        if ok:
            self._failures.pop(client, None)
        else:
            self._failures[client].append(time.time())
        return ok
