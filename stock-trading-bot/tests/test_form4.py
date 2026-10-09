"""Form 4 parsing for watchlist companies' insiders. No network.

Run: python -m unittest tests.test_form4 -v
"""

import unittest
import xml.etree.ElementTree as ET
from datetime import date

from signals.sec_edgar import Filing, SecEdgarClient


def form4(title: str = "", director: bool = False, code: str = "P") -> str:
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
      <transactionShares><value>100</value></transactionShares>
      <transactionPricePerShare><value>200</value></transactionPricePerShare>
    </transactionAmounts>
  </nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument>"""


FILING = Filing(
    cik=320193, form="4", accession="0001", filed_on=date(2026, 9, 29), report_date="", primary_document="f.xml"
)


def parse(xml_text: str, notify_others: bool):
    return SecEdgarClient._parse_form4(ET.fromstring(xml_text), xml_text, FILING, None, ["CEO"], True, notify_others)


class Form4Test(unittest.TestCase):
    def test_ceo_trade_is_tradable(self):
        signal = parse(form4("CEO"), True)
        self.assertEqual((signal.trader, signal.action, signal.notify_only), ("Jane Doe (CEO)", "buy", False))

    def test_other_officer_is_notify_only(self):
        signal = parse(form4("CFO", code="S"), True)
        self.assertEqual((signal.trader, signal.action, signal.notify_only), ("Jane Doe (CFO)", "sell", True))

    def test_director_without_title(self):
        self.assertEqual(parse(form4(director=True), True).trader, "Jane Doe (director)")

    def test_others_skipped_when_turned_off(self):
        self.assertIsNone(parse(form4("CFO"), False))


if __name__ == "__main__":
    unittest.main()
