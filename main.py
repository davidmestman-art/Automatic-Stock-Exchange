from flask import request, jsonify
from services.orb_service import get_orb_signal
@app.route("/orb-signal")
def orb_signal():
    price = request.args.get("price")

    if price is None:
        return jsonify({"error": "missing price"}), 400

    price = float(price)

    signal = get_orb_signal(price)

    return jsonify({
        "signal": signal
    })
    
@app.route("/")
def home():
    return render_template("home.html")
#!/usr/bin/env python3
"""NYSE Algorithmic Trading Engine — entry point.

Usage:
  python main.py trade                         # single cycle (Alpaca paper if keys set)
  python main.py trade --cycles 5              # 5 consecutive cycles
  python main.py trade --cycles 390 --interval 60  # run every minute for a full session
  python main.py backtest                      # backtest last 6 months
  python main.py backtest --start 2024-01-01 --end 2024-12-31
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta

from config import config
from src.backtest.backtester import Backtester
from src.trading.engine import TradingEngine


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    log_file = f"trading_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )


def print_alpaca_status(logger) -> None:
    """Print Alpaca account summary and market clock before trading starts."""
    from src.trading.alpaca_executor import AlpacaExecutor

    exec_ = AlpacaExecutor(
        config.alpaca_api_key, config.alpaca_secret_key, paper=config.paper_trading
    )

    try:
        acct = exec_.get_account_summary()
        logger.info("── Alpaca Account ──────────────────────────────────")
        logger.info(f"  Mode          : {'Paper' if config.paper_trading else 'LIVE'}")
        logger.info(f"  Equity        : ${acct['equity']:>12,.2f}")
        logger.info(f"  Cash          : ${acct['cash']:>12,.2f}")
        logger.info(f"  Buying Power  : ${acct['buying_power']:>12,.2f}")
        logger.info(f"  Daytrade cnt  : {acct['daytrade_count']}")
    except Exception as e:
        logger.error(f"  Could not fetch Alpaca account: {e}")
        logger.error("  Check ALPACA_API_KEY and ALPACA_SECRET_KEY in your .env file.")
        sys.exit(1)

    try:
        clock = exec_.get_clock_info()
        status = "OPEN" if clock["is_open"] else "CLOSED"
        logger.info(f"  Market        : {status}")
        if not clock["is_open"]:
            logger.info(f"  Next open     : {clock['next_open']}")
        logger.info("────────────────────────────────────────────────────")
    except Exception:
        pass


def run_trading(cycles: int = 1, interval_seconds: int = 0) -> None:
    logger = logging.getLogger(__name__)

    mode = "Alpaca Paper" if config.use_alpaca and config.paper_trading else (
        "Alpaca LIVE" if config.use_alpaca else "Local Simulation"
    )
    logger.info(f"NYSE Algorithmic Trading Engine  —  {mode}")
    logger.info(f"Symbols      : {', '.join(config.symbols)}")
    logger.info(f"Max positions: {config.max_open_positions}")
    logger.info(f"Stop loss    : {config.stop_loss_pct * 100:.1f}%  "
                f"Take profit: {config.take_profit_pct * 100:.1f}%")

    if config.use_alpaca:
        print_alpaca_status(logger)

    engine = TradingEngine(config)

    for i in range(cycles):
        engine.run_cycle()
        if i < cycles - 1 and interval_seconds > 0:
            logger.info(f"  Sleeping {interval_seconds}s until next cycle…")
            time.sleep(interval_seconds)

    logger.info("Session complete.")


def run_backtest(start_date: str, end_date: str) -> None:
    logger = logging.getLogger(__name__)
    logger.info(f"Backtesting {start_date} → {end_date}")
    Backtester(config).run(config.symbols, start_date, end_date)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NYSE Algorithmic Trading Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    trade_p = sub.add_parser("trade", help="Run trading engine")
    trade_p.add_argument("--cycles", type=int, default=1, help="Number of cycles")
    trade_p.add_argument(
        "--interval", type=int, default=0,
        help="Seconds between cycles (0 = run immediately back-to-back)",
    )

    bt_p = sub.add_parser("backtest", help="Run historical backtest")
    default_end = datetime.now().strftime("%Y-%m-%d")
    default_start = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    bt_p.add_argument("--start", default=default_start, help="Start date YYYY-MM-DD")
    bt_p.add_argument("--end", default=default_end, help="End date YYYY-MM-DD")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command == "trade":
        run_trading(cycles=args.cycles, interval_seconds=args.interval)
    elif args.command == "backtest":
        run_backtest(args.start, args.end)


if __name__ == "__main__":
    main()
