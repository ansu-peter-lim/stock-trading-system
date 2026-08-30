"""Network-free trading-calendar abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from datetime import date, datetime


class CalendarRangeError(LookupError):
    """A requested adjacent session is outside the loaded calendar range."""


class TradingCalendar(ABC):
    """Minimal calendar interface needed by the first backtest phase."""

    @abstractmethod
    def is_trading_day(self, day: date) -> bool:
        """Return whether *day* is an explicitly known trading session."""

    @abstractmethod
    def next_trading_day(self, day: date) -> date:
        """Return the first loaded trading session strictly after *day*."""

    @abstractmethod
    def previous_trading_day(self, day: date) -> date:
        """Return the last loaded trading session strictly before *day*."""


class ExplicitTradingCalendar(TradingCalendar):
    """Calendar backed by a small explicit list, suitable for fixtures."""

    def __init__(self, trading_days: Iterable[date]) -> None:
        values = tuple(trading_days)
        if not values:
            raise ValueError("trading_days must not be empty")
        if any(
            not isinstance(day, date) or isinstance(day, datetime) for day in values
        ):
            raise TypeError("trading_days must contain date values")
        if len(set(values)) != len(values):
            raise ValueError("trading_days must not contain duplicates")
        self._trading_days = tuple(sorted(values))
        self._trading_day_set = frozenset(values)

    @property
    def trading_days(self) -> tuple[date, ...]:
        return self._trading_days

    def is_trading_day(self, day: date) -> bool:
        return day in self._trading_day_set

    def next_trading_day(self, day: date) -> date:
        index = bisect_right(self._trading_days, day)
        if index >= len(self._trading_days):
            raise CalendarRangeError(
                f"no trading day is loaded after {day.isoformat()}"
            )
        return self._trading_days[index]

    def previous_trading_day(self, day: date) -> date:
        index = bisect_left(self._trading_days, day) - 1
        if index < 0:
            raise CalendarRangeError(
                f"no trading day is loaded before {day.isoformat()}"
            )
        return self._trading_days[index]
