from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.core_strategy import (
    DailyCoreSignalGenerator,
    DailyCoreSignalType,
    DailyTrendClassifier,
    DailyTrendState,
    core_target_weight,
    is_uptrend_ma20_near,
)
from src.backtest_engine.indicators import (
    DailyIndicatorPoint,
    calculate_daily_indicators,
)
from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar

D = Decimal
DEFAULT_SMA20 = D("100")


def point(
    *,
    slope20: Decimal | None,
    slope60: Decimal | None,
    sma20: Decimal | None = DEFAULT_SMA20,
    day: date = date(2024, 1, 2),
) -> DailyIndicatorPoint:
    return DailyIndicatorPoint(
        "005930", day, D("1"), D("99"), sma20, D("90"), slope20, slope60
    )


def daily_bar(
    day: date,
    *,
    low: str,
    close: str,
    high: str = "110",
) -> DailyBar:
    signal = Ohlcv(D("100"), D(high), D(low), D(close), 100)
    raw = Ohlcv(D("100"), D(high), D(low), D(close), 100)
    return DailyBar("005930", day, raw, signal)


class DailyTrendClassifierTests(unittest.TestCase):
    def test_all_five_states(self) -> None:
        classifier = DailyTrendClassifier()
        cases = (
            (D("1"), D("2"), DailyTrendState.UP),
            (D("-1"), D("-2"), DailyTrendState.DOWN),
            (D("1"), D("-2"), DailyTrendState.MIXED),
            (D("0"), D("2"), DailyTrendState.NEUTRAL),
            (None, D("2"), DailyTrendState.INSUFFICIENT_DATA),
        )
        for slope20, slope60, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    expected,
                    classifier.classify(point(slope20=slope20, slope60=slope60)),
                )


class UptrendMa20BandTests(unittest.TestCase):
    def test_inclusive_low_and_close_boundaries(self) -> None:
        cases = (
            (D("97"), D("110"), True, "low at lower"),
            (D("103"), D("110"), True, "low at upper"),
            (D("90"), D("97"), True, "close at lower"),
            (D("90"), D("103"), True, "close at upper"),
            (D("96"), D("104"), False, "range-only overlap"),
            (D("100"), D("104"), True, "low inside"),
            (D("96"), D("100"), True, "close inside"),
        )
        for low, close, expected, label in cases:
            with self.subTest(label=label):
                self.assertEqual(
                    expected,
                    is_uptrend_ma20_near(
                        signal_low=low,
                        signal_close=close,
                        signal_ma20=D("100"),
                    ),
                )

    def test_signal_high_range_overlap_does_not_create_entry(self) -> None:
        day = date(2024, 1, 2)
        calendar = ExplicitTradingCalendar([day, day + timedelta(days=1)])
        generator = DailyCoreSignalGenerator(calendar)
        result = generator.evaluate(
            daily_bar(day, low="96", close="104", high="110"),
            point(slope20=D("1"), slope60=D("1"), day=day),
            holding_core=False,
            stock_full_weight=D("0.10"),
        )
        self.assertIsNone(result)

    def test_up_entry_uses_full_internal_ninety_percent(self) -> None:
        day = date(2024, 1, 2)
        calendar = ExplicitTradingCalendar([day, day + timedelta(days=1)])
        generator = DailyCoreSignalGenerator(calendar)
        result = generator.evaluate(
            daily_bar(day, low="100", close="104"),
            point(slope20=D("1"), slope60=D("1"), day=day),
            holding_core=False,
            stock_full_weight=D("0.10"),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(D("0.09"), result.target_core_weight)
        self.assertEqual(DailyCoreSignalType.ENTER, result.signal_type)
        self.assertEqual(D("0.09"), core_target_weight(D("0.10")))

    def test_non_up_states_never_create_mvp_entry(self) -> None:
        day = date(2024, 1, 2)
        calendar = ExplicitTradingCalendar([day, day + timedelta(days=1)])
        generator = DailyCoreSignalGenerator(calendar)
        cases = (
            (D("-1"), D("-1")),
            (D("1"), D("-1")),
            (D("0"), D("1")),
            (None, D("1")),
        )
        for slope20, slope60 in cases:
            with self.subTest(slope20=slope20, slope60=slope60):
                self.assertIsNone(
                    generator.evaluate(
                        daily_bar(day, low="100", close="104"),
                        point(slope20=slope20, slope60=slope60, day=day),
                        holding_core=False,
                        stock_full_weight=D("0.10"),
                    )
                )


class DailyNoLookAheadTests(unittest.TestCase):
    def test_future_daily_bar_does_not_change_prior_core_signal(self) -> None:
        start = date(2024, 1, 1)
        bars: list[DailyBar] = []
        for index in range(66):
            close = D(100 + index)
            low = D("155.5") if index == 65 else close - D(1)
            value = Ohlcv(close, close + D(1), low, close, 100)
            bars.append(DailyBar("005930", start + timedelta(days=index), value, value))
        future_close = D("50")
        future = Ohlcv(future_close, D("51"), D("49"), future_close, 100)
        extended = bars + [
            DailyBar("005930", start + timedelta(days=66), future, future)
        ]
        calendar = ExplicitTradingCalendar(
            [start + timedelta(days=index) for index in range(68)]
        )
        prefix_point = calculate_daily_indicators(bars, calendar)[-1]
        full_point = calculate_daily_indicators(extended, calendar)[65]
        generator = DailyCoreSignalGenerator(calendar)

        prefix_signal = generator.evaluate(
            bars[-1],
            prefix_point,
            holding_core=False,
            stock_full_weight=D("0.10"),
        )
        full_signal = generator.evaluate(
            extended[65],
            full_point,
            holding_core=False,
            stock_full_weight=D("0.10"),
        )
        self.assertEqual(prefix_point, full_point)
        self.assertEqual(prefix_signal, full_signal)


if __name__ == "__main__":
    unittest.main()
