import logging
from typing import Dict, List

import pandas as pd

from ..data.fetcher import MarketDataFetcher
from ..signals.analyzer import SignalAnalyzer
from ..signals.indicators import TechnicalIndicators
from ..trading.portfolio import Portfolio
from ..trading.risk import RiskManager
from .metrics import BacktestMetrics, compute_metrics

logger = logging.getLogger(__name__)


class Backtester:
    def __init__(self, config):
        self.config = config

    def run(self, symbols: List[str], start_date: str, end_date: str) -> BacktestMetrics:
        logger.info(f"Backtest  {start_date} → {end_date}  symbols={symbols}")

        span_days = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days
        fetcher = MarketDataFetcher(lookback_days=span_days + 120, interval="1d")

        indicators = TechnicalIndicators(
            rsi_period=self.config.rsi_period,
            macd_fast=self.config.macd_fast,
            macd_slow=self.config.macd_slow,
            macd_signal=self.config.macd_signal,
            ema_fast=self.config.ema_fast,
            ema_slow=self.config.ema_slow,
            bb_period=self.config.bb_period,
            bb_std=self.config.bb_std,
        )
        analyzer = SignalAnalyzer(
            buy_threshold=self.config.buy_threshold,
            sell_threshold=self.config.sell_threshold,
        )
        risk = RiskManager(
            max_position_pct=self.config.max_position_pct,
            max_open_positions=self.config.max_open_positions,
            stop_loss_pct=self.config.stop_loss_pct,
            take_profit_pct=self.config.take_profit_pct,
            daily_loss_limit_pct=self.config.daily_loss_limit_pct,
        )
        portfolio = Portfolio(initial_capital=self.config.initial_capital)

        all_data: Dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            df = fetcher.fetch(symbol)
            if df is not None and not df.empty:
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                all_data[symbol] = df

        if not all_data:
            logger.error("No historical data available — aborting backtest")
            return compute_metrics([], [], 0)

        first_df = next(iter(all_data.values()))
        trading_days = first_df.index[
            (first_df.index >= pd.Timestamp(start_date))
            & (first_df.index <= pd.Timestamp(end_date))
        ]

        warm_up = max(
            self.config.ema_slow, self.config.macd_slow, self.config.bb_period
        ) + 5
        equity_curve = [self.config.initial_capital]

        for date in trading_days:
            prices: Dict[str, float] = {}
            for symbol, df in all_data.items():
                hist = df[df.index <= date]
                if len(hist) >= warm_up:
                    prices[symbol] = float(hist["Close"].iloc[-1])

            if not prices:
                continue

            # Stop-loss / take-profit exits
            for symbol in list(portfolio.positions.keys()):
                if symbol not in prices:
                    continue
                pos = portfolio.positions[symbol]
                p = prices[symbol]
                if risk.check_stop_loss(pos.entry_price, p):
                    portfolio.sell(symbol, p, "Stop loss")
                elif risk.check_take_profit(pos.entry_price, p):
                    portfolio.sell(symbol, p, "Take profit")

            # Signal-driven entries / exits
            for symbol, df in all_data.items():
                if symbol not in prices:
                    continue
                hist = df[df.index <= date]
                if len(hist) < warm_up:
                    continue

                ind = indicators.compute(hist)
                signal = analyzer.analyze(ind)

                port_val = (
                    sum(
                        portfolio.positions[s].current_value(
                            prices.get(s, portfolio.positions[s].entry_price)
                        )
                        for s in portfolio.positions
                    )
                    + portfolio.cash
                )

                if signal.action == "BUY" and not portfolio.has_position(symbol):
                    daily_pnl = (
                        (port_val - equity_curve[-1]) / equity_curve[-1]
                        if equity_curve
                        else 0.0
                    )
                    rc = risk.check_buy(
                        symbol=symbol,
                        price=prices[symbol],
                        portfolio_value=port_val,
                        cash=portfolio.cash,
                        open_positions=portfolio.open_position_count(),
                        daily_pnl_pct=daily_pnl,
                        signal_confidence=signal.confidence,
                    )
                    if rc.approved:
                        sl = risk.stop_loss_price(prices[symbol])
                        tp = risk.take_profit_price(prices[symbol])
                        portfolio.buy(symbol, rc.max_shares, prices[symbol], sl, tp, "Signal")

                elif signal.action == "SELL" and portfolio.has_position(symbol):
                    portfolio.sell(symbol, prices[symbol], "Signal sell")

            # Snapshot portfolio value
            port_val = (
                sum(
                    portfolio.positions[s].current_value(
                        prices.get(s, portfolio.positions[s].entry_price)
                    )
                    for s in portfolio.positions
                )
                + portfolio.cash
            )
            equity_curve.append(port_val)

        days = len(trading_days)
        metrics = compute_metrics(equity_curve, portfolio.trades, days)
        logger.info(f"\n{metrics}")
        return metrics
