"""Extended-hours price monitor using the shared Alpaca quote cache.

Instead of a separate yfinance call, this reads from the 60-second quote
cache already populated by AlpacaExecutor during normal trading cycles.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ExtendedHoursMonitor:
    def __init__(self, cache_ttl_seconds: int = 120):
        self.cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, dict] = {}
        self._cache_ts: Dict[str, datetime] = {}

    def fetch(self, symbols: List[str]) -> List[dict]:
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
            from src.trading.alpaca_executor import get_cached_price
            price = get_cached_price(symbol)
        except Exception:
            price = None

        if price is None:
            return None

        result = {
            "symbol": symbol,
            "regular_price": round(float(price), 2),
            "pre_market_price": round(float(price), 2),
            "pre_market_change_pct": None,
            "post_market_price": round(float(price), 2),
            "post_market_change_pct": None,
        }
        self._cache[symbol] = result
        self._cache_ts[symbol] = now
        return result

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_ts.clear()
