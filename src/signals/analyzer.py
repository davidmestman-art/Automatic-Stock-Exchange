from dataclasses import dataclass, field
from typing import Dict, List, Literal

from .indicators import IndicatorValues

SignalAction = Literal["BUY", "SELL", "HOLD"]


@dataclass
class SignalResult:
    action: SignalAction
    score: float                        # -1.0 (strong sell) to +1.0 (strong buy)
    confidence: float                   # 0.0 to 1.0 — abs(score)
    reasons: List[str]
    indicator_scores: Dict[str, float]


class SignalAnalyzer:
    def __init__(self, buy_threshold: float = 0.35, sell_threshold: float = -0.35):
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold

        self._weights = {
            "rsi": 0.25,
            "macd": 0.30,
            "ema_cross": 0.25,
            "bollinger": 0.20,
        }

    def analyze(self, ind: IndicatorValues) -> SignalResult:
        scores: Dict[str, float] = {}
        reasons: List[str] = []

        rsi_score, rsi_reasons = self._rsi_signal(ind)
        scores["rsi"] = rsi_score
        reasons.extend(rsi_reasons)

        macd_score, macd_reasons = self._macd_signal(ind)
        scores["macd"] = macd_score
        reasons.extend(macd_reasons)

        ema_score, ema_reasons = self._ema_cross_signal(ind)
        scores["ema_cross"] = ema_score
        reasons.extend(ema_reasons)

        bb_score, bb_reasons = self._bollinger_signal(ind)
        scores["bollinger"] = bb_score
        reasons.extend(bb_reasons)

        vol_mult = self._volume_multiplier(ind)
        composite = sum(scores[k] * self._weights[k] for k in scores) * vol_mult
        composite = max(-1.0, min(1.0, composite))

        if composite >= self.buy_threshold:
            action: SignalAction = "BUY"
        elif composite <= self.sell_threshold:
            action = "SELL"
        else:
            action = "HOLD"

        return SignalResult(
            action=action,
            score=composite,
            confidence=abs(composite),
            reasons=reasons,
            indicator_scores=scores,
        )

    def _rsi_signal(self, ind: IndicatorValues):
        if ind.rsi is None:
            return 0.0, []
        rsi = ind.rsi
        reasons: List[str] = []

        if rsi <= 30:
            score = 1.0 - (rsi / 30)
            reasons.append(f"RSI oversold ({rsi:.1f})")
        elif rsi >= 70:
            score = -((rsi - 70) / 30)
            reasons.append(f"RSI overbought ({rsi:.1f})")
        elif rsi < 45:
            score = 0.3
            reasons.append(f"RSI near oversold ({rsi:.1f})")
        elif rsi > 55:
            score = -0.3
            reasons.append(f"RSI near overbought ({rsi:.1f})")
        else:
            score = 0.0

        return score, reasons

    def _macd_signal(self, ind: IndicatorValues):
        if ind.macd_hist is None or ind.macd_hist_prev is None:
            return 0.0, []
        reasons: List[str] = []
        hist, hist_prev = ind.macd_hist, ind.macd_hist_prev

        if hist > 0 and hist_prev <= 0:
            score = 1.0
            reasons.append("MACD bullish crossover")
        elif hist < 0 and hist_prev >= 0:
            score = -1.0
            reasons.append("MACD bearish crossover")
        elif hist > 0 and hist > hist_prev:
            score = 0.5
            reasons.append("MACD bullish momentum")
        elif hist < 0 and hist < hist_prev:
            score = -0.5
            reasons.append("MACD bearish momentum")
        elif hist > 0:
            score = 0.2
        elif hist < 0:
            score = -0.2
        else:
            score = 0.0

        return score, reasons

    def _ema_cross_signal(self, ind: IndicatorValues):
        if None in (ind.ema_fast, ind.ema_slow, ind.ema_fast_prev, ind.ema_slow_prev):
            return 0.0, []
        reasons: List[str] = []

        above_now = ind.ema_fast > ind.ema_slow
        above_prev = ind.ema_fast_prev > ind.ema_slow_prev

        if above_now and not above_prev:
            score = 1.0
            reasons.append("EMA golden cross")
        elif not above_now and above_prev:
            score = -1.0
            reasons.append("EMA death cross")
        elif above_now:
            spread = (ind.ema_fast - ind.ema_slow) / ind.ema_slow
            score = min(0.6, spread * 10)
            if score > 0.2:
                reasons.append(f"EMA bullish trend ({spread * 100:.2f}% spread)")
        else:
            spread = (ind.ema_slow - ind.ema_fast) / ind.ema_slow
            score = -min(0.6, spread * 10)
            if score < -0.2:
                reasons.append(f"EMA bearish trend ({spread * 100:.2f}% spread)")

        return score, reasons

    def _bollinger_signal(self, ind: IndicatorValues):
        if None in (ind.bb_upper, ind.bb_lower, ind.bb_middle, ind.close):
            return 0.0, []
        reasons: List[str] = []

        price = ind.close
        band_width = ind.bb_upper - ind.bb_lower
        if band_width == 0:
            return 0.0, []

        position = (price - ind.bb_middle) / (band_width / 2)

        if price <= ind.bb_lower:
            score = 1.0
            reasons.append("Price at lower Bollinger Band (oversold)")
        elif price >= ind.bb_upper:
            score = -1.0
            reasons.append("Price at upper Bollinger Band (overbought)")
        elif position < -0.5:
            score = 0.5
            reasons.append("Price approaching lower Bollinger Band")
        elif position > 0.5:
            score = -0.5
            reasons.append("Price approaching upper Bollinger Band")
        else:
            score = -position * 0.3

        return score, reasons

    def _volume_multiplier(self, ind: IndicatorValues) -> float:
        if not ind.volume or not ind.avg_volume or ind.avg_volume == 0:
            return 1.0
        ratio = ind.volume / ind.avg_volume
        if ratio > 1.5:
            return min(1.3, 1.0 + (ratio - 1.5) * 0.2)
        if ratio < 0.5:
            return 0.8
        return 1.0
