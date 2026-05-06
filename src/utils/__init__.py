from datetime import datetime


def now_et() -> datetime:
    """Return current datetime in America/New_York (EDT/EST, DST-aware)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except ImportError:
        import pytz
        return datetime.now(pytz.timezone("America/New_York"))
