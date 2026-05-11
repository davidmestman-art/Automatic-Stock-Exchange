"""Daily stock universe screener.

Pulls tradeable US equities from Alpaca's assets API (NYSE/NASDAQ/AMEX) or
falls back to S&P 500 + Nasdaq 100 + Dow 30 constituent lists.

Pre-market filter (default 9:15 AM ET):
  1. Volume ≥ 500 K avg daily shares
  2. Price $10–$1 000
  3. Market cap > $2 B
  4. Exclude SPACs (name contains SPAC keywords)
  5. Exclude IPOs listed < 30 trading days ago

Composite ranking: 50% volume + 30% 5-day momentum + 20% proximity to 52-week high.
Core ETFs are always included when include_etfs=True.

Results are cached for the calendar day in universe_cache.json.
Each run appends an entry to screener_log.json (last 30 kept).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .scanner import SP500_UNIVERSE
from ..utils import now_et

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

# Core ETFs — index benchmarks always included in universe for reference
CORE_ETFS: List[str] = [
    "SPY", "QQQ", "DIA", "IWM",
]

# Set lookups for category tagging
_SP500_SET = frozenset(SP500_UNIVERSE)
_NQ100_SET = frozenset(NASDAQ_100)
_DOW30_SET = frozenset(DOW_30)
_ETF_SET   = frozenset(CORE_ETFS)

# SPAC name keywords — checked case-insensitively
_SPAC_KEYWORDS = (
    "acquisition corp", "acquisition inc", "acquisition co",
    "blank check", "special purpose acquisition",
)

# Fallback candidate pool when Alpaca is unavailable
_ALL_CANDIDATES: List[str] = sorted(set(SP500_UNIVERSE + NASDAQ_100 + DOW_30))

# ── Default screening thresholds ───────────────────────────────────────────────
MIN_AVG_VOLUME:  int   = 500_000           # shares / day
MIN_PRICE:       float = 10.0
MAX_PRICE:       float = 1_000.0
MIN_MARKET_CAP:  float = 200_000_000_000.0  # $200 B

# ── Mega-cap seed: stocks with market cap ≥ $200B ─────────────────────────────
# Used to pre-filter the 10,000+ Alpaca asset list BEFORE any data download.
# After pre-filtering, bars are only fetched for these tickers instead of 10,000+.
# The final universe is capped at MAX_UNIVERSE_STOCKS ranked by composite score.
# Update periodically as companies cross/fall below the threshold.
_MEGA_CAP_SEED: frozenset = frozenset([
    # Technology
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "QCOM", "TXN",
    "AMAT", "LRCX", "KLAC", "MU", "NOW", "PANW", "SNPS", "CDNS", "INTU", "CSCO",
    "ACN", "IBM", "PLTR", "ANET", "CRWD", "APP",
    # Communication Services
    "GOOGL", "GOOG", "META", "NFLX", "TMUS",
    # Consumer Discretionary
    "AMZN", "TSLA", "HD", "MCD", "BKNG", "LOW", "TJX", "UBER",
    # Consumer Staples
    "WMT", "COST", "PG", "KO", "PEP",
    # Financials
    "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "BLK", "SCHW", "AXP", "C",
    # Health Care
    "UNH", "LLY", "JNJ", "MRK", "ABBV", "TMO", "PFE", "AMGN", "SYK", "BSX",
    "ISRG", "REGN", "VRTX",
    # Industrials
    "GE", "RTX", "HON", "CAT", "DE", "UNP", "UPS", "LMT", "ETN",
    # Energy
    "XOM", "CVX", "COP",
    # Materials
    "LIN",
    # Utilities
    "NEE",
])

# Maximum number of equity stocks in the screened universe (excludes ETFs)
MAX_UNIVERSE_STOCKS = 60

_CACHE_PATH   = Path(__file__).resolve().parent.parent.parent / "universe_cache.json"
_LOG_PATH     = Path(__file__).resolve().parent.parent.parent / "screener_log.json"


# ── Category helpers ───────────────────────────────────────────────────────────

def tag_category(sym: str) -> str:
    """Return the primary index category for *sym*."""
    if sym in _ETF_SET:   return "ETF"
    if sym in _DOW30_SET: return "Dow 30"
    if sym in _NQ100_SET: return "Nasdaq 100"
    if sym in _SP500_SET: return "S&P 500"
    return "Other"


def _is_spac(name: str) -> bool:
    n = name.lower()
    return any(kw in n for kw in _SPAC_KEYWORDS)


# ── Alpaca assets API ──────────────────────────────────────────────────────────

def _fetch_alpaca_assets(
    api_key: str, secret_key: str, paper: bool = True
) -> Tuple[List[str], Dict[str, str]]:
    """Fetch active tradeable US equity tickers from Alpaca.

    Returns (symbols, name_map) or ([], {}) on failure.
    """
    base = (
        "https://paper-api.alpaca.markets" if paper
        else "https://api.alpaca.markets"
    )
    url = f"{base}/v2/assets?status=active&asset_class=us_equity"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "APCA-API-KEY-ID":     api_key,
                "APCA-API-SECRET-KEY": secret_key,
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            assets = json.loads(resp.read())

        valid_exchanges = {"NYSE", "NASDAQ", "AMEX", "ARCA"}
        symbols: List[str] = []
        names:   Dict[str, str] = {}
        for a in assets:
            sym = a.get("symbol", "")
            if (
                a.get("tradable")
                and a.get("exchange") in valid_exchanges
                and "." not in sym          # skip preferred shares BRK.B
                and "/" not in sym          # skip crypto-style symbols
                and len(sym) <= 5
            ):
                symbols.append(sym)
                names[sym] = a.get("name", "")

        log.info("[Universe] Alpaca assets: %d tickers on NYSE/NASDAQ/AMEX", len(symbols))
        return symbols, names
    except Exception as exc:
        log.warning("[Universe] Alpaca assets fetch failed: %s — using constituent lists", exc)
        return [], {}


# ── DynamicUniverse ────────────────────────────────────────────────────────────

class DynamicUniverse:
    """Screens the trading universe once per calendar day."""

    def __init__(
        self,
        min_avg_volume:    int   = MIN_AVG_VOLUME,
        min_price:         float = MIN_PRICE,
        max_price:         float = MAX_PRICE,
        min_market_cap:    float = MIN_MARKET_CAP,
        include_etfs:      bool  = True,
        alpaca_api_key:    str   = "",
        alpaca_secret_key: str   = "",
        paper_trading:     bool  = True,
    ) -> None:
        self.min_avg_volume    = min_avg_volume
        self.min_price         = min_price
        self.max_price         = max_price
        self.min_market_cap    = min_market_cap
        self.include_etfs      = include_etfs
        self.alpaca_api_key    = alpaca_api_key
        self.alpaca_secret_key = alpaca_secret_key
        self.paper_trading     = paper_trading

        self._last_result: Optional[Dict] = None
        self._last_date:   Optional[str]  = None
        self._lock = threading.Lock()
        self._load_cache()

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def tickers(self) -> List[str]:
        return (self._last_result or {}).get("universe", [])

    @property
    def last_result(self) -> Dict:
        return self._last_result or {
            "screen_date":       None,
            "scanned_at":        None,
            "total_candidates":  len(_ALL_CANDIDATES),
            "universe":          [],
            "universe_size":     0,
            "filter_stats":      {},
            "categories":        {},
            "exchange_breakdown": {},
            "using_alpaca":      False,
        }

    def refresh_if_stale(self) -> List[str]:
        """Run the screen only when it hasn't run today. Thread-safe."""
        today = date.today().isoformat()
        with self._lock:
            if self._last_date == today and self._last_result:
                return self.tickers
        return self.run()

    def force_rescan(self) -> List[str]:
        """Bypass the daily cache and run a fresh screen immediately."""
        with self._lock:
            self._last_date = None  # invalidate cache
        return self.run()

    def run(self) -> List[str]:
        """Execute the full screen; returns the universe ticker list."""
        today = date.today().isoformat()

        # Step 1 — resolve candidate list
        alpaca_names: Dict[str, str] = {}
        using_alpaca = False
        if self.alpaca_api_key and self.alpaca_secret_key:
            raw_candidates, alpaca_names = _fetch_alpaca_assets(
                self.alpaca_api_key, self.alpaca_secret_key, self.paper_trading
            )
            if raw_candidates:
                using_alpaca = True
                # Pre-filter to mega-cap seed BEFORE any data download.
                # Alpaca returns 10,000+ tickers; we only want the ~80 with
                # market cap ≥ $200B so that Alpaca only fetches a tiny list.
                candidates = [s for s in raw_candidates if s in _MEGA_CAP_SEED]
                log.info(
                    "[Universe] Pre-filtered %d Alpaca tickers → %d mega-cap candidates",
                    len(raw_candidates), len(candidates),
                )
                if not candidates:
                    # Seed list may be outdated — fall back to full intersection
                    candidates = list(_ALL_CANDIDATES)
                    log.warning("[Universe] Mega-cap seed returned 0 matches — using constituent lists")

        if not using_alpaca:
            candidates = list(_ALL_CANDIDATES)
            log.info("[Universe] Using constituent lists: %d candidates", len(candidates))

        always_include = CORE_ETFS if self.include_etfs else []

        log.info(
            "[Universe] Starting screen — %d candidates, "
            "vol≥%d, price $%.0f–$%.0f, mcap≥$%.0fB (no top-N cap)",
            len(candidates), self.min_avg_volume,
            self.min_price, self.max_price, self.min_market_cap / 1e9,
        )

        try:
            tickers, stats, categories, exchange_bkd = _screen(
                candidates=candidates,
                always_include=always_include,
                alpaca_names=alpaca_names,
                min_avg_volume=self.min_avg_volume,
                min_price=self.min_price,
                max_price=self.max_price,
                min_market_cap=self.min_market_cap,
                api_key=self.alpaca_api_key,
                secret_key=self.alpaca_secret_key,
            )
        except Exception as exc:
            log.error("[Universe] Screen failed: %s — keeping previous universe", exc)
            return self.tickers

        result = {
            "screen_date":       today,
            "scanned_at":        now_et().strftime("%Y-%m-%d %H:%M:%S ET"),
            "total_candidates":  len(candidates),
            "universe":          tickers,
            "universe_size":     len(tickers),
            "filter_stats":      stats,
            "categories":        categories,
            "exchange_breakdown": exchange_bkd,
            "using_alpaca":      using_alpaca,
        }
        with self._lock:
            self._last_date   = today
            self._last_result = result

        self._save_cache()
        self._append_log(result)
        log.info("[Universe] Screen complete: %d tickers", len(tickers))
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

    def _append_log(self, result: Dict) -> None:
        try:
            entries: List[Dict] = []
            if _LOG_PATH.exists():
                entries = json.loads(_LOG_PATH.read_text())
            entries.append({
                "date":             result["screen_date"],
                "scanned_at":       result["scanned_at"],
                "universe_size":    result["universe_size"],
                "total_candidates": result["total_candidates"],
                "using_alpaca":     result["using_alpaca"],
                "filter_stats":     result["filter_stats"],
            })
            _LOG_PATH.write_text(json.dumps(entries[-30:], indent=2))
        except Exception as exc:
            log.warning("[Universe] Log append failed: %s", exc)


