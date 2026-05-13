"""Passive ETF rebalancer — deploys idle cash into a diversified basket once per day.

Strategy:
  - Runs once per trading day, after market open (called from the engine)
  - Allocates a configurable slice of idle cash to a fixed ETF target allocation
  - Only buys (never sells) — tax-efficient dollar-cost averaging
  - Skips any ETF where the price fetch fails or the notional is < $1
"""

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default target weights — must sum to 1.0
DEFAULT_ETF_TARGETS: Dict[str, float] = {
    "VTI":  0.35,   # US total market
    "VEA":  0.15,   # Developed international
    "VWO":  0.10,   # Emerging markets
    "BND":  0.15,   # Total bond market
    "IAU":  0.10,   # Gold
    "VNQ":  0.05,   # Real estate
    "IBIT": 0.05,   # Bitcoin ETF
    "SPY":  0.05,   # S&P 500 (overlap intentional as anchor)
}

_MIN_NOTIONAL = 1.00   # skip any ETF whose allocation is below $1


class ETFRebalancer:
    """Dollar-cost average idle cash into a fixed ETF basket once per trading day."""

    def __init__(
        self,
        target_weights: Optional[Dict[str, float]] = None,
        cash_deploy_pct: float = 0.30,   # fraction of idle cash to invest each day
        orb_reserve_pct: float = 0.40,   # keep this much cash untouched for ORB trades
    ):
        self.target_weights = target_weights or DEFAULT_ETF_TARGETS
        self.cash_deploy_pct = cash_deploy_pct
        self.orb_reserve_pct = orb_reserve_pct
        self._last_run_date: Optional[str] = None
        self._last_buys: List[dict] = []

    # ── Public API ─────────────────────────────────────────────────────────────

    def should_run(self, today: str) -> bool:
        return self._last_run_date != today

    def run(self, portfolio, executor, fetcher, today: str) -> List[dict]:
        """Buy ETFs with idle cash. Returns list of buy records."""
        orb_reserve = portfolio.cash * self.orb_reserve_pct
        deployable  = (portfolio.cash - orb_reserve) * self.cash_deploy_pct

        if deployable < _MIN_NOTIONAL * len(self.target_weights):
            logger.info(f"[ETF] Insufficient deployable cash (${deployable:.2f}) — skipping")
            self._last_run_date = today
            return []

        # Fetch current prices for all target ETFs
        prices = self._get_prices(fetcher)
        if not prices:
            logger.warning("[ETF] Could not fetch ETF prices — skipping")
            self._last_run_date = today
            return []

        bought: List[dict] = []
        for etf, weight in self.target_weights.items():
            price = prices.get(etf)
            if price is None or price <= 0:
                logger.debug(f"[ETF] No price for {etf} — skipping")
                continue
            notional = deployable * weight
            if notional < _MIN_NOTIONAL:
                continue
            shares = notional / price
            try:
                executor.buy_fractional(etf, notional)
                bought.append({"symbol": etf, "notional": round(notional, 2),
                               "shares": round(shares, 6), "price": round(price, 4)})
                logger.info(f"[ETF] BUY {etf}  ${notional:.2f}  ({shares:.4f} shares @ ${price:.2f})")
            except Exception as exc:
                logger.warning(f"[ETF] Order failed for {etf}: {exc}")

        self._last_run_date = today
        self._last_buys = bought
        if bought:
            syms = ", ".join(b["symbol"] for b in bought)
            total = sum(b["notional"] for b in bought)
            logger.info(f"[ETF] Deployed ${total:.2f} across {len(bought)} ETFs: {syms}")
        return bought

    @property
    def last_buys(self) -> List[dict]:
        return self._last_buys

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_prices(self, fetcher) -> Dict[str, float]:
        etfs = list(self.target_weights.keys())
        try:
            data = fetcher.fetch_many(etfs, force_refresh=False)
            prices: Dict[str, float] = {}
            for sym, df in data.items():
                if df is not None and not df.empty:
                    prices[sym] = float(df["Close"].iloc[-1])
            return prices
        except Exception as exc:
            logger.warning(f"[ETF] Price fetch error: {exc}")
            return {}
