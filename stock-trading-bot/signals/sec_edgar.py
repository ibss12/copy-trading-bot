"""SEC EDGAR: Form 4 insider trades and 13F hedge-fund holdings (free, no API key).

EDGAR fair-access rules: https://www.sec.gov/os/accessing-edgar-data
"""

import json
import logging
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from signals.http import CachedHttp
from signals.models import TraderSignal

logger = logging.getLogger(__name__)

SEC_DATA_URL = "https://data.sec.gov"
SEC_WWW_URL = "https://www.sec.gov"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

SUBMISSIONS_TTL_S = 6 * 3600
TICKERS_TTL_S = 7 * 24 * 3600

# Form 4 transaction codes: P = open-market purchase, S = open-market sale.
FORM4_ACTIONS = {"P": "buy", "S": "sell"}

# 13F position changes that count as a signal.
THIRTEENF_ADD_THRESHOLD = 0.25
THIRTEENF_CUT_THRESHOLD = 0.50


@dataclass(frozen=True)
class Filing:
    cik: int
    form: str
    accession: str
    filed_on: date
    report_date: str
    primary_document: str

    @property
    def folder_url(self) -> str:
        return f"{SEC_WWW_URL}/Archives/edgar/data/{self.cik}/{self.accession.replace('-', '')}"


@dataclass(frozen=True)
class Holding:
    cusip: str
    name: str
    shares: float


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(element: ET.Element, name: str) -> ET.Element | None:
    for child in element.iter():
        if _local(child.tag) == name:
            return child
    return None


def _text(element: ET.Element, name: str) -> str:
    """Text of the first descendant named `name`, unwrapping Form 4 `<value>` nodes."""
    found = _find(element, name)
    if found is None:
        return ""
    value = _find(found, "value")
    node = value if value is not None else found
    return (node.text or "").strip()


def _is_director(owners: list[ET.Element]) -> bool:
    return any(_text(owner, "isDirector").lower() in ("1", "true") for owner in owners)


def _float(raw: str) -> float:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return 0.0


