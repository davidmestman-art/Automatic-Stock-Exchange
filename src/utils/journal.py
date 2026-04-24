import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "trade_journal.jsonl"


class TradeJournal:
    """Append-only JSONL trade log with indicator snapshots."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DEFAULT_PATH

    def log(
        self,
        action: str,
        symbol: str,
        shares: float,
        price: float,
        reason: str,
        indicators: Optional[Dict[str, Any]] = None,
        pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
    ) -> None:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "symbol": symbol,
            "shares": shares,
            "price": price,
            "reason": reason,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "indicators": indicators or {},
        }
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning(f"Journal write failed: {e}")

    def read_all(self) -> List[Dict]:
        if not self.path.exists():
            return []
        entries: List[Dict] = []
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.warning(f"Journal read failed: {e}")
        return entries

    def read_recent(self, n: int = 100) -> List[Dict]:
        return self.read_all()[-n:]

    def stats(self) -> Dict[str, Any]:
        """Compute summary stats from the journal."""
        trades = self.read_all()
        sells = [t for t in trades if t["action"] == "SELL" and t.get("pnl") is not None]
        if not sells:
            return {"total_trades": len(trades), "sell_trades": 0}

        pnls = [t["pnl"] for t in sells]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]

        return {
            "total_trades": len(trades),
            "sell_trades": len(sells),
            "win_rate": round(len(winners) / len(sells) * 100, 1),
            "avg_gain": round(sum(winners) / len(winners), 2) if winners else 0,
            "avg_loss": round(sum(losers) / len(losers), 2) if losers else 0,
            "best_trade": round(max(pnls), 2),
            "worst_trade": round(min(pnls), 2),
            "total_pnl": round(sum(pnls), 2),
        }
