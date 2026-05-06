"""Market regime detector using SPY SMA50/SMA200 via Alpaca bars.

VIX is not available on Alpaca; regime is determined purely by SPY
moving-average alignment.  All symbols pass when data is unavailable.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..utils import now_et
from typing import Optional

logger = logging.getLogger(__name__)

_REQUIRED_BARS = 210    # need 200+ to compute SMA200


@dataclass
class RegimeResult:
    regime: str             # "BULL", "BEAR", "CHOPPY"
    spy_price: float
    sma50: float
    sma200: float
    vix: Optional[float]
    above_sma50: bool
    above_sma200: bool
    checked_at: str

    def to_dict(self) -> dict:
        return {
            "regime": self.regime,
            "spy_price": round(self.spy_price, 2),
            "sma50": round(self.sma50, 2),
            "sma200": round(self.sma200, 2),
            "vix": round(self.vix, 2) if self.vix is not None else None,
            "above_sma50": self.above_sma50,
            "above_sma200": self.above_sma200,
            "checked_at": self.checked_at,
        }


class RegimeDetector:
    """Classify the broad market as BULL / BEAR / CHOPPY using SPY MAs.

    Results are cached for `cache_hours` to avoid redundant network calls on
    every 60-second trading cycle.
    """

    def __init__(
        self,
        cache_hours: int = 4,
        bull_vix_max: float = 25.0,
        bear_vix_min: float = 27.0,
        api_key: str = "",
        secret_key: str = "",
    ):
        self.cache_hours = cache_hours
        self.bull_vix_max = bull_vix_max
        self.bear_vix_min = bear_vix_min
        self._api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self._secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        self._cached: Optional[RegimeResult] = None
        self._cached_at: Optional[datetime] = None

    def detect(self, force: bool = False) -> Optional[RegimeResult]:
        """Return the current market regime, using cache unless stale."""
        if not force and self._cached is not None and self._cached_at is not None:
            age = datetime.utcnow() - self._cached_at
            if age < timedelta(hours=self.cache_hours):
                return self._cached

        result = self._fetch_and_classify()
        if result is not None:
            self._cached = result
            self._cached_at = datetime.utcnow()
        return result

    def _fetch_and_classify(self) -> Optional[RegimeResult]:
        try:
            import pandas as pd
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            if not self._api_key or not self._secret_key:
                logger.warning("RegimeDetector: no Alpaca credentials")
                return None

            client = StockHistoricalDataClient(self._api_key, self._secret_key)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=int(_REQUIRED_BARS * 1.6))

            req = StockBarsRequest(
                symbol_or_symbols="SPY",
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed="iex",
            )
            df_all = client.get_stock_bars(req).df

            if df_all is None or df_all.empty:
                logger.warning("RegimeDetector: empty SPY data")
                return None

            # Extract from MultiIndex if present
            if isinstance(df_all.index, pd.MultiIndex):
                df = df_all.xs("SPY", level=0).copy()
            else:
                df = df_all.copy()

            if hasattr(df.index, "tz") and df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            close = df["close"] if "close" in df.columns else df["Close"]
            if len(close) < _REQUIRED_BARS:
                logger.warning("RegimeDetector: insufficient SPY bars (%d)", len(close))
                return None

            spy_price = float(close.iloc[-1])
            sma50 = float(close.rolling(50).mean().iloc[-1])
            sma200 = float(close.rolling(200).mean().iloc[-1])

            above_sma50 = spy_price > sma50
            above_sma200 = spy_price > sma200
            sma50_above_sma200 = sma50 > sma200

            regime = self._classify(above_sma50, above_sma200, sma50_above_sma200)

            return RegimeResult(
                regime=regime,
                spy_price=spy_price,
                sma50=sma50,
                sma200=sma200,
                vix=None,
                above_sma50=above_sma50,
                above_sma200=above_sma200,
                checked_at=now_et().strftime("%Y-%m-%d %H:%M:%S ET"),
            )

        except Exception as e:
            logger.error(f"RegimeDetector: fetch failed: {e}")
            return None

    def _classify(
        self,
        above_sma50: bool,
        above_sma200: bool,
        sma50_above_sma200: bool,
    ) -> str:
        if not above_sma200:
            return "BEAR"
        if above_sma200 and sma50_above_sma200:
            return "BULL"
        return "CHOPPY"
