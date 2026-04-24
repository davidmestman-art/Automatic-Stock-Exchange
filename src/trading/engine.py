import logging
from datetime import datetime
from typing import Dict, List, Optional, Union

from ..data.earnings import EarningsCalendar
from ..data.fetcher import MarketDataFetcher
from ..data.scanner import StockScanner
from ..data.voo_monitor import VOOMonitor
from ..signals.analyzer import SignalAnalyzer, SignalResult
from ..signals.indicators import TechnicalIndicators
from ..utils.journal import TradeJournal
from ..utils.notifications import Notifier
from ..utils.sectors import sector_position_count
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
            max_positions_per_sector=config.max_positions_per_sector,
        )

        # Optional components
        fundamental_filter = None
        if config.use_fundamental_filter:
            from ..data.fundamentals import FundamentalFilter
            fundamental_filter = FundamentalFilter(
                pe_max=config.fundamental_pe_max,
                de_max=config.fundamental_de_max,
            )

        earnings_cal = None
        if config.use_earnings_protection:
            earnings_cal = EarningsCalendar(buffer_days=config.earnings_buffer_days)
        self.earnings_cal = earnings_cal

        self.scanner = StockScanner(
            universe=config.sp500_universe,
            volume_top_n=config.universe_size,
            signal_top_n=config.watchlist_size,
            lookback_days=config.lookback_days,
            fundamental_filter=fundamental_filter,
            earnings_calendar=earnings_cal,
        )
        self.portfolio = Portfolio(initial_capital=config.initial_capital)

        self.voo_monitor = VOOMonitor(alert_threshold_pct=config.voo_alert_threshold_pct)
        self.notifier = Notifier(
            ntfy_topic=config.ntfy_topic,
            pushover_token=config.pushover_token,
            pushover_user=config.pushover_user,
        )
        self.journal = TradeJournal()

        # Multi-timeframe analyzer (created lazily if enabled)
        self._mtf_analyzer = None
        if config.use_multi_timeframe:
            from ..data.multi_timeframe import MultiTimeframeAnalyzer
            self._mtf_analyzer = MultiTimeframeAnalyzer(
                indicators=self.indicators,
                analyzer=self.analyzer,
                buy_threshold=config.buy_threshold,
                sell_threshold=config.sell_threshold,
            )

        if config.use_alpaca:
            from .alpaca_executor import AlpacaExecutor
            self.executor: Union[AlpacaExecutor, PaperExecutor] = AlpacaExecutor(
                api_key=config.alpaca_api_key,
                secret_key=config.alpaca_secret_key,
                paper=config.paper_trading,
            )
            mode = "Alpaca Paper" if config.paper_trading else "Alpaca LIVE"
        else:
            self.executor = PaperExecutor()
            mode = "Local Simulation"

        self._use_alpaca = config.use_alpaca
        self._cycle = 0
        self._session_date: Optional[str] = None
        self.watchlist: List[str] = list(config.symbols)

        logger.info(f"TradingEngine initialised  [mode={mode}]")

    # ── Public API ────────────────────────────────────────────────────────────

    def run_cycle(self) -> Dict[str, SignalResult]:
        self._cycle += 1
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"\n{_SEP}")
        logger.info(f"  Trading Cycle #{self._cycle}  —  {ts}")
        logger.info(_SEP)

        if self._use_alpaca and not self._market_is_open():
            return {}

        self._maybe_refresh_watchlist()

        # VOO monitor — run once per cycle, send alert if near/crossing 200W MA
        voo = self.voo_monitor.check()
        if voo and voo.alert:
            self.notifier.voo_alert(voo.price, voo.ma200w, voo.gap_pct)

        if self._use_alpaca:
            self.executor.sync_portfolio(self.portfolio, risk_mgr=self.risk)

        market_data = self.fetcher.fetch_many(self.watchlist, force_refresh=True)
        if not market_data:
            logger.error("No market data returned — skipping cycle")
            return {}

        prices = self._get_prices(list(market_data.keys()))

        if self._cycle == 1:
            self.portfolio.update_day_start(prices)

        if not self._use_alpaca:
            self._check_exit_conditions(prices)

        results: Dict[str, SignalResult] = {}

        for symbol in self.watchlist:
            if symbol not in market_data or symbol not in prices:
                continue

            df = market_data[symbol]
            current_price = prices[symbol]

            ind = self.indicators.compute(df)
            ind.close = current_price

            signal = self._compute_signal(symbol, ind)
            results[symbol] = signal

            rsi_str = f"RSI {ind.rsi:5.1f}" if ind.rsi else "RSI  n/a"
            logger.info(
                f"  {symbol:6s}  ${current_price:>9.2f}  "
                f"signal={signal.action:4s}  score={signal.score:+.3f}  "
                f"{rsi_str}  {'  '.join(signal.reasons[:2])}"
            )

            portfolio_value = self.portfolio.total_value_at(prices)
            daily_pnl = self.portfolio.daily_pnl_pct(prices)

            ind_snap = self._indicator_snapshot(ind, signal)

            if signal.action == "BUY" and not self.portfolio.has_position(symbol):
                # Earnings protection (also applied in scanner, but double-check at execution)
                if self.earnings_cal and self.earnings_cal.has_upcoming_earnings(symbol):
                    logger.debug(f"  Earnings protection: skipping BUY {symbol}")
                    continue

                rc = self.risk.check_buy(
                    symbol=symbol,
                    price=current_price,
                    portfolio_value=portfolio_value,
                    cash=self.portfolio.cash,
                    open_positions=self.portfolio.open_position_count(),
                    daily_pnl_pct=daily_pnl,
                    signal_confidence=signal.confidence,
                    sector_positions=sector_position_count(symbol, self.portfolio.positions),
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
                    self.notifier.trade_buy(
                        symbol, rc.max_shares, current_price, ", ".join(signal.reasons[:2])
                    )
                    self.journal.log(
                        action="BUY",
                        symbol=symbol,
                        shares=rc.max_shares,
                        price=current_price,
                        reason=", ".join(signal.reasons[:2]),
                        indicators=ind_snap,
                    )
                else:
                    logger.debug(f"  Risk rejected {symbol}: {rc.reason}")

            elif signal.action == "SELL" and self.portfolio.has_position(symbol):
                pos = self.portfolio.positions.get(symbol)
                self.executor.execute_sell(
                    symbol=symbol,
                    price=current_price,
                    reason=f"Signal: {', '.join(signal.reasons[:2])}",
                    portfolio=self.portfolio,
                )
                if pos:
                    pnl = (current_price - pos.entry_price) * pos.shares
                    self.notifier.trade_sell(
                        symbol, pos.shares, current_price, pnl,
                        f"Signal: {', '.join(signal.reasons[:2])}"
                    )
                    self.journal.log(
                        action="SELL",
                        symbol=symbol,
                        shares=pos.shares,
                        price=current_price,
                        reason=f"Signal: {', '.join(signal.reasons[:2])}",
                        indicators=ind_snap,
                        pnl=pnl,
                        pnl_pct=(current_price - pos.entry_price) / pos.entry_price,
                    )

        self._log_summary(prices)
        return results

    def get_signals(self):
        """Fetch data and compute signals without placing any orders."""
        symbols = self.watchlist or self.config.symbols
        market_data = self.fetcher.fetch_many(symbols, force_refresh=True)
        prices = self._get_prices(list(market_data.keys()))
        signals, ind_map = {}, {}
        for symbol in symbols:
            if symbol not in market_data or symbol not in prices:
                continue
            ind = self.indicators.compute(market_data[symbol])
            ind.close = prices.get(symbol, ind.close)
            signals[symbol] = self._compute_signal(symbol, ind)
            ind_map[symbol] = ind
        return signals, prices, ind_map

    def refresh_watchlist(self) -> List[str]:
        """Force an immediate session scan and return the new watchlist."""
        result = self.scanner.scan(self.indicators, self.analyzer, force=True)
        self.watchlist = result.watchlist
        self._session_date = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"Watchlist force-refreshed: {self.watchlist}")
        return self.watchlist

    # ── Signal computation ────────────────────────────────────────────────────

    def _compute_signal(self, symbol: str, ind) -> SignalResult:
        """Return a signal, optionally blending multi-timeframe scores."""
        base_signal = self.analyzer.analyze(ind)
        if self._mtf_analyzer is None:
            return base_signal

        mtf = self._mtf_analyzer.analyze(symbol)
        if mtf is None:
            return base_signal

        # Blend: MTF composite replaces the score/action; keep 1d reasons for context
        action = mtf.action
        score = mtf.composite
        confidence = mtf.confidence
        reasons = [
            f"MTF 1d={mtf.score_1d:+.3f} 1h={mtf.score_1h:+.3f} 15m={mtf.score_15m:+.3f}",
            *base_signal.reasons[:2],
        ]
        return SignalResult(
            action=action,
            score=score,
            confidence=confidence,
            reasons=reasons,
            indicator_scores={
                "1d": mtf.score_1d,
                "1h": mtf.score_1h,
                "15m": mtf.score_15m,
                **base_signal.indicator_scores,
            },
        )

    @staticmethod
    def _indicator_snapshot(ind, signal: SignalResult) -> dict:
        return {
            "rsi": round(ind.rsi, 2) if ind.rsi is not None else None,
            "macd_hist": round(ind.macd_hist, 4) if ind.macd_hist is not None else None,
            "ema_fast": round(ind.ema_fast, 2) if ind.ema_fast is not None else None,
            "ema_slow": round(ind.ema_slow, 2) if ind.ema_slow is not None else None,
            "score": round(signal.score, 4),
            "confidence": round(signal.confidence, 4),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _maybe_refresh_watchlist(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._session_date == today:
            return
        logger.info(f"  New session ({today}) — running watchlist scan…")
        try:
            result = self.scanner.scan(self.indicators, self.analyzer)
            if result.watchlist:
                self.watchlist = result.watchlist
                self._session_date = today
                logger.info(f"  Watchlist set to: {self.watchlist}")
            else:
                logger.warning("  Scan returned empty watchlist — keeping previous")
        except Exception as e:
            logger.error(f"  Watchlist scan failed: {e} — keeping previous")

    def _market_is_open(self) -> bool:
        clock = self.executor.get_clock_info()
        if clock["is_open"]:
            return True
        logger.warning(f"  Market is CLOSED  (next open: {clock['next_open']})")
        return False

    def _get_prices(self, symbols: list) -> Dict[str, float]:
        if self._use_alpaca:
            prices = self.executor.get_live_prices(symbols)
            if prices:
                return prices
            logger.warning("Alpaca quotes unavailable — falling back to yfinance")
        prices: Dict[str, float] = {}
        for symbol in symbols:
            p = self.fetcher.get_current_price(symbol)
            if p:
                prices[symbol] = p
        return prices

    def _check_exit_conditions(self, prices: Dict[str, float]) -> None:
        exits = []
        for symbol, pos in self.portfolio.positions.items():
            price = prices.get(symbol, pos.entry_price)
            if self.risk.check_stop_loss(pos.entry_price, price):
                exits.append((symbol, price, "Stop loss triggered"))
            elif self.risk.check_take_profit(pos.entry_price, price):
                exits.append((symbol, price, "Take profit triggered"))
        for symbol, price, reason in exits:
            pos = self.portfolio.positions.get(symbol)
            self.executor.execute_sell(symbol, price, reason, self.portfolio)
            if pos:
                pnl = (price - pos.entry_price) * pos.shares
                self.notifier.trade_sell(symbol, pos.shares, price, pnl, reason)
                self.journal.log(
                    action="SELL",
                    symbol=symbol,
                    shares=pos.shares,
                    price=price,
                    reason=reason,
                    pnl=pnl,
                    pnl_pct=(price - pos.entry_price) / pos.entry_price,
                )

    def _log_summary(self, prices: Dict[str, float]) -> None:
        s = self.portfolio.get_summary(prices)
        sign = "+" if s["total_pnl"] >= 0 else ""
        logger.info(f"\n  Portfolio Summary")
        logger.info(f"  {'Total Value':15s} ${s['total_value']:>12,.2f}")
        logger.info(f"  {'Cash':15s} ${s['cash']:>12,.2f}")
        logger.info(
            f"  {'Positions':15s} ${s['position_value']:>12,.2f}"
            f"  ({s['open_positions']} open)"
        )
        logger.info(
            f"  {'Total P&L':15s} {sign}${s['total_pnl']:>11,.2f}"
            f"  ({sign}{s['total_pnl_pct']:.2f}%)"
        )
        logger.info(_SEP)
