from dataclasses import dataclass


@dataclass
class RiskCheck:
    approved: bool
    reason: str
    max_shares: float = 0.0


class RiskManager:
    def __init__(
        self,
        max_position_pct: float = 0.10,
        max_open_positions: int = 8,
        stop_loss_pct: float = 0.05,
        take_profit_pct: float = 0.15,
        daily_loss_limit_pct: float = 0.03,
        max_positions_per_sector: int = 3,
        use_trailing_stop: bool = True,
        trailing_stop_pct: float = 0.05,
    ):
        self.max_position_pct = max_position_pct
        self.max_open_positions = max_open_positions
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_positions_per_sector = max_positions_per_sector
        self.use_trailing_stop = use_trailing_stop
        self.trailing_stop_pct = trailing_stop_pct

    def check_buy(
        self,
        symbol: str,
        price: float,
        portfolio_value: float,
        cash: float,
        open_positions: int,
        daily_pnl_pct: float,
        signal_confidence: float,
        sector_positions: int = 0,
    ) -> RiskCheck:
        if daily_pnl_pct <= -self.daily_loss_limit_pct:
            return RiskCheck(
                False,
                f"Daily loss limit reached ({daily_pnl_pct * 100:.2f}%)",
            )

        if open_positions >= self.max_open_positions:
            return RiskCheck(
                False,
                f"Max open positions ({self.max_open_positions}) reached",
            )

        if self.max_positions_per_sector > 0 and sector_positions >= self.max_positions_per_sector:
            return RiskCheck(
                False,
                f"Sector limit ({self.max_positions_per_sector}) reached",
            )

        # Scale position size by signal confidence, capped at max_position_pct
        position_pct = self.max_position_pct * min(1.0, signal_confidence * 1.5)
        position_value = min(portfolio_value * position_pct, cash * 0.95)

        if position_value < price:
            return RiskCheck(False, "Insufficient capital for minimum position")

        return RiskCheck(True, "Risk checks passed", max_shares=position_value / price)

    def update_trailing_stop(self, pos, current_price: float) -> bool:
        """Ratchet pos.stop_loss up when price makes a new high. Returns True if updated."""
        if not self.use_trailing_stop:
            return False
        if current_price > pos.highest_price:
            pos.highest_price = current_price
            pos.stop_loss = round(current_price * (1 - self.trailing_stop_pct), 4)
            return True
        return False

    def check_stop_loss(self, entry_price: float, current_price: float, pos=None) -> bool:
        if self.use_trailing_stop and pos is not None:
            return current_price <= pos.stop_loss
        return current_price <= entry_price * (1 - self.stop_loss_pct)

    def check_take_profit(self, entry_price: float, current_price: float) -> bool:
        return current_price >= entry_price * (1 + self.take_profit_pct)

    def stop_loss_price(self, entry_price: float) -> float:
        return entry_price * (1 - self.stop_loss_pct)

    def take_profit_price(self, entry_price: float) -> float:
        return entry_price * (1 + self.take_profit_pct)
