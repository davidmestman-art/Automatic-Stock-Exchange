"""Dynamic stock scanner.

Pipeline executed once per trading session:
  1. Batch-fetch 1-day volume for the full S&P 500 universe via yfinance
  2. Keep the top ``volume_top_n`` symbols (default 100) by volume
  3. Compute technical indicators + composite signal score for every candidate
  4. Rank by |score| and return the top ``signal_top_n`` (default 10) symbols

Results are cached for the calendar day so repeated calls within the same
session are free.  Call ``scan()`` with ``force=True`` to override.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── S&P 500 universe ──────────────────────────────────────────────────────────
# ~200 most liquid constituents.  Covers every GICS sector and provides ample
# diversity for the volume + signal filter to select from.

SP500_UNIVERSE: List[str] = [
    # Technology
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "QCOM", "TXN",
    "INTC", "AMAT", "LRCX", "KLAC", "MU", "ADI", "MRVL", "NOW", "PANW", "SNPS",
    "CDNS", "FTNT", "ANSS", "KEYS", "CTSH", "IBM", "ACN", "CSCO", "HPQ", "CDW",
    # Communication services
    "GOOGL", "META", "NFLX", "T", "VZ", "TMUS", "DIS", "CMCSA", "CHTR", "WBD",
    # Consumer discretionary
    "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW", "BKNG", "MAR",
    "CMG", "HLT", "YUM", "DG", "DLTR", "ORLY", "AZO", "GM", "F", "PHM",
    # Consumer staples
    "WMT", "COST", "PG", "KO", "PEP", "PM", "MO", "CL", "MDLZ", "GIS",
    "STZ", "SYY", "HSY", "CHD", "EL", "CLX", "KHC",
    # Financials
    "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "BLK", "SCHW", "AXP",
    "C", "USB", "PNC", "TFC", "COF", "CME", "ICE", "CB", "MET", "PRU",
    "AFL", "AON", "MMC", "BX", "KKR", "SPGI", "MCO", "FIS", "PYPL", "FI",
    # Health care
    "UNH", "LLY", "JNJ", "MRK", "ABBV", "TMO", "ABT", "DHR", "BMY", "PFE",
    "AMGN", "GILD", "CVS", "CI", "ELV", "HUM", "MDT", "SYK", "BSX", "ISRG",
    "ZTS", "REGN", "VRTX", "DXCM", "IDXX", "RMD", "BAX", "MRNA", "BIIB", "IQV",
    # Industrials
    "GE", "RTX", "HON", "CAT", "DE", "BA", "UNP", "UPS", "FDX", "LMT",
    "NOC", "GD", "EMR", "ETN", "ROK", "PH", "ITW", "MMM", "IR", "CTAS",
    "CSX", "NSC", "EXPD", "GWW", "FAST", "VRSK", "PWR", "ODFL", "CARR", "OTIS",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "MPC", "PSX", "VLO", "BKR",
    "HAL", "DVN", "HES", "MRO", "WMB", "KMI", "OKE", "LNG", "TRGP",
    # Materials
    "LIN", "APD", "SHW", "ECL", "NEM", "FCX", "NUE", "VMC", "MLM", "ALB",
    "DOW", "PPG", "IFF", "DD", "RPM",
    # Utilities
    "NEE", "SO", "DUK", "D", "AEP", "EXC", "SRE", "PEG", "XEL", "ES",
    # Real estate
    "AMT", "PLD", "EQIX", "CCI", "PSA", "O", "SPG", "WELL", "AVB", "DLR",
]


class ScanResult:
    """Holds the output of a single scanner run."""

    def __init__(
        self,
        watchlist: List[str],
        volume_candidates: List[str],
        scores: Dict[str, float],
        actions: Dict[str, str],
        scan_date: str,
        scanned_at: str,
        fund_passed: int = 0,
        fund_failed: int = 0,
        fund_enabled: bool = False,
    ):
        self.watchlist = watchlist
        self.volume_candidates = volume_candidates
        self.scores = scores
        self.actions = actions
        self.scan_date = scan_date
        self.scanned_at = scanned_at
        self.fund_passed = fund_passed
        self.fund_failed = fund_failed
        self.fund_enabled = fund_enabled

    def to_dict(self) -> dict:
        return {
            "watchlist": self.watchlist,
            "volume_candidates_count": len(self.volume_candidates),
            "scores": {s: round(v, 3) for s, v in self.scores.items()},
            "actions": self.actions,
            "scan_date": self.scan_date,
            "scanned_at": self.scanned_at,
            "fund_enabled": self.fund_enabled,
            "fund_passed": self.fund_passed,
            "fund_failed": self.fund_failed,
        }


class StockScanner:
    def __init__(
        self,
        universe: List[str] = SP500_UNIVERSE,
        volume_top_n: int = 100,
        signal_top_n: int = 10,
        lookback_days: int = 120,
        fundamental_filter=None,
        earnings_calendar=None,
    ):
        self.universe = list(dict.fromkeys(universe))   # deduplicate, preserve order
        self.volume_top_n = volume_top_n
        self.signal_top_n = signal_top_n
        self.lookback_days = lookback_days
        self.fundamental_filter = fundamental_filter
        self.earnings_calendar = earnings_calendar
        self._last_result: Optional[ScanResult] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def scan(self, indicators, analyzer, force: bool = False) -> ScanResult:
        """Run the full pipeline and return a ScanResult.

        Cached for the calendar day unless *force* is True.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if not force and self._last_result and self._last_result.scan_date == today:
            logger.info("Scanner: using cached watchlist from today's session scan")
            return self._last_result

        logger.info(
            f"Scanner: starting session scan  "
            f"(universe={len(self.universe)}, "
            f"volume_top={self.volume_top_n}, "
            f"signal_top={self.signal_top_n})"
        )

        # Step 1 — volume filter
        volume_candidates = self._top_by_volume()
        fund_passed = fund_failed = 0
        fund_enabled = self.fundamental_filter is not None

        # Step 2 — fundamental filter (optional)
        if self.fundamental_filter is not None:
            before_fund = len(volume_candidates)
            volume_candidates = self.fundamental_filter.filter(volume_candidates)
            fund_passed = len(volume_candidates)
            fund_failed = before_fund - fund_passed

        # Step 3 — earnings protection (optional): remove symbols with imminent earnings
        if self.earnings_calendar is not None:
            before = len(volume_candidates)
            volume_candidates = [
                s for s in volume_candidates
                if not self.earnings_calendar.has_upcoming_earnings(s)
            ]
            removed = before - len(volume_candidates)
            if removed:
                logger.info(f"Scanner: earnings filter removed {removed} symbols")

        # Step 4 — signal ranking
        watchlist, scores, actions = self._top_by_signal(
            volume_candidates, indicators, analyzer
        )

        result = ScanResult(
            watchlist=watchlist,
            volume_candidates=volume_candidates,
            scores=scores,
            actions=actions,
            scan_date=today,
            scanned_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            fund_passed=fund_passed,
            fund_failed=fund_failed,
            fund_enabled=fund_enabled,
        )
        self._last_result = result
        logger.info(f"Scanner: watchlist → {watchlist}")
        return result

    @property
    def last_result(self) -> Optional[ScanResult]:
        return self._last_result

    # ── Volume filter ─────────────────────────────────────────────────────────

    def _top_by_volume(self) -> List[str]:
        logger.info(f"Scanner: fetching volume for {len(self.universe)} symbols…")
        try:
            raw = yf.download(
                tickers=self.universe,
                period="3d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            # Multi-ticker download returns MultiIndex columns (metric, symbol)
            if isinstance(raw.columns, pd.MultiIndex):
                vol_row = raw["Volume"].iloc[-1].dropna()
            else:
                # Fallback: single-ticker edge case
                sym = self.universe[0]
                vol_row = pd.Series({sym: float(raw["Volume"].iloc[-1])})

            vol_row = vol_row[vol_row > 0].sort_values(ascending=False)
            top = list(vol_row.index[: self.volume_top_n])
            logger.info(
                f"Scanner: volume filter → {len(top)} symbols "
                f"(min vol {vol_row.iloc[min(len(vol_row)-1, self.volume_top_n-1)]:.0f})"
            )
            return top
        except Exception as e:
            logger.error(f"Scanner: volume fetch failed — {e}; falling back to universe head")
            return self.universe[: self.volume_top_n]

    # ── Signal ranking ────────────────────────────────────────────────────────

    def _top_by_signal(
        self, candidates: List[str], indicators, analyzer
    ) -> Tuple[List[str], Dict[str, float], Dict[str, str]]:
        from ..data.fetcher import MarketDataFetcher

        fetcher = MarketDataFetcher(lookback_days=self.lookback_days, interval="1d")
        market_data = fetcher.fetch_many(candidates)

        scored: List[Tuple[str, float, str]] = []
        all_scores: Dict[str, float] = {}
        all_actions: Dict[str, str] = {}

        for symbol in candidates:
            if symbol not in market_data:
                continue
            try:
                ind = indicators.compute(market_data[symbol])
                sig = analyzer.analyze(ind)
                scored.append((symbol, abs(sig.score), sig.action))
                all_scores[symbol] = round(sig.score, 3)
                all_actions[symbol] = sig.action
            except Exception as e:
                logger.debug(f"Scanner: skipped {symbol} — {e}")

        scored.sort(key=lambda x: -x[1])
        watchlist = [s[0] for s in scored[: self.signal_top_n]]
        return watchlist, all_scores, all_actions
