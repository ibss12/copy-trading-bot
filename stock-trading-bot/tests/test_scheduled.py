"""Scheduled (GitHub Actions + Discord) bot check against the fake Trading 212 server. No network.

Run: python -m unittest tests.test_scheduled -v
"""

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import config
from notify.discord import DiscordNotifier
from scheduled.check import ScheduledCheck, Session
from signals.feed import SignalFeed
from signals.models import TraderSignal
from strategies.rules import SmaSignal
from tests.test_trading212 import KEY, SECRET, ServerTestCase
from trading212.accounts import AccountProfile

NOW = datetime(2026, 9, 30, 13, 59, tzinfo=timezone.utc)
OPEN = Session(NOW - timedelta(minutes=29), NOW + timedelta(hours=6))
CLOSED = Session(NOW - timedelta(hours=8), NOW - timedelta(hours=1))
TODAY = date(2026, 9, 30)


def series(*prices: float) -> list[tuple[date, float]]:
    return [(TODAY - timedelta(days=len(prices) - 1 - i), p) for i, p in enumerate(prices)]


FLAT = series(10, 10, 10, 10, 10)
GOLDEN = series(10, 10, 10, 9, 12)  # 2-day SMA crosses above 3-day SMA today
DEATH = series(10, 10, 10, 11, 8)


class Capture(DiscordNotifier):
    def __init__(self):
        super().__init__("", echo=False)
        self.sent: list[str] = []
        self.bodies: list[str] = []

    def flush(self) -> int:
        self.sent += [n.title for n in self.notices]
        self.bodies += [n.body for n in self.notices]
        self.notices = []
        return 0


SETTINGS = {
    "STRATEGY": "sma",
    "WATCHLIST": ["AAPL", "MSFT"],
    "SMA_SHORT_WINDOW": 2,
    "SMA_LONG_WINDOW": 3,
    "MAX_POSITION_PCT": 0.10,
    "MAX_OPEN_POSITIONS": 8,
    "MAX_DAILY_LOSS_PCT": 0.03,
    "LIQUIDATE_ON_DAILY_LOSS": False,
    "PRICE_ALERT_PCTS": [5.0, 10.0],
    "SIGNAL_LOOKBACK_DAYS": 45,
    "FOLLOW_EXPANDS_WATCHLIST": True,
    "REQUIRE_SMA_CONFIRMATION": True,
}


class ScheduledTestCase(ServerTestCase):
    def setUp(self):
        super().setUp()
        for name, value in SETTINGS.items():
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.profile = AccountProfile(
            id="default", name="Test", api_key=KEY, api_secret=SECRET, data_dir=self.tmp, base_url=self.url
        )
        self.notifier = Capture()
        self.state_path = self.tmp / "state.json"

    def check(self, closes=None, now=NOW, session=OPEN, trade=True, feed=None):
        closes = closes or {}
        return ScheduledCheck(
            self.notifier,
            self.profile if trade else None,
            now=now,
            state_path=self.state_path,
            session_for=lambda day: session,
            closes_for=lambda symbol: closes.get(symbol, FLAT),
            feed_loader=lambda watchlist, since: feed or SignalFeed([]),
            fx=lambda currency: 1.0,
        )

    def sent(self, text: str) -> list[str]:
        return [t for t in self.notifier.sent if text in t]

    @property
    def bot_ledger(self):
        return self.profile.ledger()


