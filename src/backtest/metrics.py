from dataclasses import dataclass
from typing import List

import numpy as np


@dataclass
class BacktestMetrics:
    total_return_pct: float
    annualized_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_win_pct: float
    avg_loss_pct: float

    def __str__(self) -> str:
        w = self.total_return_pct
        sign = "+" if w >= 0 else ""
        lines = [
            "┌────────────────────────────────────────┐",
            "│           BACKTEST  RESULTS            │",
            "├────────────────────────────────────────┤",
            f"│  Total Return      {sign}{self.total_return_pct:>+8.2f}%            │",
            f"│  Ann. Return       {self.annualized_return_pct:>+8.2f}%            │",
            f"│  Sharpe Ratio      {self.sharpe_ratio:>9.3f}             │",
            f"│  Max Drawdown      {self.max_drawdown_pct:>+8.2f}%            │",
            f"│  Win Rate          {self.win_rate_pct:>8.1f}%            │",
            f"│  Profit Factor     {self.profit_factor:>9.2f}             │",
            f"│  Total Trades      {self.total_trades:>9d}             │",
            f"│  Win / Loss        {self.winning_trades:>4d} / {self.losing_trades:<4d}             │",
            f"│  Avg Win           {self.avg_win_pct:>+8.2f}%            │",
            f"│  Avg Loss          {self.avg_loss_pct:>+8.2f}%            │",
            "└────────────────────────────────────────┘",
        ]
        return "\n".join(lines)


def compute_metrics(equity_curve: List[float], trades: list, days: int) -> BacktestMetrics:
    if not equity_curve or len(equity_curve) < 2:
        return BacktestMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    equity = np.array(equity_curve, dtype=float)
    daily_returns = np.diff(equity) / equity[:-1]

    total_return = (equity[-1] - equity[0]) / equity[0] * 100

    years = max(days / 252, 1 / 252)
    annualized = ((equity[-1] / equity[0]) ** (1 / years) - 1) * 100

    rf_daily = 0.02 / 252
    excess = daily_returns - rf_daily
    sharpe = float(
        np.mean(excess) / np.std(excess) * np.sqrt(252)
        if np.std(excess) > 0
        else 0.0
    )

    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak * 100
    max_dd = float(np.min(drawdown))

    sell_trades = [t for t in trades if t.action == "SELL" and t.pnl is not None]
    winning = [t for t in sell_trades if t.pnl > 0]
    losing = [t for t in sell_trades if t.pnl <= 0]

    win_rate = len(winning) / len(sell_trades) * 100 if sell_trades else 0.0
    avg_win = float(np.mean([t.pnl_pct * 100 for t in winning])) if winning else 0.0
    avg_loss = float(np.mean([t.pnl_pct * 100 for t in losing])) if losing else 0.0

    gross_profit = sum(t.pnl for t in winning) if winning else 0.0
    gross_loss = abs(sum(t.pnl for t in losing)) if losing else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return BacktestMetrics(
        total_return_pct=total_return,
        annualized_return_pct=annualized,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd,
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        total_trades=len(sell_trades),
        winning_trades=len(winning),
        losing_trades=len(losing),
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
    )
