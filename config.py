import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


@dataclass
class TradingConfig:
    # Symbols to trade (large-cap NYSE/NASDAQ)
    symbols: List[str] = field(default_factory=lambda: [
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
        "META", "TSLA", "JPM", "V", "JNJ",
    ])

    # Portfolio settings
    initial_capital: float = 100_000.0
    max_position_pct: float = 0.10      # Max 10% of portfolio per position
    max_open_positions: int = 8

    # Risk management
    stop_loss_pct: float = 0.05         # 5% stop loss below entry
    take_profit_pct: float = 0.15       # 15% take profit above entry
    daily_loss_limit_pct: float = 0.03  # Halt trading after 3% daily loss

    # Signal thresholds
    buy_threshold: float = 0.35         # Composite score >= this → BUY
    sell_threshold: float = -0.35       # Composite score <= this → SELL

    # Technical indicator periods
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    ema_fast: int = 20
    ema_slow: int = 50
    bb_period: int = 20
    bb_std: float = 2.0

    # Data settings
    data_interval: str = "1d"
    lookback_days: int = 120

    # ── Alpaca integration ────────────────────────────────────────────────────
    # use_alpaca: True  → submit real orders to Alpaca (paper or live account)
    # use_alpaca: False → pure local simulation (no API calls, offline-safe)
    use_alpaca: bool = field(
        default_factory=lambda: bool(os.getenv("ALPACA_API_KEY"))
    )
    # paper_trading: True  → Alpaca paper account  (fake money, real market data)
    # paper_trading: False → Alpaca live account   (real money — use with caution)
    paper_trading: bool = True

    alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))


config = TradingConfig()
