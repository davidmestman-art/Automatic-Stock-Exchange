"""Daily stock universe screener.

Combines S&P 500, Nasdaq 100, and Dow 30 constituents, then filters to the
top 50 stocks by average daily volume that also pass price ($20–$500) and
market-cap (>$10 B) thresholds.  Results are cached for the calendar day and
persisted to universe_cache.json so they survive process restarts.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

from .scanner import SP500_UNIVERSE

log = logging.getLogger(__name__)

# ── Index constituent lists ────────────────────────────────────────────────────

NASDAQ_100: List[str] = [
    "MSFT", "AAPL", "NVDA", "AMZN", "META", "TSLA", "GOOGL", "GOOG", "AVGO",
    "COST", "NFLX", "AMD", "ADBE", "QCOM", "PEP", "TMUS", "INTC", "INTU",
    "AMAT", "CSCO", "CMCSA", "TXN", "HON", "AMGN", "BKNG", "VRTX", "ISRG",
    "REGN", "PANW", "SBUX", "MU", "GILD", "LRCX", "ADP", "SNPS", "CDNS",
    "MELI", "KLAC", "ORLY", "MAR", "PYPL", "WDAY", "CSX", "CRWD",
    "ABNB", "FTNT", "DXCM", "MCHP", "ADSK", "KDP", "CHTR", "MNST", "PAYX",
    "CTAS", "ODFL", "MRNA", "PCAR", "FAST", "VRSK", "BIIB",
    "ZS", "DDOG", "CPRT", "DLTR", "IDXX", "XEL", "ANSS", "NXPI",
    "ALGN", "EA", "EBAY", "GFS", "CEG", "FANG", "TTWO",
]

DOW_30: List[str] = [
    "AAPL", "AMGN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "DOW",
    "GS", "HD", "HON", "IBM", "INTC", "JNJ", "JPM", "KO", "MCD", "MMM",
    "MRK", "MSFT", "NKE", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT",
]

# Combined unique pool used for every screen
_ALL_CANDIDATES: List[str] = sorted(set(SP500_UNIVERSE + NASDAQ_100 + DOW_30))

# ── Screening thresholds ───────────────────────────────────────────────────────
MIN_AVG_VOLUME:  int   = 1_000_000        # 1 M shares / day
MIN_PRICE:       float = 20.0
MAX_PRICE:       float = 500.0
MIN_MARKET_CAP:  float = 10_000_000_000.0  # $10 B
TOP_N:           int   = 50

_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "universe_cache.json"


class DynamicUniverse:
    """Screens the combined index universe once per calendar day."""

    def __init__(self) -> None:
        self._last_result: Optional[Dict] = None
        self._last_date:   Optional[str]  = None
        self._lock = threading.Lock()
        self._load_cache()

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def tickers(self) -> List[str]:
        if self._last_result:
            return self._last_result.get("universe", [])
        return []

    @property
    def last_result(self) -> Dict:
        return self._last_result or {
            "screen_date":      None,
            "total_candidates": len(_ALL_CANDIDATES),
            "universe":         [],
            "universe_size":    0,
            "filter_stats":     {},
        }

    def refresh_if_stale(self) -> List[str]:
        """Run the screen only when it hasn't run today. Thread-safe."""
        today = date.today().isoformat()
        with self._lock:
            if self._last_date == today and self._last_result:
                return self.tickers
        return self.run()

    def run(self) -> List[str]:
        """Execute the full screen; returns the top-N ticker list."""
        today = date.today().isoformat()
        log.info(
            "[Universe] Starting daily screen — %d candidates (S&P 500 + Nasdaq 100 + Dow 30)",
            len(_ALL_CANDIDATES),
        )
        try:
            tickers, stats = _screen(_ALL_CANDIDATES)
        except Exception as exc:
            log.error("[Universe] Screen failed: %s — keeping previous universe", exc)
            return self.tickers

        with self._lock:
            self._last_date   = today
            self._last_result = {
                "screen_date":      today,
                "total_candidates": len(_ALL_CANDIDATES),
                "universe":         tickers,
                "universe_size":    len(tickers),
                "filter_stats":     stats,
            }
        self._save_cache()
        log.info(
            "[Universe] Screen complete: %d/%d tickers passed",
            len(tickers), len(_ALL_CANDIDATES),
        )
        return tickers

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load_cache(self) -> None:
        try:
            if _CACHE_PATH.exists():
                data = json.loads(_CACHE_PATH.read_text())
                self._last_date   = data.get("screen_date")
                self._last_result = data
                log.info(
                    "[Universe] Loaded cache from %s (%d tickers)",
                    self._last_date, len(data.get("universe", [])),
                )
        except Exception as exc:
            log.warning("[Universe] Cache load failed: %s", exc)

    def _save_cache(self) -> None:
        try:
            _CACHE_PATH.write_text(json.dumps(self._last_result, indent=2))
        except Exception as exc:
            log.warning("[Universe] Cache save failed: %s", exc)


# ── Screening logic (module-level, no state) ───────────────────────────────────

def _screen(candidates: List[str]) -> Tuple[List[str], Dict]:
    """Return (top_tickers, filter_stats) for the given candidate list."""
    stats: Dict[str, int] = {}

    # Step 1 — batch-fetch 10 trading days of price + volume
    log.info("[Universe] Fetching 10d OHLCV for %d tickers…", len(candidates))
    raw = yf.download(
        tickers=candidates,
        period="10d",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    if raw.empty:
        raise RuntimeError("yf.download returned empty DataFrame")

    if isinstance(raw.columns, pd.MultiIndex):
        close  = raw["Close"]
        volume = raw["Volume"]
    else:
        sym    = candidates[0]
        close  = pd.DataFrame({sym: raw["Close"]})
        volume = pd.DataFrame({sym: raw["Volume"]})

    avg_vol    = volume.mean(skipna=True)
    last_price = close.iloc[-1]
    stats["total"] = len(candidates)

    # Step 2 — volume + price filter
    mask = (
        (avg_vol    >= MIN_AVG_VOLUME) &
        (last_price >= MIN_PRICE)      &
        (last_price <= MAX_PRICE)
    )
    after_vol_price = avg_vol[mask].sort_values(ascending=False)
    stats["after_vol_price"] = int(mask.sum())
    log.info("[Universe] After vol/price filter: %d tickers", stats["after_vol_price"])

    # Step 3 — market-cap filter on the top 150 candidates (parallel)
    check_list = [str(s) for s in after_vol_price.index[:150]]
    mc_map     = _fetch_market_caps(check_list)

    passed: List[str] = []
    for sym in check_list:
        mc = mc_map.get(sym)
        # If market cap is unavailable, include the ticker (it's a major-index member)
        if mc is None or mc >= MIN_MARKET_CAP:
            passed.append(sym)
        if len(passed) >= TOP_N:
            break

    stats["after_mcap"] = len(passed)
    log.info("[Universe] After market-cap filter: %d tickers (target %d)", len(passed), TOP_N)
    return passed, stats


def _fetch_market_caps(symbols: List[str]) -> Dict[str, Optional[float]]:
    """Parallel fast_info.market_cap lookup — up to 20 concurrent workers."""
    results: Dict[str, Optional[float]] = {}

    def _get(sym: str) -> Tuple[str, Optional[float]]:
        try:
            mc = yf.Ticker(sym).fast_info.market_cap
            return sym, float(mc) if mc else None
        except Exception:
            return sym, None

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_get, sym): sym for sym in symbols}
        for fut in as_completed(futures, timeout=90):
            try:
                sym, mc = fut.result()
                results[sym] = mc
            except Exception:
                pass

    return results
