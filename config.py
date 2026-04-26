import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

from src.data.scanner import SP500_UNIVERSE

load_dotenv(Path(__file__).resolve().parent / ".env")


@dataclass
class TradingConfig:
    # ── Dynamic watchlist ─────────────────────────────────────────────────────
    # The engine scans sp500_universe once per trading session:
    #   1. Filter to the top universe_size symbols by today's volume
    #   2. Score all candidates with the signal engine
    #   3. Trade the top watchlist_size by |score|
    sp500_universe: List[str] = field(default_factory=lambda: SP500_UNIVERSE)
    universe_size: int = 100        # Top N by volume to score
    watchlist_size: int = 10        # Top N by signal to trade

    # Fallback static watchlist used by the backtester and on scan failure
    symbols: List[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
        "META", "TSLA", "JPM", "V", "JNJ",
    ])

    # ── Portfolio settings ────────────────────────────────────────────────────
    initial_capital: float = 100_000.0
    max_position_pct: float = 0.10
    max_open_positions: int = 8

    # ── Risk management ───────────────────────────────────────────────────────
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.15
    daily_loss_limit_pct: float = 0.03

    # ── Trailing stop-loss ────────────────────────────────────────────────────
    use_trailing_stop: bool = True
    trailing_stop_pct: float = 0.05        # stop ratchets to -5% below new highs

    # ── Entry confirmation ────────────────────────────────────────────────────
    use_confirmation: bool = True
    confirmation_tolerance_pct: float = 0.005   # allow 0.5% pullback before rejecting

    # ── Mean reversion signal ─────────────────────────────────────────────────
    use_mean_reversion: bool = True        # adds z-score component to composite signal

    # ── Correlation filter ────────────────────────────────────────────────────
    use_correlation_filter: bool = True
    correlation_threshold: float = 0.70   # block if |ρ| ≥ this vs any open position
    correlation_lookback: int = 30        # days of returns used to estimate ρ

    # ── Adaptive position sizing ──────────────────────────────────────────────
    use_adaptive_sizing: bool = True
    adaptive_target_vol_pct: float = 0.01  # target 1 % daily vol per position
    min_position_pct: float = 0.03         # floor: 3 % of portfolio per position

    # ── Signal thresholds ─────────────────────────────────────────────────────
    buy_threshold: float = 0.20
    sell_threshold: float = -0.20

    # ── Technical indicator periods ───────────────────────────────────────────
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    ema_fast: int = 20
    ema_slow: int = 50
    bb_period: int = 20
    bb_std: float = 2.0

    # ── Data settings ─────────────────────────────────────────────────────────
    data_interval: str = "1d"
    lookback_days: int = 120

    # ── Alpaca integration ────────────────────────────────────────────────────
    use_alpaca: bool = field(
        default_factory=lambda: bool(os.getenv("ALPACA_API_KEY"))
    )
    paper_trading: bool = True
    alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))

    # ── Fundamental filter ────────────────────────────────────────────────────
    use_fundamental_filter: bool = True    # P/E < 30, D/E < 2, positive FCF + EPS growth
    fundamental_pe_max: float = 30.0
    fundamental_de_max: float = 2.0

    # ── Sector diversification ────────────────────────────────────────────────
    max_positions_per_sector: int = 3

    # ── Multiple timeframes ───────────────────────────────────────────────────
    use_multi_timeframe: bool = True       # blends 1d/1h/15m signals when True

    # ── Earnings calendar protection ──────────────────────────────────────────
    use_earnings_protection: bool = True
    earnings_buffer_days: int = 3

    # ── VOO 200-week MA monitor ───────────────────────────────────────────────
    voo_alert_threshold_pct: float = 2.0

    # ── Multi-timeframe confirmation ──────────────────────────────────────────
    mtf_min_agreeing: int = 2            # require ≥ N timeframes to agree before trading

    # ── Market regime detection ───────────────────────────────────────────────
    use_regime_detection: bool = True
    regime_bull_vix_max: float = 25.0
    regime_bear_vix_min: float = 27.0
    regime_size_mult_bull: float = 1.0
    regime_size_mult_choppy: float = 0.70
    regime_size_mult_bear: float = 0.35
    regime_bear_min_score_mult: float = 1.8   # BEAR: only trade if score > threshold × mult

    # ── Momentum signals ──────────────────────────────────────────────────────
    use_momentum_signals: bool = True   # ROC + StochRSI component in signal scoring

    # ── ML signal ranking ─────────────────────────────────────────────────────
    use_ml_ranking: bool = True
    ml_min_samples: int = 20

    # ── Notifications ─────────────────────────────────────────────────────────
    ntfy_topic: str = field(default_factory=lambda: os.getenv("NTFY_TOPIC", ""))
    pushover_token: str = field(default_factory=lambda: os.getenv("PUSHOVER_TOKEN", ""))
    pushover_user: str = field(default_factory=lambda: os.getenv("PUSHOVER_USER", ""))


config = TradingConfig()
