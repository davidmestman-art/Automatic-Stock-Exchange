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