class TradingTest(ScheduledTestCase):
    def test_buy_is_sent_once_then_fill_becomes_bot_shares(self):
        self.check({"AAPL": GOLDEN}).run()
        self.assertEqual(self.fake.posts, [{"ticker": "AAPL_US_EQ", "quantity": 83, "extendedHours": False}])
        self.assertTrue(self.sent("Bot BUYING 83 AAPL"))
        self.assertTrue(self.sent("Stock bot is connected"))

        self.check({"AAPL": GOLDEN}, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual(len(self.fake.posts), 1, "no second buy while the first is still open")

        self.fake.fill(self.fake.pending[0]["id"], price=12.0)
        self.check({"AAPL": GOLDEN}, now=NOW + timedelta(minutes=60)).run()
        self.assertEqual(self.bot_ledger.bot_shares(), {"AAPL": 83})
        self.assertTrue(self.sent("Filled: bot bought 83 AAPL at $12.00"))
        self.assertEqual(len(self.fake.posts), 1, "already holds it: no new buy")
        self.assertEqual(len(self.sent("Stock bot is connected")), 1)

    def test_never_sells_your_own_shares(self):
        self.fake.positions["AAPL_US_EQ"] = 5
        self.check({"AAPL": DEATH}).run()
        self.assertEqual(self.fake.posts, [])

    def test_sell_is_capped_to_the_bots_shares(self):
        self.fake.positions["AAPL_US_EQ"] = 10
        self.bot_ledger.apply_fill("old", "AAPL", 3)
        self.check({"AAPL": DEATH}).run()
        self.assertEqual([p["quantity"] for p in self.fake.posts], [-3])
        self.assertTrue(self.sent("Bot SELLING 3 AAPL"))

    def test_uncertain_order_is_never_resent_and_found_later(self):
        self.fake.post_status = 500
        self.fake.place_on_error = True
        self.check({"AAPL": GOLDEN}).run()
        self.assertTrue(self.sent("Not sure the bot's BUY 83 AAPL"))
        self.fake.post_status = 200
        self.check({"AAPL": GOLDEN}, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual(len(self.fake.posts), 1)
        self.assertTrue(self.sent("BUY 83 AAPL order did go through"))
        self.assertTrue(self.bot_ledger.is_bot_order(self.fake.pending[0]["id"]))

    def test_uncertain_order_that_never_arrived_is_dropped_not_resent(self):
        self.fake.post_status = 500
        self.check({"AAPL": GOLDEN}).run()
        self.fake.post_status = 200
        self.check({"AAPL": GOLDEN}, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual(len(self.fake.posts), 1, "still unsure: wait, don't resend")
        self.check({"AAPL": FLAT}, now=NOW + timedelta(minutes=60)).run()
        self.assertTrue(self.sent("BUY 83 AAPL order was not placed"))
        self.assertEqual(len(self.fake.posts), 1)

    def test_daily_loss_limit_blocks_buys(self):
        self.state_path.write_text(json.dumps({"welcomed": True, "day": TODAY.isoformat(), "day_start_value": 20000}))
        self.check({"AAPL": GOLDEN}).run()
        self.assertTrue(self.sent("Daily loss limit hit"))
        self.assertEqual(self.fake.posts, [])

    def test_max_open_positions(self):
        config.MAX_OPEN_POSITIONS = 1
        self.check({"AAPL": GOLDEN, "MSFT": GOLDEN}).run()
        self.assertEqual([p["ticker"] for p in self.fake.posts], ["AAPL_US_EQ"])

    def test_closed_market_no_trades_and_one_summary(self):
        self.check({"AAPL": GOLDEN}, session=CLOSED).run()
        self.check({"AAPL": GOLDEN}, session=CLOSED, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(len(self.sent("Daily summary")), 1)

    def test_holiday_does_nothing(self):
        self.check({"AAPL": GOLDEN}, session=None).run()
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.sent("Daily summary"), [])

    def test_bad_keys_warn_once_a_day(self):
        self.profile = AccountProfile(
            id="default", name="Test", api_key="wrong", api_secret="x", data_dir=self.tmp, base_url=self.url
        )
        self.check({"AAPL": GOLDEN}).run()
        self.check({"AAPL": GOLDEN}, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual(len(self.sent("Couldn't reach your Trading 212 practice account")), 1)
        self.assertEqual(self.fake.posts, [])

    def test_refuses_real_money_server(self):
        self.profile = AccountProfile(
            id="default",
            name="Test",
            api_key=KEY,
            api_secret=SECRET,
            data_dir=self.tmp,
            base_url="https://live.trading212.com/api/v0",
        )
        with self.assertRaises(ValueError):
            self.check()


class AlertsTest(ScheduledTestCase):
    def test_advice_without_trading_keys(self):
        self.check({"AAPL": FLAT}, trade=False).run()
        self.check({"AAPL": GOLDEN}, trade=False, now=NOW + timedelta(minutes=30)).run()
        self.assertTrue(self.sent("AAPL is now a BUY"))
        self.assertEqual(self.fake.posts, [])

    def test_new_filings_are_sent_once(self):
        config.STRATEGY = "smart_money"
        old = TraderSignal("Warren Buffett", "13F", "MSFT", "buy", TODAY - timedelta(days=3), None, "added 30%")
        new = TraderSignal("Nancy Pelosi", "Congress", "AAPL", "sell", TODAY, TODAY - timedelta(days=20), "sold")
        self.check(feed=SignalFeed([old]), trade=False, session=None).run()
        self.assertTrue(self.sent("Following 1 recent big-trader filings"))
        self.check(feed=SignalFeed([old, new]), trade=False, session=None).run()
        self.check(feed=SignalFeed([old, new]), trade=False, session=None).run()
        self.assertEqual(self.sent("Warren Buffett"), [])
        self.assertEqual(len(self.sent("Nancy Pelosi sold AAPL")), 1)

    def test_other_insiders_are_announced_but_never_traded(self):
        config.STRATEGY = "smart_money"
        cfo = TraderSignal("Jane Doe (CFO)", "SEC Form 4", "AAPL", "buy", TODAY, TODAY, "bought", notify_only=True)
        self.check(feed=SignalFeed([]), trade=False, session=None).run()
        self.check(feed=SignalFeed([cfo]), trade=False, session=None).run()
        self.assertEqual(self.sent("Jane Doe (CFO) bought AAPL"), ["Jane Doe (CFO) bought AAPL"])
        self.assertEqual(len([b for b in self.notifier.bodies if "For your info only" in b]), 1)
        self.assertEqual(SignalFeed([cfo]).net_scores(TODAY, 45), {})
        self.assertEqual(SignalFeed([cfo]).reasons("AAPL", TODAY, 45), [])

    def test_price_move_alert_once_per_level(self):
        jump = series(100, 100, 100, 100, 106)
        self.check({"MSFT": jump}, trade=False).run()
        self.check({"MSFT": jump}, trade=False, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual(len(self.sent("MSFT up 6.0% today")), 1)


class DiscordNotifierTest(unittest.TestCase):
    def test_batches_and_retries_rate_limit(self):
        notifier = DiscordNotifier("https://discord.example/api/webhooks/1/abc")
        for i in range(12):
            notifier.add("info", f"n{i}", "x" * 5000)
        limited = mock.Mock(status_code=429)
        limited.json.return_value = {"retry_after": 0.01}
        ok = mock.Mock(status_code=200)
        with mock.patch("notify.discord.requests.post", side_effect=[limited, ok, ok]) as post, mock.patch(
            "notify.discord.time.sleep"
        ):
            self.assertEqual(notifier.flush(), 12)
        payloads = [c.kwargs["json"] for c in post.call_args_list]
        self.assertEqual([len(p["embeds"]) for p in payloads], [10, 10, 2])
        self.assertLessEqual(len(payloads[0]["embeds"][0]["description"]), 4096)
        self.assertEqual(payloads[0]["allowed_mentions"], {"parse": []})

    def test_without_webhook_nothing_is_sent(self):
        notifier = DiscordNotifier("", echo=False)
        notifier.add("info", "hello")
        with mock.patch("notify.discord.requests.post") as post:
            self.assertEqual(notifier.flush(), 0)
        post.assert_not_called()


class RulesTest(unittest.TestCase):
    def test_crossovers(self):
        up = SmaSignal.from_closes([c for _, c in GOLDEN], 2, 3)
        down = SmaSignal.from_closes([c for _, c in DEATH], 2, 3)
        self.assertTrue(up.crossed_up and up.trend_up and not up.crossed_down)
        self.assertTrue(down.crossed_down and not down.trend_up)
        self.assertIsNone(SmaSignal.from_closes([1, 2, 3], 2, 3))


if __name__ == "__main__":
    unittest.main()
