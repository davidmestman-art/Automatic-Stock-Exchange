from dataclasses import dataclass, field
from typing import Dict, List, Literal

from .indicators import IndicatorValues

SignalAction = Literal["BUY", "SELL", "HOLD"]

# Keys that participate in the composite score (in weight order)
_WEIGHTED_KEYS = ["rsi", "macd", "ema_cross", "bollinger", "vwap", "adx", "sector_mom"]


@dataclass
class SignalResult:
    action: SignalAction
    score: float                        # -1.0 (strong sell) to +1.0 (strong buy)
    confidence: float                   # 0.0 to 1.0 — abs(score)
    reasons: List[str]
    indicator_scores: Dict[str, float]


class SignalAnalyzer:
    # New weight distribution per user specification:
    #   RSI 25%, MACD 25%, EMA trend 20%, Bollinger 15%, new indicators 15%
    #   New indicators split evenly: VWAP 5%, ADX 5%, sector_mom 5%
    _WEIGHTS: Dict[str, float] = {
        "rsi":        0.25,
        "macd":       0.25,
        "ema_cross":  0.20,
        "bollinger":  0.15,
        "vwap":       0.05,
        "adx":        0.05,
        "sector_mom": 0.05,
    }

    def __init__(
        self,
        buy_threshold: float = 0.35,
        sell_threshold: float = -0.35,
        use_mean_reversion: bool = True,
        use_momentum: bool = True,
    ):
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.use_mean_reversion = use_mean_reversion
        self.use_momentum = use_momentum

    def analyze(self, ind: IndicatorValues) -> SignalResult:
        scores: Dict[str, float] = {}
        reasons: List[str] = []

        # Core signals (weighted)
        scores["rsi"],        r = self._rsi_signal(ind);         reasons.extend(r)
        scores["macd"],       r = self._macd_signal(ind);        reasons.extend(r)
        scores["ema_cross"],  r = self._ema_cross_signal(ind);   reasons.extend(r)
        scores["bollinger"],  r = self._bollinger_signal(ind);   reasons.extend(r)
        scores["vwap"],       r = self._vwap_signal(ind);        reasons.extend(r)
        scores["adx"],        r = self._adx_signal(ind);         reasons.extend(r)
        scores["sector_mom"], r = self._sector_mom_signal(ind);  reasons.extend(r)

        # Supplemental signals — still computed for signal reasons but not weighted
        _, mr_reasons  = self._mean_reversion_signal(ind)
        _, mom_reasons = self._momentum_signal(ind)
        reasons.extend(mr_reasons)
        reasons.extend(mom_reasons)

        vol_mult  = self._volume_multiplier(ind)
        composite = sum(scores.get(k, 0.0) * self._WEIGHTS[k] for k in _WEIGHTED_KEYS) * vol_mult
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

    # ── Core weighted signals ──────────────────────────────────────────────────

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

        above_now  = ind.ema_fast > ind.ema_slow
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

    def _vwap_signal(self, ind: IndicatorValues):
        """Price below VWAP is bullish (trading at a discount to fair value)."""
        if ind.vwap is None or ind.close is None or ind.vwap == 0:
            return 0.0, []
        dev = (ind.close - ind.vwap) / ind.vwap
        reasons: List[str] = []

        if dev <= -0.03:
            score = min(1.0, abs(dev) * 20)
            reasons.append(f"Price {abs(dev)*100:.1f}% below VWAP (${ind.vwap:.2f})")
        elif dev <= -0.01:
            score = 0.4
        elif dev < 0:
            score = 0.2
        elif dev >= 0.03:
            score = -min(1.0, dev * 20)
            reasons.append(f"Price {dev*100:.1f}% above VWAP (${ind.vwap:.2f})")
        elif dev >= 0.01:
            score = -0.4
        else:
            score = -0.2

        return score, reasons

    def _adx_signal(self, ind: IndicatorValues):
        """ADX < 20 → ranging/no trend; use +DI vs -DI for direction when trending."""
        if ind.adx is None or ind.adx_plus_di is None or ind.adx_minus_di is None:
            return 0.0, []
        adx = ind.adx
        reasons: List[str] = []

        if adx < 20:
            # No established trend — signal suppressed
            return 0.0, []

        # Scale strength from 0 (ADX=20) to 1.0 (ADX=50+)
        strength = min(1.0, (adx - 20) / 30)

        if ind.adx_plus_di > ind.adx_minus_di:
            score = strength
            if adx >= 25:
                reasons.append(f"ADX strong uptrend ({adx:.0f}, +DI>{ind.adx_minus_di:.0f})")
        else:
            score = -strength
            if adx >= 25:
                reasons.append(f"ADX strong downtrend ({adx:.0f}, -DI>{ind.adx_plus_di:.0f})")

        return score, reasons

    def _sector_mom_signal(self, ind: IndicatorValues):
        """Stock outperforming its sector over 5 days is a bullish momentum signal."""
        sm = getattr(ind, "sector_mom", None)
        if sm is None:
            return 0.0, []
        reasons: List[str] = []

        if sm >= 0.03:
            score = min(1.0, sm * 20)
            reasons.append(f"Outperforming sector by {sm*100:.1f}% (5d)")
        elif sm >= 0.01:
            score = 0.4
        elif sm >= -0.01:
            score = 0.0
        elif sm >= -0.03:
            score = -0.4
        else:
            score = max(-1.0, sm * 20)
            reasons.append(f"Underperforming sector by {abs(sm)*100:.1f}% (5d)")

        return score, reasons

    # ── Supplemental signals (generate reasons but not weighted) ───────────────

    def _mean_reversion_signal(self, ind: IndicatorValues):
        if ind.z_score is None or not self.use_mean_reversion:
            return 0.0, []
        z = ind.z_score
        reasons: List[str] = []

        if z <= -2.0:
            score = 1.0
            reasons.append(f"Mean reversion: extreme oversold (z={z:.2f})")
        elif z <= -1.5:
            score = 0.7
            reasons.append(f"Mean reversion: oversold (z={z:.2f})")
        elif z <= -1.0:
            score = 0.3
        elif z >= 2.0:
            score = -1.0
            reasons.append(f"Mean reversion: extreme overbought (z={z:.2f})")
        elif z >= 1.5:
            score = -0.7
            reasons.append(f"Mean reversion: overbought (z={z:.2f})")
        elif z >= 1.0:
            score = -0.3
        else:
            score = 0.0

        return score, reasons

    def _momentum_signal(self, ind: IndicatorValues):
        if not self.use_momentum or ind.roc_10 is None:
            return 0.0, []
        roc = ind.roc_10
        reasons: List[str] = []

        if roc >= 0.07:
            score = 1.0
            reasons.append(f"Strong momentum (+{roc * 100:.1f}% / 10d)")
        elif roc >= 0.03:
            score = 0.5
            reasons.append(f"Positive momentum (+{roc * 100:.1f}% / 10d)")
        elif roc >= 0.01:
            score = 0.2
        elif roc <= -0.07:
            score = -1.0
            reasons.append(f"Bearish momentum ({roc * 100:.1f}% / 10d)")
        elif roc <= -0.03:
            score = -0.5
            reasons.append(f"Negative momentum ({roc * 100:.1f}% / 10d)")
        elif roc <= -0.01:
            score = -0.2
        else:
            score = roc * 20

        if ind.stoch_rsi is not None:
            if ind.stoch_rsi < 20:
                score = min(score + 0.25, 1.0)
                if score > 0.3:
                    reasons.append(f"StochRSI oversold ({ind.stoch_rsi:.0f})")
            elif ind.stoch_rsi > 80:
                score = max(score - 0.25, -1.0)
                if score < -0.3:
                    reasons.append(f"StochRSI overbought ({ind.stoch_rsi:.0f})")

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
