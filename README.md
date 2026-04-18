# Automatic-Stock-Exchange
Exchange stocks from the NYSE with smart market algorithm 
# Automatic-Stock-Exchange

Exchange stocks from the NYSE with a smart market algorithm.

## What it does

Automatic-Stock-Exchange is a paper-trading engine that uses multi-indicator signal analysis to automate buy, sell, and hold decisions for NYSE-listed stocks — no API key required for paper mode.

## Features

- Multi-indicator signal analysis: RSI, MACD, EMA crossover, Bollinger Bands
- Confidence-weighted composite scoring for buy/sell/hold decisions
- Risk management: position sizing, stop-loss, take-profit, daily loss limit
- Paper (virtual) order executor and portfolio tracker
- Historical backtester with Sharpe ratio, drawdown, and win-rate metrics
- Powered by yfinance — no API key needed for paper trading

## Project structure


## How to run

```bash
pip install -r requirements.txt

# Single paper-trading cycle
python main.py trade

# 5 cycles, 1 hour apart
python main.py trade --cycles 5

# Backtest last 6 months
python main.py backtest

# Custom date range
python main.py backtest --start 2024-01-01 --end 2024-06-30
