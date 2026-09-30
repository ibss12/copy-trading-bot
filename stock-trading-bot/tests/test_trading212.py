"""Trading 212 practice integration tests against a local fake server (no network, no real orders).

Run: python -m unittest discover -s tests -v
"""

import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from lumibot.entities import Asset, Order

from trading212 import client as t212
from trading212.broker import Trading212Broker
from trading212.client import OrderUncertain, Trading212Client, Trading212Error, symbol_from_ticker, validate_base_url
from trading212.ledger import BotLedger, split_position

KEY, SECRET = "demo-key", "demo-secret"
INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "currencyCode": "USD", "type": "STOCK"},
    {"ticker": "BRK_B_US_EQ", "shortName": "BRK.B", "currencyCode": "USD", "type": "STOCK"},
    {"ticker": "MSFT_US_EQ", "shortName": "MSFT", "currencyCode": "USD", "type": "STOCK"},
]


class FakeTrading212:
    """In-memory stand-in for the demo API. `post_status` forces the next market-order response code."""

    def __init__(self):
        self.positions: dict[str, float] = {}
        self.pending: list[dict] = []
        self.history: list[dict] = []
        self.posts: list[dict] = []
        self.post_status = 200
        self.place_on_error = False
        self.next_id = 1000
        self.auto_fill = False

    def place(self, ticker: str, quantity: float) -> dict:
        self.next_id += 1
        row = {
            "id": self.next_id,
            "ticker": ticker,
            "quantity": quantity,
            "side": "BUY" if quantity > 0 else "SELL",
            "type": "MARKET",
            "status": "NEW",
            "initiatedFrom": "API",
            "createdAt": "2026-09-30T14:00:00Z",
        }
        self.pending.append(row)
        return row

    def fill(self, order_id: int, price: float = 100.0, quantity: float | None = None) -> None:
        row = next(r for r in self.pending if r["id"] == order_id)
        self.pending.remove(row)
        qty = row["quantity"] if quantity is None else quantity
        self.positions[row["ticker"]] = self.positions.get(row["ticker"], 0.0) + qty
        done = {**row, "status": "FILLED", "filledQuantity": abs(qty), "filledValue": abs(qty) * price}
        self.history.insert(
            0, {"order": done, "fill": {"quantity": qty, "price": price, "filledAt": "2026-09-30T14:01Z"}}
        )

    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code: int, body) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self) -> bool:
                expected = "Basic " + base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    self._send(401, {"code": "AuthenticationFailed"})
                    return False
                return True

            def do_GET(self):
                if not self._authed():
                    return
                path = self.path.split("?")[0]
                query = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&")) if "?" in self.path else {}
                if path == "/api/v0/equity/account/summary":
                    invested = sum(q * 100 for q in fake.positions.values())
                    self._send(
                        200,
                        {"currency": "USD", "totalValue": 10000 + invested, "cash": {"availableToTrade": 10000}},
                    )
                elif path == "/api/v0/equity/positions":
                    self._send(
                        200,
                        [
                            {
                                "instrument": {"ticker": t},
                                "quantity": q,
                                "averagePricePaid": 100,
                                "currentPrice": 101,
                                "walletImpact": {
                                    "currentValue": q * 101,
                                    "totalCost": q * 100,
                                    "unrealizedProfitLoss": q,
                                },
                            }
                            for t, q in fake.positions.items()
                            if q
                        ],
                    )
                elif path == "/api/v0/equity/orders":
                    self._send(200, fake.pending)
                elif path.startswith("/api/v0/equity/orders/"):
                    order_id = int(path.rsplit("/", 1)[1])
                    row = next((r for r in fake.pending if r["id"] == order_id), None)
                    self._send(200, row) if row else self._send(404, {"code": "NotFound"})
                elif path == "/api/v0/equity/history/orders":
                    items = [
                        i for i in fake.history if "ticker" not in query or i["order"]["ticker"] == query["ticker"]
                    ]
                    self._send(200, {"items": items, "nextPagePath": None})
                elif path == "/api/v0/equity/metadata/instruments":
                    self._send(200, INSTRUMENTS)
                else:
                    self._send(404, {"code": "NotFound"})

            def do_POST(self):
                if not self._authed():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.posts.append(body)
                if fake.post_status != 200:
                    if fake.place_on_error:
                        fake.place(body["ticker"], body["quantity"])
                    self._send(fake.post_status, {"code": "Boom"})
                    return
                row = fake.place(body["ticker"], body["quantity"])
                if fake.auto_fill:
                    fake.fill(row["id"])
                self._send(200, row)

            def do_DELETE(self):
                if not self._authed():
                    return
                order_id = int(self.path.rsplit("/", 1)[1])
                row = next((r for r in fake.pending if r["id"] == order_id), None)
                if not row:
                    self._send(404, {"code": "NotFound"})
                    return
                fake.pending.remove(row)
                fake.history.insert(0, {"order": {**row, "status": "CANCELLED"}, "fill": None})
                self._send(200, {})

        return Handler


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = FakeTrading212()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.fake.handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v0"
        patcher = mock.patch.dict(t212.RATE_LIMITS, {k: 0.0 for k in t212.RATE_LIMITS})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Trading212Client(KEY, SECRET, self.url, self.tmp)
        self.ledger = BotLedger(self.tmp / "ledger.json")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()


