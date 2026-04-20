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


config = TradingConfig()
