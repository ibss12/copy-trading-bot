"""Starts/stops `run_live.py` (Alpaca paper) as a child process and streams its output."""

import asyncio
import os
import re
import sys
import time
from collections import deque
from collections.abc import Callable

import config

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class BotRunner:
    def __init__(self, on_line: Callable[[str], None], on_status: Callable[[], None]):
        self.on_line = on_line
        self.on_status = on_status
        self.lines: deque[str] = deque(maxlen=400)
        self.process: asyncio.subprocess.Process | None = None
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.strategy: str | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def status(self) -> dict:
        return {
            "running": self.running,
            "pid": self.process.pid if self.running else None,
            "started_at": int(self.started_at * 1000) if self.started_at and self.running else None,
            "exit_code": self.exit_code,
            "strategy": self.strategy,
            "can_start": bool(config.ALPACA_API_KEY and config.ALPACA_API_SECRET),
        }

    async def start(self, watchlist: list[str], strategy: str) -> None:
        if self.running:
            return
        if not (config.ALPACA_API_KEY and config.ALPACA_API_SECRET):
            raise RuntimeError("Add your Alpaca paper keys to stock-trading-bot/.env first")
        env = {**os.environ, "WATCHLIST": ",".join(watchlist), "STRATEGY": strategy, "PYTHONUNBUFFERED": "1"}
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(config.BASE_DIR / "run_live.py"),
            cwd=config.BASE_DIR,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self.started_at = time.time()
        self.exit_code = None
        self.strategy = strategy
        self._emit(f"Bot started ({strategy}, paper account) watching {', '.join(watchlist)}")
        self.on_status()
        asyncio.create_task(self._pump(self.process))

    async def stop(self) -> None:
        if not self.running:
            return
        self._emit("Stopping bot...")
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), 15)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()

    async def _pump(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            line = ANSI.sub("", raw.decode(errors="replace")).rstrip()
            if line:
                self._emit(line)
        self.exit_code = await process.wait()
        self._emit(f"Bot stopped (exit code {self.exit_code})")
        self.on_status()

    def _emit(self, line: str) -> None:
        stamped = f"{time.strftime('%H:%M:%S')}  {line}"
        self.lines.append(stamped)
        self.on_line(stamped)
