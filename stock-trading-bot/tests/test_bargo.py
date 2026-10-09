"""Free congressional trades from Bargo, with the HTTP call faked. No network.

Run: python -m unittest tests.test_bargo -v
"""

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import requests

from signals.bargo import BARGO_SOURCE, KEYED_PAGE_SIZE, PAGE_SIZE, bargo_congress_signals
from signals.http import RETRY_AFTER_ERROR_S


def trade(ticker: str, kind: str, disclosed: str, member: str = "Nancy Pelosi") -> dict:
    return {
        "member": member,
        "ticker": ticker,
        "type": kind,
        "amount_range": "$500,001 - $1,000,000",
        "transaction_date": "2026-07-24",
        "disclosure_date": disclosed,
    }


class FakeResponse:
    def __init__(self, trades: list[dict]):
        self.text = json.dumps({"trades": trades, "page": 0, "limit": PAGE_SIZE, "count": len(trades)})

    def raise_for_status(self) -> None:
        pass


class BargoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)

    def fetch(self, pages: list[list[dict]], since: date = date(2026, 8, 1)):
        with mock.patch("signals.http.requests.get", side_effect=[FakeResponse(p) for p in pages]) as get:
            signals = bargo_congress_signals(self.cache, ["Nancy Pelosi"], since)
        return signals, get

    def test_buys_and_sells_become_signals(self):
        signals, get = self.fetch(
            [
                [
                    trade("INTC", "purchase", "2026-08-21"),
                    trade("AAPL", "sale", "2026-08-25"),
                    trade("MSFT", "exchange", "2026-08-25"),
                    trade("", "purchase", "2026-08-25"),
                    trade("NVDA", "purchase", "2026-07-01"),
                ]
            ]
        )
        self.assertEqual(get.call_count, 1)
        self.assertIn("member=Nancy+Pelosi", get.call_args.args[0])
        self.assertEqual([(s.symbol, s.action) for s in signals], [("INTC", "buy"), ("AAPL", "sell")])
        first = signals[0]
        self.assertEqual(first.trader, "Nancy Pelosi")
        self.assertEqual(first.source, BARGO_SOURCE)
        self.assertEqual(first.disclosed_on, date(2026, 8, 21))
        self.assertEqual(first.traded_on, date(2026, 7, 24))
        self.assertEqual(first.detail, "Purchase $500,001 - $1,000,000")

    def test_reads_next_page_only_when_full(self):
        full = [trade("INTC", "purchase", "2026-08-21")] * PAGE_SIZE
        signals, get = self.fetch([full, [trade("AAPL", "sale", "2026-08-25")]])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(signals), PAGE_SIZE + 1)

    def test_reads_every_full_page(self):
        full = [trade("INTC", "purchase", "2026-08-21")] * PAGE_SIZE
        signals, get = self.fetch([full] * 4 + [[]])
        self.assertEqual(get.call_count, 5)
        self.assertEqual(len(signals), 4 * PAGE_SIZE)

    def test_stops_at_page_older_than_lookback(self):
        old = [trade("NVDA", "purchase", "2026-07-01")] * PAGE_SIZE
        signals, get = self.fetch([old, [trade("AAPL", "sale", "2026-08-25")]])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(signals, [])

    def test_asks_only_for_recent_window(self):
        _, get = self.fetch([[]], since=date(2026, 8, 20))
        self.assertIn("from=2026-07-06", get.call_args.args[0])

    def test_waits_before_retrying_after_rate_limit(self):
        self.fetch([[trade("INTC", "purchase", "2026-08-21")]])
        for path in (self.cache / "bargo").iterdir():
            os.utime(path, (0, 0))
        with mock.patch("signals.http.requests.get", side_effect=requests.HTTPError("429 Too Many Requests")) as get:
            bargo_congress_signals(self.cache, ["Nancy Pelosi"], date(2026, 8, 1))
            signals = bargo_congress_signals(self.cache, ["Nancy Pelosi"], date(2026, 8, 1))
        self.assertEqual(get.call_count, 1)
        self.assertEqual([s.symbol for s in signals], ["INTC"])

    def test_retries_a_few_hours_after_rate_limit(self):
        self.fetch([[trade("INTC", "purchase", "2026-08-21")]])
        for path in (self.cache / "bargo").iterdir():
            os.utime(path, (0, 0))
        with mock.patch("signals.http.requests.get", side_effect=requests.HTTPError("429 Too Many Requests")):
            bargo_congress_signals(self.cache, ["Nancy Pelosi"], date(2026, 8, 1))
        for path in (self.cache / "bargo").iterdir():
            os.utime(path, (path.stat().st_mtime - RETRY_AFTER_ERROR_S - 1,) * 2)
        signals, get = self.fetch([[trade("MCD", "sale", "2026-08-25")]])
        self.assertEqual(get.call_count, 1)
        self.assertEqual([s.symbol for s in signals], ["MCD"])

    def test_free_key_sent_with_bigger_pages(self):
        with mock.patch("signals.http.requests.get", side_effect=[FakeResponse([])]) as get:
            bargo_congress_signals(self.cache, ["Nancy Pelosi"], date(2026, 8, 1), api_key="k123")
        self.assertEqual(get.call_args.kwargs["headers"]["X-Api-Key"], "k123")
        self.assertIn(f"limit={KEYED_PAGE_SIZE}", get.call_args.args[0])

    def test_cached_between_runs(self):
        self.fetch([[trade("INTC", "purchase", "2026-08-21")]])
        signals, get = self.fetch([])
        self.assertEqual(get.call_count, 0)
        self.assertEqual(len(signals), 1)

    def test_uses_old_copy_when_rate_limited(self):
        self.fetch([[trade("INTC", "purchase", "2026-08-21")]])
        with mock.patch("signals.bargo.CONGRESS_TTL_S", 0), mock.patch(
            "signals.http.requests.get", side_effect=requests.HTTPError("429 Too Many Requests")
        ):
            signals = bargo_congress_signals(self.cache, ["Nancy Pelosi"], date(2026, 8, 1))
        self.assertEqual([s.symbol for s in signals], ["INTC"])

    def test_rate_limited_without_old_copy_raises(self):
        with mock.patch("signals.http.requests.get", side_effect=requests.HTTPError("429 Too Many Requests")):
            with self.assertRaises(requests.HTTPError):
                bargo_congress_signals(self.cache, ["Nancy Pelosi"], date(2026, 8, 1))


if __name__ == "__main__":
    unittest.main()
