import logging
from datetime import datetime
from typing import Dict, List, Optional, Set, Union

from ..data.earnings import EarningsCalendar
from ..data.fetcher import MarketDataFetcher
from ..data.scanner import StockScanner
from ..data.universe import DynamicUniverse
from ..data.voo_monitor import VOOMonitor
from ..signals.analyzer import SignalAnalyzer, SignalResult
from ..signals.indicators import TechnicalIndicators
from ..utils.emailer import TradeEmailer
from ..utils.journal import TradeJournal
from ..utils.notifications import Notifier
from ..utils.sectors import SECTOR_ETFS, get_sector, sector_position_count
from .executor import PaperExecutor
from .orb import (
    ORBSession,
    fetch_gap_pcts,
    fetch_opening_range_bars,
    fetch_prev_day_levels,
    fetch_latest_1min_volume,
    screen_orb_universe,
)
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
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
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
            max_sector_exposure_pct=getattr(config, "max_sector_exposure_pct", 0.30),
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

        self.voo_monitor = VOOMonitor(
            alert_threshold_pct=config.voo_alert_threshold_pct,
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
        )
        self.notifier = Notifier(
            ntfy_topic=config.ntfy_topic,
            pushover_token=config.pushover_token,
            pushover_user=config.pushover_user,
        )
        self.journal = TradeJournal()
        self.emailer = TradeEmailer.from_env()

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
                api_key=config.alpaca_api_key,
                secret_key=config.alpaca_secret_key,
            )

        # Market regime detector
        self._regime_detector = None
        self._regime_result = None
        if getattr(config, "use_regime_detection", False):
            from ..data.market_regime import RegimeDetector
            self._regime_detector = RegimeDetector(
                bull_vix_max=getattr(config, "regime_bull_vix_max", 25.0),
                bear_vix_min=getattr(config, "regime_bear_vix_min", 27.0),
                api_key=config.alpaca_api_key,
                secret_key=config.alpaca_secret_key,
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
            try:
                self.executor: Union[AlpacaExecutor, PaperExecutor] = AlpacaExecutor(
                    api_key=config.alpaca_api_key,
                    secret_key=config.alpaca_secret_key,
                    paper=config.paper_trading,
                )
                mode = "Alpaca Paper" if config.paper_trading else "Alpaca LIVE"
            except Exception as exc:
                logger.warning(f"[ENGINE] AlpacaExecutor init failed ({exc}) — falling back to PaperExecutor")
                self.executor = PaperExecutor()
                config.use_alpaca = False
                mode = "Local Simulation"
        else:
            self.executor = PaperExecutor()
            mode = "Local Simulation"

        self._use_alpaca = config.use_alpaca
        self._cycle = 0
        self._session_date: Optional[str] = None
        self._voo_alert_sent_date: Optional[str] = None  # send at most one VOO alert per day
        self.watchlist: List[str] = list(config.symbols)
        use_alpaca_uni = getattr(config, "universe_use_alpaca", True)
        self.dynamic_universe = DynamicUniverse(
            min_avg_volume    = getattr(config, "universe_min_avg_volume", 500_000),
            min_price         = getattr(config, "universe_min_price",      10.0),
            max_price         = getattr(config, "universe_max_price",      1_000.0),
            min_market_cap    = getattr(config, "universe_min_market_cap", 200_000_000_000.0),
            include_etfs      = getattr(config, "universe_include_etfs",   True),
            alpaca_api_key    = config.alpaca_api_key    if use_alpaca_uni else "",
            alpaca_secret_key = config.alpaca_secret_key if use_alpaca_uni else "",
            paper_trading     = config.paper_trading,
        )
        # Seed watchlist from cached universe immediately so first page load
        # never shows an empty/hardcoded list.
        _cached_uni = self.dynamic_universe.tickers
        if _cached_uni:
            self.watchlist = list(_cached_uni)
            self.scanner.universe     = list(dict.fromkeys(_cached_uni))
            self.scanner.volume_top_n = len(_cached_uni)
        elif not self.watchlist:
            # No cache and no config symbols — trigger a background scan
            import threading as _th
            _th.Thread(target=self.dynamic_universe.run, daemon=True).start()
        # Sector ETF 5-day returns — refreshed once per calendar day
        self._sector_returns: Dict[str, float] = {}
        self._sector_returns_date: Optional[str] = None
        # BUY signals waiting for next-candle confirmation (symbol → {signal_price, queued_at})
        self._pending_signals: Dict[str, dict] = {}
        # Symbols blocked by correlation filter in the most recent signal pass
        self._last_corr_blocked: Dict[str, str] = {}
        # 3-day rebuy cooldown after selling: symbol → datetime of last sell
        self._rebuy_cooldowns: Dict[str, datetime] = {}
        # Symbols with an in-flight order (duplicate guard)
        self._order_in_flight: Set[str] = set()
        # ── ORB strategy session state ─────────────────────────────────────────
        self._orb_session = ORBSession()

        logger.info(f"TradingEngine initialised  [mode={mode}]")

    # ── Public API ────────────────────────────────────────────────────────────

    def run_cycle(self) -> Dict[str, SignalResult]:
        self._cycle += 1
        now_et  = self._get_et_time()
        ts      = now_et.strftime("%Y-%m-%d %H:%M:%S ET")
        et_mins = now_et.hour * 60 + now_et.minute
        today   = now_et.strftime("%Y-%m-%d")

        logger.info(f"\n{_SEP}")
        logger.info(f"  Trading Cycle #{self._cycle}  —  {ts}")
        logger.info(_SEP)

        # Reset ORB session at the start of each new calendar day
        if self._orb_session.session_date != today:
            self._orb_session.reset(today)

        # ── Phase 1: Pre-market universe scan (9:15–9:29 ET) ─────────────────
        if 9 * 60 + 15 <= et_mins < 9 * 60 + 30:
            if not self._orb_session.screened:
                self._orb_do_scan()
            logger.info(
                f"  [ORB] PRE-MARKET scan done — "
                f"{len(self._orb_session.states)} stocks queued for OR tracking"
            )
            return {}

        # ── Phase 2: Opening range formation (9:30–9:59 ET) — no trading ─────
        if 9 * 60 + 30 <= et_mins < 10 * 60:
            self._orb_session.phase = "FORMING"
            if self._orb_session.screened:
                self._orb_update_ranges()
            elapsed   = et_mins - 9 * 60 - 30
            remaining = 30 - elapsed
            valid     = sum(1 for s in self._orb_session.states.values() if s.or_high)
            logger.info(
                f"  [ORB] FORMING — {elapsed}m elapsed / {remaining}m left "
                f"— {valid} ranges active"
            )
            if self._use_alpaca:
                self.executor.sync_portfolio(self.portfolio, risk_mgr=self.risk)
            prices = self._get_prices(list(self._orb_session.states.keys()))
            if self._cycle == 1:
                self.portfolio.update_day_start(prices)
            return {}

        # ── Late-start catch-up: server restarted after 10:00 AM ────────────
        # If it's ACTIVE time but we missed the pre-market scan window (cycle 1
        # only), do a quick screen + backfill the OR from 1-min bar history.
        if (et_mins >= 10 * 60 and et_mins < 15 * 60 + 45
                and not self._orb_session.screened
                and self._cycle == 1):
            logger.info("  [ORB] Late-start catch-up — backfilling OR from 1-min bars")
            try:
                syms, pm_vols, avg_vols = screen_orb_universe(
                    api_key=self.config.alpaca_api_key,
                    secret_key=self.config.alpaca_secret_key,
                )
                self._orb_session.set_universe(syms, pm_vols, avg_vols)
                or_bars = fetch_opening_range_bars(
                    syms,
                    api_key=self.config.alpaca_api_key,
                    secret_key=self.config.alpaca_secret_key,
                )
                for sym, (oh, ol) in or_bars.items():
                    self._orb_session.update_range(sym, oh, ol)
                logger.info(f"  [ORB] Catch-up: {len(or_bars)} OR ranges loaded")
            except Exception as exc:
                logger.warning(f"  [ORB] Late-start catch-up failed: {exc}")

        # ── Finalize opening range once at 10:00 ─────────────────────────────
        if not self._orb_session.range_formed and self._orb_session.screened:
            self._orb_update_ranges()
            self._orb_session.finalize_range()
            try:
                logger.info("  [ORB] Fetching prev-day high/low (take-profit targets)…")
                pd_levels = fetch_prev_day_levels(self._orb_session.watchlist())
                for sym, (ph, pl) in pd_levels.items():
                    self._orb_session.set_prev_day(sym, ph, pl)
                logger.info(f"  [ORB] Prev-day levels loaded for {len(pd_levels)} symbols")
            except Exception as e:
                logger.warning(f"  [ORB] Prev-day fetch failed: {e}")
            try:
                logger.info("  [ORB] Fetching gap data (open vs prev close)…")
                gap_data = fetch_gap_pcts(self._orb_session.watchlist())
                for sym, gap in gap_data.items():
                    self._orb_session.set_gap_pct(sym, gap)
                n_gapped = sum(1 for g in gap_data.values() if abs(g) > 0.03)
                logger.info(
                    f"  [ORB] Gap data loaded — {n_gapped}/{len(gap_data)} stocks gap-filtered (>3%)"
                )
            except Exception as e:
                logger.warning(f"  [ORB] Gap data fetch failed: {e}")

        # ── Phase 4: 3:45 PM — close everything and stop trading ─────────────
        if et_mins >= 15 * 60 + 45:
            if self.portfolio.positions:
                logger.info("  [ORB] 3:45 PM — closing all open positions")
                self._orb_close_all()
            # ── FIX 9: Also close any non-ORB positions at EOD ───────────────
            if self.portfolio.positions:
                logger.info("  [EOD] 3:45 PM — closing remaining non-ORB positions")
                eod_prices = self._get_prices(list(self.portfolio.positions.keys()))
                for sym in list(self.portfolio.positions.keys()):
                    pos = self.portfolio.positions[sym]
                    price = eod_prices.get(sym, pos.entry_price)
                    self.executor.execute_sell(sym, price, "EOD close 3:45 PM", self.portfolio)
                    self._rebuy_cooldowns[sym] = datetime.now()
            self._orb_session.phase = "DONE"
            logger.info(f"  [CYCLE] #{self._cycle} — session DONE")
            return {}

        # ── Standard market-closed guard (Alpaca mode only) ───────────────────
        if self._use_alpaca and not self._market_is_open():
            logger.info(
                f"  [CYCLE] #{self._cycle} alive — market CLOSED — "
                f"watchlist={len(self.watchlist)} stocks"
            )
            return {}

        # ── Determine active watchlist ────────────────────────────────────────
        if self._orb_session.screened and self._orb_session.states:
            self.watchlist = self._orb_session.watchlist()
        else:
            self._maybe_refresh_watchlist()

        self._refresh_sector_returns()

        if self._regime_detector is not None:
            try:
                self._regime_result = self._regime_detector.detect()
            except Exception as e:
                logger.warning(f"  Regime detection failed: {e}")

        voo = self.voo_monitor.check()
        if voo and voo.alert:
            today_str = self._get_et_time().strftime("%Y-%m-%d")
            if self._voo_alert_sent_date != today_str:
                self.notifier.voo_alert(voo.price, voo.ma200w, voo.gap_pct)
                self._voo_alert_sent_date = today_str

        if self._use_alpaca:
            self.executor.sync_portfolio(self.portfolio, risk_mgr=self.risk)

        market_data = self.fetcher.fetch_many(self.watchlist, force_refresh=True)
        if not market_data:
            logger.error("No market data returned — skipping cycle")
            return {}

        if self.config.use_correlation_filter and self.portfolio.positions:
            pos_syms = [s for s in self.portfolio.positions if s not in market_data]
            if pos_syms:
                market_data.update(self.fetcher.fetch_many(pos_syms, force_refresh=False))

        self._last_corr_blocked = self._compute_corr_blocks(market_data)
        prices = self._get_prices(list(market_data.keys()))

        if self._cycle == 1:
            self.portfolio.update_day_start(prices)

        self._check_exit_conditions(prices)

        # Confirmations (only used outside ORB ACTIVE phase)
        _confirmed_buys: set = set()
        orb_active = self._orb_session.range_formed
        if not orb_active and self.config.use_confirmation and self._pending_signals:
            tol = self.config.confirmation_tolerance_pct
            for sym, info in list(self._pending_signals.items()):
                if sym in prices:
                    cp    = prices[sym]
                    floor = info["signal_price"] * (1 - tol)
                    if cp >= floor:
                        _confirmed_buys.add(sym)
                    else:
                        logger.info(
                            f"  Confirmation FAILED {sym}: ${cp:.2f} dropped below ${floor:.2f}"
                        )
            self._pending_signals.clear()

        # Fetch latest 1-min bar volume for ORB breakout/retest candidates
        vol_1min: Dict[str, float] = {}
        if orb_active:
            candidates = [
                s for s in self.watchlist
                if s in prices and (st := self._orb_session.get(s)) and st.or_high
                and (
                    prices[s] > st.or_high or prices[s] < st.or_low
                    or (st.retest_eligible and prices[s] >= st.or_high * 0.997)
                )
            ]
            if candidates:
                try:
                    vol_1min = fetch_latest_1min_volume(candidates)
                except Exception as e:
                    logger.debug(f"  [ORB] vol fetch failed: {e}")

        results: Dict[str, SignalResult] = {}

        for symbol in self.watchlist:
            if symbol not in market_data or symbol not in prices:
                continue

            df            = market_data[symbol]
            current_price = prices[symbol]

            ind = self.indicators.compute(df)
            if len(df) >= 6:
                stock_5d = float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-6]) - 1
                sec_name = get_sector(symbol)
                if sec_name and sec_name in self._sector_returns:
                    ind.sector_mom = stock_5d - self._sector_returns[sec_name]
            ind.close = current_price

            if orb_active:
                orb_st = self._orb_session.get(symbol)
                orb_sig = self._compute_orb_signal(symbol, ind, current_price, orb_st, vol_1min)
                # Fall back to momentum signal when ORB range is not yet formed
                if orb_sig.action == "HOLD" and (orb_st is None or not (orb_st.formed if orb_st else False)):
                    signal = self._compute_signal(symbol, ind)
                else:
                    signal = orb_sig
            else:
                signal = self._compute_signal(symbol, ind)

            # ── FIX 1: Confluence gate for BUY signals (non-ORB only) ─────────
            confluence_ok = True
            if signal.action == "BUY" and not orb_active:
                if ind.rsi is not None and not (40 <= ind.rsi <= 70):
                    logger.info(
                        f"  {symbol}: confluence FAIL — RSI {ind.rsi:.1f} outside 40-70, downgrading to HOLD"
                    )
                    signal = SignalResult(action="HOLD", score=signal.score,
                                         confidence=signal.confidence, reasons=signal.reasons)
                    confluence_ok = False
                elif ind.vwap is not None and current_price < ind.vwap:
                    logger.info(
                        f"  {symbol}: confluence FAIL — price ${current_price:.2f} < VWAP ${ind.vwap:.2f}, downgrading to HOLD"
                    )
                    signal = SignalResult(action="HOLD", score=signal.score,
                                         confidence=signal.confidence, reasons=signal.reasons)
                    confluence_ok = False
                elif ind.adx is not None and ind.adx < 20:
                    logger.info(
                        f"  {symbol}: confluence FAIL — ADX {ind.adx:.1f} < 20, downgrading to HOLD"
                    )
                    signal = SignalResult(action="HOLD", score=signal.score,
                                         confidence=signal.confidence, reasons=signal.reasons)
                    confluence_ok = False

            results[symbol] = signal

            rsi_str = f"RSI {ind.rsi:5.1f}" if ind.rsi else "RSI  n/a"
            logger.info(
                f"  {symbol:6s}  ${current_price:>9.2f}  "
                f"signal={signal.action:4s}  score={signal.score:+.3f}  "
                f"{rsi_str}  {'  '.join(signal.reasons[:2])}"
            )

            # ── FIX 5: Structured decision log ───────────────────────────────
            _rsi_str = f"{ind.rsi:.1f}" if ind.rsi is not None else "n/a"
            _adx_str = f"{ind.adx:.1f}" if ind.adx is not None else "n/a"
            _vwap_ok = (current_price >= ind.vwap) if ind.vwap is not None else "n/a"
            logger.info(
                f"  DECISION {symbol}: action={signal.action} score={signal.score:+.3f}"
                f" confluence={'PASS' if confluence_ok else 'FAIL'}"
                f" rsi={_rsi_str} adx={_adx_str} vwap_ok={_vwap_ok}"
            )

            portfolio_value = self.portfolio.total_value_at(prices)
            daily_pnl       = self.portfolio.daily_pnl_pct(prices)
            ind_snap        = self._indicator_snapshot(ind, signal)

            if signal.action == "BUY" and not self.portfolio.has_position(symbol):
                # ── FIX 4: Skip if order already in-flight ────────────────────
                if symbol in self._order_in_flight:
                    logger.info(f"  {symbol}: order already in-flight, skipping duplicate BUY")
                    continue

                # ── FIX 2: 3-day rebuy cooldown check ────────────────────────
                if symbol in self._rebuy_cooldowns:
                    days_since = (datetime.now() - self._rebuy_cooldowns[symbol]).days
                    if days_since < 3:
                        logger.info(f"  {symbol}: rebuy cooldown active ({days_since}d < 3d), skipping")
                        continue

                if self.earnings_cal and self.earnings_cal.has_upcoming_earnings(symbol):
                    logger.debug(f"  Earnings protection: skipping BUY {symbol}")
                    continue

                corr_reduced = symbol in self._last_corr_blocked

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
                    sector_value_pct=self._sector_value_pct(symbol, prices),
                    atr_pct=ind.atr_pct,
                )
                if rc.approved:
                    if corr_reduced:
                        rc.max_shares *= 0.5
                    if self._regime_result is not None:
                        mult_key   = f"regime_size_mult_{self._regime_result.regime.lower()}"
                        regime_mult = getattr(
                            self.config, mult_key,
                            _REGIME_SIZE_MULT.get(self._regime_result.regime, 1.0),
                        )
                        rc.max_shares *= regime_mult

                    # ORB: stop = OR midpoint, take-profit = prev-day high (absolute $)
                    orb_st = self._orb_session.get(symbol) if orb_active else None
                    if orb_st and orb_st.or_midpoint:
                        sl = orb_st.or_midpoint
                        tp = orb_st.prev_day_high or self.risk.take_profit_price(current_price)
                    elif not orb_active and self.config.use_confirmation and symbol not in _confirmed_buys:
                        self._pending_signals[symbol] = {
                            "signal_price": current_price,
                            "queued_at": datetime.now().isoformat(),
                        }
                        logger.info(
                            f"  {symbol}: BUY queued for confirmation @ ${current_price:.2f}"
                        )
                        continue
                    else:
                        sl = self.risk.stop_loss_price(current_price)
                        tp = self.risk.take_profit_price(current_price)

                    # ── FIX 6: Log qty/price/sl/tp before executing ───────────
                    logger.info(
                        f"  {symbol}: BUY approved qty={rc.max_shares:.0f}"
                        f" @ ${current_price:.2f} sl=${sl:.2f} tp=${tp:.2f}"
                    )

                    # ── FIX 4: Mark order as in-flight ────────────────────────
                    self._order_in_flight.add(symbol)
                    try:
                        self.executor.execute_buy(
                            symbol=symbol,
                            shares=rc.max_shares,
                            price=current_price,
                            stop_loss=sl,
                            take_profit=tp,
                            reason=", ".join(signal.reasons[:2]),
                            portfolio=self.portfolio,
                        )
                    finally:
                        self._order_in_flight.discard(symbol)

                    # ── FIX 7: Confirm fill before updating dashboard ──────────
                    if not self.portfolio.has_position(symbol):
                        logger.warning(
                            f"  {symbol}: BUY submitted but position not confirmed in portfolio"
                        )

                    if orb_st:
                        orb_st.breakout = "up"
                    self.notifier.trade_buy(
                        symbol, rc.max_shares, current_price, ", ".join(signal.reasons[:2])
                    )
                    self.journal.log(
                        action="BUY", symbol=symbol, shares=rc.max_shares,
                        price=current_price, reason=", ".join(signal.reasons[:2]),
                        indicators=ind_snap,
                    )
                    self.emailer.send_trade(
                        action="BUY", symbol=symbol, shares=rc.max_shares,
                        price=current_price, score=signal.score,
                        reasons=signal.reasons, indicators=ind_snap,
                    )
                else:
                    logger.debug(f"  Risk rejected {symbol}: {rc.reason}")

            elif signal.action == "SELL" and self.portfolio.has_position(symbol):
                pos = self.portfolio.positions.get(symbol)
                if pos and pos.is_short:
                    # Covering an existing short on a BUY signal is handled below
                    pass
                else:
                    # ── FIX 4: Verify position still exists before selling ─────
                    if symbol not in self.portfolio.positions:
                        logger.info(f"  {symbol}: position already closed/sold, skipping SELL")
                    else:
                        if orb_active and (orb_st := self._orb_session.get(symbol)):
                            orb_st.breakout = "down"
                        self.executor.execute_sell(
                            symbol=symbol,
                            price=current_price,
                            reason=f"Signal: {', '.join(signal.reasons[:2])}",
                            portfolio=self.portfolio,
                        )
                        # ── FIX 2: Set rebuy cooldown after sell ──────────────
                        self._rebuy_cooldowns[symbol] = datetime.now()
                        if pos:
                            pnl     = (current_price - pos.entry_price) * pos.shares
                            pnl_pct = (current_price - pos.entry_price) / pos.entry_price
                            self.notifier.trade_sell(
                                symbol, pos.shares, current_price, pnl,
                                f"Signal: {', '.join(signal.reasons[:2])}"
                            )
                            self.journal.log(
                                action="SELL", symbol=symbol, shares=pos.shares,
                                price=current_price,
                                reason=f"Signal: {', '.join(signal.reasons[:2])}",
                                indicators=ind_snap, pnl=pnl, pnl_pct=pnl_pct,
                            )
                            self.emailer.send_trade(
                                action="SELL", symbol=symbol, shares=pos.shares,
                                price=current_price, score=signal.score,
                                reasons=signal.reasons, indicators=ind_snap,
                                pnl=pnl, pnl_pct=pnl_pct,
                            )

            # ── Cover short on BUY signal ─────────────────────────────────────
            elif signal.action == "BUY" and self.portfolio.has_position(symbol):
                pos = self.portfolio.positions.get(symbol)
                if pos and pos.is_short:
                    cover_reason = f"Cover short: {', '.join(signal.reasons[:2])}"
                    executor_cover = getattr(self.executor, "execute_cover", None)
                    if executor_cover:
                        trade = executor_cover(symbol, current_price, cover_reason, self.portfolio)
                    else:
                        trade = self.executor.execute_sell(symbol, current_price, cover_reason, self.portfolio)
                    if trade and trade.pnl is not None:
                        self.notifier.trade_sell(symbol, trade.shares, current_price, trade.pnl, cover_reason)
                        self.journal.log(
                            action="SELL", symbol=symbol, shares=trade.shares,
                            price=current_price, reason=cover_reason,
                            indicators=ind_snap, pnl=trade.pnl, pnl_pct=trade.pnl_pct,
                        )
                        self.emailer.send_trade(
                            action="SELL", symbol=symbol, shares=trade.shares,
                            price=current_price, score=signal.score,
                            reasons=signal.reasons, indicators=ind_snap,
                            pnl=trade.pnl, pnl_pct=trade.pnl_pct,
                        )

            # ── Open short position on SELL signal (no existing position) ─────
            elif signal.action == "SELL" and not self.portfolio.has_position(symbol):
                allow_short = getattr(self.config, "use_short_selling", False)
                if not allow_short or orb_active:
                    continue
                regime = self._regime_result.regime if self._regime_result else None
                allowed_regimes = getattr(self.config, "short_regimes", ["BEAR"])
                if regime not in allowed_regimes:
                    logger.debug(f"  Short skipped {symbol}: regime={regime} not in {allowed_regimes}")
                    continue
                if self.earnings_cal and self.earnings_cal.has_upcoming_earnings(symbol):
                    logger.debug(f"  Earnings protection: skipping SHORT {symbol}")
                    continue

                sl_pct = getattr(self.config, "short_stop_loss_pct", 0.05)
                tp_pct = getattr(self.config, "short_take_profit_pct", 0.10)
                short_sl = round(current_price * (1 + sl_pct), 2)
                short_tp = round(current_price * (1 - tp_pct), 2)

                rc = self.risk.check_buy(
                    symbol=symbol,
                    price=current_price,
                    portfolio_value=portfolio_value,
                    cash=self.portfolio.cash,
                    open_positions=self.portfolio.open_position_count(),
                    daily_pnl_pct=daily_pnl,
                    signal_confidence=signal.confidence,
                    sector_positions=sector_position_count(symbol, self.portfolio.positions),
                    sector_value_pct=self._sector_value_pct(symbol, prices),
                    atr_pct=ind.atr_pct,
                )
                if rc.approved:
                    regime_mult = _REGIME_SIZE_MULT.get(regime, 1.0)
                    shares = rc.max_shares * regime_mult
                    executor_short = getattr(self.executor, "execute_short", None)
                    if executor_short:
                        success = executor_short(
                            symbol=symbol, shares=shares, price=current_price,
                            stop_loss=short_sl, take_profit=short_tp,
                            reason=f"Short: {', '.join(signal.reasons[:2])}",
                            portfolio=self.portfolio,
                        )
                    else:
                        success = False
                    if success:
                        self.journal.log(
                            action="BUY", symbol=symbol, shares=shares,
                            price=current_price,
                            reason=f"SHORT {', '.join(signal.reasons[:2])}",
                            indicators=ind_snap,
                        )
                        logger.info(f"  SHORT opened: {symbol} {shares:.0f} @ ${current_price:.2f}")

        if results:
            top5 = sorted(results.items(), key=lambda kv: abs(kv[1].score), reverse=True)[:5]
            logger.info(
                f"  [SIGNALS] Top-5: "
                f"{' | '.join(f'{s}={g.score:+.3f}({g.action})' for s, g in top5)}"
            )
            buys  = sum(1 for g in results.values() if g.action == "BUY")
            sells = sum(1 for g in results.values() if g.action == "SELL")
            logger.info(
                f"  [SIGNALS] {len(results)} scored — BUY={buys} SELL={sells} "
                f"phase={self._orb_session.phase}"
            )
        else:
            logger.warning("  [SIGNALS] No signals computed this cycle")

        self._log_summary(prices)
        return results

    @property
    def pending_confirmations(self) -> Dict[str, dict]:
        """BUY signals queued for next-candle confirmation."""
        return dict(self._pending_signals)

    @property
    def rebuy_cooldowns(self) -> Dict[str, datetime]:
        """Symbols under 3-day rebuy cooldown and when the cooldown started."""
        return dict(self._rebuy_cooldowns)

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
        self._session_date = self._get_et_time().strftime("%Y-%m-%d")
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
            "rsi":        round(ind.rsi,        2) if ind.rsi        is not None else None,
            "macd_hist":  round(ind.macd_hist,  4) if ind.macd_hist  is not None else None,
            "ema_fast":   round(ind.ema_fast,   2) if ind.ema_fast   is not None else None,
            "ema_slow":   round(ind.ema_slow,   2) if ind.ema_slow   is not None else None,
            "z_score":    round(ind.z_score,    4) if getattr(ind, "z_score",    None) is not None else None,
            "atr_pct":    round(ind.atr_pct,    4) if getattr(ind, "atr_pct",    None) is not None else None,
            "roc_10":     round(ind.roc_10,     4) if getattr(ind, "roc_10",     None) is not None else None,
            "stoch_rsi":  round(ind.stoch_rsi,  2) if getattr(ind, "stoch_rsi",  None) is not None else None,
            "vwap":       round(ind.vwap,        2) if getattr(ind, "vwap",       None) is not None else None,
            "adx":        round(ind.adx,         1) if getattr(ind, "adx",        None) is not None else None,
            "sector_mom": round(ind.sector_mom,  4) if getattr(ind, "sector_mom", None) is not None else None,
            "score":      round(signal.score,    4),
            "confidence": round(signal.confidence, 4),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _get_et_time():
        """Return current datetime in America/New_York (DST-aware)."""
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("America/New_York"))
        except ImportError:
            import pytz
            return datetime.now(pytz.timezone("America/New_York"))

    def _maybe_refresh_watchlist(self) -> None:
        now_et = self._get_et_time()
        today  = now_et.strftime("%Y-%m-%d")

        # Defer until 9:15 AM ET (pre-market screener window)
        if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 15):
            logger.info(
                f"  [SCREENER] Waiting for 9:15 AM ET — current ET time "
                f"{now_et.strftime('%H:%M')} — screener will run later"
            )
            return

        if self._session_date == today:
            return

        if self._pending_signals:
            logger.info(f"  New session — clearing {len(self._pending_signals)} stale pending signals")
            self._pending_signals.clear()

        logger.info(
            f"  [SCREENER] New session {today} — starting universe screen "
            f"(ET {now_et.strftime('%H:%M')})"
        )

        # ── Step 1: Universe screener ─────────────────────────────────────────
        try:
            new_universe = self.dynamic_universe.refresh_if_stale()
            stats = self.dynamic_universe.last_result.get("filter_stats", {})
            if new_universe:
                self.scanner.universe    = list(dict.fromkeys(new_universe))
                self.scanner.volume_top_n = len(new_universe)
                logger.info(
                    f"  [SCREENER] Universe screen complete — "
                    f"{len(new_universe)} tickers passed "
                    f"(candidates={stats.get('total', '?')} "
                    f"→ vol/price={stats.get('after_vol_price', '?')} "
                    f"→ mcap={stats.get('after_mcap', '?')} "
                    f"ipo_removed={stats.get('removed_ipo', 0)} "
                    f"spac_removed={stats.get('removed_spac', 0)})"
                )
            else:
                logger.warning(
                    f"  [SCREENER] Universe screen returned 0 tickers — "
                    f"keeping scanner universe ({len(self.scanner.universe)} symbols)"
                )
        except Exception as e:
            logger.warning(f"  [SCREENER] Universe refresh failed: {e} — keeping previous scanner universe")

        # ── Step 2: Watchlist scan ────────────────────────────────────────────
        logger.info(
            f"  [SCREENER] Running watchlist scan on "
            f"{len(self.scanner.universe)} candidates…"
        )
        try:
            result = self.scanner.scan(self.indicators, self.analyzer)
            if result.watchlist:
                self.watchlist = result.watchlist
                self._session_date = today
                # Log top scores so we can verify signals are alive
                top = sorted(
                    result.scores.items(), key=lambda kv: abs(kv[1]), reverse=True
                )[:5]
                top_str = "  ".join(f"{s}={v:+.3f}" for s, v in top)
                logger.info(
                    f"  [SCREENER] Watchlist ({len(self.watchlist)} stocks): "
                    f"{self.watchlist}"
                )
                logger.info(f"  [SCREENER] Top scanner scores: {top_str}")
            else:
                # Explicit fallback so trading never stops
                fallback = self.config.symbols
                logger.warning(
                    f"  [SCREENER] Scan returned empty watchlist — "
                    f"falling back to config.symbols: {fallback}"
                )
                self.watchlist = list(fallback)
                self._session_date = today   # mark done so we don't retry every cycle
        except Exception as e:
            fallback = self.config.symbols
            logger.error(
                f"  [SCREENER] Watchlist scan failed ({e}) — "
                f"falling back to config.symbols: {fallback}"
            )
            self.watchlist = list(fallback)
            self._session_date = today

        # Attempt ML model (re-)training at the start of each new session
        if self._signal_ranker is not None:
            try:
                self._signal_ranker.maybe_train(self.journal)
            except Exception as e:
                logger.warning(f"  ML training failed: {e}")

    def _refresh_sector_returns(self) -> None:
        """Fetch and cache 5-day returns for all sector ETFs. Runs once per calendar day."""
        today = self._get_et_time().strftime("%Y-%m-%d")
        if self._sector_returns_date == today and self._sector_returns:
            return
        etfs = list(set(SECTOR_ETFS.values()))
        try:
            data = self.fetcher.fetch_many(etfs, force_refresh=False)
            returns: Dict[str, float] = {}
            for sector, etf in SECTOR_ETFS.items():
                df_etf = data.get(etf)
                if df_etf is not None and len(df_etf) >= 6:
                    ret = float(df_etf["Close"].iloc[-1]) / float(df_etf["Close"].iloc[-6]) - 1
                    returns[sector] = ret
            self._sector_returns = returns
            self._sector_returns_date = today
            logger.info(f"  Sector returns updated: {len(returns)} sectors")
        except Exception as exc:
            logger.warning(f"  Sector returns fetch failed: {exc}")

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
            logger.warning("Alpaca live quotes unavailable — falling back to bar cache")
        prices: Dict[str, float] = {}
        for symbol in symbols:
            p = self.fetcher.get_current_price(symbol)
            if p:
                prices[symbol] = p
        return prices

    def get_cached_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Read fresh Alpaca quote prices from the shared 60s cache without API calls.

        Used by dashboard endpoints (heatmap, watchlist) to share the same price
        data already fetched during the engine cycle instead of hitting Alpaca again.
        Returns {} when Alpaca is not configured or no cached prices exist yet.
        """
        if not self._use_alpaca:
            return {}
        from .alpaca_executor import get_cached_price
        return {sym: p for sym in symbols if (p := get_cached_price(sym)) is not None}

    @property
    def last_corr_blocked(self) -> Dict[str, str]:
        """Symbols blocked by correlation filter in the last signal pass {sym: reason}."""
        return dict(self._last_corr_blocked)

    def _sector_value_pct(self, symbol: str, prices: Dict[str, float]) -> float:
        """Fraction of portfolio value already in the same sector as symbol."""
        sec = get_sector(symbol)
        if not sec or not self.portfolio.positions:
            return 0.0
        pv = self.portfolio.total_value_at(prices)
        if not pv:
            return 0.0
        sector_val = sum(
            prices.get(s, pos.entry_price) * pos.shares
            for s, pos in self.portfolio.positions.items()
            if get_sector(s) == sec
        )
        return sector_val / pv

    def risk_rules_status(self, prices: Dict[str, float]) -> dict:
        """Return current status of every active risk rule for the dashboard."""
        portfolio_value = self.portfolio.total_value_at(prices) or 1.0
        daily_pnl = self.portfolio.daily_pnl_pct(prices)
        daily_limit = self.risk.daily_loss_limit_pct

        sector_values: Dict[str, float] = {}
        for sym, pos in self.portfolio.positions.items():
            sec = get_sector(sym) or "Other"
            val = prices.get(sym, pos.entry_price) * pos.shares
            sector_values[sec] = sector_values.get(sec, 0.0) + val

        sector_pcts = {
            sec: round(val / portfolio_value * 100, 1)
            for sec, val in sector_values.items()
        }
        max_sector_pct = max(sector_pcts.values()) if sector_pcts else 0.0
        sector_limit_pct = self.risk.max_sector_exposure_pct * 100

        return {
            "signal_sizing": {
                "active": self.risk.use_adaptive_sizing,
            },
            "correlation": {
                "active": self.config.use_correlation_filter,
                "threshold": self.config.correlation_threshold,
                "reduced_count": len(self._last_corr_blocked),
                "status": "REDUCED" if self._last_corr_blocked else "OK",
            },
            "sector_exposure": {
                "limit_pct": round(sector_limit_pct, 0),
                "sector_pcts": sector_pcts,
                "max_sector_pct": round(max_sector_pct, 1),
                "status": (
                    "LIMIT" if max_sector_pct >= sector_limit_pct
                    else "WARNING" if max_sector_pct >= sector_limit_pct * 0.8
                    else "OK"
                ),
            },
            "daily_loss": {
                "limit_pct": round(daily_limit * 100, 1),
                "current_pct": round(daily_pnl * 100, 2),
                "triggered": daily_pnl <= -daily_limit,
                "status": (
                    "TRIGGERED" if daily_pnl <= -daily_limit
                    else "WARNING" if daily_pnl <= -daily_limit * 0.7
                    else "OK"
                ),
            },
        }

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
        partial_exits = []   # (symbol, shares, price, reason) — partial sells
        exits = []           # (symbol, price, reason) — full closes

        for symbol, pos in self.portfolio.positions.items():
            price = prices.get(symbol, pos.entry_price)

            if pos.is_short:
                # Short: profit when price falls; stop is above entry
                if not pos.partial_exit_done and pos.take_profit < pos.entry_price:
                    halfway = pos.entry_price - 0.5 * (pos.entry_price - pos.take_profit)
                    if price <= halfway and pos.shares >= 2:
                        half_shares = pos.shares / 2
                        partial_exits.append((symbol, half_shares, price, "Partial take-profit (50% ladder)"))
                        pos.partial_exit_done = True
                        pos.stop_loss = pos.entry_price  # move stop to breakeven
                if price >= pos.stop_loss:
                    exits.append((symbol, price, "Short stop triggered"))
                elif price <= pos.take_profit:
                    exits.append((symbol, price, "Short take profit triggered"))
                continue

            self.risk.update_trailing_stop(pos, price)

            # Partial ladder exit: at 50% of the way to TP, sell half and set breakeven stop
            use_ladder = getattr(self.config, "use_partial_exits", True)
            if (
                use_ladder
                and not pos.partial_exit_done
                and pos.take_profit > pos.entry_price
                and pos.shares >= 2
            ):
                halfway = pos.entry_price + 0.5 * (pos.take_profit - pos.entry_price)
                if price >= halfway:
                    half_shares = pos.initial_shares / 2
                    partial_exits.append((symbol, half_shares, price, "Partial take-profit (50% ladder)"))
                    pos.partial_exit_done = True
                    # Move stop to breakeven after locking in half
                    if pos.stop_loss < pos.entry_price:
                        pos.stop_loss = pos.entry_price
                        logger.info(f"  Breakeven stop set for {symbol} @ ${pos.entry_price:.2f}")
            elif not use_ladder and pos.take_profit > pos.entry_price:
                # Legacy: just move stop to breakeven at halfway (no partial sell)
                halfway = pos.entry_price + 0.5 * (pos.take_profit - pos.entry_price)
                if price >= halfway and pos.stop_loss < pos.entry_price:
                    pos.stop_loss = pos.entry_price
                    logger.debug(f"  Breakeven stop set for {symbol} @ ${pos.entry_price:.2f}")

            stop_reason = (
                "Trailing stop triggered" if self.risk.use_trailing_stop
                else "Stop loss triggered"
            )
            if self.risk.check_stop_loss(pos.entry_price, price, pos):
                # ── FIX 10: Log stop-loss hit explicitly ──────────────────────
                logger.info(
                    f"  {symbol}: STOP-LOSS HIT @ ${price:.2f}"
                    f" (stop=${pos.stop_loss:.2f})"
                )
                exits.append((symbol, price, stop_reason))
            elif price >= pos.take_profit:
                exits.append((symbol, price, "Take profit triggered"))

        # Execute partial exits first (before full closes so positions still exist)
        for symbol, shares, price, reason in partial_exits:
            if symbol not in self.portfolio.positions:
                continue
            pos = self.portfolio.positions[symbol]
            trade = self.portfolio.partial_sell(symbol, shares, price, reason)
            if trade:
                self.executor.execute_partial_sell(symbol, shares, price, reason, self.portfolio)
                pnl = trade.pnl or 0.0
                pnl_pct = trade.pnl_pct or 0.0
                logger.info(
                    f"  [PARTIAL] {symbol}: sold {trade.shares:.0f} shares @ ${price:.2f}"
                    f"  P&L +${pnl:,.2f} ({pnl_pct * 100:.2f}%)  remaining={pos.shares:.0f}"
                )
                self.notifier.trade_sell(symbol, trade.shares, price, pnl, reason)
                self.journal.log(
                    action="SELL", symbol=symbol, shares=trade.shares,
                    price=price, reason=reason, pnl=pnl, pnl_pct=pnl_pct,
                )
                self.emailer.send_trade(
                    action="SELL", symbol=symbol, shares=trade.shares,
                    price=price, score=0.0, reasons=[reason],
                    pnl=pnl, pnl_pct=pnl_pct,
                )

        for symbol, price, reason in exits:
            pos = self.portfolio.positions.get(symbol)
            self.executor.execute_sell(symbol, price, reason, self.portfolio)
            # ── FIX 2: Set rebuy cooldown after exit ──────────────────────────
            self._rebuy_cooldowns[symbol] = datetime.now()
            if pos:
                pnl     = (price - pos.entry_price) * pos.shares if not pos.is_short else (pos.entry_price - price) * pos.shares
                pnl_pct = (price - pos.entry_price) / pos.entry_price if not pos.is_short else (pos.entry_price - price) / pos.entry_price
                self.notifier.trade_sell(symbol, pos.shares, price, pnl, reason)
                self.journal.log(
                    action="SELL",
                    symbol=symbol,
                    shares=pos.shares,
                    price=price,
                    reason=reason,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                )
                self.emailer.send_trade(
                    action="SELL",
                    symbol=symbol,
                    shares=pos.shares,
                    price=price,
                    score=0.0,
                    reasons=[reason],
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                )

    # ── ORB helpers ───────────────────────────────────────────────────────────

    def _orb_do_scan(self) -> None:
        """9:15–9:29 ET: screen universe and lock ORB watchlist."""
        logger.info("  [ORB] Starting pre-market universe scan…")
        try:
            symbols, pm_vols, avg_vols = screen_orb_universe()
            self._orb_session.set_universe(symbols, pm_vols, avg_vols)
            self.watchlist = self._orb_session.watchlist()
            logger.info(
                f"  [ORB] Universe ready: {len(symbols)} stocks — "
                f"top 5 by PM vol: {symbols[:5]}"
            )
        except Exception as e:
            logger.error(f"  [ORB] Pre-market scan failed: {e}")

    def _orb_update_ranges(self) -> None:
        """9:30–9:59 ET: pull latest 1-min bars and widen OR high/low."""
        symbols = self._orb_session.watchlist()
        if not symbols:
            return
        try:
            bars = fetch_opening_range_bars(symbols)
            for sym, (hi, lo) in bars.items():
                self._orb_session.update_range(sym, hi, lo)
            logger.debug(f"  [ORB] OR bars updated for {len(bars)} symbols")
        except Exception as e:
            logger.warning(f"  [ORB] Range update failed: {e}")

    def _orb_close_all(self) -> None:
        """Close every open position — called at 3:45 PM ET."""
        for symbol in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions.get(symbol)
            prices = self._get_prices([symbol])
            price = prices.get(symbol, pos.entry_price if pos else 0)
            if not price:
                continue
            reason = "ORB EOD close 3:45 PM"
            self.executor.execute_sell(symbol, price, reason, self.portfolio)
            if pos:
                pnl     = (price - pos.entry_price) * pos.shares
                pnl_pct = (price - pos.entry_price) / pos.entry_price
                self.notifier.trade_sell(symbol, pos.shares, price, pnl, reason)
                self.journal.log(
                    action="SELL", symbol=symbol, shares=pos.shares,
                    price=price, reason=reason, pnl=pnl, pnl_pct=pnl_pct,
                )
                self.emailer.send_trade(
                    action="SELL", symbol=symbol, shares=pos.shares,
                    price=price, score=0.0, reasons=[reason],
                    pnl=pnl, pnl_pct=pnl_pct,
                )
            logger.info(f"  [ORB] EOD closed {symbol} @ ${price:.2f}")

    def _compute_orb_signal(
        self,
        symbol: str,
        ind,
        current_price: float,
        orb_st,
        vol_1min: Dict[str, float],
    ) -> "SignalResult":
        """
        ORB signal with three additional filters:

        Gap filter:    skip any stock gapped >3% from prev close at open (both dirs)
        Trend filter:  BUY only above 20-day EMA; SELL only below 20-day EMA
        Retest entry:  on initial high-vol breakout above OR high, wait for pullback
                       within 0.3% of OR high on lower volume before entering
        RSI blocks:    RSI>80 blocks BUY; RSI<15 blocks SELL (extreme only)
        """
        if orb_st is None or not orb_st.formed or orb_st.or_high is None:
            return SignalResult(action="HOLD", score=0.0, confidence=0.0,
                                reasons=["ORB range not yet formed"])

        # ── FIX 8: Only trade breakouts after 10:00AM ET ─────────────────────
        now_et = self._get_et_time()
        if now_et.hour < 10 or (now_et.hour == 10 and now_et.minute == 0):
            return SignalResult(action="HOLD", score=0.0, confidence=0.0,
                                reasons=["ORB: waiting for 10:00AM ET before trading breakouts"])

        or_high = orb_st.or_high
        or_low  = orb_st.or_low

        # ── Gap filter (both directions) ──────────────────────────────────────
        if orb_st.gap_pct is not None and abs(orb_st.gap_pct) > 0.03:
            return SignalResult(action="HOLD", score=0.0, confidence=0.0,
                                reasons=[f"Gap filter: {orb_st.gap_pct * 100:.1f}% at open"])

        # ── Breakdown SELL ────────────────────────────────────────────────────
        if current_price < or_low:
            if ind.rsi is not None and ind.rsi < 15:
                return SignalResult(action="HOLD", score=-0.1, confidence=0.1,
                                    reasons=[f"ORB breakdown — RSI {ind.rsi:.1f} extreme oversold"])
            if ind.ema_fast is not None and current_price >= ind.ema_fast:
                return SignalResult(action="HOLD", score=-0.1, confidence=0.1,
                                    reasons=["ORB breakdown — price above 20-day EMA, skip SELL"])
            return SignalResult(action="SELL", score=-0.8, confidence=0.8,
                                reasons=[f"ORB breakdown below ${or_low:.2f}"])

        avg_per_min = orb_st.avg_daily_volume / 390.0 if orb_st.avg_daily_volume > 0 else 0
        bar_vol     = vol_1min.get(symbol, 0)

        # ── Retest zone: price pulled back within 0.3% of OR high ────────────
        if orb_st.retest_eligible:
            retest_low = or_high * 0.997
            in_zone    = retest_low <= current_price <= or_high * 1.003
            low_vol    = orb_st.breakout_volume and bar_vol < orb_st.breakout_volume
            if in_zone and low_vol:
                # Apply confluence filters at the retest level
                if ind.ema_fast is not None and current_price <= ind.ema_fast:
                    return SignalResult(action="HOLD", score=0.1, confidence=0.1,
                                        reasons=["Retest — below 20-day EMA"])
                if ind.rsi is not None and ind.rsi > 80:
                    return SignalResult(action="HOLD", score=0.1, confidence=0.1,
                                        reasons=[f"Retest — RSI {ind.rsi:.1f} extreme overbought"])
                if ind.vwap is not None and current_price <= ind.vwap:
                    return SignalResult(action="HOLD", score=0.1, confidence=0.1,
                                        reasons=[f"Retest — below VWAP ${ind.vwap:.2f}"])
                if ind.adx is not None and ind.adx <= 20:
                    return SignalResult(action="HOLD", score=0.1, confidence=0.1,
                                        reasons=[f"Retest — ADX {ind.adx:.1f} ≤ 20"])
                reasons = [f"ORB retest @ ${current_price:.2f} (OR high ${or_high:.2f})"]
                if orb_st.breakout_volume:
                    reasons.append(f"Vol {bar_vol:.0f} < breakout {orb_st.breakout_volume:.0f}")
                if ind.ema_fast:
                    reasons.append(f"EMA ${ind.ema_fast:.2f} OK")
                if ind.adx:
                    reasons.append(f"ADX {ind.adx:.1f} OK")
                score = round(min(0.85, 0.5 + (current_price - or_high) / or_high), 4)
                return SignalResult(action="BUY", score=score,
                                    confidence=round(min(0.9, score + 0.1), 4), reasons=reasons)
            # Retest-eligible but not in zone — keep waiting
            return SignalResult(action="HOLD", score=0.0, confidence=0.0,
                                reasons=["Waiting for ORB retest"])

        # ── Price inside OR ───────────────────────────────────────────────────
        if current_price <= or_high:
            return SignalResult(action="HOLD", score=0.0, confidence=0.0,
                                reasons=["Price inside OR"])

        # ── Initial breakout above OR high — detect and wait for retest ───────
        if avg_per_min > 0 and bar_vol < 1.5 * avg_per_min:
            return SignalResult(action="HOLD", score=0.1, confidence=0.1,
                                reasons=[
                                    f"ORB breakout above ${or_high:.2f} — "
                                    f"vol {bar_vol:.0f} < 1.5x avg {avg_per_min:.0f}"
                                ])
        orb_st.retest_eligible = True
        orb_st.breakout_volume = bar_vol if bar_vol > 0 else None
        logger.info(
            f"  [ORB] {symbol}: initial breakout @ ${current_price:.2f} "
            f"vol={bar_vol:.0f} — waiting for retest near ${or_high:.2f}"
        )
        return SignalResult(action="HOLD", score=0.2, confidence=0.2,
                            reasons=[f"ORB breakout detected — awaiting retest near ${or_high:.2f}"])

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