class SecEdgarClient:
    def __init__(self, cache_dir: Path, user_agent: str, openfigi_api_key: str = ""):
        self.http = CachedHttp(
            cache_dir / "sec",
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            min_interval_s=0.12,
        )
        figi_headers = {"Content-Type": "application/json"}
        if openfigi_api_key:
            figi_headers["X-OPENFIGI-APIKEY"] = openfigi_api_key
        self.figi_http = CachedHttp(
            cache_dir / "openfigi", figi_headers, min_interval_s=0.3 if openfigi_api_key else 2.6
        )
        self.figi_batch_size = 100 if openfigi_api_key else 10
        self.cusip_map_path = cache_dir / "cusip_to_ticker.json"
        self._cusip_map: dict[str, str | None] = (
            json.loads(self.cusip_map_path.read_text()) if self.cusip_map_path.exists() else {}
        )

    # --- Generic helpers -----------------------------------------------------

    def filings(self, cik: int, forms: set[str], since: date, max_count: int) -> list[Filing]:
        """Newest-first filings of the given form types filed on/after `since`."""
        data = self.http.get_json(f"{SEC_DATA_URL}/submissions/CIK{cik:010d}.json", ttl_s=SUBMISSIONS_TTL_S)
        recent = data["filings"]["recent"]
        result = []
        for form, accession, filed, report_date, primary in zip(
            recent["form"],
            recent["accessionNumber"],
            recent["filingDate"],
            recent["reportDate"],
            recent["primaryDocument"],
        ):
            if form not in forms:
                continue
            filed_on = date.fromisoformat(filed)
            if filed_on < since or len(result) >= max_count:
                break
            result.append(Filing(cik, form, accession, filed_on, report_date, primary))
        return result

    def issuer_ciks(self) -> dict[str, int]:
        data = self.http.get_json(f"{SEC_WWW_URL}/files/company_tickers.json", ttl_s=TICKERS_TTL_S)
        return {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}

    # --- Form 4 --------------------------------------------------------------

    def form4_signals(
        self,
        cik: int,
        trader_label: str | None,
        since: date,
        max_filings: int,
        officer_titles: list[str] | None = None,
        ignore_planned_sales: bool = True,
        other_insiders_notify_only: bool = False,
    ) -> list[TraderSignal]:
        """Open-market buys/sells from Form 4 filings listed under `cik`.

        `cik` can be a reporting person (e.g. Donald J. Trump) or an issuer (e.g. Apple).
        When `officer_titles` is set, only filings whose reporting owner's officer title
        matches one of them are kept (e.g. CEO trades in the issuer's own stock).
        With `other_insiders_notify_only`, everyone else's trades are kept as notify-only.
        """
        signals = []
        for filing in self.filings(cik, {"4"}, since, max_filings):
            document = filing.primary_document.rsplit("/", 1)[-1]
            try:
                xml_text = self.http.get_text(f"{filing.folder_url}/{document}")
                root = ET.fromstring(xml_text)
            except Exception as exc:  # malformed/legacy filings shouldn't stop the feed
                logger.debug("Skipping Form 4 %s: %s", filing.accession, exc)
                continue
            signal = self._parse_form4(
                root, xml_text, filing, trader_label, officer_titles, ignore_planned_sales, other_insiders_notify_only
            )
            if signal:
                signals.append(signal)
        return signals

    @staticmethod
    def _parse_form4(
        root: ET.Element,
        xml_text: str,
        filing: Filing,
        trader_label: str | None,
        officer_titles: list[str] | None,
        ignore_planned_sales: bool,
        other_insiders_notify_only: bool = False,
    ) -> TraderSignal | None:
        symbol = _text(root, "issuerTradingSymbol").upper()
        if not symbol or symbol == "NONE":
            return None

        owners = [owner for owner in root.iter() if _local(owner.tag) == "reportingOwner"]
        titles = [_text(owner, "officerTitle") for owner in owners]
        notify_only = False
        if officer_titles:
            wanted = [t.lower() for t in officer_titles]
            if not any(w in title.lower() for title in titles for w in wanted):
                if not other_insiders_notify_only:
                    return None
                notify_only = True
        owner_name = _text(root, "rptOwnerName")
        role = ", ".join(t for t in titles if t) or ("director" if _is_director(owners) else "insider")
        label = trader_label or f"{owner_name} ({role})"

        planned = _text(root, "aff10b5One").lower() in ("1", "true") or "10b5-1" in xml_text
        net_shares = 0.0
        notional = 0.0
        traded_on = None
        for txn in root.iter():
            if _local(txn.tag) != "nonDerivativeTransaction":
                continue
            action = FORM4_ACTIONS.get(_text(txn, "transactionCode"))
            if action is None or (action == "sell" and planned and ignore_planned_sales):
                continue
            shares = _float(_text(txn, "transactionShares"))
            price = _float(_text(txn, "transactionPricePerShare"))
            sign = 1 if action == "buy" else -1
            net_shares += sign * shares
            notional += sign * shares * price
            traded_raw = _text(txn, "transactionDate")[:10]
            if traded_raw:
                traded_on = date.fromisoformat(traded_raw)

        if net_shares == 0:
            return None
        action = "buy" if net_shares > 0 else "sell"
        verb = "bought" if action == "buy" else "sold"
        return TraderSignal(
            trader=label,
            source="SEC Form 4",
            symbol=symbol,
            action=action,
            disclosed_on=filing.filed_on,
            traded_on=traded_on,
            detail=f"{verb} {abs(net_shares):,.0f} shares (~${abs(notional):,.0f})",
            notify_only=notify_only,
        )

    # --- 13F -----------------------------------------------------------------

    def thirteenf_signals(self, cik: int, trader_label: str, since: date) -> list[TraderSignal]:
        """Buys/sells inferred by diffing consecutive quarterly 13F-HR holdings reports filed since `since`."""
        filings = self.filings(cik, {"13F-HR"}, date.min, sys.maxsize)
        in_range = sum(1 for f in filings if f.filed_on >= since)
        filings = filings[: in_range + 1]
        holdings = [(filing, h) for filing in reversed(filings) if (h := self._holdings(filing)) is not None]

        changes = []
        for (_, before), (filing, after) in zip(holdings, holdings[1:]):
            for cusip in before.keys() | after.keys():
                old = before[cusip].shares if cusip in before else 0.0
                new = after[cusip].shares if cusip in after else 0.0
                name = (after.get(cusip) or before[cusip]).name
                if old == 0 and new > 0:
                    changes.append((filing, cusip, "buy", f"opened a new position in {name}"))
                elif old > 0 and new == 0:
                    changes.append((filing, cusip, "sell", f"exited {name}"))
                elif new >= old * (1 + THIRTEENF_ADD_THRESHOLD):
                    changes.append((filing, cusip, "buy", f"added {new / old - 1:.0%} to {name}"))
                elif new <= old * (1 - THIRTEENF_CUT_THRESHOLD):
                    changes.append((filing, cusip, "sell", f"cut {1 - new / old:.0%} of {name}"))

        tickers = self._cusips_to_tickers({cusip for _, cusip, _, _ in changes})
        signals = []
        for filing, cusip, action, detail in changes:
            symbol = tickers.get(cusip)
            if not symbol:
                continue
            signals.append(
                TraderSignal(
                    trader=trader_label,
                    source="SEC 13F",
                    symbol=symbol,
                    action=action,
                    disclosed_on=filing.filed_on,
                    traded_on=None,
                    detail=f"{detail} during quarter ending {filing.report_date}",
                )
            )
        return signals

    def _holdings(self, filing: Filing) -> dict[str, Holding] | None:
        index = self.http.get_json(f"{filing.folder_url}/index.json")
        names = [item["name"] for item in index["directory"]["item"]]
        candidates = [n for n in names if n.lower().endswith(".xml") and n.lower() != "primary_doc.xml"]
        root = None
        for name in candidates:
            document = ET.fromstring(self.http.get_text(f"{filing.folder_url}/{name}"))
            if any(_local(element.tag) == "infoTable" for element in document.iter()):
                root = document
                break
        if root is None:
            logger.warning("No 13F information table found in %s - skipping filing", filing.folder_url)
            return None

        shares: dict[str, float] = defaultdict(float)
        issuer_names: dict[str, str] = {}
        for row in root.iter():
            if _local(row.tag) != "infoTable":
                continue
            if _text(row, "putCall") or _text(row, "sshPrnamtType").upper() != "SH":
                continue
            cusip = _text(row, "cusip").upper()
            shares[cusip] += _float(_text(row, "sshPrnamt"))
            issuer_names[cusip] = _text(row, "nameOfIssuer")
        return {cusip: Holding(cusip, issuer_names[cusip], qty) for cusip, qty in shares.items()}

    def _cusips_to_tickers(self, cusips: set[str]) -> dict[str, str | None]:
        missing = sorted(c for c in cusips if c not in self._cusip_map)
        for start in range(0, len(missing), self.figi_batch_size):
            batch = missing[start : start + self.figi_batch_size]
            results = self.figi_http.post_json(OPENFIGI_URL, [{"idType": "ID_CUSIP", "idValue": c} for c in batch])
            for cusip, result in zip(batch, results):
                listings = result.get("data", [])
                us = [
                    x
                    for x in listings
                    if x.get("exchCode") == "US"
                    and x.get("marketSector") == "Equity"
                    and " " not in x.get("ticker", " ")
                ]
                ticker = us[0].get("ticker") if us else None
                self._cusip_map[cusip] = ticker.replace("/", ".") if ticker else None
            self.cusip_map_path.write_text(json.dumps(self._cusip_map, indent=0, sort_keys=True))
        return {c: self._cusip_map.get(c) for c in cusips}
