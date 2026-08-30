from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from src.backtest_engine.indicators import (
    PivotKind,
    calculate_daily_indicators,
    calculate_intraday_indicators,
    daily_returns,
    detect_daily_pivots,
    intraday_indicators_as_of,
    is_dead_cross,
    is_golden_cross,
    moving_average_slope,
    pivots_as_of,
    simple_moving_average,
)
from src.backtest_engine.models import (
    DailyBar,
    FiveMinuteBar,
    Ohlcv,
    TimestampSemantics,
)
from src.backtest_engine.validation import KOREA_TZ

D = Decimal
KST = KOREA_TZ
ZERO = D("0")


def price_bar(
    close: Decimal, *, raw_close: Decimal | None = None
) -> tuple[Ohlcv, Ohlcv]:
    signal = Ohlcv(close, close + D("1"), close - D("1"), close, 100)
    raw_value = raw_close if raw_close is not None else close
    raw = Ohlcv(raw_value, raw_value + D("1"), raw_value - D("1"), raw_value, 100)
    return raw, signal


def daily_series(count: int, *, raw_offset: Decimal = ZERO) -> list[DailyBar]:
    start = date(2024, 1, 1)
    result: list[DailyBar] = []
    for index in range(count):
        close = D(10 + index)
        raw, signal = price_bar(close, raw_close=close + raw_offset)
        result.append(DailyBar("005930", start + timedelta(days=index), raw, signal))
    return result


def minute(
    start: datetime,
    close: Decimal,
    *,
    raw_close: Decimal | None = None,
    available_at: datetime | None = None,
) -> FiveMinuteBar:
    raw, signal = price_bar(close, raw_close=raw_close)
    return FiveMinuteBar.from_source_timestamp(
        stock_code="005930",
        source_timestamp=start,
        source_timestamp_semantics=TimestampSemantics.START,
        raw=raw,
        signal=signal,
        signal_available_at=available_at,
    )


class MovingAverageTests(unittest.TestCase):
    def test_sma_normal_boundary_and_insufficient_data(self) -> None:
        values = [D(index) for index in range(1, 11)]
        result = simple_moving_average(values, 10)
        self.assertEqual([None] * 9, result[:9])
        self.assertEqual(D("5.5"), result[9])
        self.assertEqual([None] * 9, simple_moving_average(values[:9], 10))
        with self.assertRaisesRegex(ValueError, "positive"):
            simple_moving_average(values, 0)

    def test_ma_slope_normal_zero_and_insufficient_data(self) -> None:
        increasing = [D("10"), D("11"), D("12"), D("13"), D("14"), D("20")]
        self.assertEqual(D("100"), moving_average_slope(increasing, 5)[5])
        self.assertEqual(D("0"), moving_average_slope([D("10")] * 6, 5)[5])
        self.assertEqual([None] * 5, moving_average_slope([D("10")] * 5, 5))

    def test_daily_return(self) -> None:
        self.assertEqual(
            [None, D("10.0"), D("-10.0")], daily_returns([D("10"), D("11"), D("9.9")])
        )


class DailyIndicatorTests(unittest.TestCase):
    def test_daily_sma_slope_and_raw_signal_separation(self) -> None:
        signal_based = calculate_daily_indicators(
            daily_series(65, raw_offset=D("1000"))
        )
        same_signal = calculate_daily_indicators(daily_series(65, raw_offset=D("5000")))
        self.assertEqual(signal_based, same_signal)
        self.assertIsNone(signal_based[8].sma10)
        self.assertEqual(D("14.5"), signal_based[9].sma10)
        self.assertIsNotNone(signal_based[-1].ma60_slope_5)

    def test_future_removal_does_not_change_prior_daily_indicators(self) -> None:
        bars = daily_series(70)
        full = calculate_daily_indicators(bars)
        for cutoff in (10, 20, 65):
            self.assertEqual(full[:cutoff], calculate_daily_indicators(bars[:cutoff]))


