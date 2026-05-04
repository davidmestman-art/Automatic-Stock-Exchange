"""Multi-timeframe signal analyzer using Alpaca bars via MarketDataFetcher."""

import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_WEIGHTS: Dict[str, float] = {"1d": 0.50, "1h": 0.30, "15m": 0.20}

_LOOKBACK: Dict[str, int] = {"1d": 120, "1h": 60, "15m": 7}


@dataclass
class MTFSignal:
    symbol: str
    score_1d: float
    score_1h: float
    score_15m: float
    composite: float
    action: str
    confidence: float
    agreement: int = 0


class MultiTimeframeAnalyzer:
    """Blend signal scores from 1d / 1h / 15m into a weighted composite.

    Falls back gracefully when intraday data is unavailable — missing
    timeframes are excluded and remaining weights are renormalized.

    When min_agreeing > 0, trades are only taken when at least that many
    timeframes point in the same direction as the composite; otherwise the
    action is downgraded to HOLD.
    """

    def __init__(
        self,
        indicators,
        analyzer,
        buy_threshold: float = 0.20,
        sell_threshold: float = -0.20,
        min_agreeing: int = 2,
        api_key: str = "",
        secret_key: str = "",
    ):
        self.indicators = indicators
        self.analyzer = analyzer
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.min_agreeing = min_agreeing
        self._api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self._secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY", "")

    def analyze(self, symbol: str) -> Optional[MTFSignal]:
        scores: Dict[str, float] = {}
        for interval in _WEIGHTS:
            s = self._score_interval(symbol, interval)
            if s is not None:
                scores[interval] = s

        if not scores:
            return None

        total_weight = sum(_WEIGHTS[tf] for tf in scores)
        composite = sum(scores[tf] * _WEIGHTS[tf] for tf in scores) / total_weight
        composite = max(-1.0, min(1.0, composite))

        action = (
            "BUY" if composite >= self.buy_threshold
            else "SELL" if composite <= self.sell_threshold
            else "HOLD"
        )
        confidence = min(1.0, abs(composite) / max(self.buy_threshold, 0.01))

        agreement = 0
        if action != "HOLD":
            for tf_score in scores.values():
                if action == "BUY" and tf_score > 0:
                    agreement += 1
                elif action == "SELL" and tf_score < 0:
                    agreement += 1

        required = min(self.min_agreeing, len(scores))
        if action != "HOLD" and len(scores) >= 2 and agreement < required:
            action = "HOLD"

        return MTFSignal(
            symbol=symbol,
            score_1d=round(scores.get("1d", 0.0), 4),
            score_1h=round(scores.get("1h", 0.0), 4),
            score_15m=round(scores.get("15m", 0.0), 4),
            composite=round(composite, 4),
            action=action,
            confidence=round(confidence, 3),
            agreement=agreement,
        )

    def _score_interval(self, symbol: str, interval: str) -> Optional[float]:
        try:
            from src.data.fetcher import MarketDataFetcher

            fetcher = MarketDataFetcher(
                lookback_days=_LOOKBACK[interval],
                interval=interval,
                api_key=self._api_key,
                secret_key=self._secret_key,
            )
            df = fetcher.fetch(symbol)
            if df is None or df.empty or len(df) < 15:
                return None

            ind = self.indicators.compute(df)
            sig = self.analyzer.analyze(ind)
            return sig.score

        except Exception as e:
            logger.debug(f"MTF {symbol}/{interval}: {e}")
            return None
