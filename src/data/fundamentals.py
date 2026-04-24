import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class FundamentalFilter:
    """Filter symbols by fundamental quality.

    Criteria (all configurable):
      - Forward P/E < pe_max         (default 30)
      - Debt-to-equity < de_max      (default 2.0)
      - Free cash flow > 0
      - EPS / earnings growth > 0
    Data is fetched once per symbol per day and cached in memory.
    On any fetch error the symbol is allowed through so a temporary
    data outage does not silently block the whole universe.
    """

    def __init__(
        self,
        pe_max: float = 30.0,
        de_max: float = 2.0,
        require_positive_fcf: bool = True,
        require_positive_eps_growth: bool = True,
    ):
        self.pe_max = pe_max
        self.de_max = de_max
        self.require_positive_fcf = require_positive_fcf
        self.require_positive_eps_growth = require_positive_eps_growth
        self._cache: Dict[str, Optional[bool]] = {}
        self._cache_date: Optional[str] = None

    def _reset_if_stale(self) -> None:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        if self._cache_date != today:
            self._cache.clear()
            self._cache_date = today

    def passes(self, symbol: str) -> bool:
        self._reset_if_stale()
        if symbol in self._cache:
            return self._cache[symbol] is True

        try:
            import yfinance as yf
            info = yf.Ticker(symbol).info

            # P/E — prefer forwardPE, fall back to trailingPE
            pe = info.get("forwardPE") or info.get("trailingPE")
            if pe is not None and pe > self.pe_max:
                logger.debug(f"FundFilter: {symbol} P/E {pe:.1f} > {self.pe_max}")
                self._cache[symbol] = False
                return False

            # Debt-to-equity (yfinance returns the ratio * 100 as a percentage)
            de_raw = info.get("debtToEquity")
            if de_raw is not None:
                de = de_raw / 100.0
                if de > self.de_max:
                    logger.debug(f"FundFilter: {symbol} D/E {de:.2f} > {self.de_max}")
                    self._cache[symbol] = False
                    return False

            # Free cash flow
            if self.require_positive_fcf:
                fcf = info.get("freeCashflow")
                if fcf is not None and fcf <= 0:
                    logger.debug(f"FundFilter: {symbol} FCF ${fcf:,.0f} <= 0")
                    self._cache[symbol] = False
                    return False

            # EPS / earnings growth
            if self.require_positive_eps_growth:
                growth = info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
                if growth is not None and growth <= 0:
                    logger.debug(f"FundFilter: {symbol} EPS growth {growth:.1%} <= 0")
                    self._cache[symbol] = False
                    return False

            self._cache[symbol] = True
            return True

        except Exception as e:
            logger.debug(f"FundFilter: {symbol} fetch error ({e}) — allowing through")
            self._cache[symbol] = True
            return True

    def filter(self, symbols: List[str]) -> List[str]:
        passed = [s for s in symbols if self.passes(s)]
        logger.info(
            f"FundFilter: {len(passed)}/{len(symbols)} passed "
            f"(P/E<{self.pe_max}, D/E<{self.de_max})"
        )
        return passed
