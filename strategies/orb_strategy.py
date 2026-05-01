from dataclasses import dataclass
from datetime import datetime, time


@dataclass
class ORBLevels:
    high: float
    low: float


class ORBStrategy:
    """
    Opening Range Breakout Strategy (stateful version)
    - Only trades AFTER opening range is set
    - Only 1 signal per direction per day
    """

    def __init__(self, opening_minutes=30):
        self.opening_minutes = opening_minutes
        self.opening_range = None
        self.previous_high = None
        self.previous_low = None

        self.range_set = False
        self.traded_today = False
        self.direction_taken = None

        self.session_start = time(9, 30)
        self.range_end = time(10, 0)  # default 30 min ORB

    # ---------------------------
    # Setup
    # ---------------------------

    def set_previous_day_levels(self, high, low):
        self.previous_high = high
        self.previous_low = low

    def set_opening_range(self, high, low):
        self.opening_range = ORBLevels(high, low)
        self.range_set = True

    # ---------------------------
    # Core logic
    # ---------------------------

    def can_trade(self, now: datetime):
        """
        Only allow trades after ORB window is complete
        """
        return now.time() > self.range_end

    def check_breakout(self, price: float, now: datetime = None):
        """
        Main ORB decision engine
        Returns trade signal or None
        """

        if not self.range_set:
            return None

        if self.traded_today:
            return None

        if now and not self.can_trade(now):
            return None

        # ---------------------------
        # LONG BREAKOUT
        # ---------------------------
        if price > self.opening_range.high:
            self.traded_today = True
            self.direction_taken = "long"

            return {
                "direction": "long",
                "entry": price,
                "target": self.previous_high,
                "stop": self.opening_range.low
            }

        # ---------------------------
        # SHORT BREAKOUT
        # ---------------------------
        if price < self.opening_range.low:
            self.traded_today = True
            self.direction_taken = "short"

            return {
                "direction": "short",
                "entry": price,
                "target": self.previous_low,
                "stop": self.opening_range.high
            }

        return None

    # ---------------------------
    # Reset daily state
    # ---------------------------

    def reset(self):
        self.traded_today = False
        self.direction_taken = None