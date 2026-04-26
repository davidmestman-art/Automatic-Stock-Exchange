from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class IndicatorValues:
    rsi: Optional[float] = None

    macd_line: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    macd_hist_prev: Optional[float] = None

    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    ema_fast_prev: Optional[float] = None
    ema_slow_prev: Optional[float] = None

    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None

    close: Optional[float] = None
    volume: Optional[float] = None
    avg_volume: Optional[float] = None

    # Mean reversion
    z_score: Optional[float] = None        # (close - sma20) / std20

    # Volatility — used by adaptive position sizing
    atr: Optional[float] = None            # 14-day ATR in dollars
    atr_pct: Optional[float] = None        # ATR as fraction of close price

    # Momentum
    roc_10: Optional[float] = None         # 10-day rate of change
    roc_20: Optional[float] = None         # 20-day rate of change
    stoch_rsi: Optional[float] = None      # Stochastic RSI [0, 100]


class TechnicalIndicators:
    def __init__(
        self,
        rsi_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        ema_fast: int = 20,
        ema_slow: int = 50,
        bb_period: int = 20,
        bb_std: float = 2.0,
    ):
        self.rsi_period = rsi_period
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal_period = macd_signal
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.bb_period = bb_period
        self.bb_std = bb_std

    def compute(self, df: pd.DataFrame) -> IndicatorValues:
        min_bars = max(self.macd_slow, self.ema_slow, self.bb_period) + 10
        if len(df) < min_bars:
            return IndicatorValues()

        close = df["Close"]
        volume = df["Volume"]

        vals = IndicatorValues()
        vals.close = float(close.iloc[-1])
        vals.volume = float(volume.iloc[-1])
        vals.avg_volume = float(volume.rolling(20).mean().iloc[-1])

        rsi_ser = self._rsi_series(close)
        vals.rsi = float(rsi_ser.iloc[-1])
        vals.stoch_rsi = self._stoch_rsi(rsi_ser)

        macd_line, signal_line, histogram = self._macd(close)
        vals.macd_line = float(macd_line.iloc[-1])
        vals.macd_signal = float(signal_line.iloc[-1])
        vals.macd_hist = float(histogram.iloc[-1])
        vals.macd_hist_prev = float(histogram.iloc[-2])

        ema_f = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema_s = close.ewm(span=self.ema_slow, adjust=False).mean()
        vals.ema_fast = float(ema_f.iloc[-1])
        vals.ema_slow = float(ema_s.iloc[-1])
        vals.ema_fast_prev = float(ema_f.iloc[-2])
        vals.ema_slow_prev = float(ema_s.iloc[-2])

        sma = close.rolling(self.bb_period).mean()
        std = close.rolling(self.bb_period).std()
        vals.bb_upper = float((sma + self.bb_std * std).iloc[-1])
        vals.bb_middle = float(sma.iloc[-1])
        vals.bb_lower = float((sma - self.bb_std * std).iloc[-1])

        # Z-score: how many std-devs the close is from its 20-day SMA
        z = (close - sma) / std.replace(0, np.nan)
        vals.z_score = float(z.iloc[-1]) if not np.isnan(z.iloc[-1]) else None

        # ATR (14-day EMA of True Range) for volatility-based position sizing
        high = df["High"]
        low = df["Low"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_ser = tr.ewm(span=14, adjust=False).mean()
        vals.atr = float(atr_ser.iloc[-1])
        vals.atr_pct = float(vals.atr / vals.close) if vals.close else None

        if len(close) > 11:
            vals.roc_10 = float(close.iloc[-1] / close.iloc[-11] - 1)
        if len(close) > 21:
            vals.roc_20 = float(close.iloc[-1] / close.iloc[-21] - 1)

        return vals

    def _rsi_series(self, close: pd.Series) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=self.rsi_period - 1, min_periods=self.rsi_period).mean()
        avg_loss = loss.ewm(com=self.rsi_period - 1, min_periods=self.rsi_period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def _stoch_rsi(self, rsi_ser: pd.Series, period: int = 14) -> Optional[float]:
        lo = rsi_ser.rolling(period).min()
        hi = rsi_ser.rolling(period).max()
        rng = (hi - lo).replace(0, np.nan)
        stoch = (rsi_ser - lo) / rng * 100
        val = stoch.iloc[-1]
        return float(val) if not np.isnan(val) else None

    def _macd(self, close: pd.Series):
        ema_fast = close.ewm(span=self.macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.macd_slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=self.macd_signal_period, adjust=False).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram
