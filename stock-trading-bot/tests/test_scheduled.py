"""Scheduled (GitHub Actions + Discord) bot check against the fake Trading 212 server. No network.

Run: python -m unittest tests.test_scheduled -v
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import config
from notify.discord import MAX_DESCRIPTION, DiscordNotifier
from scheduled.check import SUMMARY_DELAY, ScheduledCheck, Session, next_check_at
from signals.feed import SignalFeed
from signals.models import InsiderTrade, TraderSignal
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
    "INSIDER_DAILY_SUMMARY": True,
    "WEEKLY_SCORECARD": True,
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

    def check(self, closes=None, now=NOW, session=OPEN, trade=True, feed=None, insider_loader=None, session_for=None):
        closes = closes or {}
        return ScheduledCheck(
            self.notifier,
            self.profile if trade else None,
            now=now,
            state_path=self.state_path,
            session_for=session_for or (lambda day: session),
            closes_for=lambda symbol: closes.get(symbol, FLAT),
            feed_loader=lambda watchlist, since: feed or SignalFeed([]),
            fx=lambda currency: 1.0,
            insider_loader=insider_loader or (lambda watchlist, since: []),
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

    def test_stop_loss_from_the_bots_fill_price(self):
        self.check({"AAPL": GOLDEN}).run()
        self.fake.fill(self.fake.pending[0]["id"], price=12.0)
        drop = series(10, 10, 10, 9, 12, 10.5)
        self.check({"AAPL": drop}, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual([p["quantity"] for p in self.fake.posts], [83, -83])
        self.assertTrue(any("Why: stop-loss" in b and "entry 12.00" in b for b in self.notifier.bodies))

    def test_trailing_stop_remembers_the_peak_between_runs(self):
        self.fake.positions["AAPL_US_EQ"] = 3
        self.bot_ledger.apply_fill("old", "AAPL", 3)
        with mock.patch.object(config, "TAKE_PROFIT_PCT", None):
            self.check({"AAPL": series(90, 95, 100, 105, 120)}).run()
            self.assertEqual(self.fake.posts, [], "entry seeded from Trading 212's average price (100)")
            self.check({"AAPL": series(95, 100, 105, 120, 105)}, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual([p["quantity"] for p in self.fake.posts], [-3])
        self.assertTrue(any("Why: trailing stop" in b and "peak 120.00" in b for b in self.notifier.bodies))

    def test_take_profit_from_the_bots_fill_price(self):
        self.check({"AAPL": GOLDEN}).run()
        self.fake.fill(self.fake.pending[0]["id"], price=12.0)
        self.check({"AAPL": series(10, 10, 12, 13, 16)}, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual([p["quantity"] for p in self.fake.posts], [83, -83])
        self.assertTrue(any("Why: take-profit" in b and "entry 12.00" in b for b in self.notifier.bodies))

    def test_exit_below_short_sma(self):
        self.check({"AAPL": GOLDEN}).run()
        self.fake.fill(self.fake.pending[0]["id"], price=12.0)
        self.check({"AAPL": series(10, 10, 12, 13, 12.2)}, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual([p["quantity"] for p in self.fake.posts], [83, -83])
        self.assertTrue(any("Why: below short SMA" in b for b in self.notifier.bodies))

    def test_mixed_shares_ignore_trading212_average_price(self):
        self.fake.positions["AAPL_US_EQ"] = 10
        self.bot_ledger.apply_fill("old", "AAPL", 3)
        self.check({"AAPL": FLAT}).run()
        self.assertEqual(self.fake.posts, [], "average price includes your shares, so entry is the first price seen")
        self.check({"AAPL": series(10, 10, 10, 10, 8.9)}, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual([p["quantity"] for p in self.fake.posts], [-3])
        self.assertTrue(any("Why: stop-loss" in b and "entry 10.00" in b for b in self.notifier.bodies))

    def test_rsi_filter_blocks_buy(self):
        hot = series(*[10 + i for i in range(20)], 22, 40)  # crosses up with RSI(14) ~82
        with mock.patch.object(config, "RSI_MAX_FOR_ENTRY", 70.0):
            self.check({"AAPL": hot}).run()
        self.assertEqual(self.fake.posts, [])
        self.check({"AAPL": hot}, now=NOW + timedelta(minutes=30)).run()
        self.assertEqual(len(self.fake.posts), 1)

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
        self.assertEqual(self.sent("Weekly scorecard"), [], "Wednesday is not the last trading day of the week")

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

    def test_daily_insider_digest_groups_buys_before_sales_and_runs_once(self):
        trades = [
            InsiderTrade("MSFT", "Seller", "director", "sell", 10, 1000, False, False, TODAY, "s1"),
            InsiderTrade("MSFT", "Planned", "officer", "sell", 10, 2000, True, False, TODAY, "s2"),
            InsiderTrade("MSFT", "Tax", "insider", "sell", 10, 500, False, True, TODAY, "s3"),
            InsiderTrade("TSLA", "Single seller", "director", "sell", 2, 400, False, False, TODAY, "s4"),
            InsiderTrade("AAPL", "Jane Doe", "CEO", "buy", 100, 2_000_000, False, False, TODAY, "b1"),
            InsiderTrade("AAPL", "Planned Seller", "CFO", "sell", 50, 500_000, True, False, TODAY, "s5"),
        ]

        def loader(watchlist, since):
            return trades

        self.check(trade=False, session=CLOSED, insider_loader=loader).run()
        digests = [i for i, title in enumerate(self.notifier.sent) if "Insider trades:" in title]
        self.assertEqual(len(digests), 1)
        body = self.notifier.bodies[digests[0]]
        self.assertIn("AAPL: BOUGHT $2.0M (Jane Doe, CEO) · SOLD $500k by Planned Seller, CFO (1 pre-planned)", body)
        self.assertIn("MSFT: SOLD $4k by 3 people (1 pre-planned, 1 tax)", body)
        self.assertIn("TSLA: SOLD $400 by Single seller, director", body)
        self.assertLess(body.index("AAPL:"), body.index("MSFT:"))

        self.check(trade=False, session=CLOSED, insider_loader=loader).run()
        self.assertEqual(len([title for title in self.notifier.sent if "Insider trades:" in title]), 1)

    def test_daily_insider_digest_truncates_long_stock_lines_with_correct_count(self):
        trades = [
            InsiderTrade(
                f"X{i:02}",
                "N" * 180,
                "R" * 100,
                "buy",
                1,
                100,
                False,
                False,
                TODAY,
                f"long-{i}",
            )
            for i in range(30)
        ]
        self.check(
            trade=False,
            session=CLOSED,
            insider_loader=lambda watchlist, since: trades,
        ).run()

        body = self.notifier.bodies[self.notifier.sent.index("Insider trades: Wed 30 Sep")]
        legend = (
            "Pre-planned = scheduled months ahead (10b5-1), usually less meaningful. "
            "Tax = shares sold to pay tax on vested stock."
        )
        stock_lines = [line for line in body.splitlines() if line != legend and not line.startswith("...and ")]
        omitted_line = next(line for line in body.splitlines() if line.startswith("...and "))
        omitted = int(omitted_line.removeprefix("...and ").removesuffix(" more stocks"))

        self.assertLessEqual(len(body), MAX_DESCRIPTION)
        self.assertTrue(body.endswith(legend))
        self.assertLessEqual(len(stock_lines), 25)
        self.assertEqual(omitted, len(trades) - len(stock_lines))

    def test_daily_insider_digest_first_run_shows_today_only_then_only_unseen(self):
        yesterday = TODAY - timedelta(days=1)
        old = InsiderTrade("AAPL", "Old", "director", "buy", 1, 100, False, False, yesterday, "old")
        today = InsiderTrade("AAPL", "Today", "director", "buy", 1, 200, False, False, TODAY, "today")
        self.check(trade=False, session=CLOSED, insider_loader=lambda watchlist, since: [old, today]).run()
        first = self.notifier.bodies[self.notifier.sent.index("Insider trades: Wed 30 Sep")]
        self.assertIn("Today, director", first)
        self.assertNotIn("Old, director", first)

        tomorrow = TODAY + timedelta(days=1)
        new = InsiderTrade("MSFT", "New", "officer", "sell", 2, 300, False, False, tomorrow, "new")
        next_day = NOW + timedelta(days=1)
        session = Session(next_day - timedelta(hours=8), next_day - timedelta(hours=1))
        self.check(
            trade=False,
            now=next_day,
            session=session,
            insider_loader=lambda watchlist, since: [old, today, new],
        ).run()
        digest = [body for title, body in zip(self.notifier.sent, self.notifier.bodies) if "Insider trades:" in title][
            -1
        ]
        self.assertIn("MSFT: SOLD $300 by New, officer", digest)
        self.assertNotIn("AAPL:", digest)

    def test_insider_loader_error_keeps_daily_summary_and_warns_once(self):
        def fail(watchlist, since):
            raise RuntimeError("SEC unavailable")

        self.check(trade=False, session=CLOSED, insider_loader=fail).run()
        self.check(trade=False, session=CLOSED, insider_loader=fail).run()
        self.assertEqual(len(self.sent("Daily summary")), 1)
        self.assertEqual(len(self.sent("Couldn't load insider trades")), 1)

    def test_insider_digest_can_be_disabled(self):
        with mock.patch.object(config, "INSIDER_DAILY_SUMMARY", False):
            self.check(trade=False, session=CLOSED, insider_loader=lambda watchlist, since: []).run()
        self.assertEqual(self.sent("Insider trades:"), [])


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


class NextCheckTest(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 9, 30)
        self.open = datetime(2026, 9, 30, 13, 30, tzinfo=timezone.utc)
        self.close = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
        self.session = Session(self.open, self.close)
        self.sessions = {self.today: self.session}
        self.session_for = lambda day: self.sessions.get(day)

    def test_preopen_and_mid_session(self):
        self.assertEqual(
            next_check_at(datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc), {}, self.session_for),
            self.open + timedelta(minutes=2),
        )
        self.assertEqual(
            next_check_at(datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc), {}, self.session_for),
            datetime(2026, 9, 30, 15, 15, tzinfo=timezone.utc),
        )

    def test_near_close_and_after_close_summary_pending(self):
        near_close = datetime(2026, 9, 30, 19, 55, tzinfo=timezone.utc)
        expected = self.close + SUMMARY_DELAY + timedelta(minutes=1)
        self.assertEqual(next_check_at(near_close, {}, self.session_for), expected)
        just_after_close = self.close + timedelta(minutes=1)
        self.assertEqual(next_check_at(just_after_close, {}, self.session_for), expected)
        later_after_close = self.close + timedelta(minutes=30)
        self.assertEqual(
            next_check_at(later_after_close, {}, self.session_for),
            later_after_close + timedelta(minutes=15),
        )

    def test_after_close_summary_done_plans_next_session(self):
        tomorrow = self.today + timedelta(days=1)
        next_open = datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)
        self.sessions[tomorrow] = Session(next_open, next_open + timedelta(hours=6))
        now = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
        self.sessions[self.today] = Session(self.open, now - timedelta(hours=1))
        self.assertEqual(
            next_check_at(now, {"summary_day": self.today.isoformat()}, self.session_for),
            next_open + timedelta(minutes=2),
        )

    def test_friday_evening_caps_weekend_sleep(self):
        friday = date(2026, 10, 2)
        monday = date(2026, 10, 5)
        now = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)
        sessions = {
            friday: Session(now - timedelta(hours=2), now - timedelta(hours=1)),
            monday: Session(datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc), self.close),
        }
        planned = next_check_at(now, {"summary_day": friday.isoformat()}, sessions.get)
        self.assertEqual(planned, now + timedelta(hours=5, minutes=30))

    def test_holiday_finds_next_available_session(self):
        holiday = self.today
        next_day = holiday + timedelta(days=1)
        now = datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc)
        next_open = now + timedelta(hours=4)
        sessions = {next_day: Session(next_open, next_open + timedelta(hours=6))}
        self.assertEqual(next_check_at(now, {}, sessions.get), next_open + timedelta(minutes=2))


class PlanNextTest(unittest.TestCase):
    def test_plan_flag_writes_output_and_falls_back_without_config_validation(self):
        import run_scheduled

        with self.subTest("scheduled"):
            with tempfile.TemporaryDirectory() as directory:
                state_path = Path(directory) / "state.json"
                output_path = Path(directory) / "output.txt"
                state_path.write_text('{"summary_day":"2026-09-30"}')
                planned = datetime(2026, 9, 30, 20, 6, tzinfo=timezone.utc)
                stdout = io.StringIO()
                with (
                    mock.patch.object(run_scheduled, "STATE_PATH", state_path),
                    mock.patch.object(run_scheduled, "next_check_at", return_value=planned) as planner,
                    mock.patch.object(run_scheduled.config, "validate", side_effect=AssertionError),
                    mock.patch.object(run_scheduled.sys, "argv", ["run_scheduled.py", "--plan-next"]),
                    mock.patch.dict("os.environ", {"GITHUB_OUTPUT": str(output_path)}),
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(run_scheduled.main(), 0)
                planner.assert_called_once_with(mock.ANY, {"summary_day": "2026-09-30"})
                self.assertEqual(stdout.getvalue().strip(), planned.isoformat())
                self.assertEqual(output_path.read_text(), f"next_check_epoch={int(planned.timestamp())}\n")

        with self.subTest("fallback"):
            stdout = io.StringIO()
            before = datetime.now(timezone.utc)
            with (
                mock.patch.object(run_scheduled, "next_check_at", side_effect=RuntimeError("calendar error")),
                mock.patch.object(run_scheduled.sys, "argv", ["run_scheduled.py", "--plan-next"]),
                mock.patch.dict("os.environ", {}, clear=True),
                redirect_stdout(stdout),
            ):
                self.assertEqual(run_scheduled.main(), 0)
            fallback = datetime.fromisoformat(stdout.getvalue().strip())
            self.assertAlmostEqual((fallback - before).total_seconds(), 15 * 60, delta=2)


FRIDAY = date(2026, 10, 2)


def week_series(*prices: float) -> list[tuple[date, float]]:
    return [(FRIDAY - timedelta(days=len(prices) - 1 - i), p) for i, p in enumerate(prices)]


def weekday_session(day: date) -> Session | None:
    if day.weekday() >= 5:
        return None
    start = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
    return Session(start, start + timedelta(hours=6, minutes=30))


class WeeklyScorecardTest(ScheduledTestCase):
    FRI = weekday_session(FRIDAY)
    AFTER_CLOSE = FRI.close + SUMMARY_DELAY + timedelta(minutes=1)

    def run_at(self, closes, now, **kwargs):
        closes = {"SPY": week_series(100, 100, 100, 100, 100, 100, 102), **closes}
        self.check(closes, now=now, session_for=kwargs.pop("session_for", weekday_session), **kwargs).run()

    def scorecards(self) -> list[str]:
        return [b for t, b in zip(self.notifier.sent, self.notifier.bodies) if t.startswith("Weekly scorecard")]

    def test_friday_scorecard_once(self):
        self.run_at({"AAPL": week_series(10, 10, 10, 9, 12)}, self.FRI.open + timedelta(minutes=30))
        self.fake.fill(self.fake.pending[0]["id"], price=12.0)
        self.run_at({"AAPL": week_series(10, 10, 12, 13, 16)}, self.FRI.open + timedelta(minutes=60))
        self.fake.fill(self.fake.pending[-1]["id"], price=16.0)
        self.run_at({"AAPL": week_series(10, 10, 12, 13, 16)}, self.AFTER_CLOSE)
        self.run_at({"AAPL": week_series(10, 10, 12, 13, 16)}, self.AFTER_CLOSE + timedelta(minutes=30))

        cards = self.scorecards()
        self.assertEqual(len(cards), 1)
        self.assertIn("Weekly scorecard: week of 28 Sep", self.notifier.sent)
        card = cards[0]
        self.assertIn("S&P 500 (SPY) this week: +2.0%", card)
        self.assertIn("Bot sold 1 this week: 1 won, 0 lost", card)
        self.assertIn("AAPL +33.3%", card)
        self.assertIn("take-profit", card)
        self.assertIn("Bot bought: AAPL", card)
        self.assertIn("All time: 1 sells, 100% won", card)
        self.assertLessEqual(len(card), MAX_DESCRIPTION)

    def test_thursday_when_friday_is_a_holiday(self):
        thursday = FRIDAY - timedelta(days=1)

        def holiday_friday(day):
            return None if day == FRIDAY else weekday_session(day)

        self.run_at(
            {}, weekday_session(thursday).close + SUMMARY_DELAY + timedelta(minutes=1), session_for=holiday_friday
        )
        self.assertEqual(len(self.scorecards()), 1)
        self.assertIn("Bot sold nothing this week.", self.scorecards()[0])

    def test_can_be_turned_off(self):
        with mock.patch.object(config, "WEEKLY_SCORECARD", False):
            self.run_at({}, self.AFTER_CLOSE)
        self.assertEqual(self.scorecards(), [])
        self.assertEqual(len(self.sent("Daily summary")), 1)


if __name__ == "__main__":
    unittest.main()
