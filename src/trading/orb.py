"""Opening Range Breakout (ORB) strategy — state management and data helpers.

Phases each trading day:
  IDLE     — before 9:15 AM ET
  SCANNING — 9:15–9:30 AM ET (universe screened once)
  FORMING  — 9:30–10:00 AM ET (1-min bars track OR high/low, no trades)
  ACTIVE   — 10:00–15:45 ET (ORB signals; BUY on breakout, SELL on breakdown)
  CLOSING  — 15:45–16:00 ET (close all positions)
  DONE     — after 16:00 ET
"""

import logging
from dataclasses import dataclass
from datetime import time as _time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ~100 NYSE/NASDAQ mega-cap seed symbols for ORB screening
_ORB_SEED: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO", "BRK.B",
    "JPM", "LLY", "V", "UNH", "XOM", "MA", "JNJ", "PG", "COST", "HD",
    "ABBV", "MRK", "CVX", "BAC", "NFLX", "KO", "WMT", "CRM", "AMD", "PEP",
    "ACN", "MCD", "TMO", "CSCO", "NOW", "ABT", "LIN", "IBM", "PM", "INTU",
    "TXN", "AMGN", "ISRG", "GE", "CAT", "ORCL", "GS", "SPGI", "BLK", "UNP",
    "HON", "BKNG", "SYK", "GILD", "AXP", "VRTX", "ADI", "PLD", "MDLZ", "C",
    "DE", "REGN", "TJX", "PANW", "ETN", "CI", "MO", "ZTS", "BSX", "CME",
    "NEE", "DUK", "SO", "EOG", "SLB", "OXY", "MPC", "PSX", "WFC", "MS",
    "SCHW", "BK", "FI", "PYPL", "ADSK", "SNPS", "CDNS", "KLAC", "LRCX", "AMAT",
    "MU", "INTC", "QCOM", "MRVL", "NXPI", "DELL", "HPQ", "STX", "KEYS", "ZBRA",
]


@dataclass
class ORBState:
    """Per-symbol opening range state for one trading session."""
    symbol: str
    or_high: Optional[float] = None       # OR high (max of 9:30–10:00 bars)
    or_low: Optional[float] = None        # OR low  (min of 9:30–10:00 bars)
    prev_day_high: Optional[float] = None
    prev_day_low: Optional[float] = None
    formed: bool = False                  # True after 10:00 AM
    breakout: Optional[str] = None        # 'up', 'down', or None
    pre_market_volume: float = 0.0
    avg_daily_volume: float = 0.0
    # Gap filter — (today_open - prev_close) / prev_close; populated at 10:00
    gap_pct: Optional[float] = None
    # Multi-timeframe confluence levels
    high_20d: Optional[float] = None      # 20-day high (excluding today)
    high_52w: Optional[float] = None      # 52-week high (excluding today)

    @property
    def or_midpoint(self) -> Optional[float]:
        if self.or_high is not None and self.or_low is not None:
            return round((self.or_high + self.or_low) / 2, 4)
        return None

    @property
    def or_range(self) -> Optional[float]:
        if self.or_high is not None and self.or_low is not None:
            return round(self.or_high - self.or_low, 4)
        return None


class ORBSession:
    """Manages a single calendar day's ORB session across all phases."""

    def __init__(self):
        self.states: Dict[str, ORBState] = {}
        self.session_date: str = ""
        self.phase: str = "IDLE"
        self._screened: bool = False
        self._range_formed: bool = False

    def reset(self, date_str: str) -> None:
        self.states = {}
        self.session_date = date_str
        self.phase = "IDLE"
        self._screened = False
        self._range_formed = False
        logger.info(f"  [ORB] New session: {date_str}")

    @property
    def screened(self) -> bool:
        return self._screened

    @property
    def range_formed(self) -> bool:
        return self._range_formed

    def set_universe(
        self,
        symbols: List[str],
        pm_vols: Dict[str, float],
        avg_vols: Dict[str, float],
    ) -> None:
        self.states = {
            s: ORBState(
                symbol=s,
                pre_market_volume=pm_vols.get(s, 0.0),
                avg_daily_volume=avg_vols.get(s, 0.0),
            )
            for s in symbols
        }
        self._screened = True
        self.phase = "FORMING"
        logger.info(f"  [ORB] Universe locked: {len(symbols)} stocks")

    def update_range(self, symbol: str, bar_high: float, bar_low: float) -> None:
        if symbol not in self.states:
            return
        s = self.states[symbol]
        s.or_high = max(s.or_high, bar_high) if s.or_high is not None else bar_high
        s.or_low  = min(s.or_low,  bar_low)  if s.or_low  is not None else bar_low

    def set_prev_day(self, symbol: str, high: float, low: float) -> None:
        if symbol in self.states:
            self.states[symbol].prev_day_high = high
            self.states[symbol].prev_day_low  = low

    def set_gap_pct(self, symbol: str, gap: float) -> None:
        if symbol in self.states:
            self.states[symbol].gap_pct = gap

    def set_historical_highs(self, symbol: str, high_20d: float, high_52w: float) -> None:
        if symbol in self.states:
            self.states[symbol].high_20d = high_20d
            self.states[symbol].high_52w = high_52w

    def finalize_range(self) -> None:
        for s in self.states.values():
            s.formed = True
        self._range_formed = True
        self.phase = "ACTIVE"
        valid = sum(1 for s in self.states.values() if s.or_high is not None)
        logger.info(f"  [ORB] Opening range finalised — {valid} stocks valid — phase=ACTIVE")

    def get(self, symbol: str) -> Optional[ORBState]:
        return self.states.get(symbol)

    def watchlist(self) -> List[str]:
        return list(self.states.keys())