class IntradayIndicatorTests(unittest.TestCase):
    def test_five_minute_ma_continues_across_date_boundary(self) -> None:
        day_one_start = datetime(2024, 1, 2, 14, 45, tzinfo=KST)
        bars = [
            minute(day_one_start + timedelta(minutes=5 * i), D(10 + i))
            for i in range(9)
        ]
        bars.append(minute(datetime(2024, 1, 3, 9, 0, tzinfo=KST), D("19")))
        points = calculate_intraday_indicators(bars)
        self.assertEqual(D("14.5"), points[-1].sma10)

    def test_golden_and_dead_cross_boundary_conditions(self) -> None:
        self.assertTrue(is_golden_cross(D("10"), D("10"), D("11"), D("10")))
        self.assertFalse(is_golden_cross(D("11"), D("10"), D("12"), D("10")))
        self.assertFalse(is_golden_cross(None, D("10"), D("11"), D("10")))
        self.assertTrue(is_dead_cross(D("10"), D("10"), D("9"), D("10")))
        self.assertFalse(is_dead_cross(D("9"), D("10"), D("8"), D("10")))

    def test_signal_is_unavailable_before_bar_availability(self) -> None:
        start = datetime(2024, 1, 2, 9, 0, tzinfo=KST)
        bar = minute(start, D("10"))
        self.assertEqual(
            [],
            intraday_indicators_as_of([bar], start + timedelta(minutes=4, seconds=59)),
        )
        available = intraday_indicators_as_of([bar], start + timedelta(minutes=5))
        self.assertEqual(1, len(available))
        self.assertTrue(available[0].is_usable_at(start + timedelta(minutes=5)))

    def test_raw_prices_do_not_affect_intraday_indicators(self) -> None:
        start = datetime(2024, 1, 2, 9, 0, tzinfo=KST)
        first = [
            minute(start + timedelta(minutes=5 * i), D(10 + i), raw_close=D(100 + i))
            for i in range(20)
        ]
        second = [
            minute(start + timedelta(minutes=5 * i), D(10 + i), raw_close=D(1000 + i))
            for i in range(20)
        ]
        self.assertEqual(
            calculate_intraday_indicators(first), calculate_intraday_indicators(second)
        )

    def test_future_removal_does_not_change_prior_intraday_indicators(self) -> None:
        start = datetime(2024, 1, 2, 9, 0, tzinfo=KST)
        bars = [minute(start + timedelta(minutes=5 * i), D(10 + i)) for i in range(70)]
        full = calculate_intraday_indicators(bars)
        for cutoff in (10, 20, 65):
            self.assertEqual(
                full[:cutoff], calculate_intraday_indicators(bars[:cutoff])
            )


class PivotTests(unittest.TestCase):
    def pivot_fixture(self) -> list[DailyBar]:
        lows = [D("10"), D("9"), D("5"), D("9"), D("10")]
        highs = [D("12"), D("13"), D("11"), D("13"), D("12")]
        bars: list[DailyBar] = []
        for index, (low, high) in enumerate(zip(lows, highs)):
            middle = (low + high) / D("2")
            values = Ohlcv(middle, high, low, middle, 100)
            bars.append(
                DailyBar(
                    "005930", date(2024, 1, 2) + timedelta(days=index), values, values
                )
            )
        return bars

    def test_pivot_is_confirmed_at_t_plus_two_close(self) -> None:
        pivots = detect_daily_pivots(self.pivot_fixture())
        self.assertEqual(1, len(pivots))
        pivot = pivots[0]
        self.assertEqual(PivotKind.LOW, pivot.kind)
        self.assertEqual(date(2024, 1, 4), pivot.pivot_trade_date)
        self.assertEqual(
            datetime.combine(date(2024, 1, 6), time(15, 30), tzinfo=KST),
            pivot.confirmed_at,
        )

    def test_pivot_is_not_usable_before_confirmation(self) -> None:
        pivots = detect_daily_pivots(self.pivot_fixture())
        before = datetime(2024, 1, 6, 15, 29, 59, tzinfo=KST)
        at_confirmation = datetime(2024, 1, 6, 15, 30, tzinfo=KST)
        self.assertEqual([], pivots_as_of(pivots, before))
        self.assertEqual(pivots, pivots_as_of(pivots, at_confirmation))

    def test_future_removal_does_not_expose_unconfirmed_pivot(self) -> None:
        bars = self.pivot_fixture()
        full_pivots = detect_daily_pivots(bars)
        as_of_t_plus_one = datetime(2024, 1, 5, 15, 30, tzinfo=KST)
        self.assertEqual([], pivots_as_of(full_pivots, as_of_t_plus_one))
        self.assertEqual([], detect_daily_pivots(bars[:4]))


if __name__ == "__main__":
    unittest.main()
