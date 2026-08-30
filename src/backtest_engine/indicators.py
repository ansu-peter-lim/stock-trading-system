"""Pure trailing indicators computed exclusively from signal prices."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, localcontext
from enum import Enum

from .models import DailyBar, FiveMinuteBar
from .trading_calendar import TradingCalendar
from .validation import KOREA_TZ, validate_daily_bars, validate_five_minute_bars

HUNDRED = Decimal(100)
DEFAULT_DECIMAL_PRECISION = 28


@dataclass(frozen=True, slots=True)
class DailyIndicatorPoint:
    stock_code: str
    trade_date: date
    daily_return: Decimal | None
    sma10: Decimal | None
    sma20: Decimal | None
    sma60: Decimal | None
    ma20_slope_5: Decimal | None
    ma60_slope_5: Decimal | None


@dataclass(frozen=True, slots=True)
class IntradayIndicatorPoint:
    stock_code: str
    bar_start_at: datetime
    bar_end_at: datetime
    signal_available_at: datetime
    sma10: Decimal | None
    sma20: Decimal | None
    sma60: Decimal | None
    ma20_ma60_golden_cross: bool
    ma10_ma20_dead_cross: bool

    def is_usable_at(self, as_of: datetime) -> bool:
        _require_aware(as_of, "as_of")
        return self.signal_available_at <= as_of


class PivotKind(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


@dataclass(frozen=True, slots=True)
class DailyPivotCandidate:
    stock_code: str
    pivot_trade_date: date
    kind: PivotKind
    price: Decimal
    confirmed_at: datetime

    def is_usable_at(self, as_of: datetime) -> bool:
        _require_aware(as_of, "as_of")
        return self.confirmed_at <= as_of


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def simple_moving_average(
    values: Sequence[Decimal],
    window: int,
) -> list[Decimal | None]:
    """Return a trailing SMA aligned to *values*."""

    if window <= 0:
        raise ValueError("window must be positive")
    if any(not isinstance(value, Decimal) for value in values):
        raise TypeError("SMA values must be Decimal")
    result: list[Decimal | None] = [None] * len(values)
    running = Decimal(0)
    divisor = Decimal(window)
    with localcontext() as context:
        context.prec = DEFAULT_DECIMAL_PRECISION
        for index, value in enumerate(values):
            running += value
            if index >= window:
                running -= values[index - window]
            if index >= window - 1:
                result[index] = running / divisor
    return result


def moving_average_slope(
    moving_averages: Sequence[Decimal | None],
    lookback: int = 5,
) -> list[Decimal | None]:
    """Return percentage change from the MA value *lookback* sessions ago."""

    if lookback <= 0:
        raise ValueError("lookback must be positive")
    result: list[Decimal | None] = [None] * len(moving_averages)
    with localcontext() as context:
        context.prec = DEFAULT_DECIMAL_PRECISION
        for index in range(lookback, len(moving_averages)):
            current = moving_averages[index]
            previous = moving_averages[index - lookback]
            if current is None or previous is None:
                continue
            if previous == 0:
                continue
            result[index] = (current / previous - Decimal(1)) * HUNDRED
    return result


def daily_returns(values: Sequence[Decimal]) -> list[Decimal | None]:
    """Return close-to-close percentage returns aligned to *values*."""

    if any(not isinstance(value, Decimal) for value in values):
        raise TypeError("daily return values must be Decimal")
    result: list[Decimal | None] = [None] * len(values)
    with localcontext() as context:
        context.prec = DEFAULT_DECIMAL_PRECISION
        for index in range(1, len(values)):
            previous = values[index - 1]
            if previous == 0:
                continue
            result[index] = (values[index] / previous - Decimal(1)) * HUNDRED
    return result


def is_golden_cross(
    previous_fast: Decimal | None,
    previous_slow: Decimal | None,
    current_fast: Decimal | None,
    current_slow: Decimal | None,
) -> bool:
    """Return true only for a new MA20/MA60 cross above."""

    if None in (previous_fast, previous_slow, current_fast, current_slow):
        return False
    return previous_fast <= previous_slow and current_fast > current_slow


def is_dead_cross(
    previous_fast: Decimal | None,
    previous_slow: Decimal | None,
    current_fast: Decimal | None,
    current_slow: Decimal | None,
) -> bool:
    """Return true only for a new MA10/MA20 cross below."""

    if None in (previous_fast, previous_slow, current_fast, current_slow):
        return False
    return previous_fast >= previous_slow and current_fast < current_slow


def _group_indexes_by_stock(
    bars: Iterable[DailyBar | FiveMinuteBar],
) -> dict[str, list[tuple[int, DailyBar | FiveMinuteBar]]]:
    grouped: dict[str, list[tuple[int, DailyBar | FiveMinuteBar]]] = {}
    for index, bar in enumerate(bars):
        grouped.setdefault(bar.stock_code, []).append((index, bar))
    return grouped


def calculate_daily_indicators(
    bars: Sequence[DailyBar],
    calendar: TradingCalendar | None = None,
) -> list[DailyIndicatorPoint]:
    """Calculate trailing daily indicators from signal prices only."""

    validate_daily_bars(bars, calendar)
    output: list[DailyIndicatorPoint | None] = [None] * len(bars)
    for entries in _group_indexes_by_stock(bars).values():
        typed_entries = [
            (index, bar) for index, bar in entries if isinstance(bar, DailyBar)
        ]
        closes = [bar.signal.close for _, bar in typed_entries]
        returns = daily_returns(closes)
        sma10 = simple_moving_average(closes, 10)
        sma20 = simple_moving_average(closes, 20)
        sma60 = simple_moving_average(closes, 60)
        slope20 = moving_average_slope(sma20, 5)
        slope60 = moving_average_slope(sma60, 5)
        for local_index, (source_index, bar) in enumerate(typed_entries):
            output[source_index] = DailyIndicatorPoint(
                stock_code=bar.stock_code,
                trade_date=bar.trade_date,
                daily_return=returns[local_index],
                sma10=sma10[local_index],
                sma20=sma20[local_index],
                sma60=sma60[local_index],
                ma20_slope_5=slope20[local_index],
                ma60_slope_5=slope60[local_index],
            )
    return [point for point in output if point is not None]


def calculate_intraday_indicators(
    bars: Sequence[FiveMinuteBar],
    calendar: TradingCalendar | None = None,
) -> list[IntradayIndicatorPoint]:
    """Calculate continuous, cross-session five-minute indicators."""

    validate_five_minute_bars(bars, calendar)
    output: list[IntradayIndicatorPoint | None] = [None] * len(bars)
    for entries in _group_indexes_by_stock(bars).values():
        typed_entries = [
            (index, bar) for index, bar in entries if isinstance(bar, FiveMinuteBar)
        ]
        closes = [bar.signal.close for _, bar in typed_entries]
        sma10 = simple_moving_average(closes, 10)
        sma20 = simple_moving_average(closes, 20)
        sma60 = simple_moving_average(closes, 60)
        for local_index, (source_index, bar) in enumerate(typed_entries):
            previous_index = local_index - 1
            output[source_index] = IntradayIndicatorPoint(
                stock_code=bar.stock_code,
                bar_start_at=bar.bar_start_at,
                bar_end_at=bar.bar_end_at,
                signal_available_at=bar.signal_available_at,
                sma10=sma10[local_index],
                sma20=sma20[local_index],
                sma60=sma60[local_index],
                ma20_ma60_golden_cross=(
                    previous_index >= 0
                    and is_golden_cross(
                        sma20[previous_index],
                        sma60[previous_index],
                        sma20[local_index],
                        sma60[local_index],
                    )
                ),
                ma10_ma20_dead_cross=(
                    previous_index >= 0
                    and is_dead_cross(
                        sma10[previous_index],
                        sma20[previous_index],
                        sma10[local_index],
                        sma20[local_index],
                    )
                ),
            )
    return [point for point in output if point is not None]


def intraday_indicators_as_of(
    bars: Sequence[FiveMinuteBar],
    as_of: datetime,
    calendar: TradingCalendar | None = None,
) -> list[IntradayIndicatorPoint]:
    """Return only the sequential bar prefix available at *as_of*.

    If an earlier bar is not yet available, later bars for the same stock are
    also withheld even if their metadata is inconsistent and claims an earlier
    availability time.
    """

    _require_aware(as_of, "as_of")
    validate_five_minute_bars(bars, calendar)
    eligible_indexes: set[int] = set()
    for entries in _group_indexes_by_stock(bars).values():
        for index, bar in entries:
            if not isinstance(bar, FiveMinuteBar) or bar.signal_available_at > as_of:
                break
            eligible_indexes.add(index)
    eligible = [bar for index, bar in enumerate(bars) if index in eligible_indexes]
    return calculate_intraday_indicators(eligible, calendar)


def detect_daily_pivots(
    bars: Sequence[DailyBar],
    calendar: TradingCalendar | None = None,
) -> list[DailyPivotCandidate]:
    """Detect strict left-2/right-2 pivots and attach their confirmation time."""

    validate_daily_bars(bars, calendar)
    pivots: list[DailyPivotCandidate] = []
    for entries in _group_indexes_by_stock(bars).values():
        typed_bars = [bar for _, bar in entries if isinstance(bar, DailyBar)]
        for center_index in range(2, len(typed_bars) - 2):
            center = typed_bars[center_index]
            neighbours = (
                typed_bars[center_index - 2],
                typed_bars[center_index - 1],
                typed_bars[center_index + 1],
                typed_bars[center_index + 2],
            )
            confirmation_day = typed_bars[center_index + 2].trade_date
            confirmed_at = datetime.combine(
                confirmation_day,
                time(15, 30),
                tzinfo=KOREA_TZ,
            )
            if all(center.signal.low < item.signal.low for item in neighbours):
                pivots.append(
                    DailyPivotCandidate(
                        stock_code=center.stock_code,
                        pivot_trade_date=center.trade_date,
                        kind=PivotKind.LOW,
                        price=center.signal.low,
                        confirmed_at=confirmed_at,
                    )
                )
            if all(center.signal.high > item.signal.high for item in neighbours):
                pivots.append(
                    DailyPivotCandidate(
                        stock_code=center.stock_code,
                        pivot_trade_date=center.trade_date,
                        kind=PivotKind.HIGH,
                        price=center.signal.high,
                        confirmed_at=confirmed_at,
                    )
                )
    return pivots


def pivots_as_of(
    pivots: Iterable[DailyPivotCandidate],
    as_of: datetime,
) -> list[DailyPivotCandidate]:
    """Filter pivots by confirmation availability."""

    _require_aware(as_of, "as_of")
    return [pivot for pivot in pivots if pivot.is_usable_at(as_of)]
