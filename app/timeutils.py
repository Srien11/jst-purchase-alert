from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import settings


def business_now() -> datetime:
    return datetime.now(ZoneInfo(getattr(settings, "timezone", "Asia/Shanghai")))


def business_today() -> date:
    return business_now().date()


def business_naive_now() -> datetime:
    return business_now().replace(tzinfo=None)
