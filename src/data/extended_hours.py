"""Pre-market and after-hours price data.

Fetches extended-hours quotes for a list of symbols via yfinance.
Results are cached for ``cache_ttl_seconds`` (default 120 s) so rapid
dashboard refreshes don't hammer the API.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

import yfinance as yf

logger = logging.getLogger(__name__)


class ExtendedHoursMonitor:
    def __init__(self, cache_ttl_seconds: int = 120):
        self.cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, dict] = {}
        self._cache_ts: Dict[str, datetime] = {}

    def fetch(self, symbols: List[str]) -> List[dict]:
        """Return extended-hours data for each symbol (cached)."""
        results = []
        for sym in symbols:
            data = self._fetch_one(sym)
            if data:
                results.append(data)
        return results

    def _fetch_one(self, symbol: str) -> Optional[dict]:
        now = datetime.now()
        ts = self._cache_ts.get(symbol)
        if ts and (now - ts).total_seconds() < self.cache_ttl:
            return self._cache.get(symbol)

        try:
            info = yf.Ticker(symbol).info
            regular = info.get("regularMarketPrice") or info.get("currentPrice")
            pre = info.get("preMarketPrice")
            post = info.get("postMarketPrice")

            def pct(ext_price):
                if ext_price and regular:
                    return round((ext_price / regular - 1) * 100, 2)
                return None

            result = {
                "symbol": symbol,
                "regular_price": round(float(regular), 2) if regular else None,
                "pre_market_price": round(float(pre), 2) if pre else None,
                "pre_market_change_pct": pct(pre),
                "post_market_price": round(float(post), 2) if post else None,
                "post_market_change_pct": pct(post),
            }
            self._cache[symbol] = result
            self._cache_ts[symbol] = now
            return result
        except Exception as e:
            logger.debug(f"Extended hours fetch failed for {symbol}: {e}")
            return None

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_ts.clear()
