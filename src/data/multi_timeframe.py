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
    agreement: int = 0          # how many TFs agree with the composite direction


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
    ):
        self.indicators = indicators
        self.analyzer = analyzer
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.min_agreeing = min_agreeing

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

        # Count how many TFs agree with the composite direction
        agreement = 0
        if action != "HOLD":
            for tf_score in scores.values():
                if action == "BUY" and tf_score > 0:
                    agreement += 1
                elif action == "SELL" and tf_score < 0:
                    agreement += 1

        # Enforce minimum agreement — downgrade to HOLD when insufficient
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