# ── Alpaca bars helper ─────────────────────────────────────────────────────────

def _alpaca_bars(
    symbols: List[str],
    interval: str,
    lookback_days: int,
    api_key: str = "",
    secret_key: str = "",
):
    """Fetch Alpaca bars; returns long-format DataFrame or None."""
    import os
    from datetime import datetime, timezone, timedelta
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        key = api_key or os.getenv("ALPACA_API_KEY", "")
        sec = secret_key or os.getenv("ALPACA_SECRET_KEY", "")
        if not key or not sec:
            return None

        tf_map = {
            "1m":  TimeFrame.Minute,
            "5m":  TimeFrame(5,  TimeFrameUnit.Minute),
            "1h":  TimeFrame.Hour,
            "1d":  TimeFrame.Day,
        }
        tf = tf_map.get(interval, TimeFrame.Day)
        client = StockHistoricalDataClient(key, sec)
        end    = datetime.now(timezone.utc)
        start  = end - timedelta(days=int(lookback_days * 1.5))
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=tf,
            start=start,
            end=end,
            feed="iex",
            adjustment="split",
        )
        return client.get_stock_bars(req).df
    except Exception as e:
        logger.debug(f"[ORB] _alpaca_bars {symbols}: {e}")
        return None


def _sym_df(df_all, sym: str):
    """Extract single-symbol slice from a (possibly MultiIndex) DataFrame."""
    import pandas as pd
    if df_all is None or df_all.empty:
        return None
    if isinstance(df_all.index, pd.MultiIndex):
        try:
            sl = df_all.xs(sym, level=0).copy()
        except KeyError:
            return None
    else:
        sl = df_all.copy()
    if hasattr(sl.index, "tz") and sl.index.tz is not None:
        sl.index = sl.index.tz_localize(None)
    return sl if not sl.empty else None


# ── Data helpers ──────────────────────────────────────────────────────────────

def screen_orb_universe(
    min_avg_volume: float = 1e6,
    target_n: int = 50,
    api_key: str = "",
    secret_key: str = "",
) -> Tuple[List[str], Dict[str, float], Dict[str, float]]:
    """Filter _ORB_SEED ($200B+ mega-caps) to stocks with avg daily volume > min_avg_volume."""
    candidates = list(dict.fromkeys(_ORB_SEED))

    # Fetch 30 days of daily bars for all candidates in one call
    df_all = _alpaca_bars(candidates, "1d", 35, api_key, secret_key)

    results = []
    for sym in candidates:
        try:
            sl = _sym_df(df_all, sym)
            if sl is None or len(sl) < 3:
                continue
            avg_vol = float(sl["volume"].tail(20).mean()) if "volume" in sl.columns else 0.0
            if avg_vol < min_avg_volume:
                continue
            results.append((sym, avg_vol))
        except Exception as e:
            logger.debug(f"  [ORB] screen {sym}: {e}")

    results.sort(key=lambda x: x[1], reverse=True)
    top = results[:target_n]

    symbols  = [r[0] for r in top]
    pm_vols  = {r[0]: 0.0 for r in top}   # Alpaca IEX feed doesn't support prepost
    avg_vols = {r[0]: r[1] for r in top}

    logger.info(
        f"  [ORB] Screened {len(results)} large caps → top {len(symbols)} by avg vol"
    )
    return symbols, pm_vols, avg_vols


