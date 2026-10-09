"""Form 4 parsing for watchlist companies' insiders. No network.

Run: python -m unittest tests.test_form4 -v
"""

import unittest
import xml.etree.ElementTree as ET
from datetime import date

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


if __name__ == "__main__":
    unittest.main()
