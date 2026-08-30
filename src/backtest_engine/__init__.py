"""Reusable market-data foundation for the Strategy V1 backtest engine."""

from .indicators import (
    DailyIndicatorPoint,
    DailyPivotCandidate,
    IntradayIndicatorPoint,
    PivotKind,
    calculate_daily_indicators,
    calculate_intraday_indicators,
    detect_daily_pivots,
    intraday_indicators_as_of,
    pivots_as_of,
)
from .models import (
    DailyBar,
    FiveMinuteBar,
    Ohlcv,
    TimestampSemantics,
    TradingSession,
)
from .trading_calendar import ExplicitTradingCalendar, TradingCalendar
from .validation import (
    MarketDataValidationError,
    validate_daily_bars,
    validate_five_minute_bars,
)

__all__ = [
    "DailyBar",
    "DailyIndicatorPoint",
    "DailyPivotCandidate",
    "ExplicitTradingCalendar",
    "FiveMinuteBar",
    "IntradayIndicatorPoint",
    "MarketDataValidationError",
    "Ohlcv",
    "PivotKind",
    "TimestampSemantics",
    "TradingCalendar",
    "TradingSession",
    "calculate_daily_indicators",
    "calculate_intraday_indicators",
    "detect_daily_pivots",
    "intraday_indicators_as_of",
    "pivots_as_of",
    "validate_daily_bars",
    "validate_five_minute_bars",
]
