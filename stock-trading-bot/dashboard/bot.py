"""Starts/stops `run_live.py` (Alpaca paper or a Trading 212 practice account) as a child process."""

import asyncio
import os
import re
import sys
import time
from collections import deque
from collections.abc import Callable

import config

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# A bot that crashes after running this long is restarted automatically; quicker crashes are left stopped.
AUTO_RESTART_AFTER_SECONDS = 10 * 60
AUTO_RESTART_DELAY_SECONDS = 60


class BotRunner:
    def __init__(
        self,
        account_id: str,
        account_label: str,
        can_start: Callable[[], bool],
        on_line: Callable[[str], None],
        on_status: Callable[[], None],
        on_crash: Callable[[str, bool], None] = lambda message, restarting: None,
    ):
        self.account_id = account_id
        self.account_label = account_label
        self.can_start = can_start
        self.on_line = on_line
        self.on_status = on_status
        self.on_crash = on_crash
        self.lines: deque[str] = deque(maxlen=400)
        self.process: asyncio.subprocess.Process | None = None
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.strategy: str | None = None
        self.watchlist: list[str] = []
        self._stopping = False

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def status(self) -> dict:
        return {
            "account": self.account_id,
            "running": self.running,
            "pid": self.process.pid if self.running else None,
            "started_at": int(self.started_at * 1000) if self.started_at and self.running else None,
            "exit_code": self.exit_code,
            "strategy": self.strategy,
            "can_start": self.can_start(),
            "broker": config.BROKER,
        }

    async def start(self, watchlist: list[str], strategy: str) -> None:
        if self.running:
            return
        if not self.can_start():
            if config.BROKER == "trading212":
                raise RuntimeError("Add a Trading 212 practice account first")
            raise RuntimeError("Add your Alpaca paper keys to stock-trading-bot/.env first")
        env = {
            **os.environ,
            "WATCHLIST": ",".join(watchlist),
            "STRATEGY": strategy,
            "TRADING212_ACCOUNT": self.account_id,
            "PYTHONUNBUFFERED": "1",
        }
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
        self.watchlist = list(watchlist)
        self._stopping = False
        self._emit(f"Bot started ({strategy}, {self.account_label}) watching {', '.join(watchlist)}")
        self.on_status()
        asyncio.create_task(self._pump(self.process))

    async def stop(self) -> None:
        self._stopping = True
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
        if self._stopping or process is not self.process:
            return
        ran_for = time.time() - (self.started_at or time.time())
        restart = ran_for >= AUTO_RESTART_AFTER_SECONDS
        self.on_crash(
            f"The bot on {self.account_label} stopped unexpectedly (exit code {self.exit_code})."
            + (f" Restarting it in {AUTO_RESTART_DELAY_SECONDS} seconds." if restart else " Check the bot log."),
            restart,
        )
        if restart:
            await asyncio.sleep(AUTO_RESTART_DELAY_SECONDS)
            if not self.running and not self._stopping:
                try:
                    await self.start(self.watchlist, self.strategy or config.STRATEGY)
                except RuntimeError as exc:
                    self._emit(f"Couldn't restart the bot: {exc}")

    def _emit(self, line: str) -> None:
        stamped = f"{time.strftime('%H:%M:%S')}  {line}"
        self.lines.append(stamped)
        self.on_line(stamped)
