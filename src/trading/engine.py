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

_REGIME_SIZE_MULT = {"BULL": 1.0, "CHOPPY": 0.70, "BEAR": 0.35}

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
            use_mean_reversion=config.use_mean_reversion,
            use_momentum=getattr(config, "use_momentum_signals", True),
        )
        self.risk = RiskManager(
            max_position_pct=config.max_position_pct,
            max_open_positions=config.max_open_positions,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            daily_loss_limit_pct=config.daily_loss_limit_pct,
            max_positions_per_sector=config.max_positions_per_sector,
            use_trailing_stop=config.use_trailing_stop,
            trailing_stop_pct=config.trailing_stop_pct,
        )
        self.risk.use_adaptive_sizing = config.use_adaptive_sizing
        self.risk.adaptive_target_vol_pct = config.adaptive_target_vol_pct
        self.risk.min_position_pct = config.min_position_pct

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
                min_agreeing=getattr(config, "mtf_min_agreeing", 2),
            )

        # Market regime detector
        self._regime_detector = None
        self._regime_result = None
        if getattr(config, "use_regime_detection", False):
            from ..data.market_regime import RegimeDetector
            self._regime_detector = RegimeDetector(
                bull_vix_max=getattr(config, "regime_bull_vix_max", 25.0),
                bear_vix_min=getattr(config, "regime_bear_vix_min", 27.0),
            )

        # ML signal ranker
        self._signal_ranker = None
        if getattr(config, "use_ml_ranking", False):
            from ..ml.signal_ranker import SignalRanker
            self._signal_ranker = SignalRanker(
                min_samples=getattr(config, "ml_min_samples", 20)
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
        self._voo_alert_sent_date: Optional[str] = None  # send at most one VOO alert per day
        self.watchlist: List[str] = list(config.symbols)
        # BUY signals waiting for next-candle confirmation (symbol → {signal_price, queued_at})
        self._pending_signals: Dict[str, dict] = {}
        # Symbols blocked by correlation filter in the most recent signal pass
        self._last_corr_blocked: Dict[str, str] = {}

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

        # Refresh market regime (cached; fetches at most once every 4 h)
        if self._regime_detector is not None:
            try:
                self._regime_result = self._regime_detector.detect()
            except Exception as e:
                logger.warning(f"  Regime detection failed: {e}")

        # VOO monitor — cached daily; send alert notification at most once per day
        voo = self.voo_monitor.check()
        if voo and voo.alert:
            today = datetime.now().strftime("%Y-%m-%d")
            if self._voo_alert_sent_date != today:
                self.notifier.voo_alert(voo.price, voo.ma200w, voo.gap_pct)
                self._voo_alert_sent_date = today

        if self._use_alpaca:
            self.executor.sync_portfolio(self.portfolio, risk_mgr=self.risk)

        market_data = self.fetcher.fetch_many(self.watchlist, force_refresh=True)
        if not market_data:
            logger.error("No market data returned — skipping cycle")
            return {}

        # Ensure position data is available for correlation filter
        if self.config.use_correlation_filter and self.portfolio.positions:
            pos_syms = [s for s in self.portfolio.positions if s not in market_data]
            if pos_syms:
                extra = self.fetcher.fetch_many(pos_syms, force_refresh=False)
                market_data.update(extra)

        self._last_corr_blocked = self._compute_corr_blocks(market_data)

        prices = self._get_prices(list(market_data.keys()))

        if self._cycle == 1:
            self.portfolio.update_day_start(prices)

        # Trailing stop updates + exit checks run in all modes
        self._check_exit_conditions(prices)

        # Process confirmations queued in the previous cycle
        _confirmed_buys: set = set()
        if self.config.use_confirmation and self._pending_signals:
            tol = self.config.confirmation_tolerance_pct
            for sym, info in list(self._pending_signals.items()):
                if sym in prices:
                    cp = prices[sym]
                    floor = info["signal_price"] * (1 - tol)
                    if cp >= floor:
                        _confirmed_buys.add(sym)
                        logger.info(f"  Confirmation PASSED {sym}: ${cp:.2f} ≥ ${floor:.2f}")
                    else:
                        logger.info(f"  Confirmation FAILED {sym}: ${cp:.2f} dropped below ${floor:.2f}")
            self._pending_signals.clear()

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

                # Correlation filter
                if symbol in self._last_corr_blocked:
                    logger.info(
                        f"  Correlation blocked {symbol}: {self._last_corr_blocked[symbol]}"
                    )
                    continue

                # In BEAR regime, only trade if signal is strong enough
                if self._regime_result is not None and self._regime_result.regime == "BEAR":
                    bear_min = self.config.buy_threshold * getattr(
                        self.config, "regime_bear_min_score_mult", 1.8
                    )
                    if signal.score < bear_min:
                        logger.info(
                            f"  BEAR regime: skipping BUY {symbol} "
                            f"(score={signal.score:.3f} < {bear_min:.3f})"
                        )
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
                    atr_pct=ind.atr_pct,
                )
                if rc.approved:
                    # Apply regime-based position-size multiplier
                    if self._regime_result is not None:
                        cfg = self.config
                        mult_key = f"regime_size_mult_{self._regime_result.regime.lower()}"
                        regime_mult = getattr(cfg, mult_key, _REGIME_SIZE_MULT.get(
                            self._regime_result.regime, 1.0
                        ))
                        rc.max_shares = rc.max_shares * regime_mult
                    if self.config.use_confirmation and symbol not in _confirmed_buys:
                        # Queue for next-candle confirmation
                        self._pending_signals[symbol] = {
                            "signal_price": current_price,
                            "queued_at": datetime.now().isoformat(),
                        }
                        logger.info(f"  {symbol}: BUY queued for confirmation @ ${current_price:.2f}")
                    else:
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

    @property
    def pending_confirmations(self) -> Dict[str, dict]:
        """BUY signals queued for next-candle confirmation."""
        return dict(self._pending_signals)

    def get_signals(self):
        """Fetch data and compute signals without placing any orders."""
        symbols = self.watchlist or self.config.symbols
        market_data = self.fetcher.fetch_many(symbols, force_refresh=True)

        # Fetch position data needed for correlation filter (may not be on watchlist)
        if self.config.use_correlation_filter and self.portfolio.positions:
            pos_syms = [s for s in self.portfolio.positions if s not in market_data]
            if pos_syms:
                extra = self.fetcher.fetch_many(pos_syms, force_refresh=False)
                market_data.update(extra)

        prices = self._get_prices(list(market_data.keys()))

        # Update correlation state so dashboard always reflects current positions
        self._last_corr_blocked = self._compute_corr_blocks(market_data)

        # Refresh regime (cached)
        if self._regime_detector is not None:
            try:
                self._regime_result = self._regime_detector.detect()
            except Exception as e:
                logger.warning(f"  Regime detection failed: {e}")

        signals, ind_map = {}, {}
        for symbol in symbols:
            if symbol not in market_data or symbol not in prices:
                continue
            ind = self.indicators.compute(market_data[symbol])
            ind.close = prices.get(symbol, ind.close)
            signals[symbol] = self._compute_signal(symbol, ind)
            ind_map[symbol] = ind
        return signals, prices, ind_map

    @property
    def current_regime(self):
        """Most recently detected market regime result, or None."""
        return self._regime_result

    @property
    def ml_status(self) -> dict:
        """Current ML ranker status dict."""
        if self._signal_ranker is None:
            return {"trained": False, "samples": 0, "accuracy": None,
                    "last_trained": None, "sklearn_available": False}
        return self._signal_ranker.status()

    def refresh_watchlist(self) -> List[str]:
        """Force an immediate session scan and return the new watchlist."""
        result = self.scanner.scan(self.indicators, self.analyzer, force=True)
        self.watchlist = result.watchlist
        self._session_date = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"Watchlist force-refreshed: {self.watchlist}")
        return self.watchlist

    # ── Signal computation ────────────────────────────────────────────────────

    def _compute_signal(self, symbol: str, ind) -> SignalResult:
        """Return a signal, optionally blending multi-timeframe scores and ML ranking."""
        base_signal = self.analyzer.analyze(ind)

        if self._mtf_analyzer is not None:
            mtf = self._mtf_analyzer.analyze(symbol)
            if mtf is not None:
                action = mtf.action
                score = mtf.composite
                confidence = mtf.confidence
                reasons = [
                    f"MTF 1d={mtf.score_1d:+.3f} 1h={mtf.score_1h:+.3f} "
                    f"15m={mtf.score_15m:+.3f} agree={mtf.agreement}/3",
                    *base_signal.reasons[:2],
                ]
                indicator_scores = {
                    "1d": mtf.score_1d,
                    "1h": mtf.score_1h,
                    "15m": mtf.score_15m,
                    "mtf_agreement": float(mtf.agreement),
                    **base_signal.indicator_scores,
                }
                base_signal = SignalResult(
                    action=action,
                    score=score,
                    confidence=confidence,
                    reasons=reasons,
                    indicator_scores=indicator_scores,
                )

        # ML score adjustment — applied last as a final multiplier
        ml_mult = 1.0
        if self._signal_ranker is not None and self._signal_ranker.is_trained:
            snap = self._indicator_snapshot(ind, base_signal)
            ml_mult = self._signal_ranker.score_adjustment(snap)
            adjusted_score = max(-1.0, min(1.0, base_signal.score * ml_mult))
            buy_th = self.config.buy_threshold
            sell_th = self.config.sell_threshold
            action = (
                "BUY" if adjusted_score >= buy_th
                else "SELL" if adjusted_score <= sell_th
                else "HOLD"
            )
            base_signal = SignalResult(
                action=action,
                score=round(adjusted_score, 4),
                confidence=round(abs(adjusted_score), 4),
                reasons=base_signal.reasons,
                indicator_scores={**base_signal.indicator_scores, "ml_mult": ml_mult},
            )

        return base_signal

    @staticmethod
    def _indicator_snapshot(ind, signal: SignalResult) -> dict:
        return {
            "rsi": round(ind.rsi, 2) if ind.rsi is not None else None,
            "macd_hist": round(ind.macd_hist, 4) if ind.macd_hist is not None else None,
            "ema_fast": round(ind.ema_fast, 2) if ind.ema_fast is not None else None,
            "ema_slow": round(ind.ema_slow, 2) if ind.ema_slow is not None else None,
            "z_score": round(ind.z_score, 4) if getattr(ind, "z_score", None) is not None else None,
            "atr_pct": round(ind.atr_pct, 4) if getattr(ind, "atr_pct", None) is not None else None,
            "roc_10": round(ind.roc_10, 4) if getattr(ind, "roc_10", None) is not None else None,
            "stoch_rsi": round(ind.stoch_rsi, 2) if getattr(ind, "stoch_rsi", None) is not None else None,
            "score": round(signal.score, 4),
            "confidence": round(signal.confidence, 4),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _maybe_refresh_watchlist(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._session_date == today:
            return
        if self._pending_signals:
            logger.info(f"  New session — clearing {len(self._pending_signals)} stale pending signals")
            self._pending_signals.clear()
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

        # Attempt ML model (re-)training at the start of each new session
        if self._signal_ranker is not None:
            try:
                self._signal_ranker.maybe_train(self.journal)
            except Exception as e:
                logger.warning(f"  ML training failed: {e}")

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

    @property
    def last_corr_blocked(self) -> Dict[str, str]:
        """Symbols blocked by correlation filter in the last signal pass {sym: reason}."""
        return dict(self._last_corr_blocked)

    def _compute_corr_blocks(self, market_data: dict) -> Dict[str, str]:
        """Return {symbol: reason} for watchlist symbols too correlated with open positions."""
        import pandas as pd  # already imported at module level via fetcher; local import is fine
        if not self.config.use_correlation_filter or not self.portfolio.positions:
            return {}

        lookback = self.config.correlation_lookback
        thresh = self.config.correlation_threshold
        blocked: Dict[str, str] = {}

        for sym in self.watchlist:
            if sym in self.portfolio.positions or sym not in market_data:
                continue
            target_ret = market_data[sym]["Close"].pct_change().dropna().tail(lookback)
            if len(target_ret) < lookback // 2:
                continue
            for pos_sym in self.portfolio.positions:
                if pos_sym not in market_data:
                    continue
                pos_ret = market_data[pos_sym]["Close"].pct_change().dropna().tail(lookback)
                aligned = pd.concat([target_ret, pos_ret], axis=1).dropna()
                if len(aligned) < lookback // 2:
                    continue
                corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
                if corr >= thresh:
                    blocked[sym] = f"{pos_sym} ρ={corr:.2f}"
                    break

        if blocked:
            logger.info(f"  Correlation filter blocked: {', '.join(f'{s}({r})' for s,r in blocked.items())}")
        return blocked

    def _check_exit_conditions(self, prices: Dict[str, float]) -> None:
        exits = []
        for symbol, pos in self.portfolio.positions.items():
            price = prices.get(symbol, pos.entry_price)
            self.risk.update_trailing_stop(pos, price)
            stop_reason = (
                "Trailing stop triggered" if self.risk.use_trailing_stop
                else "Stop loss triggered"
            )
            if self.risk.check_stop_loss(pos.entry_price, price, pos):
                exits.append((symbol, price, stop_reason))
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
