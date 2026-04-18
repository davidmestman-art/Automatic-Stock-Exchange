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

        vals.rsi = self._rsi(close)

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

        return vals

    def _rsi(self, close: pd.Series) -> float:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=self.rsi_period - 1, min_periods=self.rsi_period).mean()
        avg_loss = loss.ewm(com=self.rsi_period - 1, min_periods=self.rsi_period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])

    def _macd(self, close: pd.Series):
        ema_fast = close.ewm(span=self.macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.macd_slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=self.macd_signal_period, adjust=False).mean()
        histogram = macd_line - signal_line
        return macd_line, signal_line, histogram
