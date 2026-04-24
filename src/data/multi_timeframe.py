import logging
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Contribution weight of each timeframe to the final composite score
_WEIGHTS: Dict[str, float] = {"1d": 0.50, "1h": 0.30, "15m": 0.20}

# How much history to fetch per interval
_PERIODS: Dict[str, str] = {"1d": "120d", "1h": "60d", "15m": "7d"}


@dataclass
class MTFSignal:
    symbol: str
    score_1d: float
    score_1h: float
    score_15m: float
    composite: float
    action: str
    confidence: float


class MultiTimeframeAnalyzer:
    """Blend signal scores from 1d / 1h / 15m into a weighted composite.

    Falls back gracefully when intraday data is unavailable — missing
    timeframes are excluded and remaining weights are renormalized.
    """

    def __init__(
        self,
        indicators,
        analyzer,
        buy_threshold: float = 0.20,
        sell_threshold: float = -0.20,
    ):
        self.indicators = indicators
        self.analyzer = analyzer
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold

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

        return MTFSignal(
            symbol=symbol,
            score_1d=round(scores.get("1d", 0.0), 4),
            score_1h=round(scores.get("1h", 0.0), 4),
            score_15m=round(scores.get("15m", 0.0), 4),
            composite=round(composite, 4),
            action=action,
            confidence=round(confidence, 3),
        )

    def _score_interval(self, symbol: str, interval: str) -> Optional[float]:
        try:
            import yfinance as yf

            raw = yf.download(
                symbol,
                period=_PERIODS[interval],
                interval=interval,
                auto_adjust=True,
                progress=False,
            )
            if raw is None or raw.empty or len(raw) < 15:
                return None

            # Flatten MultiIndex columns produced by single-ticker downloads
            if hasattr(raw.columns, "levels"):
                raw.columns = raw.columns.droplevel(1)

            ind = self.indicators.compute(raw)
            sig = self.analyzer.analyze(ind)
            return sig.score

        except Exception as e:
            logger.debug(f"MTF {symbol}/{interval}: {e}")
            return None
