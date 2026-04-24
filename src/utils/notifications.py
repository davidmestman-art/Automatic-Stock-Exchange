import logging
import os
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


class Notifier:
    """Send trade notifications via ntfy.sh and/or Pushover.

    Configure via .env:
      NTFY_TOPIC=your-topic-name
      PUSHOVER_TOKEN=your-app-token
      PUSHOVER_USER=your-user-key
    """

    def __init__(
        self,
        ntfy_topic: str = "",
        pushover_token: str = "",
        pushover_user: str = "",
    ):
        self.ntfy_topic = ntfy_topic
        self.pushover_token = pushover_token
        self.pushover_user = pushover_user

    @classmethod
    def from_env(cls) -> "Notifier":
        return cls(
            ntfy_topic=os.getenv("NTFY_TOPIC", ""),
            pushover_token=os.getenv("PUSHOVER_TOKEN", ""),
            pushover_user=os.getenv("PUSHOVER_USER", ""),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.ntfy_topic or (self.pushover_token and self.pushover_user))

    def notify(self, title: str, message: str, priority: str = "default") -> None:
        if self.ntfy_topic:
            self._ntfy(title, message, priority)
        if self.pushover_token and self.pushover_user:
            self._pushover(title, message)

    def _ntfy(self, title: str, message: str, priority: str) -> None:
        try:
            url = f"https://ntfy.sh/{self.ntfy_topic}"
            req = urllib.request.Request(
                url,
                data=message.encode(),
                headers={"Title": title, "Priority": priority},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
            logger.debug(f"ntfy sent: {title}")
        except Exception as e:
            logger.warning(f"ntfy notification failed: {e}")

    def _pushover(self, title: str, message: str) -> None:
        try:
            payload = urllib.parse.urlencode({
                "token": self.pushover_token,
                "user": self.pushover_user,
                "title": title,
                "message": message,
            }).encode()
            req = urllib.request.Request(
                "https://api.pushover.net/1/messages.json",
                data=payload,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
            logger.debug(f"Pushover sent: {title}")
        except Exception as e:
            logger.warning(f"Pushover notification failed: {e}")

    # ── Convenience helpers ───────────────────────────────────────────────────

    def trade_buy(self, symbol: str, shares: float, price: float, reason: str) -> None:
        if not self.enabled:
            return
        self.notify(
            f"BUY {symbol}",
            f"Bought {shares:.0f} shares @ ${price:.2f}\n{reason}",
        )

    def trade_sell(
        self, symbol: str, shares: float, price: float, pnl: float, reason: str
    ) -> None:
        if not self.enabled:
            return
        sign = "+" if pnl >= 0 else ""
        self.notify(
            f"SELL {symbol}  {sign}${pnl:.2f}",
            f"Sold {shares:.0f} shares @ ${price:.2f}\n{reason}",
            priority="default" if pnl >= 0 else "high",
        )

    def voo_alert(self, price: float, ma200w: float, gap_pct: float) -> None:
        if not self.enabled:
            return
        direction = "ABOVE" if price > ma200w else "BELOW"
        self.notify(
            f"VOO 200W MA — {direction}",
            f"VOO ${price:.2f}  200W MA ${ma200w:.2f}  gap {gap_pct:+.1f}%",
            priority="high",
        )
