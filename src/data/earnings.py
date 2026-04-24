import logging
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class EarningsCalendar:
    """Block buys when a symbol has earnings within buffer_days.

    Uses yfinance Ticker.calendar to look up the next scheduled earnings
    date.  Results are cached per calendar day so the fetches happen at
    most once per symbol per session.  On any data error the symbol is
    allowed through (fail open) to avoid silently suppressing valid trades.
    """

    def __init__(self, buffer_days: int = 3):
        self.buffer_days = buffer_days
        self._cache: Dict[str, Optional[datetime]] = {}
        self._cache_date: Optional[str] = None

    def _reset_if_stale(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._cache_date != today:
            self._cache.clear()
            self._cache_date = today

    def has_upcoming_earnings(self, symbol: str) -> bool:
        """Return True if earnings are within buffer_days from today."""
        self._reset_if_stale()

        if symbol in self._cache:
            next_dt = self._cache[symbol]
            if next_dt is None:
                return False
            days_away = (next_dt - datetime.now()).days
            return 0 <= days_away <= self.buffer_days

        try:
            import yfinance as yf
            import pandas as pd

            cal = yf.Ticker(symbol).calendar
            next_dt: Optional[datetime] = None

            if isinstance(cal, dict):
                raw = cal.get("Earnings Date")
                if raw:
                    first = raw[0] if hasattr(raw, "__getitem__") else raw
                    next_dt = pd.Timestamp(first).to_pydatetime()
            elif cal is not None and hasattr(cal, "empty") and not cal.empty:
                if "Earnings Date" in cal.index:
                    raw = cal.loc["Earnings Date"]
                    first = raw.iloc[0] if hasattr(raw, "iloc") else raw
                    next_dt = pd.Timestamp(first).to_pydatetime()

            self._cache[symbol] = next_dt
            if next_dt is None:
                return False

            days_away = (next_dt - datetime.now()).days
            if 0 <= days_away <= self.buffer_days:
                logger.info(
                    f"EarningsCalendar: {symbol} earnings in {days_away}d — blocking buy"
                )
                return True
            return False

        except Exception as e:
            logger.debug(f"EarningsCalendar: {symbol} — {e} — allowing buy")
            self._cache[symbol] = None
            return False
