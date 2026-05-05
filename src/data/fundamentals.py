"""Fundamental filter stub.

P/E, D/E, FCF, and EPS-growth data are not available from Alpaca.
All symbols pass so a missing fundamentals feed never silently blocks
the trading universe.
"""

import logging
from typing import List

logger = logging.getLogger(__name__)


class FundamentalFilter:
    def __init__(
        self,
        pe_max: float = 30.0,
        de_max: float = 2.0,
        require_positive_fcf: bool = True,
        require_positive_eps_growth: bool = True,
    ):
        self.pe_max = pe_max
        self.de_max = de_max

    def passes(self, symbol: str) -> bool:
        return True

    def filter(self, symbols: List[str]) -> List[str]:
        return list(symbols)
