"""Date ranges for requests like "the apache logs from last month".

parse_period("last month") -> Period(start, end, label), a half-open range
[start, end) in local time. The master agent can pass the user's own words
and does not have to work out dates itself.

Accepted forms (case-insensitive):

    today, yesterday
    this week, last week            weeks start on Monday
    this month, last month
    this year, last year
    2026-09                         a calendar month
    2026                            a calendar year
    2026-09-15                      a single day
    last 7 days, past 30 days       the N days up to and including today
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import errors
from ..errors import RotatorError

_N_DAYS_RE = re.compile(r"^(?:last|past)\s+(\d{1,4})\s+days?$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")

SUPPORTED = ("today", "yesterday", "this week", "last week", "this month", "last month",
             "this year", "last year", "YYYY", "YYYY-MM", "YYYY-MM-DD", "last N days")


@dataclass(frozen=True)
class Period:
    start: datetime   # inclusive
    end: datetime     # exclusive
    label: str        # safe for archive names: letters, digits, '-' and '_'

    def contains(self, timestamp: float) -> bool:
        return self.start.timestamp() <= timestamp < self.end.timestamp()

    def to_dict(self) -> dict:
        return {"label": self.label, "start": self.start.isoformat(timespec="seconds"),
                "end": self.end.isoformat(timespec="seconds")}


def _midnight(day: datetime) -> datetime:
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def _normalise_month(year: int, month: int):
    # Month arithmetic without dateutil: month 0 is December of the previous year.
    return year + (month - 1) // 12, (month - 1) % 12 + 1


def _month(year: int, month: int) -> Period:
    year, month = _normalise_month(year, month)
    following = _normalise_month(year, month + 1)
    return Period(datetime(year, month, 1), datetime(*following, 1), f"{year:04d}-{month:02d}")


def _bad(text) -> RotatorError:
    return RotatorError(errors.INVALID_REQUEST, f"Cannot understand the period {text!r}",
                        period=text, supported_periods=list(SUPPORTED))


def parse_period(text: str, now: datetime = None) -> Period:
    """Turn words like "last month" into a Period. Raises INVALID_REQUEST if not understood."""
    if not isinstance(text, str) or not text.strip():
        raise _bad(text)
    now = now or datetime.now()
    today = _midnight(now)
    words = " ".join(text.strip().lower().replace("_", " ").split())
    words = words.removeprefix("from ").removeprefix("in ").removeprefix("during ")

    if words == "today":
        return Period(today, today + timedelta(days=1), today.strftime("%Y-%m-%d"))
    if words == "yesterday":
        day = today - timedelta(days=1)
        return Period(day, today, day.strftime("%Y-%m-%d"))
    if words in ("this week", "last week"):
        start = today - timedelta(days=today.weekday())
        if words == "last week":
            start -= timedelta(days=7)
        last_day = start + timedelta(days=6)
        return Period(start, start + timedelta(days=7),
                      f"{start:%Y-%m-%d}_{last_day:%Y-%m-%d}")
    if words == "this month":
        return _month(now.year, now.month)
    if words == "last month":
        return _month(now.year, now.month - 1)
    if words == "this year":
        return Period(datetime(now.year, 1, 1), datetime(now.year + 1, 1, 1), str(now.year))
    if words == "last year":
        return Period(datetime(now.year - 1, 1, 1), datetime(now.year, 1, 1), str(now.year - 1))

    match = _N_DAYS_RE.match(words)
    if match and int(match.group(1)) > 0:
        first = today - timedelta(days=int(match.group(1)) - 1)
        return Period(first, today + timedelta(days=1), f"{first:%Y-%m-%d}_{today:%Y-%m-%d}")

    try:
        if match := _MONTH_RE.match(words):
            year, month = int(match.group(1)), int(match.group(2))
            if 1 <= month <= 12 and year >= 1:
                return _month(year, month)
        elif match := _DAY_RE.match(words):
            day = datetime(*map(int, match.groups()))
            return Period(day, day + timedelta(days=1), day.strftime("%Y-%m-%d"))
        elif match := _YEAR_RE.match(words):
            year = int(match.group(1))
            if 1 <= year < 9999:
                return Period(datetime(year, 1, 1), datetime(year + 1, 1, 1), f"{year:04d}")
    except ValueError:
        pass  # e.g. 2026-02-30
    raise _bad(text)


def filter_by_period(items: list, period: Period) -> list:
    """Keep the items whose 'mtime' (seconds since the epoch) lies inside the period."""
    return [item for item in items if period.contains(item["mtime"])]
