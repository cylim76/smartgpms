from __future__ import annotations

from datetime import datetime, timedelta, timezone

BUSINESS_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def business_now() -> datetime:
    """Return the current business time independently of the host OS timezone."""
    return datetime.now(BUSINESS_TIMEZONE)
