import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    """Classify the broad market as BULL / BEAR / CHOPPY using SPY MAs and VIX.

    Results are cached for `cache_hours` to avoid redundant network calls on
    every 60-second trading cycle.
    """

    def __init__(
        self,
        cache_hours: int = 4,
        bull_vix_max: float = 25.0,
        bear_vix_min: float = 27.0,
    ):
        self.cache_hours = cache_hours
        self.bull_vix_max = bull_vix_max
        self.bear_vix_min = bear_vix_min
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
            import yfinance as yf
            import pandas as pd

            spy_raw = yf.download(
                "SPY",
                period="14mo",
                interval="1d",
                auto_adjust=True,
                progress=False,
            )
            if spy_raw is None or spy_raw.empty or len(spy_raw) < _REQUIRED_BARS:
                logger.warning("RegimeDetector: insufficient SPY data")
                return None

            if hasattr(spy_raw.columns, "levels"):
                spy_raw.columns = spy_raw.columns.droplevel(1)

            close = spy_raw["Close"]
            spy_price = float(close.iloc[-1])
            sma50 = float(close.rolling(50).mean().iloc[-1])
            sma200 = float(close.rolling(200).mean().iloc[-1])

            vix: Optional[float] = None
            try:
                vix_raw = yf.download(
                    "^VIX",
                    period="5d",
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                )
                if vix_raw is not None and not vix_raw.empty:
                    if hasattr(vix_raw.columns, "levels"):
                        vix_raw.columns = vix_raw.columns.droplevel(1)
                    vix = float(vix_raw["Close"].iloc[-1])
            except Exception as e:
                logger.debug(f"RegimeDetector: VIX fetch failed: {e}")

            above_sma50 = spy_price > sma50
            above_sma200 = spy_price > sma200
            sma50_above_sma200 = sma50 > sma200

            regime = self._classify(
                above_sma50, above_sma200, sma50_above_sma200, vix
            )

            return RegimeResult(
                regime=regime,
                spy_price=spy_price,
                sma50=sma50,
                sma200=sma200,
                vix=vix,
                above_sma50=above_sma50,
                above_sma200=above_sma200,
                checked_at=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

        except Exception as e:
            logger.error(f"RegimeDetector: fetch failed: {e}")
            return None

    def _classify(
        self,
        above_sma50: bool,
        above_sma200: bool,
        sma50_above_sma200: bool,
        vix: Optional[float],
    ) -> str:
        # BEAR: below SMA200 and (VIX elevated or VIX unknown)
        if not above_sma200:
            if vix is None or vix > self.bear_vix_min:
                return "BEAR"

        # BULL: above SMA200, golden-cross alignment, and calm VIX
        if above_sma200 and sma50_above_sma200:
            if vix is None or vix < self.bull_vix_max:
                return "BULL"

        return "CHOPPY"
