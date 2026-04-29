from typing import Dict, List, Optional

# Sector ETF proxies for sector-momentum computation
SECTOR_ETFS: Dict[str, str] = {
    "Technology":             "XLK",
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples":       "XLP",
    "Financials":             "XLF",
    "Health Care":            "XLV",
    "Industrials":            "XLI",
    "Energy":                 "XLE",
    "Materials":              "XLB",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
}

# GICS sector assignments for every symbol in SP500_UNIVERSE
SECTOR_MAP: Dict[str, str] = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AVGO": "Technology", "ORCL": "Technology", "CRM": "Technology",
    "ADBE": "Technology", "AMD": "Technology", "QCOM": "Technology",
    "TXN": "Technology", "INTC": "Technology", "AMAT": "Technology",
    "LRCX": "Technology", "KLAC": "Technology", "MU": "Technology",
    "ADI": "Technology", "MRVL": "Technology", "NOW": "Technology",
    "PANW": "Technology", "SNPS": "Technology", "CDNS": "Technology",
    "FTNT": "Technology", "KEYS": "Technology", "CTSH": "Technology",
    "IBM": "Technology", "ACN": "Technology", "CSCO": "Technology",
    "HPQ": "Technology", "CDW": "Technology",
    "PLTR": "Technology", "DELL": "Technology", "ANET": "Technology",
    "APP": "Technology", "CRWD": "Technology",
    # Communication Services
    "GOOGL": "Communication Services", "META": "Communication Services",
    "NFLX": "Communication Services", "T": "Communication Services",
    "VZ": "Communication Services", "TMUS": "Communication Services",
    "DIS": "Communication Services", "CMCSA": "Communication Services",
    "CHTR": "Communication Services", "WBD": "Communication Services",
    # Consumer Discretionary
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary", "MCD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary", "SBUX": "Consumer Discretionary",
    "TGT": "Consumer Discretionary", "LOW": "Consumer Discretionary",
    "BKNG": "Consumer Discretionary", "MAR": "Consumer Discretionary",
    "CMG": "Consumer Discretionary", "HLT": "Consumer Discretionary",
    "YUM": "Consumer Discretionary", "DG": "Consumer Discretionary",
    "DLTR": "Consumer Discretionary", "ORLY": "Consumer Discretionary",
    "AZO": "Consumer Discretionary", "GM": "Consumer Discretionary",
    "F": "Consumer Discretionary", "PHM": "Consumer Discretionary",
    "ABNB": "Consumer Discretionary", "UBER": "Consumer Discretionary",
    # Consumer Staples
    "WMT": "Consumer Staples", "COST": "Consumer Staples",
    "PG": "Consumer Staples", "KO": "Consumer Staples",
    "PEP": "Consumer Staples", "PM": "Consumer Staples",
    "MO": "Consumer Staples", "CL": "Consumer Staples",
    "MDLZ": "Consumer Staples", "GIS": "Consumer Staples",
    "STZ": "Consumer Staples", "SYY": "Consumer Staples",
    "HSY": "Consumer Staples", "CHD": "Consumer Staples",
    "EL": "Consumer Staples", "CLX": "Consumer Staples",
    "KHC": "Consumer Staples",
    # Financials
    "JPM": "Financials", "V": "Financials", "MA": "Financials",
    "BAC": "Financials", "WFC": "Financials", "GS": "Financials",
    "MS": "Financials", "BLK": "Financials", "SCHW": "Financials",
    "AXP": "Financials", "C": "Financials", "USB": "Financials",
    "PNC": "Financials", "TFC": "Financials", "COF": "Financials",
    "CME": "Financials", "ICE": "Financials", "CB": "Financials",
    "MET": "Financials", "PRU": "Financials", "AFL": "Financials",
    "AON": "Financials", "MMC": "Financials", "BX": "Financials",
    "KKR": "Financials", "SPGI": "Financials", "MCO": "Financials",
    "FIS": "Financials", "PYPL": "Financials", "FI": "Financials",
    # Health Care
    "UNH": "Health Care", "LLY": "Health Care", "JNJ": "Health Care",
    "MRK": "Health Care", "ABBV": "Health Care", "TMO": "Health Care",
    "ABT": "Health Care", "DHR": "Health Care", "BMY": "Health Care",
    "PFE": "Health Care", "AMGN": "Health Care", "GILD": "Health Care",
    "CVS": "Health Care", "CI": "Health Care", "ELV": "Health Care",
    "HUM": "Health Care", "MDT": "Health Care", "SYK": "Health Care",
    "BSX": "Health Care", "ISRG": "Health Care", "ZTS": "Health Care",
    "REGN": "Health Care", "VRTX": "Health Care", "DXCM": "Health Care",
    "IDXX": "Health Care", "RMD": "Health Care", "BAX": "Health Care",
    "MRNA": "Health Care", "BIIB": "Health Care", "IQV": "Health Care",
    # Industrials
    "GE": "Industrials", "RTX": "Industrials", "HON": "Industrials",
    "CAT": "Industrials", "DE": "Industrials", "BA": "Industrials",
    "UNP": "Industrials", "UPS": "Industrials", "FDX": "Industrials",
    "LMT": "Industrials", "NOC": "Industrials", "GD": "Industrials",
    "EMR": "Industrials", "ETN": "Industrials", "ROK": "Industrials",
    "PH": "Industrials", "ITW": "Industrials", "MMM": "Industrials",
    "IR": "Industrials", "CTAS": "Industrials", "CSX": "Industrials",
    "NSC": "Industrials", "EXPD": "Industrials", "GWW": "Industrials",
    "FAST": "Industrials", "VRSK": "Industrials", "PWR": "Industrials",
    "ODFL": "Industrials", "CARR": "Industrials", "OTIS": "Industrials",
    "GEV": "Industrials",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "SLB": "Energy", "EOG": "Energy", "OXY": "Energy",
    "MPC": "Energy", "PSX": "Energy", "VLO": "Energy",
    "BKR": "Energy", "HAL": "Energy", "DVN": "Energy",
    "WMB": "Energy", "KMI": "Energy", "OKE": "Energy",
    "LNG": "Energy", "TRGP": "Energy", "FANG": "Energy", "APA": "Energy",
    # Materials
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials",
    "ECL": "Materials", "NEM": "Materials", "FCX": "Materials",
    "NUE": "Materials", "VMC": "Materials", "MLM": "Materials",
    "ALB": "Materials", "DOW": "Materials", "PPG": "Materials",
    "IFF": "Materials", "DD": "Materials", "RPM": "Materials",
    # Utilities
    "NEE": "Utilities", "SO": "Utilities", "DUK": "Utilities",
    "D": "Utilities", "AEP": "Utilities", "EXC": "Utilities",
    "SRE": "Utilities", "PEG": "Utilities", "XEL": "Utilities",
    "ES": "Utilities", "VST": "Utilities", "CEG": "Utilities",
    # Real Estate
    "AMT": "Real Estate", "PLD": "Real Estate", "EQIX": "Real Estate",
    "CCI": "Real Estate", "PSA": "Real Estate", "O": "Real Estate",
    "SPG": "Real Estate", "WELL": "Real Estate", "AVB": "Real Estate",
    "DLR": "Real Estate",
}


def get_sector(symbol: str) -> Optional[str]:
    return SECTOR_MAP.get(symbol)


def sector_position_count(symbol: str, open_positions: dict) -> int:
    """Return how many open positions share the same sector as symbol."""
    target = SECTOR_MAP.get(symbol)
    if not target:
        return 0
    return sum(1 for sym in open_positions if SECTOR_MAP.get(sym) == target)


def positions_by_sector(open_positions: dict) -> Dict[str, List[str]]:
    """Group position symbols by their sector."""
    result: Dict[str, List[str]] = {}
    for sym in open_positions:
        sector = SECTOR_MAP.get(sym, "Unknown")
        result.setdefault(sector, []).append(sym)
    return result
