from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from src.backtest_engine.models import (
    DailyBar,
    FiveMinuteBar,
    Ohlcv,
    TimestampSemantics,
    TradingSession,
)
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.backtest_engine.validation import (
    KOREA_TZ,
    MarketDataValidationError,
    validate_daily_bars,
    validate_five_minute_bars,
)

D = Decimal
KST = KOREA_TZ


def ohlcv(close: str = "100", *, volume: int = 100) -> Ohlcv:
    value = D(close)
    return Ohlcv(value, value + D("2"), value - D("2"), value, volume)


def daily_bar(
    day: date = date(2024, 1, 2),
    *,
    stock_code: str = "005930",
    raw: Ohlcv | None = None,
    signal: Ohlcv | None = None,
) -> DailyBar:
    return DailyBar(stock_code, day, raw or ohlcv("100"), signal or ohlcv("100"))


def minute_bar(
    start: datetime | None = None,
    *,
    stock_code: str = "005930",
    raw: Ohlcv | None = None,
    signal: Ohlcv | None = None,
) -> FiveMinuteBar:
    return FiveMinuteBar.from_source_timestamp(
        stock_code=stock_code,
        source_timestamp=start or datetime(2024, 1, 2, 9, 0, tzinfo=KST),
        source_timestamp_semantics=TimestampSemantics.START,
        raw=raw or ohlcv("100"),
        signal=signal or ohlcv("100"),
    )


class StandardModelTests(unittest.TestCase):
    def test_start_and_end_timestamp_conversion(self) -> None:
        start = datetime(2024, 1, 2, 9, 0, tzinfo=KST)
        start_labeled = minute_bar(start)
        self.assertEqual(start, start_labeled.bar_start_at)
        self.assertEqual(start + timedelta(minutes=5), start_labeled.bar_end_at)
        self.assertEqual(start_labeled.bar_end_at, start_labeled.signal_available_at)

        end = start + timedelta(minutes=5)
        end_labeled = FiveMinuteBar.from_source_timestamp(
            stock_code="005930",
            source_timestamp=end,
            source_timestamp_semantics=TimestampSemantics.END,
            raw=ohlcv(),
            signal=ohlcv(),
        )
        self.assertEqual(start, end_labeled.bar_start_at)
        self.assertEqual(end, end_labeled.bar_end_at)
        validate_five_minute_bars([start_labeled])
        validate_five_minute_bars([end_labeled])

    def test_decimal_price_contract_rejects_float(self) -> None:
        invalid = Ohlcv(100.0, D("102"), D("98"), D("100"), 100)  # type: ignore[arg-type]
        with self.assertRaisesRegex(
            MarketDataValidationError, "raw.open must be Decimal"
        ):
            validate_daily_bars([daily_bar(raw=invalid)])


class DailyValidationTests(unittest.TestCase):
    def test_valid_daily_data_and_calendar(self) -> None:
        calendar = ExplicitTradingCalendar([date(2024, 1, 2), date(2024, 1, 3)])
        validate_daily_bars(
            [daily_bar(date(2024, 1, 2)), daily_bar(date(2024, 1, 3))],
            calendar,
        )

    def test_invalid_stock_code_duplicate_and_unsorted_fail(self) -> None:
        with self.assertRaisesRegex(MarketDataValidationError, "six ASCII digits"):
            validate_daily_bars([daily_bar(stock_code="5930")])
        duplicate = daily_bar()
        with self.assertRaisesRegex(MarketDataValidationError, "duplicate"):
            validate_daily_bars([duplicate, duplicate])
        with self.assertRaisesRegex(MarketDataValidationError, "strictly ordered"):
            validate_daily_bars(
                [daily_bar(date(2024, 1, 3)), daily_bar(date(2024, 1, 2))]
            )

    def test_invalid_ohlc_volume_and_raw_signal_alignment_fail(self) -> None:
        invalid_ohlc = Ohlcv(D("100"), D("99"), D("98"), D("100"), 1)
        with self.assertRaisesRegex(MarketDataValidationError, "raw.open"):
            validate_daily_bars([daily_bar(raw=invalid_ohlc)])
        with self.assertRaisesRegex(MarketDataValidationError, "volume must be >= 0"):
            validate_daily_bars([daily_bar(signal=ohlcv(volume=-1))])
        unaligned = DailyBar("005930", date(2024, 1, 2), ohlcv(), None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(
            MarketDataValidationError, "both be present and aligned"
        ):
            validate_daily_bars([unaligned])


class IntradayValidationTests(unittest.TestCase):
    def test_timezone_semantics_bounds_and_regular_session_are_valid(self) -> None:
        calendar = ExplicitTradingCalendar([date(2024, 1, 2)])
        validate_five_minute_bars([minute_bar()], calendar)

    def test_naive_timestamp_and_missing_semantics_fail(self) -> None:
        valid = minute_bar()
        naive = replace(
            valid,
            source_timestamp=valid.source_timestamp.replace(tzinfo=None),
        )
        with self.assertRaisesRegex(MarketDataValidationError, "timezone-aware"):
            validate_five_minute_bars([naive])
        missing = replace(valid, source_timestamp_semantics=None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(MarketDataValidationError, "must be START or END"):
            validate_five_minute_bars([missing])

    def test_invalid_availability_semantics_and_duration_fail(self) -> None:
        valid = minute_bar()
        early = replace(
            valid, signal_available_at=valid.bar_end_at - timedelta(seconds=1)
        )
        with self.assertRaisesRegex(MarketDataValidationError, "bar_start_at <"):
            validate_five_minute_bars([early])
        wrong_source = replace(valid, source_timestamp=valid.bar_end_at)
        with self.assertRaisesRegex(MarketDataValidationError, "declared semantics"):
            validate_five_minute_bars([wrong_source])
        wrong_duration = replace(
            valid,
            bar_end_at=valid.bar_end_at + timedelta(minutes=1),
            signal_available_at=valid.signal_available_at + timedelta(minutes=1),
        )
        with self.assertRaisesRegex(MarketDataValidationError, "exactly five minutes"):
            validate_five_minute_bars([wrong_duration])

    def test_after_hours_and_out_of_session_fail(self) -> None:
        after_hours = replace(minute_bar(), session=TradingSession.AFTER_HOURS)
        with self.assertRaisesRegex(MarketDataValidationError, "REGULAR session only"):
            validate_five_minute_bars([after_hours])
        outside = minute_bar(datetime(2024, 1, 2, 8, 55, tzinfo=KST))
        with self.assertRaisesRegex(MarketDataValidationError, "outside KRX"):
            validate_five_minute_bars([outside])

    def test_duplicate_unsorted_and_invalid_stock_code_fail(self) -> None:
        first = minute_bar()
        with self.assertRaisesRegex(MarketDataValidationError, "duplicate"):
            validate_five_minute_bars([first, first])
        earlier = minute_bar(datetime(2024, 1, 2, 9, 5, tzinfo=KST))
        with self.assertRaisesRegex(MarketDataValidationError, "strictly ordered"):
            validate_five_minute_bars([earlier, first])
        with self.assertRaisesRegex(MarketDataValidationError, "six ASCII digits"):
            validate_five_minute_bars([minute_bar(stock_code="5930")])


if __name__ == "__main__":
    unittest.main()
