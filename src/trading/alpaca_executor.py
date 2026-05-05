"""Alpaca-backed order executor.

Submits bracket orders (market entry + stop-loss + take-profit) to Alpaca's
trading API.  Works with both paper and live accounts — the distinction is
controlled by the `paper` constructor argument.

On each engine cycle the engine calls `sync_portfolio()` first so the local
Portfolio mirror stays in sync with what Alpaca actually holds (catching any
bracket legs that fired between cycles).
"""

import logging
import math
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from .portfolio import Portfolio, Position, Trade

logger = logging.getLogger(__name__)

# ── Rate-limit handling constants ─────────────────────────────────────────────
_MAX_RETRIES      = 3
_RATE_LIMIT_WAIT  = 5.0    # seconds to wait after a 429
_MIN_CALL_GAP     = 0.35   # max ~3 Alpaca calls/second globally
_PRICES_BATCH     = 5      # symbols per quote request
_QUOTE_TTL        = 60     # seconds — quote cache TTL (shared across all endpoints)
_CLOCK_TTL        = 300    # 5 min  — market open status changes slowly
_PERF_TTL         = 300    # 5 min  — daily P&L sparkline
_POSITIONS_TTL    = 60     # 1 min  — live position list
_ACCOUNT_TTL      = 60     # 1 min  — account summary (equity / cash)
_ORDERS_TTL       = 60     # 1 min  — filled orders list

# ── Global rate limiter ───────────────────────────────────────────────────────
_rate_lock = threading.Lock()
_last_api_call: float = 0.0


def _rate_sleep() -> None:
    """Block until at least _MIN_CALL_GAP seconds have elapsed since the last call."""
    global _last_api_call
    with _rate_lock:
        now = time.time()
        gap = _MIN_CALL_GAP - (now - _last_api_call)
        if gap > 0:
            time.sleep(gap)
        _last_api_call = time.time()


# ── Shared caches ─────────────────────────────────────────────────────────────
# Quote cache: all executor instances + dashboard endpoints share a single price dict.
_quote_cache: Dict[str, Tuple[float, float]] = {}  # sym → (price, expires_at)
_quote_lock = threading.Lock()

# Shared cache for non-user-specific data (market clock — same for everyone)
_shared_cache: Dict[str, Tuple[Any, float]] = {}


