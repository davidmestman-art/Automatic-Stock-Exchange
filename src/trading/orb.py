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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    # Retest entry — set on first high-vol breakout above OR high
    retest_eligible: bool = False
    breakout_volume: Optional[float] = None

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


# ── Data helpers ──────────────────────────────────────────────────────────────

def screen_orb_universe(
    min_market_cap: float = 100e9,
    min_avg_volume: float = 1e6,
    target_n: int = 50,
) -> Tuple[List[str], Dict[str, float], Dict[str, float]]:
    """
    Filter _ORB_SEED to large caps with volume > min_avg_volume.
    Rank by today's pre-market volume. Return (symbols, pm_vols, avg_vols).
    """
    import yfinance as yf

    candidates = list(dict.fromkeys(_ORB_SEED))

    def _screen_one(sym: str):
        try:
            t = yf.Ticker(sym)
            mc = t.fast_info.market_cap
            if not mc or mc < min_market_cap:
                return sym, 0.0, 0.0, 0.0

            hist_d = t.history(period="30d", interval="1d", auto_adjust=True)
            if hist_d is None or len(hist_d) < 3:
                return sym, mc, 0.0, 0.0
            avg_vol = float(hist_d["Volume"].tail(20).mean())
            if avg_vol < min_avg_volume:
                return sym, 0.0, 0.0, 0.0

            # Pre-market 1-min bars (4:00–9:29 ET)
            hist_1m = yf.download(
                sym, period="1d", interval="1m", prepost=True,
                progress=False, auto_adjust=True,
            )
            pm_vol = 0.0
            if hist_1m is not None and not hist_1m.empty:
                try:
                    idx = hist_1m.index.tz_convert("America/New_York")
                except Exception:
                    idx = hist_1m.index
                mask = (idx.time >= _time(4, 0)) & (idx.time < _time(9, 30))
                if mask.any():
                    vcol = hist_1m["Volume"]
                    if hasattr(vcol, "squeeze"):
                        vcol = vcol.squeeze()
                    pm_vol = float(vcol.iloc[mask.values].sum())

            return sym, mc, pm_vol, avg_vol
        except Exception as e:
            logger.debug(f"  [ORB] screen {sym}: {e}")
            return sym, 0.0, 0.0, 0.0

    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_screen_one, s): s for s in candidates}
        for f in as_completed(futures):
            sym, mc, pm_vol, avg_vol = f.result()
            if mc >= min_market_cap and avg_vol >= min_avg_volume:
                results.append((sym, mc, pm_vol, avg_vol))

    results.sort(key=lambda x: x[2], reverse=True)
    top = results[:target_n]

    symbols  = [r[0] for r in top]
    pm_vols  = {r[0]: r[2] for r in top}
    avg_vols = {r[0]: r[3] for r in top}

    logger.info(
        f"  [ORB] Screened {len(results)} large caps → top {len(symbols)} by pre-market vol"
    )
    return symbols, pm_vols, avg_vols


def fetch_opening_range_bars(symbols: List[str]) -> Dict[str, Tuple[float, float]]:
    """
    Return {symbol: (or_high, or_low)} from today's 9:30–9:59 ET 1-min bars.
    """
    import yfinance as yf

    def _fetch_one(sym: str):
        try:
            hist = yf.download(sym, period="1d", interval="1m",
                               progress=False, auto_adjust=True)
            if hist is None or hist.empty:
                return sym, None
            try:
                hist.index = hist.index.tz_convert("America/New_York")
            except Exception:
                pass
            mask = (hist.index.time >= _time(9, 30)) & (hist.index.time < _time(10, 0))
            bars = hist[mask]
            if bars.empty:
                return sym, None
            hcol = bars["High"]
            lcol = bars["Low"]
            if hasattr(hcol, "squeeze"):
                hcol = hcol.squeeze()
                lcol = lcol.squeeze()
            return sym, (float(hcol.max()), float(lcol.min()))
        except Exception as e:
            logger.debug(f"  [ORB] or_bars {sym}: {e}")
            return sym, None

    result: Dict[str, Tuple[float, float]] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, s): s for s in symbols}
        for f in as_completed(futures):
            sym, hl = f.result()
            if hl is not None:
                result[sym] = hl
    return result


def fetch_prev_day_levels(symbols: List[str]) -> Dict[str, Tuple[float, float]]:
    """Return {symbol: (prev_day_high, prev_day_low)} from last 5 daily bars."""
    import yfinance as yf

    def _fetch_one(sym: str):
        try:
            hist = yf.download(sym, period="5d", interval="1d",
                               progress=False, auto_adjust=True)
            if hist is None or len(hist) < 2:
                return sym, None
            row = hist.iloc[-2]
            h = row["High"]
            l = row["Low"]
            # Handle MultiIndex columns from batch downloads
            if hasattr(h, "item"):
                h = h.item()
            if hasattr(l, "item"):
                l = l.item()
            return sym, (float(h), float(l))
        except Exception as e:
            logger.debug(f"  [ORB] prev_day {sym}: {e}")
            return sym, None

    result: Dict[str, Tuple[float, float]] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, s): s for s in symbols}
        for f in as_completed(futures):
            sym, hl = f.result()
            if hl is not None:
                result[sym] = hl
    return result


def fetch_latest_1min_volume(symbols: List[str]) -> Dict[str, float]:
    """Return the volume of the most recent 1-min bar for each symbol (for breakout volume check)."""
    import yfinance as yf

    def _fetch_one(sym: str):
        try:
            hist = yf.download(sym, period="1d", interval="1m",
                               progress=False, auto_adjust=True)
            if hist is None or hist.empty:
                return sym, 0.0
            vcol = hist["Volume"].iloc[-1]
            if hasattr(vcol, "item"):
                vcol = vcol.item()
            return sym, float(vcol)
        except Exception:
            return sym, 0.0

    result: Dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, s): s for s in symbols}
        for f in as_completed(futures):
            sym, vol = f.result()
            result[sym] = vol
    return result


def fetch_gap_pcts(symbols: List[str]) -> Dict[str, float]:
    """Return {symbol: gap_pct} where gap_pct = (today_open - prev_close) / prev_close."""
    import yfinance as yf

    def _fetch_one(sym: str):
        try:
            hist = yf.download(sym, period="2d", interval="1d",
                               progress=False, auto_adjust=True)
            if hist is None or len(hist) < 2:
                return sym, None
            prev_close = float(hist["Close"].iloc[-2])
            today_open = float(hist["Open"].iloc[-1])
            if prev_close <= 0:
                return sym, None
            if hasattr(prev_close, "item"):
                prev_close = prev_close.item()
            if hasattr(today_open, "item"):
                today_open = today_open.item()
            return sym, (today_open - prev_close) / prev_close
        except Exception as e:
            logger.debug(f"  [ORB] gap {sym}: {e}")
            return sym, None

    result: Dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, s): s for s in symbols}
        for f in as_completed(futures):
            sym, gap = f.result()
            if gap is not None:
                result[sym] = gap
    return result