class UrlSafetyTest(unittest.TestCase):
    def test_only_practice_server_allowed(self):
        self.assertEqual(validate_base_url("https://demo.trading212.com/api/v0/"), "https://demo.trading212.com/api/v0")
        for bad in (
            "https://live.trading212.com/api/v0",
            "http://demo.trading212.com/api/v0",
            "https://demo.trading212.com.evil.example/api/v0",
            "https://trading212.com/api/v0",
        ):
            with self.subTest(bad), self.assertRaises(ValueError):
                validate_base_url(bad)

    def test_client_refuses_live_server(self):
        with self.assertRaises(ValueError):
            Trading212Client(KEY, SECRET, "https://live.trading212.com/api/v0", Path(tempfile.mkdtemp()))

    def test_symbols(self):
        self.assertEqual(symbol_from_ticker("AAPL_US_EQ"), "AAPL")
        self.assertEqual(symbol_from_ticker("BRK_B_US_EQ"), "BRK.B")


class ClientTest(ServerTestCase):
    def test_basic_auth_and_reads(self):
        self.assertEqual(self.client.account_summary()["currency"], "USD")
        bad = Trading212Client(KEY, "wrong", self.url, self.tmp)
        with self.assertRaises(Trading212Error):
            bad.positions()

    def test_ticker_lookup(self):
        self.assertEqual(self.client.ticker_for("AAPL"), "AAPL_US_EQ")
        self.assertEqual(self.client.ticker_for("BRK.B"), "BRK_B_US_EQ")
        self.assertIsNone(self.client.ticker_for("NOPE"))

    def test_market_order_signs(self):
        self.client.market_order("AAPL_US_EQ", 2)
        self.client.market_order("AAPL_US_EQ", -1)
        self.assertEqual([p["quantity"] for p in self.fake.posts], [2, -1])

    def test_server_error_is_uncertain_and_never_retried(self):
        self.fake.post_status = 503
        with self.assertRaises(OrderUncertain):
            self.client.market_order("AAPL_US_EQ", 1)
        self.assertEqual(len(self.fake.posts), 1)

    def test_rejection_is_a_plain_error(self):
        self.fake.post_status = 400
        with self.assertRaises(Trading212Error) as ctx:
            self.client.market_order("AAPL_US_EQ", 1)
        self.assertNotIsInstance(ctx.exception, OrderUncertain)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "ledger.json"
        self.ledger = BotLedger(self.path)

    def test_fills_are_counted_once_and_persist(self):
        self.assertTrue(self.ledger.apply_fill("1", "AAPL", 5))
        self.assertFalse(self.ledger.apply_fill("1", "AAPL", 5))
        self.ledger.apply_fill("2", "AAPL", -2)
        self.assertEqual(BotLedger(self.path).bot_shares(), {"AAPL": 3})

    def test_manual_sell_shrinks_bot_count(self):
        self.ledger.apply_fill("1", "AAPL", 5)
        self.assertEqual(self.ledger.reconcile({"AAPL": 8}), {"AAPL": 5})
        self.assertEqual(self.ledger.reconcile({"AAPL": 3}), {"AAPL": 3})
        self.assertEqual(self.ledger.reconcile({}), {})

    def test_split(self):
        self.assertEqual(split_position(10, 4), (4, 6))
        self.assertEqual(split_position(3, 4), (3, 0))
        self.assertEqual(split_position(3, 0), (0, 3))


class BrokerTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.broker = Trading212Broker(self.client, self.ledger, data_source=mock.Mock(), connect_stream=False)

    def order(self, side: str, qty: float, symbol: str = "AAPL") -> Order:
        return Order("test", Asset(symbol), qty, side)

    def test_buy_fill_is_tracked_as_bot_shares(self):
        order = self.broker._submit_order(self.order("buy", 4))
        self.assertTrue(self.ledger.is_bot_order(order.identifier))
        self.assertEqual(self.fake.posts[-1], {"ticker": "AAPL_US_EQ", "quantity": 4.0, "extendedHours": False})
        self.fake.fill(int(order.identifier), price=150)
        self.broker.do_polling()
        self.assertEqual(self.ledger.bot_shares(), {"AAPL": 4})
        self.assertTrue(order.is_filled())
        self.broker.do_polling()
        self.assertEqual(self.ledger.bot_shares(), {"AAPL": 4})

    def test_bot_never_sells_your_shares(self):
        self.fake.positions["AAPL_US_EQ"] = 10.0
        order = self.broker._submit_order(self.order("sell", 10))
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(order.status, Order.OrderStatus.ERROR)
        self.assertEqual(self.broker._pull_positions("test"), [])

    def test_sell_is_capped_to_bot_shares(self):
        self.fake.positions["AAPL_US_EQ"] = 10.0
        self.ledger.apply_fill("old", "AAPL", 3)
        positions = self.broker._pull_positions("test")
        self.assertEqual([(p.asset.symbol, float(p.quantity)) for p in positions], [("AAPL", 3.0)])
        order = self.broker._submit_order(self.order("sell", 10))
        self.assertEqual(self.fake.posts[-1]["quantity"], -3.0)
        self.fake.fill(int(order.identifier))
        self.broker.do_polling()
        self.assertEqual(self.ledger.bot_shares(), {})
        self.assertEqual(self.fake.positions["AAPL_US_EQ"], 7.0)

    def test_uncertain_order_is_not_retried_and_is_found_later(self):
        self.fake.post_status = 502
        self.fake.place_on_error = True
        order = self.broker._submit_order(self.order("buy", 2))
        self.assertEqual(len(self.fake.posts), 1)
        self.assertTrue(order.identifier.startswith("uncertain-"))
        self.assertTrue(order.is_active())
        self.fake.post_status = 200
        self.broker.do_polling()
        self.assertTrue(order.identifier.isdigit())
        self.assertTrue(self.ledger.is_bot_order(order.identifier))
        self.fake.fill(int(order.identifier))
        self.broker.do_polling()
        self.assertEqual(self.ledger.bot_shares(), {"AAPL": 2})
        self.assertEqual(len(self.fake.posts), 1)

    def test_manual_orders_are_ignored_by_the_bot(self):
        manual = self.fake.place("MSFT_US_EQ", 5)
        self.ledger.record_manual_order(manual["id"])
        self.assertEqual(self.broker._pull_broker_all_orders(), [])
        self.fake.fill(manual["id"])
        self.broker.do_polling()
        self.assertEqual(self.ledger.bot_shares(), {})

    def test_cancelled_order(self):
        order = self.broker._submit_order(self.order("buy", 1))
        self.client.cancel_order(int(order.identifier))
        self.broker.do_polling()
        self.assertEqual(order.status, Order.OrderStatus.CANCELED)
        self.assertEqual(self.ledger.bot_shares(), {})

    def test_balances(self):
        cash, positions_value, total = self.broker._get_balances_at_broker(Asset("USD", "forex"), None)
        self.assertEqual((cash, positions_value, total), (10000.0, 0.0, 10000.0))


class DashboardAccountTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        from dashboard import trading212_account as module

        patches = [
            mock.patch.object(module.config, "TRADING212_API_KEY", KEY),
            mock.patch.object(module.config, "TRADING212_API_SECRET", SECRET),
            mock.patch.object(module, "practice_client", lambda: self.client),
            mock.patch.object(module, "bot_ledger", lambda: self.ledger),
            mock.patch.object(module, "DAY_START_PATH", self.tmp / "day_start.json"),
            mock.patch.object(module.config, "TRADING212_BASE_URL", self.url),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.account = module.Trading212Account()

    def test_manual_order_is_yours_and_duplicates_blocked(self):
        order = self.account.submit_market_order("AAPL", 2, "buy")
        self.assertEqual(order["by"], "you")
        self.assertTrue(self.ledger.is_manual_order(order["id"]))
        with self.assertRaises(RuntimeError):
            self.account.submit_market_order("AAPL", 2, "buy")
        self.assertEqual(len(self.fake.posts), 1)

    def test_snapshot_splits_bot_and_your_shares(self):
        self.fake.positions["AAPL_US_EQ"] = 10.0
        self.ledger.apply_fill("b1", "AAPL", 4)
        snap = self.account.snapshot()
        self.assertIsNone(snap["error"])
        pos = snap["positions"][0]
        self.assertEqual((pos["symbol"], pos["bot_qty"], pos["your_qty"]), ("AAPL", 4.0, 6.0))
        self.assertEqual(snap["provider"], "trading212")

    def test_uncertain_manual_order_warns(self):
        self.fake.post_status = 500
        with self.assertRaisesRegex(RuntimeError, "Check Open orders"):
            self.account.submit_market_order("AAPL", 1, "buy")
        self.assertEqual(len(self.fake.posts), 1)


if __name__ == "__main__":
    unittest.main()
