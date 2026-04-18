import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


class MarketDataFetcher:
    def __init__(self, lookback_days: int = 120, interval: str = "1d"):
        self.lookback_days = lookback_days
        self.interval = interval
        self._cache: Dict[str, pd.DataFrame] = {}

    def fetch(self, symbol: str, force_refresh: bool = False) -> Optional[pd.DataFrame]:
        if not force_refresh and symbol in self._cache:
            return self._cache[symbol]

        end = datetime.now()
        start = end - timedelta(days=self.lookback_days)

        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(start=start, end=end, interval=self.interval)
            if df.empty:
                logger.warning(f"No data returned for {symbol}")
                return None
            df.index = pd.to_datetime(df.index)
            self._cache[symbol] = df
            return df
        except Exception as e:
            logger.error(f"Failed to fetch data for {symbol}: {e}")
            return None

    def fetch_many(
        self, symbols: list, force_refresh: bool = False
    ) -> Dict[str, pd.DataFrame]:
        results = {}
        for symbol in symbols:
            df = self.fetch(symbol, force_refresh=force_refresh)
            if df is not None:
                results[symbol] = df
        return results

    def get_current_price(self, symbol: str) -> Optional[float]:
        try:
            ticker = yf.Ticker(symbol)
            price = ticker.fast_info.last_price
            if price and price > 0:
                return float(price)
        except Exception:
            pass
        # Fallback to last cached close
        if symbol in self._cache and not self._cache[symbol].empty:
            return float(self._cache[symbol]["Close"].iloc[-1])
        return None
