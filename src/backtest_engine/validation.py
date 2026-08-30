"""Strict input validation for standard backtest market data."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .models import (
    FIVE_MINUTES,
    DailyBar,
    FiveMinuteBar,
    Ohlcv,
    TimestampSemantics,
    TradingSession,
)
from .trading_calendar import TradingCalendar

KOREA_TZ = timezone(timedelta(hours=9), name="Asia/Seoul")
REGULAR_SESSION_START = time(9, 0)
REGULAR_SESSION_END = time(15, 30)


class MarketDataValidationError(ValueError):
    """Input data violates the standard market-data contract."""


def _validate_stock_code(value: object, context: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 6
        or not value.isascii()
        or not value.isdigit()
    ):
        raise MarketDataValidationError(
            f"{context}: stock_code must be exactly six ASCII digits as a string"
        )


def _validate_decimal_price(value: object, field: str, context: str) -> None:
    if not isinstance(value, Decimal):
        raise MarketDataValidationError(f"{context}: {field} must be Decimal")
    try:
        valid = value.is_finite() and value > 0
    except InvalidOperation:
        valid = False
    if not valid:
        raise MarketDataValidationError(
            f"{context}: {field} must be finite and positive"
        )


def validate_ohlcv(value: object, series: str, context: str) -> None:
    """Validate one raw or signal OHLCV without coercing it."""

    if not isinstance(value, Ohlcv):
        raise MarketDataValidationError(
            f"{context}: raw/signal OHLCV must both be present and aligned"
        )
    for field in ("open", "high", "low", "close"):
        _validate_decimal_price(getattr(value, field), f"{series}.{field}", context)
    if not isinstance(value.volume, int) or isinstance(value.volume, bool):
        raise MarketDataValidationError(
            f"{context}: {series}.volume must be an integer"
        )
    if value.volume < 0:
        raise MarketDataValidationError(f"{context}: {series}.volume must be >= 0")
    if value.low > value.high:
        raise MarketDataValidationError(f"{context}: {series}.low must be <= high")
    if not value.low <= value.open <= value.high:
        raise MarketDataValidationError(
            f"{context}: {series}.open must be between low and high"
        )
    if not value.low <= value.close <= value.high:
        raise MarketDataValidationError(
            f"{context}: {series}.close must be between low and high"
        )


def _is_timezone_aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def validate_daily_bars(
    bars: Sequence[DailyBar],
    calendar: TradingCalendar | None = None,
) -> None:
    """Validate completed daily bars, preserving input order and values."""

    seen: set[tuple[str, date]] = set()
    previous_by_stock: dict[str, date] = {}
    for index, bar in enumerate(bars):
        context = f"daily row {index}"
        if not isinstance(bar, DailyBar):
            raise MarketDataValidationError(f"{context}: expected DailyBar")
        _validate_stock_code(bar.stock_code, context)
        if not isinstance(bar.trade_date, date) or isinstance(bar.trade_date, datetime):
            raise MarketDataValidationError(f"{context}: trade_date must be a date")
        key = (bar.stock_code, bar.trade_date)
        if key in seen:
            raise MarketDataValidationError(
                f"{context}: duplicate stock_code/trade_date"
            )
        previous = previous_by_stock.get(bar.stock_code)
        if previous is not None and bar.trade_date <= previous:
            raise MarketDataValidationError(
                f"{context}: daily bars must be strictly ordered within each stock"
            )
        if calendar is not None and not calendar.is_trading_day(bar.trade_date):
            raise MarketDataValidationError(
                f"{context}: trade_date is not a trading day"
            )
        validate_ohlcv(bar.raw, "raw", context)
        validate_ohlcv(bar.signal, "signal", context)
        seen.add(key)
        previous_by_stock[bar.stock_code] = bar.trade_date


def validate_five_minute_bars(
    bars: Sequence[FiveMinuteBar],
    calendar: TradingCalendar | None = None,
) -> None:
    """Validate completed Strategy V1 regular-session five-minute bars."""

    source_keys: set[tuple[str, datetime]] = set()
    interval_keys: set[tuple[str, datetime]] = set()
    previous_by_stock: dict[str, datetime] = {}
    for index, bar in enumerate(bars):
        context = f"5-minute row {index}"
        if not isinstance(bar, FiveMinuteBar):
            raise MarketDataValidationError(f"{context}: expected FiveMinuteBar")
        _validate_stock_code(bar.stock_code, context)
        if not isinstance(bar.source_timestamp_semantics, TimestampSemantics):
            raise MarketDataValidationError(
                f"{context}: source_timestamp_semantics must be START or END"
            )
        for field in (
            "source_timestamp",
            "bar_start_at",
            "bar_end_at",
            "signal_available_at",
        ):
            if not _is_timezone_aware(getattr(bar, field)):
                raise MarketDataValidationError(
                    f"{context}: {field} must be timezone-aware"
                )
        if not bar.bar_start_at < bar.bar_end_at <= bar.signal_available_at:
            raise MarketDataValidationError(
                f"{context}: require bar_start_at < bar_end_at <= signal_available_at"
            )
        if bar.bar_end_at - bar.bar_start_at != FIVE_MINUTES:
            raise MarketDataValidationError(
                f"{context}: bar duration must be exactly five minutes"
            )
        expected_source = (
            bar.bar_start_at
            if bar.source_timestamp_semantics is TimestampSemantics.START
            else bar.bar_end_at
        )
        if bar.source_timestamp != expected_source:
            raise MarketDataValidationError(
                f"{context}: source timestamp does not match declared semantics"
            )
        if bar.session is not TradingSession.REGULAR:
            raise MarketDataValidationError(
                f"{context}: Strategy V1 accepts REGULAR session only"
            )

        start_local = bar.bar_start_at.astimezone(KOREA_TZ)
        end_local = bar.bar_end_at.astimezone(KOREA_TZ)
        if (
            start_local.date() != end_local.date()
            or start_local.time().replace(tzinfo=None) < REGULAR_SESSION_START
            or end_local.time().replace(tzinfo=None) > REGULAR_SESSION_END
        ):
            raise MarketDataValidationError(
                f"{context}: bar is outside KRX regular session 09:00-15:30"
            )
        if calendar is not None and not calendar.is_trading_day(start_local.date()):
            raise MarketDataValidationError(f"{context}: bar date is not a trading day")

        source_key = (bar.stock_code, bar.source_timestamp.astimezone(timezone.utc))
        interval_key = (bar.stock_code, bar.bar_start_at.astimezone(timezone.utc))
        if source_key in source_keys:
            raise MarketDataValidationError(
                f"{context}: duplicate stock_code/source_timestamp"
            )
        if interval_key in interval_keys:
            raise MarketDataValidationError(
                f"{context}: duplicate stock_code/bar_start_at"
            )
        previous = previous_by_stock.get(bar.stock_code)
        current = bar.bar_start_at.astimezone(timezone.utc)
        if previous is not None and current <= previous:
            raise MarketDataValidationError(
                f"{context}: 5-minute bars must be strictly ordered within each stock"
            )
        validate_ohlcv(bar.raw, "raw", context)
        validate_ohlcv(bar.signal, "signal", context)
        source_keys.add(source_key)
        interval_keys.add(interval_key)
        previous_by_stock[bar.stock_code] = current
