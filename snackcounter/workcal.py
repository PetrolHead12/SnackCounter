"""Working time. "You have 4 hours to answer" must not tick through a night, a weekend or a holiday."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo


@dataclass(frozen=True)
class WorkCalendar:
    """Working time = Monday-Friday, open_hour to close_hour in the office's own timezone, minus holidays.

    Use zoneinfo (e.g. ZoneInfo("Europe/Oslo")) in production so daylight saving is handled.
    """

    tz: tzinfo
    open_hour: int = 9
    close_hour: int = 17
    holidays: frozenset[date] = frozenset()

    def is_workday(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def _at(self, day: date, hour: int) -> datetime:
        return datetime.combine(day, time(hour), tzinfo=self.tz)

    def add_working_hours(self, start: datetime, hours: float) -> datetime:
        """The moment (UTC) at which `hours` of working time have passed since `start`."""
        t = start.astimezone(self.tz)
        remaining = timedelta(hours=hours)
        while True:
            day = t.date()
            if not self.is_workday(day) or t >= self._at(day, self.close_hour):
                t = self._at(day + timedelta(days=1), self.open_hour)
                continue
            t = max(t, self._at(day, self.open_hour))
            room = self._at(day, self.close_hour) - t
            if remaining <= room:
                return (t + remaining).astimezone(timezone.utc)
            remaining -= room
            t = self._at(day, self.close_hour)
