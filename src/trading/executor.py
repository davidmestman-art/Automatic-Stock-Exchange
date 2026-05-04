import logging
from typing import Optional

from .portfolio import Portfolio, Trade

logger = logging.getLogger(__name__)


class PaperExecutor:
    """Simulates order execution against a virtual portfolio."""

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
        success = portfolio.buy(symbol, shares, price, stop_loss, take_profit, reason)
        if success:
            logger.info(
                f"[PAPER BUY]  {shares:.4f} {symbol:6s} @ ${price:>10.2f}"
                f" | SL ${stop_loss:.2f}  TP ${take_profit:.2f}"
                f" | Cost ${shares * price:,.2f}"
            )
        else:
            logger.warning(f"[PAPER BUY REJECTED] {symbol}: insufficient funds")
        return success

    def execute_sell(
        self,
        symbol: str,
        price: float,
        reason: str,
        portfolio: Portfolio,
    ) -> Optional[Trade]:
        trade = portfolio.sell(symbol, price, reason)
        if trade and trade.pnl is not None:
            sign = "+" if trade.pnl >= 0 else ""
            logger.info(
                f"[PAPER SELL] {trade.shares:.4f} {symbol:6s} @ ${price:>10.2f}"
                f" | P&L {sign}${trade.pnl:,.2f} ({sign}{trade.pnl_pct * 100:.2f}%)"
                f" | {reason}"
            )
        return trade

    def execute_partial_sell(
        self,
        symbol: str,
        shares: float,
        price: float,
        reason: str,
        portfolio: Portfolio,
    ) -> Optional[Trade]:
        # Portfolio already reduced shares; just log the paper fill
        logger.info(
            f"[PAPER PARTIAL] {shares:.0f} {symbol:6s} @ ${price:.2f}  | {reason}"
        )
        return None

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
        success = portfolio.buy(symbol, shares, price, stop_loss, take_profit, reason, is_short=True)
        if success:
            logger.info(
                f"[PAPER SHORT] {shares:.0f} {symbol:6s} @ ${price:.2f}"
                f" | SL ${stop_loss:.2f}  TP ${take_profit:.2f}"
            )
        return success

    def execute_cover(
        self,
        symbol: str,
        price: float,
        reason: str,
        portfolio: Portfolio,
    ) -> Optional[Trade]:
        trade = portfolio.sell(symbol, price, reason)
        if trade and trade.pnl is not None:
            sign = "+" if trade.pnl >= 0 else ""
            logger.info(
                f"[PAPER COVER] {trade.shares:.0f} {symbol:6s} @ ${price:.2f}"
                f" | P&L {sign}${trade.pnl:,.2f}  | {reason}"
            )
        return trade

    def get_account_summary(self) -> dict:
        return {"equity": 0, "cash": 0, "buying_power": 0, "portfolio_value": 0, "daytrade_count": 0}

    def get_clock_info(self) -> dict:
        return {"is_open": True, "next_open": "N/A", "next_close": "N/A"}

    def is_market_open(self) -> bool:
        return True

    def sync_portfolio(self, portfolio, risk_mgr=None) -> None:
        pass

    def get_live_prices(self, symbols) -> dict:
        return {}

    def get_live_positions(self) -> list:
        return []

    def get_daily_performance(self) -> dict:
        return {}

    def get_filled_orders(self, limit: int = 30) -> list:
        return []
