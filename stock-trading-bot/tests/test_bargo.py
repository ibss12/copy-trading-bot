"""Free congressional trades from Bargo, with the HTTP call faked. No network.

Run: python -m unittest tests.test_bargo -v
"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import requests

from signals.bargo import BARGO_SOURCE, PAGE_SIZE, bargo_congress_signals


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
