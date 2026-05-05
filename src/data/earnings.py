"""Earnings calendar stub.

Alpaca does not provide earnings dates.  All symbols are allowed through so
a missing earnings feed never silently suppresses valid trades.
"""

import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class EarningsCalendar:
    def __init__(self, buffer_days: int = 3):
        self.buffer_days = buffer_days

    def has_upcoming_earnings(self, symbol: str) -> bool:
        return False