def _is_rate_limited(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate limit" in s or "too many requests" in s


def get_cached_price(symbol: str) -> Optional[float]:
    """Return the latest cached Alpaca quote price for *symbol* if still fresh, else None."""
    with _quote_lock:
        entry = _quote_cache.get(symbol)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None


class AlpacaExecutor:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.data = StockHistoricalDataClient(api_key, secret_key)
        self._tag = "PAPER" if paper else "LIVE"
        self._cache: Dict[str, Tuple[Any, float]] = {}  # per-instance (user-specific data)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _call_with_retry(self, fn: Callable, *args, **kwargs) -> Any:
        """Call fn(*args, **kwargs) with global rate limiting and 429 retry."""
        for attempt in range(_MAX_RETRIES + 1):
            try:
                _rate_sleep()   # enforce ≤3 calls/second globally across all instances
                return fn(*args, **kwargs)
            except Exception as e:
                if _is_rate_limited(e) and attempt < _MAX_RETRIES:
                    logger.warning(
                        f"[{self._tag}] Rate limited (429) — waiting {_RATE_LIMIT_WAIT}s "
                        f"(attempt {attempt + 1}/{_MAX_RETRIES})"
                    )
                    time.sleep(_RATE_LIMIT_WAIT)
                else:
                    raise

    def _get_cached(self, key: str, fn: Callable, ttl: float, shared: bool = False) -> Any:
        """Return cached value for key, or call fn() to populate it.

        shared=True uses the module-level cache (for market clock, same across all users).
        shared=False uses instance-level cache (for user-specific data like positions).
        """
        store = _shared_cache if shared else self._cache
        now = time.time()
        entry = store.get(key)
        if entry and now < entry[1]:
            return entry[0]
        val = fn()
        store[key] = (val, now + ttl)
        return val

    # ── Market clock ──────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        try:
            def _fetch():
                return self._call_with_retry(self.trading.get_clock).is_open
            return self._get_cached(f"{self._tag}:is_open", _fetch, _CLOCK_TTL, shared=True)
        except Exception as e:
            logger.warning(f"Could not fetch market clock: {e}")
            entry = _shared_cache.get(f"{self._tag}:is_open")
            return entry[0] if entry else False

    def get_clock_info(self) -> dict:
        try:
            def _fetch():
                clock = self._call_with_retry(self.trading.get_clock)
                return {
                    "is_open": clock.is_open,
                    "next_open": str(clock.next_open),
                    "next_close": str(clock.next_close),
                }
            return self._get_cached(f"{self._tag}:clock_info", _fetch, _CLOCK_TTL, shared=True)
        except Exception as e:
            logger.warning(f"Clock fetch failed: {e}")
            entry = _shared_cache.get(f"{self._tag}:clock_info")
            return entry[0] if entry else {"is_open": False, "next_open": "unknown", "next_close": "unknown"}

    # ── Account info ──────────────────────────────────────────────────────────

    def get_account_summary(self) -> dict:
        def _fetch():
            acct = self._call_with_retry(self.trading.get_account)
            return {
                "equity": float(acct.equity),
                "cash": float(acct.cash),
                "buying_power": float(acct.buying_power),
                "portfolio_value": float(acct.portfolio_value),
                "daytrade_count": int(acct.daytrade_count),
            }
        return self._get_cached(f"{self._tag}:account", _fetch, _ACCOUNT_TTL)

    # ── Live quotes ───────────────────────────────────────────────────────────

    def get_live_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Return mid-price for each symbol.

        Checks the 60-second module-level quote cache first; only fetches symbols
        not already cached.  Results are stored back so heatmap / watchlist /
        dashboard state all read from the same dict without extra API calls.
        Batched _PRICES_BATCH at a time; rate limiter in _call_with_retry
        enforces ≤3 requests/second so no extra inter-batch sleep needed.
        """
        now = time.time()
        prices: Dict[str, float] = {}
        to_fetch: List[str] = []

        with _quote_lock:
            for sym in symbols:
                entry = _quote_cache.get(sym)
                if entry and now < entry[1]:
                    prices[sym] = entry[0]
                else:
                    to_fetch.append(sym)

        for i in range(0, len(to_fetch), _PRICES_BATCH):
            batch = to_fetch[i:i + _PRICES_BATCH]
            try:
                req = StockLatestQuoteRequest(symbol_or_symbols=batch)
                quotes = self._call_with_retry(self.data.get_stock_latest_quote, req)
                expires = time.time() + _QUOTE_TTL
                batch_prices: Dict[str, float] = {}
                for sym, q in quotes.items():
                    ask = float(q.ask_price) if q.ask_price else None
                    bid = float(q.bid_price) if q.bid_price else None
                    if ask and bid:
                        batch_prices[sym] = (ask + bid) / 2
                    elif ask or bid:
                        batch_prices[sym] = float(ask or bid)
                with _quote_lock:
                    for sym, price in batch_prices.items():
                        _quote_cache[sym] = (price, expires)
                prices.update(batch_prices)
            except Exception as e:
                logger.error(f"[{self._tag}] Quote fetch failed for batch {batch}: {e}")

        return prices

    # ── Portfolio sync ────────────────────────────────────────────────────────

    def sync_portfolio(self, portfolio: Portfolio, risk_mgr=None) -> None:
        """Overwrite local portfolio state with what Alpaca actually holds.

        Called at the top of each engine cycle so bracket-triggered exits
        (stop-loss / take-profit that fired between cycles) are reflected
        locally without any manual accounting.
        """
        try:
            alpaca_positions = {p.symbol: p for p in self._call_with_retry(self.trading.get_all_positions)}
            acct = self._call_with_retry(self.trading.get_account)

            # Remove local positions that Alpaca no longer holds
            for sym in list(portfolio.positions.keys()):
                if sym not in alpaca_positions:
                    logger.info(f"  [SYNC] {sym} closed on Alpaca (bracket triggered)")
                    portfolio.positions.pop(sym, None)

            # Add / update positions that Alpaca holds
            for sym, ap in alpaca_positions.items():
                entry = float(ap.avg_entry_price)
                qty = float(ap.qty)
                current = float(ap.current_price) if ap.current_price else entry
                base_sl = entry * (1 - (risk_mgr.stop_loss_pct if risk_mgr else 0.05))
                tp = entry * (1 + (risk_mgr.take_profit_pct if risk_mgr else 0.15))

                use_trail = risk_mgr and risk_mgr.use_trailing_stop
                trail_pct = risk_mgr.trailing_stop_pct if use_trail else 0.05
                if sym in portfolio.positions:
                    # Carry over the highest_price tracked so far, then update
                    existing = portfolio.positions[sym]
                    highest = max(existing.highest_price, current)
                else:
                    logger.info(f"  [SYNC] Added position {sym} ({qty} shares @ ${entry:.2f})")
                    highest = max(entry, current)

                sl = max(highest * (1 - trail_pct), base_sl) if use_trail else base_sl
                portfolio.positions[sym] = Position(
                    symbol=sym,
                    shares=qty,
                    entry_price=entry,
                    entry_time=datetime.now(),
                    stop_loss=sl,
                    take_profit=tp,
                    highest_price=highest,
                )

            portfolio.cash = float(acct.cash)
            logger.debug(
                f"  [SYNC] {len(portfolio.positions)} positions, "
                f"cash=${portfolio.cash:,.2f}"
            )
        except Exception as e:
            logger.error(f"Portfolio sync from Alpaca failed: {e}")

    def get_live_positions(self) -> List[dict]:
        """Return all current Alpaca positions as plain dicts for dashboard display (cached 1 min)."""
        def _fetch():
            positions = self._call_with_retry(self.trading.get_all_positions)
            result = []
            for p in positions:
                result.append({
                    "symbol": p.symbol,
                    "shares": float(p.qty),
                    "entry_price": float(p.avg_entry_price),
                    "current_price": float(p.current_price) if p.current_price else None,
                    "pnl": float(p.unrealized_pl) if p.unrealized_pl else None,
                    "pnl_pct": float(p.unrealized_plpc) if p.unrealized_plpc else None,
                    "change_today": float(p.unrealized_intraday_pl) if p.unrealized_intraday_pl else None,
                    "change_today_pct": float(p.unrealized_intraday_plpc) if p.unrealized_intraday_plpc else None,
                })
            return result
        return self._get_cached(f"{self._tag}:positions", _fetch, _POSITIONS_TTL)

    def get_daily_performance(self) -> dict:
        """Return today's P&L and equity sparkline from Alpaca portfolio history (cached 5 min)."""
        from alpaca.trading.requests import GetPortfolioHistoryRequest

        def _fetch():
            try:
                hist = self._call_with_retry(
                    self.trading.get_portfolio_history,
                    GetPortfolioHistoryRequest(period="1D", timeframe="5Min", extended_hours=True),
                )
                if not hist or not hist.equity:
                    return {}
                equity = [round(float(e), 2) for e in hist.equity if e is not None]
                pnl_series = [round(float(pl), 2) for pl in hist.profit_loss if pl is not None]
                pnl_pct_series = [
                    round(float(pp) * 100, 4) for pp in hist.profit_loss_pct if pp is not None
                ]
                return {
                    "today_pnl": pnl_series[-1] if pnl_series else 0,
                    "today_pnl_pct": pnl_pct_series[-1] if pnl_pct_series else 0,
                    "sparkline": equity[-48:],
                }
            except Exception as e:
                logger.warning("Portfolio history fetch failed: %s", e)
                return {}

        try:
            return self._get_cached(f"{self._tag}:perf", _fetch, _PERF_TTL)
        except Exception as e:
            logger.warning("Daily performance cache/fetch failed: %s", e)
            return {}

    def get_filled_orders(self, limit: int = 30) -> List[dict]:
        """Return recent filled orders from Alpaca, newest first (cached 1 min)."""
        def _fetch():
            orders = self._call_with_retry(
                self.trading.get_orders,
                GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit * 2),
            )
            result = []
            for o in orders:
                if not o.filled_at:
                    continue
                side = "BUY" if "buy" in str(o.side).lower() else "SELL"
                order_class = str(getattr(o, "order_class", "") or "")
                reason = "bracket order" if "bracket" in order_class else "market"
                result.append({
                    "timestamp": o.filled_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "action": side,
                    "symbol": o.symbol,
                    "shares": round(float(o.filled_qty), 4) if o.filled_qty else 0,
                    "price": round(float(o.filled_avg_price), 2) if o.filled_avg_price else 0,
                    "pnl": None,
                    "pnl_pct": None,
                    "reason": reason,
                })
                if len(result) >= limit:
                    break
            return result
        return self._get_cached(f"{self._tag}:orders:{limit}", _fetch, _ORDERS_TTL)

    # ── Order execution ───────────────────────────────────────────────────────

    def execute_buy(
        self,
        symbol: str,
        shares: float,
        price: float,
        stop_loss: float,
        take_profit: float,
        reason: str,
        portfolio: Portfolio,
    ) -> bool:
        qty = math.floor(shares)
        if qty < 1:
            logger.warning(f"[{self._tag} BUY SKIP] {symbol}: position rounds to 0 shares")
            return False

        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
            take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
        )
        order = None
        try:
            order = self._call_with_retry(self.trading.submit_order, req)
        except Exception as e:
            logger.warning(f"[{self._tag} BUY] {symbol}: first attempt failed ({e}), retrying in 2s")
            time.sleep(2)
            try:
                order = self._call_with_retry(self.trading.submit_order, req)
            except Exception as e2:
                logger.error(f"[{self._tag} BUY FAILED] {symbol}: retry also failed: {e2}")
                return False
        # Mirror in local portfolio at the intended price
        portfolio.buy(symbol, qty, price, stop_loss, take_profit, reason)
        logger.info(
            f"[{self._tag} BUY]  {qty} {symbol:6s} @ ~${price:.2f}"
            f" | SL ${stop_loss:.2f}  TP ${take_profit:.2f}"
            f" | order={order.id}"
        )
        return True

    def execute_sell(
        self,
        symbol: str,
        price: float,
        reason: str,
        portfolio: Portfolio,
    ) -> Optional[Trade]:
        if symbol not in portfolio.positions:
            return None

        qty = math.floor(portfolio.positions[symbol].shares)
        if qty < 1:
            return portfolio.sell(symbol, price, reason)

        self._cancel_open_orders(symbol)
        sell_req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = None
        try:
            order = self._call_with_retry(self.trading.submit_order, sell_req)
        except Exception as e:
            logger.warning(f"[{self._tag} SELL] {symbol}: first attempt failed ({e}), retrying in 2s")
            time.sleep(2)
            try:
                order = self._call_with_retry(self.trading.submit_order, sell_req)
            except Exception as e2:
                logger.error(f"[{self._tag} SELL FAILED] {symbol}: retry also failed: {e2}")
                return None
        trade = portfolio.sell(symbol, price, reason)
        if trade and trade.pnl is not None:
            sign = "+" if trade.pnl >= 0 else ""
            logger.info(
                f"[{self._tag} SELL] {qty} {symbol:6s} @ ~${price:.2f}"
                f" | P&L {sign}${trade.pnl:,.2f} ({sign}{trade.pnl_pct * 100:.2f}%)"
                f" | order={order.id}  {reason}"
            )
        return trade

    def execute_partial_sell(
        self,
        symbol: str,
        shares: float,
        price: float,
        reason: str,
        portfolio: Portfolio,
    ) -> bool:
        """Sell a portion of an existing long position (partial profit-taking)."""
        import math
        qty = math.floor(shares)
        if qty < 1:
            return False
        try:
            order = self._call_with_retry(
                self.trading.submit_order,
                MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                ),
            )
            logger.info(
                f"[{self._tag} PARTIAL] {qty} {symbol:6s} @ ~${price:.2f}"
                f" | order={order.id}  {reason}"
            )
            return True
        except Exception as e:
            logger.error(f"[{self._tag} PARTIAL FAILED] {symbol}: {e}")
            return False

    def execute_short(
        self,
        symbol: str,
        shares: float,
        price: float,
        stop_loss: float,
        take_profit: float,
        reason: str,
        portfolio: Portfolio,
    ) -> bool:
        """Open a short position via a bracket sell order."""
        import math
        qty = math.floor(shares)
        if qty < 1:
            logger.warning(f"[{self._tag} SHORT SKIP] {symbol}: rounds to 0 shares")
            return False
        # For shorts: stop_loss > entry, take_profit < entry
        short_req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
            take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
        )
        order = None
        try:
            order = self._call_with_retry(self.trading.submit_order, short_req)
        except Exception as e:
            logger.warning(f"[{self._tag} SHORT] {symbol}: first attempt failed ({e}), retrying in 2s")
            time.sleep(2)
            try:
                order = self._call_with_retry(self.trading.submit_order, short_req)
            except Exception as e2:
                logger.error(f"[{self._tag} SHORT FAILED] {symbol}: retry also failed: {e2}")
                return False
        portfolio.buy(symbol, qty, price, stop_loss, take_profit, reason, is_short=True)
        logger.info(
            f"[{self._tag} SHORT] {qty} {symbol:6s} @ ~${price:.2f}"
            f" | SL ${stop_loss:.2f}  TP ${take_profit:.2f}"
            f" | order={order.id}"
        )
        return True

    def execute_cover(
        self,
        symbol: str,
        price: float,
        reason: str,
        portfolio: Portfolio,
    ) -> Optional[Trade]:
        """Cover (close) a short position with a market buy."""
        if symbol not in portfolio.positions:
            return None
        qty = math.floor(portfolio.positions[symbol].shares)
        if qty < 1:
            return portfolio.sell(symbol, price, reason)
        try:
            self._cancel_open_orders(symbol)
            order = self._call_with_retry(
                self.trading.submit_order,
                MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                ),
            )
            trade = portfolio.sell(symbol, price, reason)
            if trade and trade.pnl is not None:
                sign = "+" if trade.pnl >= 0 else ""
                logger.info(
                    f"[{self._tag} COVER] {qty} {symbol:6s} @ ~${price:.2f}"
                    f" | P&L {sign}${trade.pnl:,.2f} ({sign}{trade.pnl_pct * 100:.2f}%)"
                    f" | order={order.id}  {reason}"
                )
            return trade
        except Exception as e:
            logger.error(f"[{self._tag} COVER FAILED] {symbol}: {e}")
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _cancel_open_orders(self, symbol: str) -> None:
        try:
            open_orders = self._call_with_retry(
                self.trading.get_orders,
                GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]),
            )
            for order in open_orders:
                self.trading.cancel_order_by_id(str(order.id))
                logger.debug(f"  Cancelled order {order.id} for {symbol}")
        except Exception as e:
            logger.warning(f"Could not cancel open orders for {symbol}: {e}")
