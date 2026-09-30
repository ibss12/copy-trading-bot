"""Live Yahoo Finance data source for Lumibot (Trading 212's API has no price feed)."""

from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf
from lumibot.data_sources import DataSource
from lumibot.entities import Asset, Bars


class YahooLiveData(DataSource):
    SOURCE = "YAHOO"
    MIN_TIMESTEP = "day"
    TIMESTEP_MAPPING = [
        {"timestep": "day", "representations": ["1d", "day"]},
        {"timestep": "minute", "representations": ["1m", "minute"]},
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    def _symbol(asset) -> str:
        return (asset.symbol if isinstance(asset, Asset) else str(asset)).upper().replace(".", "-")

    def get_last_price(self, asset, quote=None, exchange=None, **kwargs):
        try:
            price = yf.Ticker(self._symbol(asset)).fast_info["last_price"]
        except Exception:
            return None
        return float(price) if price else None

    def get_historical_prices(
        self,
        asset,
        length,
        timestep="day",
        timeshift=None,
        quote=None,
        exchange=None,
        include_after_hours=True,
        **kwargs,
    ):
        if isinstance(asset, str):
            asset = Asset(asset)
        minute = timestep in ("minute", "1m")
        end = datetime.now() - (timeshift or timedelta(0))
        start = end - (timedelta(days=min(7, 1 + length // 390)) if minute else timedelta(days=int(length * 1.6) + 10))
        df = yf.Ticker(self._symbol(asset)).history(
            start=start, end=end + timedelta(days=1), interval="1m" if minute else "1d", auto_adjust=False
        )
        if df is None or df.empty:
            return None
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        df.index = pd.to_datetime(df.index)
        return Bars(df.tail(length), self.SOURCE, asset)

    def get_chains(self, asset, quote=None):
        raise NotImplementedError("Options are not supported")
