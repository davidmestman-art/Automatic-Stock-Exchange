import logging
from datetime import datetime
from typing import Dict

from ..data.fetcher import MarketDataFetcher
from ..signals.analyzer import SignalAnalyzer, SignalResult
from ..signals.indicators import TechnicalIndicators
from .executor import PaperExecutor
from .portfolio import Portfolio
from .risk import RiskManager

logger = logging.getLogger(__name__)

_SEP = "=" * 64


class TradingEngine:
    def __init__(self, config):
        self.config = config
        self.fetcher = MarketDataFetcher(
            lookback_days=config.lookback_days,
            interval=config.data_interval,
        )
        self.indicators = TechnicalIndicators(
            rsi_period=config.rsi_period,
            macd_fast=config.macd_fast,
            macd_slow=config.macd_slow,
            macd_signal=config.macd_signal,
            ema_fast=config.ema_fast,
            ema_slow=config.ema_slow,
            bb_period=config.bb_period,
            bb_std=config.bb_std,
        )
        self.analyzer = SignalAnalyzer(
            buy_threshold=config.buy_threshold,
            sell_threshold=config.sell_threshold,
        )
        self.risk = RiskManager(
            max_position_pct=config.max_position_pct,
            max_open_positions=config.max_open_positions,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            daily_loss_limit_pct=config.daily_loss_limit_pct,
        )
        self.portfolio = Portfolio(initial_capital=config.initial_capital)
        self.executor = PaperExecutor()
        self._cycle = 0

    def run_cycle(self) -> Dict[str, SignalResult]:
        self._cycle += 1
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"\n{_SEP}")
        logger.info(f"  Trading Cycle #{self._cycle}  —  {ts}")
        logger.info(_SEP)

        market_data = self.fetcher.fetch_many(self.config.symbols, force_refresh=True)
        if not market_data:
            logger.error("No market data returned — skipping cycle")
            return {}

        prices: Dict[str, float] = {}
        for symbol in market_data:
            p = self.fetcher.get_current_price(symbol)
            if p:
                prices[symbol] = p

        if self._cycle == 1:
            self.portfolio.update_day_start(prices)

        self._check_exit_conditions(prices)

        results: Dict[str, SignalResult] = {}

        for symbol in self.config.symbols:
            if symbol not in market_data or symbol not in prices:
                continue

            df = market_data[symbol]
            current_price = prices[symbol]

            ind = self.indicators.compute(df)
            ind.close = current_price  # use live price for signal generation

            signal = self.analyzer.analyze(ind)
            results[symbol] = signal

            rsi_str = f"RSI {ind.rsi:5.1f}" if ind.rsi else "RSI  n/a"
            logger.info(
                f"  {symbol:6s}  ${current_price:>9.2f}  "
                f"signal={signal.action:4s}  score={signal.score:+.3f}  "
                f"{rsi_str}  {'  '.join(signal.reasons[:2])}"
            )

            portfolio_value = self.portfolio.total_value_at(prices)
            daily_pnl = self.portfolio.daily_pnl_pct(prices)

            if signal.action == "BUY" and not self.portfolio.has_position(symbol):
                rc = self.risk.check_buy(
                    symbol=symbol,
                    price=current_price,
                    portfolio_value=portfolio_value,
                    cash=self.portfolio.cash,
                    open_positions=self.portfolio.open_position_count(),
                    daily_pnl_pct=daily_pnl,
                    signal_confidence=signal.confidence,
                )
                if rc.approved:
                    self.executor.execute_buy(
                        symbol=symbol,
                        shares=rc.max_shares,
                        price=current_price,
                        stop_loss=self.risk.stop_loss_price(current_price),
                        take_profit=self.risk.take_profit_price(current_price),
                        reason=", ".join(signal.reasons[:2]),
                        portfolio=self.portfolio,
                    )
                else:
                    logger.debug(f"  Risk rejected {symbol}: {rc.reason}")

            elif signal.action == "SELL" and self.portfolio.has_position(symbol):
                self.executor.execute_sell(
                    symbol=symbol,
                    price=current_price,
                    reason=f"Signal: {', '.join(signal.reasons[:2])}",
                    portfolio=self.portfolio,
                )

        summary = self.portfolio.get_summary(prices)
        logger.info(f"\n  Portfolio Summary")
        logger.info(f"  {'Total Value':15s} ${summary['total_value']:>12,.2f}")
        logger.info(f"  {'Cash':15s} ${summary['cash']:>12,.2f}")
        logger.info(
            f"  {'Positions':15s} ${summary['position_value']:>12,.2f}"
            f"  ({summary['open_positions']} open)"
        )
        sign = "+" if summary["total_pnl"] >= 0 else ""
        logger.info(
            f"  {'Total P&L':15s} {sign}${summary['total_pnl']:>11,.2f}"
            f"  ({sign}{summary['total_pnl_pct']:.2f}%)"
        )
        logger.info(_SEP)

        return results

    def _check_exit_conditions(self, prices: Dict[str, float]):
        exits = []
        for symbol, pos in self.portfolio.positions.items():
            price = prices.get(symbol, pos.entry_price)
            if self.risk.check_stop_loss(pos.entry_price, price):
                exits.append((symbol, price, "Stop loss triggered"))
            elif self.risk.check_take_profit(pos.entry_price, price):
                exits.append((symbol, price, "Take profit triggered"))

        for symbol, price, reason in exits:
            self.executor.execute_sell(symbol, price, reason, self.portfolio)