def fetch_opening_range_bars(
    symbols: List[str],
    api_key: str = "",
    secret_key: str = "",
) -> Dict[str, Tuple[float, float]]:
    """Return {symbol: (or_high, or_low)} from today's 9:30–9:59 ET 1-min bars."""
    df_all = _alpaca_bars(symbols, "1m", 2, api_key, secret_key)
    result: Dict[str, Tuple[float, float]] = {}
    for sym in symbols:
        try:
            sl = _sym_df(df_all, sym)
            if sl is None:
                continue
            idx = sl.index
            try:
                idx = sl.index.tz_localize("UTC").tz_convert("America/New_York")
            except Exception:
                pass
            mask = (idx.time >= _time(9, 30)) & (idx.time < _time(10, 0))
            bars = sl[mask]
            if bars.empty:
                continue
            result[sym] = (float(bars["high"].max()), float(bars["low"].min()))
        except Exception as e:
            logger.warning(f"  [ORB] or_bars {sym}: {e}")
    logger.info(f"  [ORB] fetch_opening_range_bars: {len(result)}/{len(symbols)} symbols returned OR data")
    return result


def fetch_prev_day_levels(
    symbols: List[str],
    api_key: str = "",
    secret_key: str = "",
) -> Dict[str, Tuple[float, float]]:
    """Return {symbol: (prev_day_high, prev_day_low)} from last 5 daily bars."""
    df_all = _alpaca_bars(symbols, "1d", 7, api_key, secret_key)
    result: Dict[str, Tuple[float, float]] = {}
    for sym in symbols:
        try:
            sl = _sym_df(df_all, sym)
            if sl is None or len(sl) < 2:
                continue
            row = sl.iloc[-2]
            result[sym] = (float(row["high"]), float(row["low"]))
        except Exception as e:
            logger.debug(f"  [ORB] prev_day {sym}: {e}")
    return result


def fetch_latest_1min_volume(
    symbols: List[str],
    api_key: str = "",
    secret_key: str = "",
) -> Dict[str, float]:
    """Return the volume of the most recent 1-min bar for each symbol."""
    df_all = _alpaca_bars(symbols, "1m", 2, api_key, secret_key)
    result: Dict[str, float] = {}
    for sym in symbols:
        try:
            sl = _sym_df(df_all, sym)
            result[sym] = float(sl["volume"].iloc[-1]) if sl is not None and not sl.empty else 0.0
        except Exception:
            result[sym] = 0.0
    return result


def fetch_gap_pcts(
    symbols: List[str],
    api_key: str = "",
    secret_key: str = "",
) -> Dict[str, float]:
    """Return {symbol: gap_pct} where gap_pct = (today_open - prev_close) / prev_close."""
    df_all = _alpaca_bars(symbols, "1d", 3, api_key, secret_key)
    result: Dict[str, float] = {}
    for sym in symbols:
        try:
            sl = _sym_df(df_all, sym)
            if sl is None or len(sl) < 2:
                continue
            prev_close = float(sl["close"].iloc[-2])
            today_open = float(sl["open"].iloc[-1])
            if prev_close > 0:
                result[sym] = (today_open - prev_close) / prev_close
        except Exception as e:
            logger.debug(f"  [ORB] gap {sym}: {e}")
    return result


def fetch_historical_highs(
    symbols: List[str],
    api_key: str = "",
    secret_key: str = "",
) -> Dict[str, Tuple[float, float]]:
    """Return {symbol: (high_20d, high_52w)} — highs of last 20 and 260 trading days, excluding today."""
    df_all = _alpaca_bars(symbols, "1d", 280, api_key, secret_key)
    result: Dict[str, Tuple[float, float]] = {}
    for sym in symbols:
        try:
            sl = _sym_df(df_all, sym)
            if sl is None or len(sl) < 3:
                continue
            # Exclude the last row (today's partial bar)
            hist = sl.iloc[:-1]
            high_20d = float(hist["high"].tail(20).max()) if len(hist) >= 20 else float(hist["high"].max())
            high_52w = float(hist["high"].tail(260).max()) if len(hist) >= 260 else float(hist["high"].max())
            result[sym] = (high_20d, high_52w)
        except Exception as e:
            logger.debug(f"  [ORB] hist_highs {sym}: {e}")
    return result
