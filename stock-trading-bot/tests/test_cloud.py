"""Tests for the cloud/multi-device features: login, multiple practice accounts, push, bot resume.

Run: python -m unittest discover -s tests -v
"""

import asyncio
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from dashboard import auth as auth_module
from dashboard import bot as bot_module
from dashboard import server
from dashboard.auth import COOKIE, Auth
from dashboard.bot import BotRunner
from dashboard.push import PushNotifier
from dashboard.settings import Settings
from dashboard.trading212_account import Trading212Account
from tests.test_trading212 import KEY, SECRET, ServerTestCase
from trading212.accounts import ENV_ACCOUNT_ID, AccountProfile, AccountStore

SUB = {"endpoint": "https://push.example.com/abc", "keys": {"p256dh": "p" * 20, "auth": "a" * 8}}
H = {"X-Command-Center": "1"}


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_token_roundtrip_and_tamper(self):
        auth = Auth("hunter22", self.tmp / "secret")
        token = auth.new_token()
        self.assertTrue(auth.valid(token))
        self.assertFalse(auth.valid(token[:-1] + ("0" if token[-1] != "0" else "1")))
        self.assertFalse(auth.valid(None))
        self.assertFalse(auth.valid("garbage"))
        self.assertEqual(stat.S_IMODE((self.tmp / "secret").stat().st_mode), 0o600)

    def test_expired_token_rejected(self):
        auth = Auth("hunter22", self.tmp / "secret")
        expires = int(time.time()) - 5
        self.assertFalse(auth.valid(f"{expires}.{auth._sign(expires)}"))

    def test_password_change_signs_everyone_out(self):
        token = Auth("old-password", self.tmp / "secret").new_token()
        self.assertFalse(Auth("new-password", self.tmp / "secret").valid(token))

    def test_lockout_after_repeated_failures(self):
        auth = Auth("hunter22", self.tmp / "secret")
        for _ in range(auth_module.MAX_FAILURES):
            self.assertFalse(auth.check_password("1.2.3.4", "nope"))
        self.assertTrue(auth.locked_out("1.2.3.4"))
        self.assertFalse(auth.check_password("1.2.3.4", "hunter22"), "correct password is refused while locked out")
        self.assertTrue(auth.check_password("5.6.7.8", "hunter22"), "other addresses are unaffected")


class AccountStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = AccountStore(self.tmp)
        for name, value in (("TRADING212_API_KEY", ""), ("TRADING212_API_SECRET", "")):
            p = mock.patch.object(server.config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_accounts_get_separate_folders_and_ledgers(self):
        a = self.store.add("Main", "key-a", "secret-a")
        b = self.store.add("ISA", "key-b", "secret-b")
        self.assertNotEqual(a.id, b.id)
        self.assertNotEqual(a.data_dir, b.data_dir)
        a.ledger().apply_fill("o1", "AAPL", 5)
        self.assertEqual(a.ledger().bot_shares().get("AAPL"), 5)
        self.assertEqual(b.ledger().bot_shares().get("AAPL"), None)
        self.assertEqual([p.id for p in AccountStore(self.tmp).profiles()], [a.id, b.id])
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)

    def test_only_practice_server_used(self):
        profile = self.store.add("Main", "key-a", "secret-a")
        self.assertEqual(profile.base_url, "https://demo.trading212.com/api/v0")
        self.assertIn("demo.trading212.com", profile.client().base_url)

    def test_duplicates_and_blanks_rejected(self):
        self.store.add("Main", "key-a", "secret-a")
        with self.assertRaises(ValueError):
            self.store.add("Other", "key-a", "secret-x")
        with self.assertRaises(ValueError):
            self.store.add("main", "key-b", "secret-b")
        with self.assertRaises(ValueError):
            self.store.add("Empty", "key-c", " ")

    def test_env_account_comes_first_and_cannot_be_removed(self):
        with (
            mock.patch.object(server.config, "TRADING212_API_KEY", "env-key"),
            mock.patch.object(server.config, "TRADING212_API_SECRET", "env-secret"),
        ):
            added = self.store.add("Second", "key-b", "secret-b")
            profiles = self.store.profiles()
            self.assertEqual([p.id for p in profiles], [ENV_ACCOUNT_ID, added.id])
            self.assertEqual(profiles[0].data_dir, self.tmp, "the .env account keeps the original ledger location")
            with self.assertRaises(ValueError):
                self.store.remove(ENV_ACCOUNT_ID)
        self.store.remove(added.id)
        self.assertEqual(self.store.profiles(), [])


