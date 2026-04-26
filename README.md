Automatic Stock Exchange
Algorithmic NYSE trading engine — automated buy/sell execution powered by multi-indicator
signal analysis, fundamental filtering, market regime detection, and Alpaca paper trading.
What It Does
Automatic Stock Exchange is a fully automated paper-trading platform that scans the S&P 500,
filters for fundamentally healthy companies, analyzes technical signals across multiple
timeframes, and executes trades through Alpaca's brokerage API — all from a live web
dashboard running on your local machine.
The engine gets smarter over time: it logs every trade with full indicator snapshots, and a built-in
machine learning framework learns which signal combinations lead to profitable trades as it
collects data.
Features
Signal Engine
- RSI (Relative Strength Index) — identifies overbought and oversold conditions
- MACD (Moving Average Convergence Divergence) — detects momentum shifts and
trend changes
- EMA Crossover (20/50) — signals trend direction when fast and slow moving averages
cross
- Bollinger Bands — identifies price volatility and mean reversion opportunities
- Confidence-weighted composite scoring — combines all indicators into a single
buy/sell/hold decision
Smart Trading
- Fundamental analysis pre-filter — only trades companies with positive earnings
growth, P/E under 30, low debt-to-equity, and positive free cash flow
- Multi-timeframe confirmation — checks daily, hourly, and 15-minute signals; requires 2
of 3 to agree
- Market regime detection — monitors S&P 500 trend and VIX to determine
bull/bear/choppy conditions and adjusts aggressiveness
- Correlation filter — prevents buying stocks that move too similarly to existing positions
- Adaptive position sizing — stronger signals get larger positions, weaker signals get
smaller ones
- Trailing stop-loss — stop-loss follows the price up to lock in profits
- Earnings calendar protection — avoids buying stocks within 3 days of their earnings
report
- Volume spike detection — flags stocks with unusual volume compared to 20-day
average
- Mean reversion detection — identifies stocks stretched too far from their average price
(Z-score)
- Smart entry timing — waits for candle confirmation before entering trades
Risk Management
- Position sizing — max 10% of portfolio per stock
- Maximum 8 open positions at once
- Trailing stop-loss that moves up with the price
- 15% take-profit auto-sell
- 3% daily loss limit — stops trading if portfolio drops 3% in one day
- Sector diversification — spreads positions across different industries
Dynamic S&P 500 Scanner
- Scans ~200 S&P 500 stocks once per trading session
- Filters to top 100 by volume
- Applies fundamental analysis filter (P/E < 30, D/E < 2, positive FCF, positive EPS
growth)
- Scores all candidates with the signal engine
- Selects top 10 by composite signal strength
- Re-scan button for mid-session refresh
Alpaca Integration
- Paper trading API for realistic order execution
- Bracket orders with stop-loss and take-profit
- Portfolio sync — reads positions and trades directly from Alpaca
- Market hours gating — only trades when NYSE is open
- Graceful fallback to local simulation if no API keys provided
Dashboard
Main Page — http://localhost:8080
- Stat Cards — portfolio value, cash, positions, P&L, trade count, market regime
(BULL/BEAR/CHOP)
- Unrealized P&L Ticker — live-updating at the top
- VOO 200-Week MA Monitor — price vs 200-week moving average with BUY ALERT
- Sector Allocation — pie chart of position distribution across sectors
- Watchlist — scanner picks with earnings warning badges
- Pre/Post-Market Panel — after-hours and pre-market price changes
- Signal Analysis — all stocks with signal, score, RSI, Z-score, volume ratio, sector,
multi-timeframe breakdown
- Watchlist Heat Map — color-coded grid showing daily performance at a glance
- Positions — open trades with entry, current, trailing stop, quantity, sector, unrealized
P&L
- Trades — recent history with time, ticker, side, quantity, price, P&L
- Market News — latest headlines for watchlist stocks
Stats Page — http://localhost:8080/stats
- Performance Cards — win rate, avg gain, avg loss, best/worst trade, total realized P&L
- Risk Metrics — Sharpe ratio, max drawdown, current drawdown, Calmar ratio
- Drawdown History — chart of underwater periods
- Portfolio Value — line chart over time
- 6-Month Backtest — algo return vs S&P 500, Sharpe, drawdown, win rate, profit factor
- Trade Notifications — ntfy.sh and Pushover setup
- Trade Journal — every trade with indicator snapshots
Quick Start
1. Install Python
Download from python.org if you don't have it.
2. Install dependencies
pip install -r requirements.txt
3. Configure Alpaca (optional)
Copy .env.example to .env and add your paper trading keys:
ALPACA_API_KEY=your_api_key_here
ALPACA_SECRET_KEY=your_secret_key_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets
Get free paper trading keys at app.alpaca.markets → Paper Trading → API Keys.
4. Launch
Windows: Double-click start.bat
Manual: python dashboard.py then open http://localhost:8080
The engine auto-trades every 60 seconds while the market is open.
CLI Usage
python main.py trade # Single cycle
python main.py trade --cycles 390 --interval 60 # All day
python main.py backtest # 6-month backtest
python main.py backtest --start 2024-01-01 --end 2024-06-30 # Custom range
Notifications
ntfy.sh (free): Add NTFY_TOPIC=your-topic to .env, install ntfy app on phone, subscribe to
same topic.
Pushover ($5): Add PUSHOVER_TOKEN and PUSHOVER_USER to .env.
Alerts fire on: BUY executed, SELL executed, stop-loss triggered, VOO 200W MA alert.
Configuration
Edit config.py to adjust trading parameters:
Setting Default Description
universe_size 100 Stocks to scan by volume
watchlist_size 10 Top picks to trade
max_position_pct 0.10 Max 10% per stock
max_open_positions 8 Max concurrent positions
stop_loss_pct 0.05 5% trailing stop-loss
Setting Default Description
take_profit_pct 0.15 15% take-profit
daily_loss_limit_pct 0.03 3% daily loss limit
Backtest Results (6-Month)
Metric Value
Algo Return +1.88%
Annualized +3.86%
Sharpe Ratio 0.36
Max Drawdown -4.71%
Win Rate 46.3%
Profit Factor 1.27
Total Trades 41
Tech Stack
Python 3, Flask, yfinance, Alpaca API, Chart.js, scikit-learn
Disclaimer
This project is for educational purposes only. Not financial advice. Paper trading uses simulated
money — no real funds are at risk. Past performance does not guarantee future results. Always
do your own research before making any investment decisions.
