"""Signal-only DOWN_BOX_REVERSAL_V0_1 strategy.

This module is intentionally separate from :mod:`down_strategy`.  It models
daily setup, candidate, and exit signals using adjusted Daily prices only.  It
does not place 5-minute orders, perform fills, change quantities, calculate
cash/PnL, or hand a position to the UP strategy.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any

from .core_strategy import DailyTrendClassifier, DailyTrendState
from .events import stable_id
from .indicators import (
    DailyIndicatorPoint,
    DailyPivotCandidate,
    PivotKind,
    calculate_daily_indicators,
    detect_daily_pivots,
    pivots_as_of,
    simple_moving_average,
)
from .models import DailyBar
from .trading_calendar import TradingCalendar
from .validation import KOREA_TZ, validate_daily_bars

STRATEGY_ID = "DOWN_BOX_REVERSAL_V0_1"


class DownBoxValidationError(ValueError):
    """Input or state violates the DOWN_BOX_REVERSAL_V0_1 contract."""


class BoxSetupState(str, Enum):
    REVERSAL_WAIT = "REVERSAL_WAIT"
    ENTRY_SIGNALLED = "ENTRY_SIGNALLED"
    BOX_POSITION = "BOX_POSITION"
    BREAKOUT_REENTRY_WAIT = "BREAKOUT_REENTRY_WAIT"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class BoxSetupIssue(str, Enum):
    NOT_DOWN_TREND = "NOT_DOWN_TREND"
    NO_VALID_BOX_FLOOR = "NO_VALID_BOX_FLOOR"
    NO_VALID_BOX_UPPER = "NO_VALID_BOX_UPPER"
    NO_RECENT_LOWER_ZONE_TOUCH = "NO_RECENT_LOWER_ZONE_TOUCH"
    FLOOR_BROKEN_IN_ORIGIN_CONTEXT = "FLOOR_BROKEN_IN_ORIGIN_CONTEXT"
    NO_FIRST_SMA10_BREAKOUT = "NO_FIRST_SMA10_BREAKOUT"
    ORIGIN_UPPER_SELL_ZONE = "ORIGIN_UPPER_SELL_ZONE"


class BoxTerminalOutcome(str, Enum):
    """Exactly one terminal result for each signal-proof setup."""

    ENTRY_SIGNALLED = "ENTRY_SIGNALLED"
    EXPIRED = "EXPIRED"
    FLOOR_INVALIDATED = "FLOOR_INVALIDATED"
    UPPER_INVALIDATED = "UPPER_INVALIDATED"
    END_OF_DATA_ACTIVE = "END_OF_DATA_ACTIVE"


class BoxEventType(str, Enum):
    REVERSAL_SETUP_CREATED = "REVERSAL_SETUP_CREATED"
    REVERSAL_WAIT = "REVERSAL_WAIT"
    ENTRY_SIGNALLED = "ENTRY_SIGNALLED"
    SETUP_EXPIRED = "SETUP_EXPIRED"
    SETUP_INVALIDATED = "SETUP_INVALIDATED"
    LOWER_ZONE_TOUCH = "LOWER_ZONE_TOUCH"
    FIRST_SMA10_BREAKOUT = "FIRST_SMA10_BREAKOUT"
    MA5_TURN = "MA5_TURN"
    MA5_BAND_TOUCH = "MA5_BAND_TOUCH"
    SMA10_REBREAK = "SMA10_REBREAK"
    HALF_EXIT_SIGNAL = "HALF_EXIT_SIGNAL"
    FLOOR_BREAK = "FLOOR_BREAK"
    UPPER_TAKE_PROFIT = "UPPER_TAKE_PROFIT"
    BOX_BREAKOUT = "BOX_BREAKOUT"
    BREAKOUT_REENTRY_WAIT = "BREAKOUT_REENTRY_WAIT"
    BREAKOUT_FAILED = "BREAKOUT_FAILED"
    BREAKOUT_REENTRY_CANDIDATE = "BREAKOUT_REENTRY_CANDIDATE"
    HANDOFF_TO_UP_ELIGIBLE = "HANDOFF_TO_UP_ELIGIBLE"


class BoxSignalType(str, Enum):
    ENTRY_CANDIDATE_MA5_TURN = "ENTRY_CANDIDATE_MA5_TURN"
    ENTRY_CANDIDATE_SMA10_REBREAK = "ENTRY_CANDIDATE_SMA10_REBREAK"
    ENTRY_CANDIDATE_MA5_TURN_AND_SMA10_REBREAK = (
        "ENTRY_CANDIDATE_MA5_TURN_AND_SMA10_REBREAK"
    )
    HALF_EXIT_SIGNAL = "HALF_EXIT_SIGNAL"
    FULL_EXIT_FLOOR_BREAK = "FULL_EXIT_FLOOR_BREAK"
    FULL_TAKE_PROFIT_UPPER = "FULL_TAKE_PROFIT_UPPER"
    BREAKOUT_REENTRY_CANDIDATE = "BREAKOUT_REENTRY_CANDIDATE"


@dataclass(frozen=True, slots=True)
class DownBoxStrategyConfig:
    """V0.1 constants kept explicit so proof output is reproducible."""

    strategy_id: str = STRATEGY_ID
    pivot_lookback_sessions: int = 60
    upper_min_age_sessions: int = 10
    origin_context_sessions: int = 10
    reversal_wait_sessions: int = 10
    lower_zone_pct: Decimal = Decimal(3)
    upper_sell_pct: Decimal = Decimal(3)
    ma5_band_pct: Decimal = Decimal(3)
    ma10_band_pct: Decimal = Decimal(3)

    def __post_init__(self) -> None:
        if self.strategy_id != STRATEGY_ID:
            raise DownBoxValidationError("only DOWN_BOX_REVERSAL_V0_1 is supported")
        if self.pivot_lookback_sessions != 60:
            raise DownBoxValidationError("V0.1 pivot lookback is fixed at 60 sessions")
        if self.upper_min_age_sessions != 10:
            raise DownBoxValidationError("V0.1 upper pivot age is fixed at 10 sessions")
        if self.origin_context_sessions != 10:
            raise DownBoxValidationError("V0.1 origin context is fixed at 10 sessions")
        if self.reversal_wait_sessions != 10:
            raise DownBoxValidationError("V0.1 wait is fixed at 10 sessions")
        for value in (
            self.lower_zone_pct,
            self.upper_sell_pct,
            self.ma5_band_pct,
            self.ma10_band_pct,
        ):
            if not isinstance(value, Decimal) or not Decimal(0) <= value < Decimal(100):
                raise DownBoxValidationError(
                    "band percentages must be Decimal in [0, 100)"
                )


@dataclass(frozen=True, slots=True)
class BoxSetup:
    setup_id: str
    stock_code: str
    setup_origin_date: date
    box_floor: Decimal
    box_upper: Decimal
    floor_pivot_date: date
    upper_pivot_date: date
    state: BoxSetupState = BoxSetupState.REVERSAL_WAIT
    sessions_elapsed: int = 0
    rebreak_armed: bool = False
    half_exit_done: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.setup_id, str) or not self.setup_id:
            raise DownBoxValidationError("setup_id must not be empty")
        if (
            len(self.stock_code) != 6
            or not self.stock_code.isascii()
            or not self.stock_code.isdigit()
        ):
            raise DownBoxValidationError("stock_code must be six ASCII digits")
        if self.box_floor <= 0 or self.box_upper <= self.box_floor:
            raise DownBoxValidationError("box_upper must be greater than box_floor")
        if self.sessions_elapsed < 0:
            raise DownBoxValidationError("sessions_elapsed must not be negative")


@dataclass(frozen=True, slots=True)
class BoxOriginAnalysis:
    stock_code: str
    trade_date: date
    setup: BoxSetup | None
    issue: BoxSetupIssue | None
    box_floor: Decimal | None
    box_upper: Decimal | None
    floor_pivot_date: date | None
    upper_pivot_date: date | None
    lower_zone_touched: bool
    floor_integrity: bool
    down_trend: bool


@dataclass(frozen=True, slots=True)
class BoxEvent:
    event_type: BoxEventType
    stock_code: str
    event_date: date
    setup_id: str
    reason: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BoxSignal:
    signal_id: str
    signal_type: BoxSignalType
    stock_code: str
    signal_date: date
    setup_id: str
    reason: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BoxDayDecision:
    stock_code: str
    trade_date: date
    setup_before: BoxSetup
    setup_after: BoxSetup
    events: tuple[BoxEvent, ...]
    signals: tuple[BoxSignal, ...]


def _canonical_inputs(
    bars: Sequence[DailyBar], points: Sequence[DailyIndicatorPoint]
) -> tuple[tuple[DailyBar, ...], tuple[DailyIndicatorPoint, ...]]:
    if len(bars) != len(points) or not bars:
        raise DownBoxValidationError("bars and points must be non-empty and aligned")
    pairs = sorted(zip(bars, points, strict=True), key=lambda pair: pair[0].trade_date)
    canonical_bars = tuple(pair[0] for pair in pairs)
    canonical_points = tuple(pair[1] for pair in pairs)
    validate_daily_bars(canonical_bars)
    if len({bar.stock_code for bar in canonical_bars}) != 1:
        raise DownBoxValidationError(
            "DOWN_BOX_REVERSAL_V0_1 accepts one stock at a time"
        )
    for bar, point in pairs:
        if bar.stock_code != point.stock_code or bar.trade_date != point.trade_date:
            raise DownBoxValidationError("bar and indicator point are not aligned")
    return canonical_bars, canonical_points


def _as_of(trade_date: date) -> datetime:
    return datetime.combine(trade_date, time(15, 30), tzinfo=KOREA_TZ)


def _band(value: Decimal, pct: Decimal) -> tuple[Decimal, Decimal]:
    ratio = pct / Decimal(100)
    return value * (Decimal(1) - ratio), value * (Decimal(1) + ratio)


def _touches(
    value_low: Decimal, value_close: Decimal, lower: Decimal, upper: Decimal
) -> bool:
    return lower <= value_low <= upper or lower <= value_close <= upper


def _pivot_index(
    pivots: Sequence[DailyPivotCandidate], bars: Sequence[DailyBar]
) -> dict[date, int]:
    dates = {bar.trade_date: index for index, bar in enumerate(bars)}
    return {pivot.pivot_trade_date: dates[pivot.pivot_trade_date] for pivot in pivots}


def _select_floor_and_upper(
    bars: Sequence[DailyBar],
    index: int,
    config: DownBoxStrategyConfig,
    all_pivots: Sequence[DailyPivotCandidate] | None = None,
) -> tuple[DailyPivotCandidate | None, DailyPivotCandidate | None]:
    available_pivots = pivots_as_of(
        all_pivots if all_pivots is not None else detect_daily_pivots(bars),
        _as_of(bars[index].trade_date),
    )
    stock_code = bars[0].stock_code
    available_pivots = [
        pivot for pivot in available_pivots if pivot.stock_code == stock_code
    ]
    by_date = _pivot_index(available_pivots, bars)
    first = max(0, index - config.pivot_lookback_sessions + 1)
    floor_candidates = [
        pivot
        for pivot in available_pivots
        if pivot.kind is PivotKind.LOW
        and first <= by_date[pivot.pivot_trade_date] <= index
    ]
    if not floor_candidates:
        return None, None
    floor = max(
        floor_candidates,
        key=lambda pivot: (by_date[pivot.pivot_trade_date], -pivot.price),
    )
    upper_candidates = [
        pivot
        for pivot in available_pivots
        if pivot.kind is PivotKind.HIGH
        and first
        <= by_date[pivot.pivot_trade_date]
        <= index - config.upper_min_age_sessions
        and pivot.price > floor.price
        and pivot.price > bars[index].signal.close
    ]
    if not upper_candidates:
        return floor, None
    upper = min(
        upper_candidates,
        key=lambda pivot: (pivot.price, -by_date[pivot.pivot_trade_date]),
    )
    return floor, upper


def analyze_box_origin(
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
    index: int,
    config: DownBoxStrategyConfig | None = None,
    *,
    precomputed_pivots: Sequence[DailyPivotCandidate] | None = None,
) -> BoxOriginAnalysis:
    """Evaluate one possible setup origin without changing engine state."""

    policy = config or DownBoxStrategyConfig()
    canonical, canonical_points = _canonical_inputs(bars, points)
    if not 0 <= index < len(canonical):
        raise DownBoxValidationError("origin index out of range")
    bar, point = canonical[index], canonical_points[index]
    down = (
        point.ma20_slope_5 is not None
        and point.ma60_slope_5 is not None
        and point.ma20_slope_5 < 0
        and point.ma60_slope_5 < 0
    )
    if not down:
        return BoxOriginAnalysis(
            bar.stock_code,
            bar.trade_date,
            None,
            BoxSetupIssue.NOT_DOWN_TREND,
            None,
            None,
            None,
            None,
            False,
            False,
            False,
        )
    floor_pivot, upper_pivot = _select_floor_and_upper(
        canonical, index, policy, precomputed_pivots
    )
    if floor_pivot is None:
        return BoxOriginAnalysis(
            bar.stock_code,
            bar.trade_date,
            None,
            BoxSetupIssue.NO_VALID_BOX_FLOOR,
            None,
            None,
            None,
            None,
            False,
            False,
            True,
        )
    floor = floor_pivot.price
    context_start = max(0, index - policy.origin_context_sessions)
    context = canonical[context_start : index + 1]
    lower_high = floor * (Decimal(1) + policy.lower_zone_pct / Decimal(100))
    lower_touch = any(
        _touches(item.signal.low, item.signal.close, floor, lower_high)
        for item in context
    )
    floor_integrity = all(item.signal.close >= floor for item in context)
    if upper_pivot is None:
        return BoxOriginAnalysis(
            bar.stock_code,
            bar.trade_date,
            None,
            BoxSetupIssue.NO_VALID_BOX_UPPER,
            floor,
            None,
            floor_pivot.pivot_trade_date,
            None,
            lower_touch,
            floor_integrity,
            True,
        )
    upper = upper_pivot.price
    prior_context = canonical[context_start:index]
    prior_points = canonical_points[context_start:index]
    prior_ten_below_sma10 = (
        len(prior_context) == policy.origin_context_sessions
        and all(
            item.signal.close < item_point.sma10
            for item, item_point in zip(prior_context, prior_points, strict=True)
            if item_point.sma10 is not None
        )
        and all(item_point.sma10 is not None for item_point in prior_points)
    )
    first_sma10_breakout = (
        prior_ten_below_sma10
        and canonical_points[index].sma10 is not None
        and bar.signal.close > canonical_points[index].sma10
    )
    if not first_sma10_breakout:
        issue = BoxSetupIssue.NO_FIRST_SMA10_BREAKOUT
    elif not floor_integrity:
        issue = BoxSetupIssue.FLOOR_BROKEN_IN_ORIGIN_CONTEXT
    elif not lower_touch:
        issue = BoxSetupIssue.NO_RECENT_LOWER_ZONE_TOUCH
    elif bar.signal.close >= upper * (
        Decimal(1) - policy.upper_sell_pct / Decimal(100)
    ):
        issue = BoxSetupIssue.ORIGIN_UPPER_SELL_ZONE
    else:
        issue = None
    setup = None
    if issue is None:
        setup_id = stable_id(
            "down_box_setup",
            bar.stock_code,
            bar.trade_date,
            floor_pivot.pivot_trade_date,
            upper_pivot.pivot_trade_date,
            floor,
            upper,
        )
        setup = BoxSetup(
            setup_id,
            bar.stock_code,
            bar.trade_date,
            floor,
            upper,
            floor_pivot.pivot_trade_date,
            upper_pivot.pivot_trade_date,
        )
    return BoxOriginAnalysis(
        bar.stock_code,
        bar.trade_date,
        setup,
        issue,
        floor,
        upper,
        floor_pivot.pivot_trade_date,
        upper_pivot.pivot_trade_date,
        lower_touch,
        floor_integrity,
        True,
    )


def _signal(
    setup: BoxSetup,
    signal_type: BoxSignalType,
    day: date,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> BoxSignal:
    return BoxSignal(
        stable_id("down_box_signal", setup.setup_id, signal_type.value, day, reason),
        signal_type,
        setup.stock_code,
        day,
        setup.setup_id,
        reason,
        details or {},
    )


def _event(
    setup: BoxSetup,
    event_type: BoxEventType,
    day: date,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> BoxEvent:
    return BoxEvent(
        event_type, setup.stock_code, day, setup.setup_id, reason, details or {}
    )


def _find_index(bars: Sequence[DailyBar], day: date) -> int:
    indexes = [index for index, bar in enumerate(bars) if bar.trade_date == day]
    if len(indexes) != 1:
        raise DownBoxValidationError("setup day must be a unique Daily session")
    return indexes[0]


def evaluate_reversal_wait(
    setup: BoxSetup,
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
    index: int,
    config: DownBoxStrategyConfig | None = None,
) -> BoxDayDecision:
    """Evaluate D1..D10 without assuming an execution/fill occurred."""

    policy = config or DownBoxStrategyConfig()
    canonical, _ = _canonical_inputs(bars, points)
    if setup.state is not BoxSetupState.REVERSAL_WAIT:
        raise DownBoxValidationError("setup is not in REVERSAL_WAIT")
    if not 0 <= index < len(canonical):
        raise DownBoxValidationError("evaluation index out of range")
    day = canonical[index].trade_date
    origin_index = _find_index(canonical, setup.setup_origin_date)
    elapsed = index - origin_index
    if elapsed <= 0:
        raise DownBoxValidationError("REVERSAL_WAIT requires a day after origin")
    updated = replace(setup, sessions_elapsed=elapsed)
    events: list[BoxEvent] = []
    signals: list[BoxSignal] = []
    if elapsed > policy.reversal_wait_sessions:
        updated = replace(updated, state=BoxSetupState.EXPIRED)
        events.append(_event(updated, BoxEventType.SETUP_EXPIRED, day, "D11_OR_LATER"))
        return BoxDayDecision(setup.stock_code, day, setup, updated, tuple(events), ())
    bar = canonical[index]
    if bar.signal.close < setup.box_floor:
        updated = replace(updated, state=BoxSetupState.INVALIDATED)
        events.append(
            _event(
                updated, BoxEventType.SETUP_INVALIDATED, day, "CLOSE_BELOW_BOX_FLOOR"
            )
        )
        events.append(
            _event(updated, BoxEventType.FLOOR_BREAK, day, "CLOSE_BELOW_BOX_FLOOR")
        )
        return BoxDayDecision(setup.stock_code, day, setup, updated, tuple(events), ())
    upper_sell = setup.box_upper * (Decimal(1) - policy.upper_sell_pct / Decimal(100))
    if bar.signal.high >= upper_sell:
        updated = replace(updated, state=BoxSetupState.INVALIDATED)
        events.append(
            _event(
                updated, BoxEventType.SETUP_INVALIDATED, day, "HIGH_IN_UPPER_SELL_ZONE"
            )
        )
        return BoxDayDecision(setup.stock_code, day, setup, updated, tuple(events), ())

    sma5 = simple_moving_average([item.signal.close for item in canonical], 5)
    sma10 = simple_moving_average([item.signal.close for item in canonical], 10)
    lower_zone_high = setup.box_floor * (
        Decimal(1) + policy.lower_zone_pct / Decimal(100)
    )
    if _touches(bar.signal.low, bar.signal.close, setup.box_floor, lower_zone_high):
        events.append(
            _event(updated, BoxEventType.LOWER_ZONE_TOUCH, day, "LOWER_BUY_ZONE_TOUCH")
        )
    ma5_turn = (
        index >= 2
        and sma5[index - 2] is not None
        and sma5[index - 1] is not None
        and sma5[index] is not None
        and sma5[index - 1] <= sma5[index - 2]
        and sma5[index] > sma5[index - 1]
    )
    ma5_touch = sma5[index] is not None and _touches(
        bar.signal.low,
        bar.signal.close,
        *_band(sma5[index], policy.ma5_band_pct),
    )
    if ma5_touch:
        events.append(
            _event(updated, BoxEventType.MA5_BAND_TOUCH, day, "SMA5_BAND_TOUCH")
        )
    if ma5_turn:
        events.append(
            _event(updated, BoxEventType.MA5_TURN, day, "SMA5_PULLBACK_AND_TURN")
        )
    rebreak = (
        updated.rebreak_armed
        and index >= 1
        and sma10[index - 1] is not None
        and sma10[index] is not None
        and canonical[index - 1].signal.close <= sma10[index - 1]
        and bar.signal.close > sma10[index]
    )
    if rebreak:
        events.append(_event(updated, BoxEventType.SMA10_REBREAK, day, "SMA10_REBREAK"))
    if sma10[index] is not None and bar.signal.close <= sma10[index]:
        updated = replace(updated, rebreak_armed=True)
    candidate_box = (
        bar.signal.close >= setup.box_floor and bar.signal.close < upper_sell
    )
    ma5_candidate = ma5_turn and ma5_touch
    if candidate_box and (ma5_candidate or rebreak):
        if ma5_candidate and rebreak:
            signal_type = BoxSignalType.ENTRY_CANDIDATE_MA5_TURN_AND_SMA10_REBREAK
            reason = "MA5_TURN_AND_SMA10_REBREAK"
        elif ma5_candidate:
            signal_type = BoxSignalType.ENTRY_CANDIDATE_MA5_TURN
            reason = "MA5_TURN"
        else:
            signal_type = BoxSignalType.ENTRY_CANDIDATE_SMA10_REBREAK
            reason = "SMA10_REBREAK"
        signals.append(
            _signal(
                updated,
                signal_type,
                day,
                reason,
                {"sma5": sma5[index], "sma10": sma10[index]},
            )
        )
    events.append(_event(updated, BoxEventType.REVERSAL_WAIT, day, "D1_TO_D10"))
    return BoxDayDecision(
        setup.stock_code, day, setup, updated, tuple(events), tuple(signals)
    )


def evaluate_box_position(
    setup: BoxSetup,
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
    index: int,
    config: DownBoxStrategyConfig | None = None,
) -> BoxDayDecision:
    """Evaluate signal-only BOX_POSITION exits and breakout transition."""

    policy = config or DownBoxStrategyConfig()
    canonical, canonical_points = _canonical_inputs(bars, points)
    if setup.state is not BoxSetupState.BOX_POSITION:
        raise DownBoxValidationError("setup is not in BOX_POSITION")
    if not 0 <= index < len(canonical):
        raise DownBoxValidationError("evaluation index out of range")
    day = canonical[index].trade_date
    bar, point = canonical[index], canonical_points[index]
    updated = replace(
        setup,
        sessions_elapsed=max(
            0, index - _find_index(canonical, setup.setup_origin_date)
        ),
    )
    events: list[BoxEvent] = []
    signals: list[BoxSignal] = []
    if bar.signal.close < setup.box_floor:
        signals.append(
            _signal(
                updated,
                BoxSignalType.FULL_EXIT_FLOOR_BREAK,
                day,
                "FULL_EXIT_FLOOR_BREAK",
            )
        )
        events.append(
            _event(updated, BoxEventType.FLOOR_BREAK, day, "CLOSE_BELOW_BOX_FLOOR")
        )
        return BoxDayDecision(
            setup.stock_code, day, setup, updated, tuple(events), tuple(signals)
        )
    upper_sell = setup.box_upper * (Decimal(1) - policy.upper_sell_pct / Decimal(100))
    if bar.signal.high >= upper_sell:
        signals.append(
            _signal(
                updated,
                BoxSignalType.FULL_TAKE_PROFIT_UPPER,
                day,
                "FULL_TAKE_PROFIT_UPPER",
            )
        )
        events.append(
            _event(
                updated, BoxEventType.UPPER_TAKE_PROFIT, day, "HIGH_IN_UPPER_SELL_ZONE"
            )
        )
    if bar.signal.close > setup.box_upper:
        updated = replace(updated, state=BoxSetupState.BREAKOUT_REENTRY_WAIT)
        events.append(
            _event(updated, BoxEventType.BOX_BREAKOUT, day, "CLOSE_ABOVE_BOX_UPPER")
        )
        events.append(
            _event(
                updated, BoxEventType.BREAKOUT_REENTRY_WAIT, day, "FROZEN_OLD_BOX_UPPER"
            )
        )
    half = (
        not setup.half_exit_done
        and index >= 1
        and point.sma10 is not None
        and canonical_points[index - 1].sma10 is not None
        and canonical[index - 1].signal.close >= canonical_points[index - 1].sma10
        and bar.signal.close < point.sma10
    )
    if half and not signals:
        signals.append(
            _signal(updated, BoxSignalType.HALF_EXIT_SIGNAL, day, "HALF_EXIT_SIGNAL")
        )
        events.append(
            _event(updated, BoxEventType.HALF_EXIT_SIGNAL, day, "SMA10_BREAKDOWN")
        )
        updated = replace(updated, half_exit_done=True)
    return BoxDayDecision(
        setup.stock_code, day, setup, updated, tuple(events), tuple(signals)
    )


def evaluate_breakout_reentry(
    setup: BoxSetup,
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
    index: int,
    config: DownBoxStrategyConfig | None = None,
) -> BoxDayDecision:
    """Evaluate frozen-upper re-entry eligibility; no handoff fill is made."""

    policy = config or DownBoxStrategyConfig()
    canonical, canonical_points = _canonical_inputs(bars, points)
    if setup.state is not BoxSetupState.BREAKOUT_REENTRY_WAIT:
        raise DownBoxValidationError("setup is not in BREAKOUT_REENTRY_WAIT")
    if not 0 <= index < len(canonical):
        raise DownBoxValidationError("evaluation index out of range")
    bar, point = canonical[index], canonical_points[index]
    day = bar.trade_date
    updated = setup
    events: list[BoxEvent] = []
    signals: list[BoxSignal] = []
    if bar.signal.close < setup.box_upper:
        updated = replace(setup, state=BoxSetupState.INVALIDATED)
        events.append(
            _event(
                updated,
                BoxEventType.BREAKOUT_FAILED,
                day,
                "CLOSE_BELOW_FROZEN_OLD_BOX_UPPER",
            )
        )
        return BoxDayDecision(setup.stock_code, day, setup, updated, tuple(events), ())
    sma10 = simple_moving_average([item.signal.close for item in canonical], 10)
    lower, upper = (
        _band(sma10[index], policy.ma10_band_pct)
        if sma10[index] is not None
        else (None, None)
    )
    trend = DailyTrendClassifier().classify(point)
    touch = (
        lower is not None
        and upper is not None
        and _touches(bar.signal.low, bar.signal.close, lower, upper)
    )
    if touch and bar.signal.close >= setup.box_upper and trend is DailyTrendState.UP:
        signals.append(
            _signal(
                updated,
                BoxSignalType.BREAKOUT_REENTRY_CANDIDATE,
                day,
                "BREAKOUT_REENTRY_CANDIDATE",
            )
        )
        events.append(
            _event(
                updated,
                BoxEventType.BREAKOUT_REENTRY_CANDIDATE,
                day,
                "SMA10_BAND_AND_UP_TREND",
            )
        )
    return BoxDayDecision(
        setup.stock_code, day, setup, updated, tuple(events), tuple(signals)
    )


def evaluate_box_day(
    setup: BoxSetup,
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
    index: int,
    *,
    position_state: BoxSetupState = BoxSetupState.REVERSAL_WAIT,
    config: DownBoxStrategyConfig | None = None,
) -> BoxDayDecision:
    """Dispatch a daily signal evaluation with explicit position state."""

    if position_state is BoxSetupState.REVERSAL_WAIT:
        return evaluate_reversal_wait(setup, bars, points, index, config)
    if position_state is BoxSetupState.BOX_POSITION:
        return evaluate_box_position(setup, bars, points, index, config)
    if position_state is BoxSetupState.BREAKOUT_REENTRY_WAIT:
        return evaluate_breakout_reentry(setup, bars, points, index, config)
    raise DownBoxValidationError("unsupported terminal setup state")


def run_down_box_signal_proof(
    bars: Sequence[DailyBar],
    calendar: TradingCalendar | None = None,
    config: DownBoxStrategyConfig | None = None,
) -> dict[str, Any]:
    """Run the daily setup/candidate funnel for one stock, signal-only."""

    policy = config or DownBoxStrategyConfig()
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    validate_daily_bars(canonical, calendar)
    points = tuple(calculate_daily_indicators(canonical, calendar))
    if not canonical:
        raise DownBoxValidationError("bars must not be empty")
    all_pivots = tuple(detect_daily_pivots(canonical, calendar))
    active: BoxSetup | None = None
    events: list[BoxEvent] = []
    signals: list[BoxSignal] = []
    rejections: Counter[str] = Counter()
    setup_origins: list[dict[str, Any]] = []
    setup_lifecycle: dict[str, dict[str, Any]] = {}

    def finalize_setup(
        setup: BoxSetup,
        outcome: BoxTerminalOutcome,
        day: date,
        reason: str,
    ) -> None:
        lifecycle = setup_lifecycle[setup.setup_id]
        if lifecycle["terminal_outcome"] is not None:
            raise DownBoxValidationError(
                f"setup has multiple terminal outcomes: {setup.setup_id}"
            )
        lifecycle.update(
            {
                "terminal_outcome": outcome.value,
                "terminal_date": day,
                "terminal_reason": reason,
            }
        )

    for index, bar in enumerate(canonical):
        if active is None:
            origin = analyze_box_origin(
                canonical,
                points,
                index,
                policy,
                precomputed_pivots=all_pivots,
            )
            if origin.down_trend:
                setup_origins.append(
                    {
                        "trade_date": bar.trade_date,
                        "issue": origin.issue.value if origin.issue else None,
                        "box_floor": origin.box_floor,
                        "box_upper": origin.box_upper,
                        "floor_pivot_date": origin.floor_pivot_date,
                        "upper_pivot_date": origin.upper_pivot_date,
                        "lower_zone_touched": origin.lower_zone_touched,
                        "floor_integrity": origin.floor_integrity,
                        "setup_id": origin.setup.setup_id if origin.setup else None,
                    }
                )
                if origin.issue is not None:
                    rejections[origin.issue.value] += 1
                if origin.setup is not None:
                    active = origin.setup
                    setup_lifecycle[active.setup_id] = {
                        "setup_id": active.setup_id,
                        "stock_code": active.stock_code,
                        "setup_origin_date": active.setup_origin_date,
                        "terminal_outcome": None,
                        "terminal_date": None,
                        "terminal_reason": None,
                    }
                    events.append(
                        _event(
                            active,
                            BoxEventType.REVERSAL_SETUP_CREATED,
                            bar.trade_date,
                            "DOWN_ORIGIN",
                        )
                    )
                    events.append(
                        _event(
                            active,
                            BoxEventType.FIRST_SMA10_BREAKOUT,
                            bar.trade_date,
                            "FIRST_SMA10_BREAKOUT",
                        )
                    )
                    if origin.lower_zone_touched:
                        events.append(
                            _event(
                                active,
                                BoxEventType.LOWER_ZONE_TOUCH,
                                bar.trade_date,
                                "LOWER_BUY_ZONE_TOUCH_IN_ORIGIN_CONTEXT",
                            )
                        )
            continue
        if active.state is BoxSetupState.REVERSAL_WAIT:
            decision = evaluate_reversal_wait(active, canonical, points, index, policy)
        elif active.state is BoxSetupState.BREAKOUT_REENTRY_WAIT:
            decision = evaluate_breakout_reentry(
                active, canonical, points, index, policy
            )
        else:
            break
        events.extend(decision.events)
        signals.extend(decision.signals)
        entry_signals = tuple(
            signal
            for signal in decision.signals
            if signal.signal_type.name.startswith("ENTRY_CANDIDATE")
        )
        if entry_signals:
            terminal_setup = replace(
                decision.setup_after, state=BoxSetupState.ENTRY_SIGNALLED
            )
            events.append(
                _event(
                    terminal_setup,
                    BoxEventType.ENTRY_SIGNALLED,
                    decision.trade_date,
                    "FIRST_BUY_CANDIDATE_TERMINAL",
                    {"signal_ids": tuple(signal.signal_id for signal in entry_signals)},
                )
            )
            finalize_setup(
                terminal_setup,
                BoxTerminalOutcome.ENTRY_SIGNALLED,
                decision.trade_date,
                "FIRST_BUY_CANDIDATE_TERMINAL",
            )
            active = None
        elif decision.setup_after.state is BoxSetupState.EXPIRED:
            finalize_setup(
                decision.setup_after,
                BoxTerminalOutcome.EXPIRED,
                decision.trade_date,
                "D11_OR_LATER",
            )
            active = None
        elif decision.setup_after.state is BoxSetupState.INVALIDATED:
            invalidation_reason = next(
                (
                    event.reason
                    for event in decision.events
                    if event.event_type is BoxEventType.SETUP_INVALIDATED
                ),
                "UNKNOWN_INVALIDATION",
            )
            outcome = {
                "CLOSE_BELOW_BOX_FLOOR": BoxTerminalOutcome.FLOOR_INVALIDATED,
                "HIGH_IN_UPPER_SELL_ZONE": BoxTerminalOutcome.UPPER_INVALIDATED,
            }.get(invalidation_reason)
            if outcome is None:
                raise DownBoxValidationError(
                    f"unknown setup invalidation reason: {invalidation_reason}"
                )
            finalize_setup(
                decision.setup_after, outcome, decision.trade_date, invalidation_reason
            )
            active = None
        else:
            active = decision.setup_after
    if active is not None:
        finalize_setup(
            active,
            BoxTerminalOutcome.END_OF_DATA_ACTIVE,
            canonical[-1].trade_date,
            "END_OF_DATA",
        )
    if any(item["terminal_outcome"] is None for item in setup_lifecycle.values()):
        raise DownBoxValidationError("created setup has no terminal outcome")
    lifecycle_rows = tuple(
        sorted(
            setup_lifecycle.values(),
            key=lambda item: (item["setup_origin_date"], item["setup_id"]),
        )
    )
    return {
        "strategy_id": STRATEGY_ID,
        "stock_code": canonical[0].stock_code,
        "setup_origins": setup_origins,
        "setups_created": sum(
            event.event_type is BoxEventType.REVERSAL_SETUP_CREATED for event in events
        ),
        "setup_rejections": dict(sorted(rejections.items())),
        "setup_lifecycle": lifecycle_rows,
        "events": tuple(events),
        "signals": tuple(signals),
        "entry_candidate_count": sum(
            signal.signal_type.name.startswith("ENTRY_CANDIDATE") for signal in signals
        ),
        "calendar_days": len(canonical),
    }
