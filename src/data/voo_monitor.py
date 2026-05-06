"""VOO 200-week MA monitor using Alpaca weekly bars."""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..utils import now_et
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class VOOStatus:
    price: float
    ma200w: float
    gap_pct: float      # (price - ma200w) / ma200w * 100
    above_ma: bool
    alert: bool         # True on crossover or when gap is within alert_threshold_pct
    checked_at: str

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "ma200w": self.ma200w,
            "gap_pct": self.gap_pct,
            "above_ma": self.above_ma,
            "alert": self.alert,
            "checked_at": self.checked_at,
        }


class VOOMonitor:
    """Track VOO price vs its 200-week simple moving average.

    Fetches 5 years of weekly closes once per calendar day and caches
    the result.  Call check(force=True) to bypass the cache.
    """

    def __init__(
        self,
        alert_threshold_pct: float = 2.0,
        api_key: str = "",
        secret_key: str = "",
    ):
        self.alert_threshold_pct = alert_threshold_pct
        self._api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self._secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        self._last_status: Optional[VOOStatus] = None
        self._last_check_date: Optional[str] = None
        self._prev_above_ma: Optional[bool] = None

    def check(self, force: bool = False) -> Optional[VOOStatus]:
        today = now_et().strftime("%Y-%m-%d")
        if not force and self._last_check_date == today and self._last_status:
            return self._last_status

        try:
            import pandas as pd
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            if not self._api_key or not self._secret_key:
                logger.warning("VOOMonitor: no Alpaca credentials")
                return self._last_status

            client = StockHistoricalDataClient(self._api_key, self._secret_key)
            end = datetime.now(timezone.utc)
            start = end - timedelta(weeks=210)  # ~4 years, enough for 200-week MA

            req = StockBarsRequest(
                symbol_or_symbols="VOO",
                timeframe=TimeFrame.Week,
                start=start,
                end=end,
                feed="iex",
            )
            df_all = client.get_stock_bars(req).df

            if df_all is None or df_all.empty:
                logger.warning("VOOMonitor: empty data returned")
                return self._last_status

            if isinstance(df_all.index, pd.MultiIndex):
                df = df_all.xs("VOO", level=0).copy()
            else:
                df = df_all.copy()

            if hasattr(df.index, "tz") and df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            close = (df["close"] if "close" in df.columns else df["Close"]).dropna()
            if len(close) < 10:
                logger.warning(f"VOOMonitor: only {len(close)} weeks of data")
                return self._last_status

            n = min(200, len(close))
            ma200w = float(close.iloc[-n:].mean())
            price = float(close.iloc[-1])
            gap_pct = (price - ma200w) / ma200w * 100
            above_ma = price > ma200w

            alert = False
            if self._prev_above_ma is not None and above_ma != self._prev_above_ma:
                alert = True  # crossover
            elif abs(gap_pct) <= self.alert_threshold_pct:
                alert = True  # price near MA

            self._prev_above_ma = above_ma
            self._last_check_date = today
            self._last_status = VOOStatus(
                price=round(price, 2),
                ma200w=round(ma200w, 2),
                gap_pct=round(gap_pct, 2),
                above_ma=above_ma,
                alert=alert,
                checked_at=now_et().strftime("%Y-%m-%d %H:%M:%S ET"),
            )
            logger.info(
                f"VOOMonitor: ${price:.2f}  200W MA ${ma200w:.2f}  gap {gap_pct:+.1f}%"
                + ("  *** ALERT ***" if alert else "")
            )
            return self._last_status

        except Exception as e:
            logger.error(f"VOOMonitor: fetch error — {e}")
            return self._last_status

    @property
    def last_status(self) -> Optional[VOOStatus]:
        return self._last_status
