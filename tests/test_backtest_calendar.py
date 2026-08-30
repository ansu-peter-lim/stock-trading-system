from __future__ import annotations

import unittest
from datetime import date

from src.backtest_engine.trading_calendar import (
    CalendarRangeError,
    ExplicitTradingCalendar,
)


class ExplicitTradingCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calendar = ExplicitTradingCalendar(
            [date(2024, 1, 5), date(2024, 1, 2), date(2024, 1, 4)]
        )

    def test_explicit_sessions_are_sorted_and_queryable(self) -> None:
        self.assertEqual(
            (date(2024, 1, 2), date(2024, 1, 4), date(2024, 1, 5)),
            self.calendar.trading_days,
        )
        self.assertTrue(self.calendar.is_trading_day(date(2024, 1, 4)))
        self.assertFalse(self.calendar.is_trading_day(date(2024, 1, 3)))

    def test_next_and_previous_use_strict_date_boundaries(self) -> None:
        self.assertEqual(
            date(2024, 1, 4), self.calendar.next_trading_day(date(2024, 1, 2))
        )
        self.assertEqual(
            date(2024, 1, 4), self.calendar.next_trading_day(date(2024, 1, 3))
        )
        self.assertEqual(
            date(2024, 1, 2), self.calendar.previous_trading_day(date(2024, 1, 3))
        )
        self.assertEqual(
            date(2024, 1, 4), self.calendar.previous_trading_day(date(2024, 1, 5))
        )

    def test_calendar_range_and_fixture_errors_are_explicit(self) -> None:
        with self.assertRaises(CalendarRangeError):
            self.calendar.next_trading_day(date(2024, 1, 5))
        with self.assertRaises(CalendarRangeError):
            self.calendar.previous_trading_day(date(2024, 1, 2))
        with self.assertRaisesRegex(ValueError, "duplicates"):
            ExplicitTradingCalendar([date(2024, 1, 2), date(2024, 1, 2)])
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            ExplicitTradingCalendar([])


if __name__ == "__main__":
    unittest.main()