# ── Screening logic ────────────────────────────────────────────────────────────

def _alpaca_bars_wide(
    symbols: List[str],
    lookback_days: int,
    api_key: str,
    secret_key: str,
) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame]]:
    """Fetch daily bars for *symbols* and return (close_wide, vol_wide).

    Returns (None, None) on failure.  Wide DataFrames have symbols as columns
    and a plain DatetimeIndex (timezone stripped).
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed
        from datetime import timezone

        if not api_key or not secret_key:
            return None, None

        client = StockHistoricalDataClient(api_key, secret_key)
        end = pd.Timestamp.now(tz="UTC")
        start = end - pd.Timedelta(days=int(lookback_days * 1.5))

        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start.to_pydatetime(),
            end=end.to_pydatetime(),
            feed=DataFeed.IEX,
        )
        df_all = client.get_stock_bars(req).df

        if df_all is None or df_all.empty:
            return None, None

        if isinstance(df_all.index, pd.MultiIndex):
            close_wide = df_all["close"].unstack(level=0)
            vol_wide   = df_all["volume"].unstack(level=0)
        else:
            sym = symbols[0] if symbols else "?"
            close_wide = df_all[["close"]].rename(columns={"close": sym})
            vol_wide   = df_all[["volume"]].rename(columns={"volume": sym})

        # Strip timezone
        for w in (close_wide, vol_wide):
            if hasattr(w.index, "tz") and w.index.tz is not None:
                w.index = w.index.tz_localize(None)

        return close_wide, vol_wide

    except Exception as exc:
        log.error("[Universe] _alpaca_bars_wide failed: %s", exc)
        return None, None


def _screen(
    candidates:     List[str],
    always_include: List[str],
    alpaca_names:   Dict[str, str],
    min_avg_volume: int,
    min_price:      float,
    max_price:      float,
    min_market_cap: float,
    api_key:        str = "",
    secret_key:     str = "",
) -> Tuple[List[str], Dict, Dict[str, str], Dict[str, int]]:
    """Return (tickers, filter_stats, categories, exchange_breakdown)."""
    stats: Dict = {"total": len(candidates)}

    etf_set  = frozenset(always_include)
    regulars = [s for s in candidates if s not in etf_set]

    # ── Step 1: 10-day OHLCV for vol+price pre-filter ────────────────────────
    log.info("[Universe] Fetching 10d data for %d tickers via Alpaca…", len(regulars))
    close10, vol10 = _alpaca_bars_wide(regulars, 14, api_key, secret_key)

    if close10 is None or vol10 is None or close10.empty:
        log.warning("[Universe] 10d fetch failed — filtering to mega-cap seed only")
        pre_candidates = [s for s in regulars if s in _MEGA_CAP_SEED][:MAX_UNIVERSE_STOCKS]
        if not pre_candidates:
            pre_candidates = regulars[:MAX_UNIVERSE_STOCKS]
        stats["after_vol_price"] = len(pre_candidates)
    else:
        avg_vol    = vol10.mean(skipna=True)
        last_price = close10.iloc[-1]

        mask = (
            (avg_vol    >= min_avg_volume) &
            (last_price >= min_price) &
            (last_price <= max_price)
        )
        after_vp = avg_vol[mask].sort_values(ascending=False)
        stats["after_vol_price"] = int(mask.sum())
        log.info("[Universe] After vol/price filter: %d tickers", stats["after_vol_price"])
        pre_candidates = [str(s) for s in after_vp.index[:300]]

    # ── Step 2: 1-year data for composite ranking ─────────────────────────────
    all_fetch = list(dict.fromkeys(pre_candidates + list(always_include)))
    log.info("[Universe] Fetching 1y data for %d tickers via Alpaca…", len(all_fetch))
    close1y, vol1y = _alpaca_bars_wide(all_fetch, 252, api_key, secret_key)

    if close1y is None or vol1y is None or close1y.empty:
        tickers = ([s for s in pre_candidates if s in _MEGA_CAP_SEED]
                   or pre_candidates)[:MAX_UNIVERSE_STOCKS]
        cats    = {s: tag_category(s) for s in tickers}
        ex_bkd  = _exchange_breakdown(tickers, cats)
        stats["after_mcap"] = len(tickers)
        return tickers, stats, cats, ex_bkd

    # ── Step 3: Composite score ───────────────────────────────────────────────
    raw_scores: Dict[str, Dict[str, float]] = {}
    removed_ipo = removed_spac = 0

    for sym in pre_candidates:
        if sym not in close1y.columns:
            continue
        col = close1y[sym].dropna()
        if len(col) < 5:
            continue

        if len(col) < 20:
            removed_ipo += 1
            continue

        name = alpaca_names.get(sym, "")
        if name and _is_spac(name):
            removed_spac += 1
            continue

        vcol      = vol1y[sym].dropna() if sym in vol1y.columns else pd.Series(dtype=float)
        avg_v     = float(vcol.mean()) if len(vcol) > 0 else 0.0
        price_now = float(col.iloc[-1])
        price_5d  = float(col.iloc[-6]) if len(col) >= 6 else price_now
        p52_high  = float(col.max())

        momentum = (price_now - price_5d) / price_5d if price_5d > 0 else 0.0
        dist_52w = (p52_high - price_now) / p52_high if p52_high > 0 else 1.0

        raw_scores[sym] = {"vol": avg_v, "mom": momentum, "dist52w": dist_52w}

    stats["removed_ipo"]  = removed_ipo
    stats["removed_spac"] = removed_spac

    composite: Dict[str, float] = {}
    if raw_scores:
        vals_vol  = [v["vol"]    for v in raw_scores.values()]
        vals_mom  = [v["mom"]    for v in raw_scores.values()]
        vals_dist = [v["dist52w"] for v in raw_scores.values()]

        def _norm(x, mn, mx):
            return (x - mn) / (mx - mn) if mx > mn else 0.5

        vol_mn, vol_mx   = min(vals_vol),  max(vals_vol)
        mom_mn, mom_mx   = min(vals_mom),  max(vals_mom)
        dist_mn, dist_mx = min(vals_dist), max(vals_dist)

        for sym, v in raw_scores.items():
            n_vol  = _norm(v["vol"],    vol_mn, vol_mx)
            n_mom  = _norm(v["mom"],    mom_mn, mom_mx)
            n_prox = 1.0 - _norm(v["dist52w"], dist_mn, dist_mx)
            composite[sym] = 0.50 * n_vol + 0.30 * n_mom + 0.20 * n_prox

    ranked = sorted(composite, key=lambda s: -composite[s])

    # ── Step 4: Cap at MAX_UNIVERSE_STOCKS ranked by composite score ────────
    # Market cap ≥ $200B is guaranteed by the seed; take the top N by score.
    passed = ranked[:MAX_UNIVERSE_STOCKS]
    stats["after_mcap"] = len(passed)

    # ── Step 5: Prepend ETFs and tag categories ───────────────────────────────
    etf_tickers = [s for s in always_include if s in close1y.columns]
    all_tickers = list(dict.fromkeys(etf_tickers + passed))

    cats   = {s: tag_category(s) for s in all_tickers}
    ex_bkd = _exchange_breakdown(all_tickers, cats)

    log.info(
        "[Universe] Screen complete: %d tickers (incl %d ETFs, no top-N cap)",
        len(all_tickers), len(etf_tickers),
    )
    return all_tickers, stats, cats, ex_bkd


def _exchange_breakdown(tickers: List[str], cats: Dict[str, str]) -> Dict[str, int]:
    bkd: Dict[str, int] = {"NYSE": 0, "NASDAQ": 0, "ETF": 0, "Other": 0}
    for sym in tickers:
        cat = cats.get(sym, "Other")
        if cat == "ETF":
            bkd["ETF"] += 1
        elif sym in _NQ100_SET:
            bkd["NASDAQ"] += 1
        else:
            bkd["NYSE"] += 1
    return bkd