class TwoAccountsTest(ServerTestCase):
    """Two practice accounts on the (same) fake server: each only counts its own bot's shares."""

    def test_ledgers_do_not_mix(self):
        a = Trading212Account(AccountProfile("a", "A", KEY, SECRET, self.tmp / "a", base_url=self.url))
        b = Trading212Account(AccountProfile("b", "B", KEY, SECRET, self.tmp / "b", base_url=self.url))
        self.fake.positions["AAPL_US_EQ"] = 10.0
        a.ledger.apply_fill("bot-1", "AAPL", 4)
        pos_a, pos_b = a.snapshot()["positions"][0], b.snapshot()["positions"][0]
        self.assertEqual((pos_a["bot_qty"], pos_a["your_qty"]), (4.0, 6.0))
        self.assertEqual((pos_b["bot_qty"], pos_b["your_qty"]), (0.0, 10.0))
        self.assertEqual(a.snapshot()["label"], "A (Trading 212 practice)")

    def test_live_server_refused_for_added_account(self):
        live = AccountProfile("x", "X", KEY, SECRET, self.tmp / "x", base_url="https://live.trading212.com/api/v0")
        account = Trading212Account(live)
        self.assertFalse(account.enabled)
        self.assertIn("practice server", account.error)


class PushRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.push = PushNotifier(self.tmp)
        self.sent: list[tuple[str, dict]] = []
        self.push._queue = mock.Mock(put_nowait=lambda item: self.sent.append((item[0]["endpoint"], item[1])))

    def test_key_is_persistent_and_private(self):
        self.assertEqual(PushNotifier(self.tmp).public_key, self.push.public_key)
        self.assertEqual(stat.S_IMODE(self.push.key_path.stat().st_mode), 0o600)
        self.assertGreater(len(self.push.public_key), 80)

    def test_devices_get_only_their_accounts(self):
        phone_a = {**SUB, "endpoint": "https://push.example.com/a"}
        phone_all = {**SUB, "endpoint": "https://push.example.com/all"}
        self.push.subscribe(phone_a, ["acct-a"], "Phone A")
        self.push.subscribe(phone_all, None, "Phone B")
        self.push.notify({"id": 1, "title": "Bot bought AAPL", "message": "", "account": "acct-b", "symbol": "AAPL"})
        self.assertEqual([e for e, _ in self.sent], [phone_all["endpoint"]])
        self.sent.clear()
        self.push.notify({"id": 2, "title": "NVDA is a BUY", "message": "", "account": None})
        self.assertEqual(sorted(e for e, _ in self.sent), sorted([phone_a["endpoint"], phone_all["endpoint"]]))
        self.assertEqual(self.sent[0][1]["url"], "/")

    def test_payload_has_no_secrets_and_resubscribe_replaces(self):
        self.push.subscribe(SUB, None, "Phone")
        self.push.subscribe(SUB, ["x"], "Phone")
        self.assertEqual(len(self.push.subscriptions()), 1)
        self.push.notify({"id": 3, "title": "t", "message": "m", "symbol": "TSLA", "account": "x"})
        payload = self.sent[0][1]
        self.assertEqual(set(payload), {"title", "body", "tag", "level", "url"})
        self.assertEqual(payload["url"], "/?symbol=TSLA")

    def test_invalid_subscription_rejected(self):
        with self.assertRaises(ValueError):
            self.push.subscribe({"endpoint": "http://insecure"}, None, "x")

    def test_gone_device_is_removed(self):
        from pywebpush import WebPushException

        self.push.subscribe(SUB, None, "Phone")
        gone = WebPushException("gone", response=mock.Mock(status_code=410))
        with mock.patch("dashboard.push.webpush", side_effect=gone):
            self.push.send(self.push.subscriptions()[0], {"level": "info", "title": "t"})
        self.assertEqual(self.push.subscriptions(), [])


class ServerAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        hub = server.hub
        self.push = PushNotifier(self.tmp / "push")
        self.push._queue = mock.Mock(put_nowait=lambda item: None)
        for attr, value in (("auth", Auth("hunter22", self.tmp / "secret")), ("push", self.push)):
            p = mock.patch.object(hub, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(server.app, base_url="https://testserver")

    def login(self):
        res = self.client.post("/api/login", json={"password": "hunter22"}, headers=H)
        self.assertEqual(res.status_code, 200)
        self.assertIn(COOKIE, res.cookies)

    def test_signed_out_is_blocked(self):
        res = self.client.get("/", follow_redirects=False)
        self.assertEqual((res.status_code, res.headers["location"]), (303, "/login"))
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        self.assertEqual(self.client.get("/api/stream").status_code, 401)
        self.assertEqual(self.client.post("/api/bot/start", json={}, headers=H).status_code, 401)

    def test_install_files_are_public(self):
        self.assertEqual(self.client.get("/login").status_code, 200)
        manifest = self.client.get("/manifest.webmanifest")
        self.assertEqual(manifest.json()["display"], "standalone")
        self.assertEqual(self.client.get("/sw.js").status_code, 200)
        self.assertEqual(self.client.get("/static/icons/icon-512.png").status_code, 200)
        self.assertEqual(self.client.get("/static/app.js", follow_redirects=False).status_code, 303)

    def test_wrong_password(self):
        with mock.patch.object(server.asyncio, "sleep", new=mock.AsyncMock()):
            res = self.client.post("/api/login", json={"password": "nope"}, headers=H)
        self.assertEqual(res.status_code, 401)
        self.assertNotIn(COOKIE, res.cookies)

    def test_login_sets_secure_cookie_and_allows_access(self):
        res = self.client.post("/api/login", json={"password": "hunter22"}, headers=H)
        cookie = res.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=lax"):
            self.assertIn(flag, cookie)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 200)
        res = self.client.post("/api/push/device", json={"endpoint": SUB["endpoint"]}, headers=H)
        self.assertEqual(res.json(), {"subscribed": False, "accounts": None})
        self.assertEqual(self.client.post("/api/push/device", json={"endpoint": "x"}).status_code, 403)

    def test_push_subscribe_and_test(self):
        self.login()
        res = self.client.post(
            "/api/push/subscribe", json={"subscription": SUB, "accounts": ["a"], "device": "Phone"}, headers=H
        )
        self.assertEqual(res.json()["accounts"], ["a"])
        self.assertEqual(
            self.client.post("/api/push/test", json={"endpoint": SUB["endpoint"]}, headers=H).status_code, 200
        )
        self.client.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]}, headers=H)
        self.assertEqual(
            self.client.post("/api/push/test", json={"endpoint": SUB["endpoint"]}, headers=H).status_code, 404
        )

    def test_no_password_only_answers_this_computer(self):
        with mock.patch.object(server.hub, "auth", Auth("", self.tmp / "s2")):
            self.assertEqual(self.client.get("/api/state").status_code, 403)

    def test_self_update_disabled_by_default(self):
        self.login()
        with mock.patch.object(server.config, "SELF_UPDATE", False):
            self.assertEqual(self.client.post("/api/system/update", headers=H).status_code, 400)


class HubAccountsTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        hub = server.hub
        patches = [
            mock.patch.object(hub, "auth", Auth("hunter22", self.tmp / "secret")),
            mock.patch.object(hub, "settings", Settings(self.tmp / "settings.json")),
            mock.patch.object(hub, "store", AccountStore(self.tmp / "t212")),
            mock.patch.object(hub, "views", {}),
            mock.patch.object(hub, "push", mock.Mock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.hub = hub
        self.a = hub.add_account(AccountProfile("acca", "A", KEY, SECRET, self.tmp / "a", base_url=self.url))
        self.b = hub.add_account(AccountProfile("accb", "B", KEY, SECRET, self.tmp / "b", base_url=self.url))
        self.client = TestClient(server.app, base_url="https://testserver")
        self.client.post("/api/login", json={"password": "hunter22"}, headers=H)

    def test_order_goes_to_selected_account_ledger(self):
        res = self.client.post(
            "/api/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy", "account": "accb"}, headers=H
        )
        self.assertEqual(res.status_code, 200, res.text)
        order_id = res.json()["id"]
        self.assertTrue(self.b.account.ledger.is_manual_order(order_id))
        self.assertFalse(self.a.account.ledger.is_manual_order(order_id))
        self.assertEqual(
            self.client.post(
                "/api/orders", json={"symbol": "AAPL", "qty": 1, "side": "buy", "account": "nope"}, headers=H
            ).status_code,
            404,
        )

    def test_state_lists_accounts_without_keys(self):
        with mock.patch.object(self.hub, "advice", return_value={}, create=True):
            body = self.hub.accounts_public()
        self.assertEqual([v["id"] for v in body], ["acca", "accb"])
        self.assertNotIn(KEY, str(body))
        self.assertNotIn(SECRET, str(body))

    def test_bot_start_is_remembered_and_stop_forgets(self):
        with mock.patch.object(BotRunner, "start", new=mock.AsyncMock()) as start:
            res = self.client.post("/api/bot/start", json={"strategy": "sma", "account": "accb"}, headers=H)
        self.assertEqual(res.status_code, 200, res.text)
        start.assert_awaited_once()
        self.assertEqual(self.hub.settings["running_bots"], {"accb": "sma"})
        self.client.post("/api/bot/stop", json={"account": "accb"}, headers=H)
        self.assertEqual(self.hub.settings["running_bots"], {})

    def test_resume_bots_after_restart(self):
        self.hub.settings.set_bot_running("acca", "smart_money")
        self.hub.settings.set_bot_running("gone", "sma")
        with mock.patch.object(BotRunner, "start", new=mock.AsyncMock()) as start:
            asyncio.run(self.hub.resume_bots())
        start.assert_awaited_once_with(self.hub.settings["watchlist"], "smart_money")

    def test_remove_account(self):
        added = self.hub.store.add("C", "key-c", "secret-c")
        self.hub.add_account(added)
        self.hub.settings.set_bot_running(added.id, "sma")
        self.assertEqual(self.client.delete(f"/api/accounts/{added.id}", headers=H).status_code, 200)
        self.assertNotIn(added.id, self.hub.views)
        self.assertEqual(self.hub.store.profiles(), [])
        self.assertNotIn(added.id, self.hub.settings["running_bots"])


class BotRunnerTest(unittest.TestCase):
    """Uses a harmless `sleep` child instead of run_live.py."""

    def setUp(self):
        self.calls: list[dict] = []
        self.crashes: list[tuple[str, bool]] = []
        real_exec = asyncio.create_subprocess_exec

        async def fake_exec(*args, env=None, **kwargs):
            self.calls.append(env)
            return await real_exec(sys.executable, "-c", self.child, env=env, **kwargs)

        self.child = "import time; print('hello'); time.sleep(30)"
        p = mock.patch.object(bot_module.asyncio, "create_subprocess_exec", fake_exec)
        p.start()
        self.addCleanup(p.stop)

    def runner(self):
        return BotRunner(
            "acct1", "A", lambda: True, lambda line: None, lambda: None, lambda m, r: self.crashes.append((m, r))
        )

    def test_no_duplicate_processes_and_account_passed(self):
        async def go():
            bot = self.runner()
            await bot.start(["AAPL"], "sma")
            await bot.start(["AAPL"], "sma")
            self.assertTrue(bot.running)
            await bot.stop()
            await asyncio.sleep(0.2)
            return bot

        bot = asyncio.run(go())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["TRADING212_ACCOUNT"], "acct1")
        self.assertFalse(bot.running)
        self.assertEqual(self.crashes, [], "a requested stop is not a crash")

    def test_quick_crash_is_reported_not_restarted(self):
        self.child = "import sys; sys.exit(3)"

        async def go():
            bot = self.runner()
            await bot.start(["AAPL"], "sma")
            await bot.process.wait()
            await asyncio.sleep(0.3)

        asyncio.run(go())
        self.assertEqual(len(self.crashes), 1)
        self.assertFalse(self.crashes[0][1])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
