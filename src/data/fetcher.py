"""Alpaca-backed market data fetcher (replaces yfinance).

All OHLCV history is fetched from Alpaca's historical bars API using the
free IEX data feed.  If no credentials are available the fetcher returns
None / empty dicts gracefully so the rest of the system degrades cleanly.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    _ALPACA_OK = True
except ImportError:
    _ALPACA_OK = False
    logger.warning("alpaca-py not installed — MarketDataFetcher will return empty data")

try:
    from alpaca.data.enums import DataFeed
    _IEX_FEED = DataFeed.IEX
except ImportError:
    _IEX_FEED = "iex"

if _ALPACA_OK:
    _TF_MAP: dict = {
        "1d":  TimeFrame.Day,
        "1h":  TimeFrame.Hour,
        "15m": TimeFrame(15, TimeFrameUnit.Minute),
        "5m":  TimeFrame(5, TimeFrameUnit.Minute),
        "1m":  TimeFrame.Minute,
        "1wk": TimeFrame.Week,
    }
else:
    _TF_MAP = {}

_COL_RENAME = {
    "open": "Open", "high": "High", "low": "Low",
    "close": "Close", "volume": "Volume",
}
_REQUIRED = {"Open", "High", "Low", "Close", "Volume"}


def _normalise(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Rename Alpaca lowercase columns to OHLCV standard and strip timezone."""
    if df is None or df.empty:
        return None
    df = df.rename(columns=_COL_RENAME)
    if not _REQUIRED.issubset(df.columns):
        return None
    if hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def _extract_symbol(df_all: pd.DataFrame, symbol: str) -> Optional[pd.DataFrame]:
    """Extract a single-symbol slice from a MultiIndex Alpaca response."""
    if isinstance(df_all.index, pd.MultiIndex):
        lvl0 = df_all.index.get_level_values(0)
        if symbol in lvl0:
            return df_all.xs(symbol, level=0).copy()
        if len(lvl0) > 0:
            return df_all.xs(lvl0[0], level=0).copy()
        return None
    return df_all.copy()


class MarketDataFetcher:
    """Fetch OHLCV bars from Alpaca.  Caches results in memory."""

    def __init__(
        self,
        lookback_days: int = 120,
        interval: str = "1d",
        api_key: str = "",
        secret_key: str = "",
    ):
        self.lookback_days = lookback_days
        self.interval = interval
        self._cache: Dict[str, pd.DataFrame] = {}
        self._client: Optional["StockHistoricalDataClient"] = None

        key = api_key or os.getenv("ALPACA_API_KEY", "")
        sec = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        if _ALPACA_OK and key and sec:
            self._client = StockHistoricalDataClient(key, sec)
        else:
            logger.warning("MarketDataFetcher: no Alpaca credentials — fetches will be empty")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_request(self, symbols, tf) -> Optional[pd.DataFrame]:
        if self._client is None:
            return None
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(self.lookback_days * 1.6))
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=tf,
                start=start,
                end=end,
                adjustment="split",
                feed=_IEX_FEED,
            )
            return self._client.get_stock_bars(req).df
        except Exception as e:
            logger.error(f"MarketDataFetcher._make_request: {e}")
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch(self, symbol: str, force_refresh: bool = False) -> Optional[pd.DataFrame]:
        if not force_refresh and symbol in self._cache:
            return self._cache[symbol]
        tf = _TF_MAP.get(self.interval, TimeFrame.Day)
        df_all = self._make_request(symbol, tf)
        if df_all is None:
            return None
        df = _normalise(_extract_symbol(df_all, symbol))
        if df is not None:
            self._cache[symbol] = df
        return df

    def fetch_many(
        self, symbols: List[str], force_refresh: bool = False
    ) -> Dict[str, pd.DataFrame]:
        results: Dict[str, pd.DataFrame] = {}
        to_fetch = [s for s in symbols if force_refresh or s not in self._cache]
        # Return already-cached items
        for s in symbols:
            if s not in to_fetch and s in self._cache:
                results[s] = self._cache[s]

        if not to_fetch or self._client is None:
            return results

        tf = _TF_MAP.get(self.interval, TimeFrame.Day)
        df_all = self._make_request(to_fetch, tf)
        if df_all is None:
            return results

        if isinstance(df_all.index, pd.MultiIndex):
            for sym in to_fetch:
                try:
                    df = _normalise(df_all.xs(sym, level=0).copy())
                    if df is not None:
                        self._cache[sym] = df
                        results[sym] = df
                except KeyError:
                    pass
        else:
            # Single-symbol response with a plain index
            sym = to_fetch[0]
            df = _normalise(df_all.copy())
            if df is not None:
                self._cache[sym] = df
                results[sym] = df

        return results

    def get_current_price(self, symbol: str) -> Optional[float]:
        if symbol in self._cache and not self._cache[symbol].empty:
            return float(self._cache[symbol]["Close"].iloc[-1])
        df = self.fetch(symbol)
        if df is not None and not df.empty:
            return float(df["Close"].iloc[-1])
        return None
