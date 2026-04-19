# Automatic Stock Exchange

Algorithmic NYSE trading engine — automated buy/sell execution powered by smart market signal analysis.

## Features

- **Multi-indicator signal engine** — RSI, MACD, EMA crossover, and Bollinger Bands combined into a single weighted composite score
- **Alpaca paper trading** — submits real bracket orders (entry + stop-loss + take-profit) to your Alpaca paper account
- **Risk management** — per-position sizing, daily loss halt, stop-loss, take-profit
- **Historical backtester** — Sharpe ratio, max drawdown, win rate, profit factor
- **Web dashboard** — live signals, positions, P&L, and one-click cycle execution

## Quick Start

```bash
git clone https://github.com/davidmestman-art/Automatic-Stock-Exchange
cd Automatic-Stock-Exchange

pip install -r requirements.txt

cp .env.example .env
# fill in your Alpaca paper keys
```

**.env**
```
ALPACA_API_KEY=your_paper_api_key
ALPACA_SECRET_KEY=your_paper_secret_key
```

Get keys at [app.alpaca.markets](https://app.alpaca.markets) → Paper Trading → API Keys.

## Usage

```bash
# Single trading cycle (Alpaca paper if keys are set, local simulation otherwise)
python main.py trade

# Run every 60 seconds for a full market session (390 minutes)
python main.py trade --cycles 390 --interval 60

# Backtest last 6 months
python main.py backtest

# Backtest a specific date range
python main.py backtest --start 2024-01-01 --end 2024-12-31

# Web dashboard at http://localhost:8080
python dashboard.py
```

## Architecture

```
main.py               # CLI entry point
dashboard.py          # Flask web dashboard
config.py             # All tuneable parameters
requirements.txt

src/
├── data/
│   └── fetcher.py          # yfinance historical bars + Alpaca live quotes
├── signals/
│   ├── indicators.py       # RSI, MACD, EMA, Bollinger Bands
│   └── analyzer.py         # Weighted composite signal scoring
├── trading/
│   ├── portfolio.py        # Position + P&L tracking
│   ├── risk.py             # Stop-loss, take-profit, position sizing
│   ├── executor.py         # Local paper simulation
│   ├── alpaca_executor.py  # Alpaca API order execution
│   └── engine.py           # Main trading loop
└── backtest/
    ├── metrics.py          # Sharpe, drawdown, win rate, profit factor
    └── backtester.py       # Day-by-day historical simulation
```

## Signal Engine

Each bar is scored by four weighted indicators, then combined into a composite score from −1.0 (strong sell) to +1.0 (strong buy). A volume multiplier amplifies signals on above-average volume.

| Indicator | Weight | BUY signal | SELL signal |
|---|---|---|---|
| RSI (14) | 25% | RSI < 30 (oversold) | RSI > 70 (overbought) |
| MACD (12/26/9) | 30% | Histogram crosses above zero | Histogram crosses below zero |
| EMA crossover (20/50) | 25% | Fast crosses above slow (golden cross) | Fast crosses below slow (death cross) |
| Bollinger Bands (20, 2σ) | 20% | Price touches lower band | Price touches upper band |

- Score ≥ **+0.35** → **BUY**
- Score ≤ **−0.35** → **SELL**
- Otherwise → **HOLD**

## Risk Management

| Parameter | Default | Description |
|---|---|---|
| `max_position_pct` | 10% | Maximum portfolio allocation per position |
| `max_open_positions` | 8 | Maximum simultaneous open positions |
| `stop_loss_pct` | 5% | Stop-loss below entry price |
| `take_profit_pct` | 15% | Take-profit above entry price |
| `daily_loss_limit_pct` | 3% | Halt all trading after 3% daily drawdown |

Position size is scaled by signal confidence so stronger signals get larger allocations up to the cap.

## Alpaca Integration

When `ALPACA_API_KEY` is set the engine automatically routes through Alpaca:

- **Orders** — bracket orders: one API call places market entry + stop-loss OCO + take-profit OCO
- **Signal sells** — open bracket legs are cancelled before a market sell is submitted
- **Portfolio sync** — Alpaca positions are mirrored locally on every cycle, catching bracket exits that fired between runs
- **Live quotes** — Alpaca mid-quote `(ask + bid) / 2`; falls back to yfinance
- **Market hours** — Alpaca clock API is used to gate cycles (respects NYSE holidays)

Set `paper_trading = False` in `config.py` to switch to a live account.

## Dashboard

```bash
python dashboard.py   # http://localhost:8080
```

- Stat cards: total value, cash, in-positions value, total P&L, open positions, trade count
- **Signals table** — all symbols ranked by signal strength with score bars and RSI
- **Positions table** — entry vs current price and unrealized P&L per position
- **Trades table** — last 30 executions with realized P&L
- **Refresh** — read-only signal analysis, no orders placed
- **Run Cycle** — full execution cycle, submits orders to Alpaca
- Auto-refreshes every 30 seconds

## Configuration

All parameters live in `config.py`:

```python
config.symbols            # List of ticker symbols to trade
config.initial_capital    # Starting capital (paper mode)
config.buy_threshold      # Composite score threshold for BUY (default 0.35)
config.sell_threshold     # Composite score threshold for SELL (default -0.35)
config.paper_trading      # True = Alpaca paper, False = Alpaca live
```
