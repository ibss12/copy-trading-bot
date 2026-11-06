"""Form 4 parsing for watchlist companies' insiders. No network.

Run: python -m unittest tests.test_form4 -v
"""

import unittest
import xml.etree.ElementTree as ET
from datetime import date
from unittest import mock

from signals.feed import MAX_FORM4_FILINGS_PER_FILER, load_insider_activity
from signals.models import InsiderTrade
from signals.sec_edgar import Filing, SecEdgarClient

SENIOR = ["CEO", "Chief Executive", "CFO", "Chief Financial", "President", "Chair", "Chairman"]
PRICE_NOTE = "The price reported is a weighted average price."
GIFT_NOTE = "Sold by a family trust for estate planning purposes."
TAX_NOTE = "Shares sold to cover tax withholding on vested stock awards."


def form4(title: str = "", director: bool = False, code: str = "P", shares: int = 100, notes=()) -> str:
    refs = "".join(f'<footnoteId id="F{i}"/>' for i in range(1, len(notes) + 1))
    footnotes = "".join(f'<footnote id="F{i}">{n}</footnote>' for i, n in enumerate(notes, 1))
    return f"""<ownershipDocument>
  <issuer><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Jane Doe</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{int(director)}</isDirector>
      <officerTitle>{title}</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable><nonDerivativeTransaction>
    <transactionDate><value>2026-09-25</value></transactionDate>
    <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
    <transactionAmounts>
      <transactionShares><value>{shares}</value></transactionShares>
      <transactionPricePerShare><value>200</value>{refs}</transactionPricePerShare>
    </transactionAmounts>
  </nonDerivativeTransaction></nonDerivativeTable>
  <footnotes>{footnotes}</footnotes>
</ownershipDocument>"""


def activity_form4(transactions, *, title="", director=False, ten_percent=False, planned=False) -> str:
    rows = []
    all_notes = []
    for code, shares, price, notes in transactions:
        refs = "".join(f'<footnoteId id="F{i}"/>' for i in range(len(all_notes) + 1, len(all_notes) + len(notes) + 1))
        all_notes.extend(notes)
        rows.append(
            f"""<nonDerivativeTransaction>
              <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
              <transactionAmounts>
                <transactionShares><value>{shares}</value></transactionShares>
                <transactionPricePerShare><value>{price}</value>{refs}</transactionPricePerShare>
              </transactionAmounts>
            </nonDerivativeTransaction>"""
        )
    footnotes = "".join(f'<footnote id="F{i}">{text}</footnote>' for i, text in enumerate(all_notes, 1))
    planned_tag = "<aff10b5One><value>true</value></aff10b5One>" if planned else ""
    return f"""<ownershipDocument>
      <issuer><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
      <reportingOwner>
        <reportingOwnerId><rptOwnerName>Jane Doe</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship>
          <isDirector>{int(director)}</isDirector>
          <isTenPercentOwner>{int(ten_percent)}</isTenPercentOwner>
          <officerTitle>{title}</officerTitle>
        </reportingOwnerRelationship>
      </reportingOwner>
      {planned_tag}<nonDerivativeTable>{"".join(rows)}</nonDerivativeTable>
      <footnotes>{footnotes}</footnotes>
    </ownershipDocument>"""


FILING = Filing(
    cik=320193, form="4", accession="0001", filed_on=date(2026, 9, 29), report_date="", primary_document="f.xml"
)


def parse(xml_text: str, notify_titles=SENIOR, min_sell: float = 500_000, ceo_titles=("CEO",)):
    return SecEdgarClient._parse_form4(
        ET.fromstring(xml_text),
        xml_text,
        FILING,
        None,
        list(ceo_titles) if ceo_titles else None,
        True,
        notify_titles,
        min_sell,
    )


