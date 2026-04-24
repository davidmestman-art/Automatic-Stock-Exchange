from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class Position:
    symbol: str
    shares: float
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit: float

    @property
    def cost_basis(self) -> float:
        return self.shares * self.entry_price

    def current_value(self, price: float) -> float:
        return self.shares * price

    def unrealized_pnl(self, price: float) -> float:
        return self.current_value(price) - self.cost_basis

    def unrealized_pnl_pct(self, price: float) -> float:
        return (price - self.entry_price) / self.entry_price


@dataclass
class Trade:
    symbol: str
    action: str                 # "BUY" or "SELL"
    shares: float
    price: float
    timestamp: datetime
    reason: str
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    indicator_snapshot: Optional[dict] = None


class Portfolio:
    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self._day_start_value = initial_capital

    def total_value_at(self, prices: Dict[str, float]) -> float:
        position_value = sum(
            pos.current_value(prices.get(sym, pos.entry_price))
            for sym, pos in self.positions.items()
        )
        return self.cash + position_value

    def open_position_count(self) -> int:
        return len(self.positions)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions

    def buy(
        self,
        symbol: str,
        shares: float,
        price: float,
        stop_loss: float,
        take_profit: float,
        reason: str,
        indicator_snapshot: Optional[dict] = None,
    ) -> bool:
        cost = shares * price
        if cost > self.cash:
            return False
        self.cash -= cost
        self.positions[symbol] = Position(
            symbol=symbol,
            shares=shares,
            entry_price=price,
            entry_time=datetime.now(),
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        self.trades.append(
            Trade(
                symbol=symbol,
                action="BUY",
                shares=shares,
                price=price,
                timestamp=datetime.now(),
                reason=reason,
                indicator_snapshot=indicator_snapshot,
            )
        )
        return True

    def sell(
        self,
        symbol: str,
        price: float,
        reason: str,
        indicator_snapshot: Optional[dict] = None,
    ) -> Optional[Trade]:
        if symbol not in self.positions:
            return None
        pos = self.positions.pop(symbol)
        proceeds = pos.shares * price
        self.cash += proceeds
        pnl = proceeds - pos.cost_basis
        pnl_pct = (price - pos.entry_price) / pos.entry_price
        trade = Trade(
            symbol=symbol,
            action="SELL",
            shares=pos.shares,
            price=price,
            timestamp=datetime.now(),
            reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            indicator_snapshot=indicator_snapshot,
        )
        self.trades.append(trade)
        return trade

    def update_day_start(self, prices: Dict[str, float]):
        self._day_start_value = self.total_value_at(prices)

    def daily_pnl_pct(self, prices: Dict[str, float]) -> float:
        if self._day_start_value == 0:
            return 0.0
        return (self.total_value_at(prices) - self._day_start_value) / self._day_start_value

    def total_pnl(self, prices: Dict[str, float]) -> float:
        return self.total_value_at(prices) - self.initial_capital

    def total_pnl_pct(self, prices: Dict[str, float]) -> float:
        return self.total_pnl(prices) / self.initial_capital

    def get_summary(self, prices: Dict[str, float]) -> dict:
        total_val = self.total_value_at(prices)
        return {
            "total_value": total_val,
            "cash": self.cash,
            "position_value": total_val - self.cash,
            "total_pnl": self.total_pnl(prices),
            "total_pnl_pct": self.total_pnl_pct(prices) * 100,
            "open_positions": self.open_position_count(),
            "total_trades": len(self.trades),
        }
