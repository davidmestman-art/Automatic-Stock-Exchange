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
from datetime import datetime
from typing import Dict, List, Optional

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


class AlpacaExecutor:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.data = StockHistoricalDataClient(api_key, secret_key)
        self._tag = "PAPER" if paper else "LIVE"

    # ── Market clock ──────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        try:
            return self.trading.get_clock().is_open
        except Exception as e:
            logger.warning(f"Could not fetch market clock: {e}")
            return False

    def get_clock_info(self) -> dict:
        try:
            clock = self.trading.get_clock()
            return {
                "is_open": clock.is_open,
                "next_open": str(clock.next_open),
                "next_close": str(clock.next_close),
            }
        except Exception as e:
            logger.warning(f"Clock fetch failed: {e}")
            return {"is_open": False, "next_open": "unknown", "next_close": "unknown"}

    # ── Account info ──────────────────────────────────────────────────────────

    def get_account_summary(self) -> dict:
        acct = self.trading.get_account()
        return {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "portfolio_value": float(acct.portfolio_value),
            "daytrade_count": int(acct.daytrade_count),
        }

    # ── Live quotes ───────────────────────────────────────────────────────────

    def get_live_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Returns mid-price (ask+bid)/2 for each symbol."""
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=symbols)
            quotes = self.data.get_stock_latest_quote(req)
            prices: Dict[str, float] = {}
            for sym, q in quotes.items():
                ask = float(q.ask_price) if q.ask_price else None
                bid = float(q.bid_price) if q.bid_price else None
                if ask and bid:
                    prices[sym] = (ask + bid) / 2
                elif ask or bid:
                    prices[sym] = float(ask or bid)
            return prices
        except Exception as e:
            logger.error(f"Alpaca quote fetch failed: {e}")
            return {}

    # ── Portfolio sync ────────────────────────────────────────────────────────

    def sync_portfolio(self, portfolio: Portfolio, risk_mgr=None) -> None:
        """Overwrite local portfolio state with what Alpaca actually holds.

        Called at the top of each engine cycle so bracket-triggered exits
        (stop-loss / take-profit that fired between cycles) are reflected
        locally without any manual accounting.
        """
        try:
            alpaca_positions = {p.symbol: p for p in self.trading.get_all_positions()}
            acct = self.trading.get_account()

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
        """Return all current Alpaca positions as plain dicts for dashboard display."""
        positions = self.trading.get_all_positions()
        result = []
        for p in positions:
            result.append({
                "symbol": p.symbol,
                "shares": float(p.qty),
                "entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price) if p.current_price else None,
                "pnl": float(p.unrealized_pl) if p.unrealized_pl else None,
                "pnl_pct": float(p.unrealized_plpc) if p.unrealized_plpc else None,
            })
        return result

    def get_filled_orders(self, limit: int = 30) -> List[dict]:
        """Return recent filled orders from Alpaca, newest first."""
        orders = self.trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=limit * 2)
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

        try:
            order = self.trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET,
                    stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
                    take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
                )
            )
            # Mirror in local portfolio at the intended price
            portfolio.buy(symbol, qty, price, stop_loss, take_profit, reason)
            logger.info(
                f"[{self._tag} BUY]  {qty} {symbol:6s} @ ~${price:.2f}"
                f" | SL ${stop_loss:.2f}  TP ${take_profit:.2f}"
                f" | order={order.id}"
            )
            return True
        except Exception as e:
            logger.error(f"[{self._tag} BUY FAILED] {symbol}: {e}")
            return False

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

        try:
            self._cancel_open_orders(symbol)
            order = self.trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            trade = portfolio.sell(symbol, price, reason)
            if trade and trade.pnl is not None:
                sign = "+" if trade.pnl >= 0 else ""
                logger.info(
                    f"[{self._tag} SELL] {qty} {symbol:6s} @ ~${price:.2f}"
                    f" | P&L {sign}${trade.pnl:,.2f} ({sign}{trade.pnl_pct * 100:.2f}%)"
                    f" | order={order.id}  {reason}"
                )
            return trade
        except Exception as e:
            logger.error(f"[{self._tag} SELL FAILED] {symbol}: {e}")
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _cancel_open_orders(self, symbol: str) -> None:
        try:
            open_orders = self.trading.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
            )
            for order in open_orders:
                self.trading.cancel_order_by_id(str(order.id))
                logger.debug(f"  Cancelled order {order.id} for {symbol}")
        except Exception as e:
            logger.warning(f"Could not cancel open orders for {symbol}: {e}")