class Form4Test(unittest.TestCase):
    def test_ceo_trade_is_tradable_with_reason(self):
        signal = parse(form4("CEO"))
        self.assertEqual((signal.trader, signal.action, signal.notify_only), ("Jane Doe (CEO)", "buy", False))
        self.assertIn("open market with their own money", signal.reason)

    def test_cfo_buy_is_notify_only(self):
        signal = parse(form4("EVP &amp; Chief Financial Officer"))
        self.assertEqual((signal.action, signal.notify_only), ("buy", True))

    def test_small_cfo_sale_is_skipped_big_one_kept(self):
        self.assertIsNone(parse(form4("CFO", code="S")))
        signal = parse(form4("CFO", code="S", shares=5000))
        self.assertEqual((signal.action, signal.notify_only), ("sell", True))

    def test_less_senior_people_are_skipped(self):
        self.assertIsNone(parse(form4(director=True)))
        self.assertIsNone(parse(form4("Senior Vice President, Sales")))
        self.assertIsNone(parse(form4("Vice Chairman")))
        self.assertIsNone(parse(form4("")))

    def test_others_skipped_when_turned_off(self):
        self.assertIsNone(parse(form4("CFO"), notify_titles=None))

    def test_ceo_still_announced_when_not_traded_on(self):
        signal = parse(form4("CEO"), ceo_titles=None)
        self.assertTrue(signal.notify_only)

    def test_tax_withholding_sale_is_routine(self):
        self.assertIsNone(parse(form4("CFO", code="S", shares=5000, notes=(TAX_NOTE,))))
        signal = parse(form4("CEO", code="S", notes=(TAX_NOTE,)))
        self.assertIn("pay tax", signal.reason)

    def test_reason_uses_their_note_not_price_details(self):
        signal = parse(form4("CEO", code="S", notes=(PRICE_NOTE, GIFT_NOTE)))
        self.assertIn("not as part of a pre-planned sale", signal.reason)
        self.assertIn(GIFT_NOTE, signal.reason)
        self.assertNotIn("weighted", signal.reason)

    def activity(self, xml_text):
        client = object.__new__(SecEdgarClient)
        client.http = mock.Mock()
        client.http.get_text.return_value = xml_text
        with mock.patch.object(client, "filings", return_value=[FILING]):
            return client.form4_activity(FILING.cik, date(2026, 9, 1), 150)

    def test_activity_director_purchase_has_role_and_notional(self):
        trades = self.activity(activity_form4([("P", 100, 200, ())], director=True))
        self.assertEqual(len(trades), 1)
        self.assertEqual((trades[0].symbol, trades[0].name, trades[0].role), ("AAPL", "Jane Doe", "director"))
        self.assertEqual((trades[0].action, trades[0].shares, trades[0].usd), ("buy", 100, 20_000))
        ten_percent_owner = self.activity(activity_form4([("P", 1, 100, ())], ten_percent=True))[0]
        self.assertEqual(ten_percent_owner.role, "10% owner")

    def test_activity_marks_planned_and_tax_sales(self):
        planned = self.activity(activity_form4([("S", 50, 200, ())], planned=True))[0]
        tax = self.activity(activity_form4([("S", 50, 200, (TAX_NOTE,))]))[0]
        self.assertTrue(planned.planned)
        self.assertFalse(planned.tax)
        self.assertTrue(tax.tax)

    def test_activity_emits_both_actions_and_ignores_other_codes(self):
        both = self.activity(activity_form4([("P", 100, 200, ()), ("P", 50, 200, ()), ("S", 30, 210, ())]))
        self.assertEqual(
            [(trade.action, trade.shares, trade.usd) for trade in both],
            [
                ("buy", 150, 30_000),
                ("sell", 30, 6_300),
            ],
        )
        ignored = self.activity(activity_form4([(code, 10, 100, ()) for code in ("A", "M", "F", "G")]))
        self.assertEqual(ignored, [])


class InsiderActivityLoaderTest(unittest.TestCase):
    def test_loads_watchlist_symbols_and_skips_one_failed_issuer(self):
        aapl = InsiderTrade("AAPL", "Jane Doe", "director", "buy", 1, 100, False, False, FILING.filed_on, "1")
        other = InsiderTrade("TSLA", "Jane Doe", "director", "buy", 1, 100, False, False, FILING.filed_on, "2")

        def form4_activity(cik, since, max_filings):
            self.assertEqual(max_filings, MAX_FORM4_FILINGS_PER_FILER)
            if cik == FILING.cik:
                return [aapl, other]
            raise RuntimeError("SEC unavailable")

        with mock.patch("signals.feed.SecEdgarClient") as sec_client:
            sec_client.return_value.issuer_ciks.return_value = {"AAPL": FILING.cik, "MSFT": 1}
            sec_client.return_value.form4_activity.side_effect = form4_activity
            trades = load_insider_activity(["AAPL", "MSFT"], date(2026, 9, 1))

        self.assertEqual(trades, [aapl])


if __name__ == "__main__":
    unittest.main()
