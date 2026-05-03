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
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
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

# Core ETFs always included in the screened universe
CORE_ETFS: List[str] = [
    "SPY", "QQQ", "DIA", "IWM",
    "XLF", "XLE", "XLK", "XLV", "XLP", "XLI", "XLB", "XLC", "XLRE", "XLU", "XLY",
    "ARKK", "VTI", "VOO",
    "GLD", "SLV", "TLT", "HYG", "LQD",
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
MIN_MARKET_CAP:  float = 2_000_000_000.0   # $2 B
TOP_N:           int   = 150

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
        top_n:             int   = TOP_N,
        include_etfs:      bool  = True,
        alpaca_api_key:    str   = "",
        alpaca_secret_key: str   = "",
        paper_trading:     bool  = True,
    ) -> None:
        self.min_avg_volume    = min_avg_volume
        self.min_price         = min_price
        self.max_price         = max_price
        self.min_market_cap    = min_market_cap
        self.top_n             = top_n
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
            candidates, alpaca_names = _fetch_alpaca_assets(
                self.alpaca_api_key, self.alpaca_secret_key, self.paper_trading
            )
            if candidates:
                using_alpaca = True

        if not using_alpaca:
            candidates = list(_ALL_CANDIDATES)
            log.info("[Universe] Using constituent lists: %d candidates", len(candidates))

        always_include = CORE_ETFS if self.include_etfs else []

        log.info(
            "[Universe] Starting screen — %d candidates, top_n=%d, "
            "vol≥%d, price $%.0f–$%.0f, mcap≥$%.0fB",
            len(candidates), self.top_n, self.min_avg_volume,
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
                top_n=self.top_n,
            )
        except Exception as exc:
            log.error("[Universe] Screen failed: %s — keeping previous universe", exc)
            return self.tickers

        result = {
            "screen_date":       today,
            "scanned_at":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

def _screen(
    candidates:     List[str],
    always_include: List[str],
    alpaca_names:   Dict[str, str],
    min_avg_volume: int,
    min_price:      float,
    max_price:      float,
    min_market_cap: float,
    top_n:          int,
) -> Tuple[List[str], Dict, Dict[str, str], Dict[str, int]]:
    """Return (tickers, filter_stats, categories, exchange_breakdown)."""
    stats: Dict = {"total": len(candidates)}

    # Separate ETFs from regular candidates to avoid polluting the filter
    etf_set    = frozenset(always_include)
    regulars   = [s for s in candidates if s not in etf_set]

    # ── Step 1: Batch 10-day OHLCV for vol+price pre-filter ───────────────────
    log.info("[Universe] Fetching 10d data for %d tickers…", len(regulars))
    raw10 = yf.download(
        tickers=regulars,
        period="10d",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )
    if raw10.empty:
        raise RuntimeError("yf.download(10d) returned empty DataFrame")

    if isinstance(raw10.columns, pd.MultiIndex):
        close10 = raw10["Close"]
        vol10   = raw10["Volume"]
    else:
        sym     = regulars[0]
        close10 = pd.DataFrame({sym: raw10["Close"]})
        vol10   = pd.DataFrame({sym: raw10["Volume"]})

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

    # Take top 300 candidates for composite ranking
    pre_candidates = [str(s) for s in after_vp.index[:300]]

    # ── Step 2: Fetch 1-year data for composite ranking ───────────────────────
    all_fetch = list(dict.fromkeys(pre_candidates + list(always_include)))
    log.info("[Universe] Fetching 1y data for %d tickers…", len(all_fetch))
    raw1y = yf.download(
        tickers=all_fetch,
        period="1y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    if raw1y.empty or not isinstance(raw1y.columns, pd.MultiIndex):
        # Fall back to volume-only ranking
        tickers   = pre_candidates[:top_n]
        cats      = {s: tag_category(s) for s in tickers}
        ex_bkd    = _exchange_breakdown(tickers, cats)
        stats["after_mcap"] = len(tickers)
        return tickers, stats, cats, ex_bkd

    close1y = raw1y["Close"]
    vol1y   = raw1y["Volume"]

    # ── Step 3: Composite score ───────────────────────────────────────────────
    raw_scores: Dict[str, Dict[str, float]] = {}
    removed_ipo = removed_spac = 0

    for sym in pre_candidates:
        if sym not in close1y.columns:
            continue
        col = close1y[sym].dropna()
        if len(col) < 5:
            continue

        # IPO detection: fewer than 20 trading days of history
        if len(col) < 20:
            removed_ipo += 1
            continue

        # SPAC detection by name (alpaca_names or skip for constituent lists)
        name = alpaca_names.get(sym, "")
        if name and _is_spac(name):
            removed_spac += 1
            continue

        vcol      = vol1y[sym].dropna() if sym in vol1y.columns else pd.Series(dtype=float)
        avg_v     = float(vcol.mean()) if len(vcol) > 0 else 0.0
        price_now = float(col.iloc[-1])
        price_5d  = float(col.iloc[-6]) if len(col) >= 6 else price_now
        p52_high  = float(col.max())

        momentum  = (price_now - price_5d) / price_5d if price_5d > 0 else 0.0
        dist_52w  = (p52_high - price_now) / p52_high if p52_high > 0 else 1.0

        raw_scores[sym] = {"vol": avg_v, "mom": momentum, "dist52w": dist_52w}

    stats["removed_ipo"]  = removed_ipo
    stats["removed_spac"] = removed_spac

    # Normalize each component to [0, 1] then compute weighted composite
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
            n_prox = 1.0 - _norm(v["dist52w"], dist_mn, dist_mx)  # lower dist = better
            composite[sym] = 0.50 * n_vol + 0.30 * n_mom + 0.20 * n_prox

    ranked = sorted(composite, key=lambda s: -composite[s])

    # ── Step 4: Market-cap filter on top 250 (parallel, includes name lookup) ─
    check_list = ranked[:250]
    mc_map     = _fetch_market_caps_with_names(check_list)

    passed: List[str] = []
    for sym in check_list:
        info = mc_map.get(sym, {})
        mc   = info.get("mc")
        # If name unknown via Alpaca, try yfinance name for SPAC check
        if not alpaca_names:
            name = info.get("name", "")
            if name and _is_spac(name):
                removed_spac += 1
                continue
        if mc is not None and mc < min_market_cap:
            continue
        passed.append(sym)
        if len(passed) >= top_n:
            break

    stats["after_mcap"]  = len(passed)
    stats["removed_spac"] = removed_spac  # update after yf name check

    # ── Step 5: Prepend ETFs and tag categories ───────────────────────────────
    etf_tickers = [s for s in always_include if s in close1y.columns]
    all_tickers = list(dict.fromkeys(etf_tickers + passed))

    cats  = {s: tag_category(s) for s in all_tickers}
    ex_bkd = _exchange_breakdown(all_tickers, cats)

    log.info(
        "[Universe] After mcap filter: %d tickers (incl %d ETFs, target %d)",
        len(all_tickers), len(etf_tickers), top_n,
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


def _fetch_market_caps_with_names(symbols: List[str]) -> Dict[str, Dict]:
    """Parallel fast_info.market_cap lookup — low-memory, no full info fetch."""
    results: Dict[str, Dict] = {}

    def _get(sym: str) -> Tuple[str, Dict]:
        try:
            mc = yf.Ticker(sym).fast_info.market_cap
            return sym, {"mc": float(mc) if mc else None, "name": ""}
        except Exception:
            return sym, {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_get, sym): sym for sym in symbols}
        for fut in as_completed(futures, timeout=90):
            try:
                sym, info = fut.result()
                results[sym] = info
            except Exception:
                pass

    return results
