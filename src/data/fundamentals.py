"""Quality Factor fundamental filter using yfinance.

Screens for high-quality companies: strong ROE, low debt, consistent
earnings growth. Results are cached per symbol for 24 hours so the
filter doesn't slow down intraday trading cycles.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_cache: Dict[str, Tuple[bool, datetime]] = {}   # {symbol: (passes, cached_at)}
_CACHE_TTL_HOURS = 24


class FundamentalFilter:
    def __init__(
        self,
        pe_max: float = 30.0,
        de_max: float = 2.0,
        roe_min: float = 0.10,          # ROE >= 10%
        require_positive_fcf: bool = True,
        require_positive_eps_growth: bool = True,
    ):
        self.pe_max = pe_max
        self.de_max = de_max
        self.roe_min = roe_min
        self.require_positive_eps_growth = require_positive_eps_growth

    def passes(self, symbol: str) -> bool:
        cached = _cache.get(symbol)
        if cached:
            result, ts = cached
            if datetime.utcnow() - ts < timedelta(hours=_CACHE_TTL_HOURS):
                return result
        result = self._check(symbol)
        _cache[symbol] = (result, datetime.utcnow())
        return result

    def filter(self, symbols: List[str]) -> List[str]:
        return [s for s in symbols if self.passes(s)]

    def _check(self, symbol: str) -> bool:
        try:
            import yfinance as yf
            info = yf.Ticker(symbol).info

            # ── Return on Equity ─────────────────────────────────────────────
            roe = info.get("returnOnEquity")
            if roe is not None and roe < self.roe_min:
                logger.debug(f"  [QUALITY] {symbol}: ROE {roe:.1%} < {self.roe_min:.1%} — fail")
                return False

            # ── Debt / Equity ────────────────────────────────────────────────
            de = info.get("debtToEquity")
            if de is not None and de > self.de_max * 100:   # yfinance returns %, not ratio
                logger.debug(f"  [QUALITY] {symbol}: D/E {de:.0f} > {self.de_max * 100:.0f} — fail")
                return False

            # ── P/E ratio ────────────────────────────────────────────────────
            pe = info.get("trailingPE") or info.get("forwardPE")
            if pe is not None and pe > self.pe_max:
                logger.debug(f"  [QUALITY] {symbol}: P/E {pe:.1f} > {self.pe_max:.0f} — fail")
                return False

            # ── Earnings growth ──────────────────────────────────────────────
            if self.require_positive_eps_growth:
                eg = info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
                if eg is not None and eg < 0:
                    logger.debug(f"  [QUALITY] {symbol}: earnings growth {eg:.1%} < 0 — fail")
                    return False

            logger.debug(f"  [QUALITY] {symbol}: passed (ROE={roe}, D/E={de}, P/E={pe})")
            return True

        except Exception as e:
            # On any data error, allow the symbol through (fail open)
            logger.debug(f"  [QUALITY] {symbol}: data error ({e}) — allowing through")
            return True
