#!/usr/bin/env python3
"""NYSE Algorithmic Trading Engine — entry point.

Usage:
  python main.py trade                       # single paper-trading cycle
  python main.py trade --cycles 5            # 5 consecutive cycles
  python main.py trade --cycles 5 --interval 3600   # every hour
  python main.py backtest                    # last 6 months
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


def run_paper_trading(cycles: int = 1, interval_seconds: int = 0) -> None:
    logger = logging.getLogger(__name__)
    logger.info("NYSE Algorithmic Trading Engine  —  Paper Trading Mode")
    logger.info(f"Symbols       : {', '.join(config.symbols)}")
    logger.info(f"Capital       : ${config.initial_capital:,.2f}")
    logger.info(f"Max positions : {config.max_open_positions}")
    logger.info(f"Stop loss     : {config.stop_loss_pct * 100:.1f}%")
    logger.info(f"Take profit   : {config.take_profit_pct * 100:.1f}%")

    engine = TradingEngine(config)
    for i in range(cycles):
        engine.run_cycle()
        if i < cycles - 1 and interval_seconds > 0:
            logger.info(f"Sleeping {interval_seconds}s until next cycle…")
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

    trade_p = sub.add_parser("trade", help="Run paper trading engine")
    trade_p.add_argument("--cycles", type=int, default=1, help="Number of cycles to run")
    trade_p.add_argument(
        "--interval", type=int, default=0, help="Seconds between cycles (0 = immediate)"
    )

    bt_p = sub.add_parser("backtest", help="Run historical backtest")
    default_end = datetime.now().strftime("%Y-%m-%d")
    default_start = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    bt_p.add_argument("--start", default=default_start, help="Start date YYYY-MM-DD")
    bt_p.add_argument("--end", default=default_end, help="End date YYYY-MM-DD")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command == "trade":
        run_paper_trading(cycles=args.cycles, interval_seconds=args.interval)
    elif args.command == "backtest":
        run_backtest(args.start, args.end)


if __name__ == "__main__":
    main()
