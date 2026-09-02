"""Strategy V1 DOWN-path Daily signals and surge-pullback state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum

from .core_strategy import (
    DailyCoreSignalType,
    DailyTrendClassifier,
    DailyTrendState,
    StrategyConfig,
    core_target_weight,
)
from .events import stable_id
from .execution import OrderSide
from .indicators import DailyIndicatorPoint
from .models import DailyBar
from .trading_calendar import TradingCalendar
from .validation import KOREA_TZ


class DownStrategyValidationError(ValueError):
    """A DOWN-path input or configuration violates the fixed V1 contract."""


class DownRiseBranch(str, Enum):
    BELOW_FIVE = "RISE_BELOW_FIVE"
    FIVE_TO_TEN = "RISE_FIVE_TO_TEN_INCLUSIVE"
    ABOVE_TEN = "RISE_ABOVE_TEN"


class DownEntryBranch(str, Enum):
    REVERSAL = "REVERSAL_FIVE_TO_TEN"
    RED_THREE_SOLDIERS = "RED_THREE_SOLDIERS_BELOW_FIVE"
    SURGE_PULLBACK = "SURGE_PULLBACK"


class DownBlockReason(str, Enum):
    STEEP_MA20 = "MA20_SLOPE_AT_OR_BELOW_MINUS_FIVE_PERCENT"
    MA20_RESISTANCE = "HIGH_NEAR_MA20_AND_CLOSE_BELOW_MA20"
    MA60_RESISTANCE = "HIGH_NEAR_MA60_AND_CLOSE_BELOW_MA60"


class SurgeSetupEventType(str, Enum):
    CREATED = "CREATED"
    SUPERSEDED = "SUPERSEDED"
    TOUCHED = "TOUCHED"
    ENTRY = "ENTRY"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class DownStrategyConfig:
    strategy_version: str = "V1"
    reversal_min_rise_pct: Decimal = Decimal(5)
    reversal_max_rise_pct: Decimal = Decimal(10)
    steep_ma20_slope_pct: Decimal = Decimal(-5)
    resistance_band_pct: Decimal = Decimal(3)
    pullback_band_pct: Decimal = Decimal(3)
    surge_valid_sessions: int = 10
    core_fraction_of_full: Decimal = Decimal("0.90")

    def __post_init__(self) -> None:
        decimals = (
            self.reversal_min_rise_pct,
            self.reversal_max_rise_pct,
            self.steep_ma20_slope_pct,
            self.resistance_band_pct,
            self.pullback_band_pct,
            self.core_fraction_of_full,
        )
        if self.strategy_version != "V1" or any(
            not isinstance(value, Decimal) for value in decimals
        ):
            raise DownStrategyValidationError("invalid DOWN Strategy V1 config")
        if not Decimal(0) <= self.reversal_min_rise_pct <= self.reversal_max_rise_pct:
            raise DownStrategyValidationError("invalid reversal rise boundaries")
        if self.steep_ma20_slope_pct >= 0:
            raise DownStrategyValidationError("steep slope threshold must be negative")
        if not Decimal(0) <= self.resistance_band_pct < Decimal(100):
            raise DownStrategyValidationError("invalid resistance band")
        if not Decimal(0) <= self.pullback_band_pct < Decimal(100):
            raise DownStrategyValidationError("invalid pullback band")
        if self.surge_valid_sessions != 10:
            raise DownStrategyValidationError("Strategy V1 setup lasts 10 sessions")
        StrategyConfig(core_fraction_of_full=self.core_fraction_of_full)


@dataclass(frozen=True, slots=True)
class DownEntryFacts:
    trade_date: date
    down_trend: bool
    prior_ten_below_sma10: bool
    sma10_breakout: bool
    rise_pct: Decimal | None
    rise_branch: DownRiseBranch | None
    red_three_soldiers: bool
    block_reasons: tuple[DownBlockReason, ...]

    @property
    def origin_context_satisfied(self) -> bool:
        return self.down_trend and self.prior_ten_below_sma10 and self.sma10_breakout


@dataclass(frozen=True, slots=True)
class SurgePullbackSetup:
    setup_id: str
    stock_code: str
    origin_trade_date: date
    sessions_elapsed: int


@dataclass(frozen=True, slots=True)
class SurgeSetupEvent:
    event_type: SurgeSetupEventType
    setup_id: str
    trade_date: date
    sessions_elapsed: int
    block_reasons: tuple[DownBlockReason, ...] = ()


@dataclass(frozen=True, slots=True)
class DownDailySignal:
    signal_id: str
    stock_code: str
    signal_type: DailyCoreSignalType
    side: OrderSide
    signal_generated_at: datetime
    signal_available_at: datetime
    generated_trade_date: date
    activation_trade_date: date
    stock_full_weight: Decimal
    target_core_weight: Decimal
    trend_state: DailyTrendState
    reason: str
    signal_low: Decimal
    signal_close: Decimal
    signal_sma10: Decimal
    entry_branch: DownEntryBranch | None


@dataclass(frozen=True, slots=True)
class DownDailyDecision:
    facts: DownEntryFacts
    signal: DownDailySignal | None
    setup_events: tuple[SurgeSetupEvent, ...]
    active_setup: SurgePullbackSetup | None


def analyze_down_entry(
    bars: tuple[DailyBar, ...],
    points: tuple[DailyIndicatorPoint, ...],
    index: int,
    config: DownStrategyConfig | None = None,
) -> DownEntryFacts:
    """Compute trailing-only DOWN entry facts for one completed Daily bar."""

    policy = config or DownStrategyConfig()
    if len(bars) != len(points) or not 0 <= index < len(bars):
        raise DownStrategyValidationError("bars/points must align at index")
    bar = bars[index]
    point = points[index]
    if bar.stock_code != point.stock_code or bar.trade_date != point.trade_date:
        raise DownStrategyValidationError("Daily bar and indicator are not aligned")
    classifier = DailyTrendClassifier()
    down = classifier.classify(point) is DailyTrendState.DOWN
    prior_ten = index >= 10 and all(
        points[position].sma10 is not None
        and bars[position].signal.close < points[position].sma10
        for position in range(index - 10, index)
    )
    breakout = point.sma10 is not None and bar.signal.close > point.sma10
    rise = (
        (bar.signal.close / bars[index - 1].signal.close - Decimal(1)) * Decimal(100)
        if index > 0
        else None
    )
    branch: DownRiseBranch | None = None
    if rise is not None:
        if rise < policy.reversal_min_rise_pct:
            branch = DownRiseBranch.BELOW_FIVE
        elif rise <= policy.reversal_max_rise_pct:
            branch = DownRiseBranch.FIVE_TO_TEN
        else:
            branch = DownRiseBranch.ABOVE_TEN
    soldiers = (
        index >= 2
        and all(
            bars[position].signal.close > bars[position].signal.open
            for position in range(index - 2, index + 1)
        )
        and (
            bars[index - 2].signal.close
            < bars[index - 1].signal.close
            < bars[index].signal.close
        )
    )
    return DownEntryFacts(
        bar.trade_date,
        down,
        prior_ten,
        breakout,
        rise,
        branch,
        soldiers,
        down_block_reasons(bar, point, policy),
    )


def down_block_reasons(
    bar: DailyBar,
    point: DailyIndicatorPoint,
    config: DownStrategyConfig | None = None,
) -> tuple[DownBlockReason, ...]:
    policy = config or DownStrategyConfig()
    reasons: list[DownBlockReason] = []
    if (
        point.ma20_slope_5 is not None
        and point.ma20_slope_5 <= policy.steep_ma20_slope_pct
    ):
        reasons.append(DownBlockReason.STEEP_MA20)
    if _is_resistance(bar.signal.high, bar.signal.close, point.sma20, policy):
        reasons.append(DownBlockReason.MA20_RESISTANCE)
    if _is_resistance(bar.signal.high, bar.signal.close, point.sma60, policy):
        reasons.append(DownBlockReason.MA60_RESISTANCE)
    return tuple(reasons)


def touches_sma10_pullback(
    bar: DailyBar,
    point: DailyIndicatorPoint,
    config: DownStrategyConfig | None = None,
) -> bool:
    policy = config or DownStrategyConfig()
    if point.sma10 is None:
        return False
    ratio = policy.pullback_band_pct / Decimal(100)
    lower = point.sma10 * (Decimal(1) - ratio)
    upper = point.sma10 * (Decimal(1) + ratio)
    return lower <= bar.signal.low <= upper or lower <= bar.signal.close <= upper


class DownDailySignalGenerator:
    """Stateful V1 DOWN generator with one active surge setup per stock."""

    def __init__(
        self,
        calendar: TradingCalendar,
        config: DownStrategyConfig | None = None,
    ) -> None:
        self._calendar = calendar
        self.config = config or DownStrategyConfig()
        self._active_setup: SurgePullbackSetup | None = None

    @property
    def active_setup(self) -> SurgePullbackSetup | None:
        return self._active_setup

    def evaluate(
        self,
        bars: tuple[DailyBar, ...],
        points: tuple[DailyIndicatorPoint, ...],
        index: int,
        *,
        holding_core: bool,
        entry_allowed: bool,
        stock_full_weight: Decimal,
    ) -> DownDailyDecision:
        facts = analyze_down_entry(bars, points, index, self.config)
        bar = bars[index]
        point = points[index]
        events: list[SurgeSetupEvent] = []

        if holding_core:
            self._active_setup = None
            signal = None
            if point.sma10 is not None and bar.signal.close < point.sma10:
                signal = self._signal(
                    bar,
                    point,
                    stock_full_weight,
                    DailyCoreSignalType.FULL_EXIT,
                    None,
                    "HOLDING_DOWN_CORE_AND_SIGNAL_CLOSE_BELOW_DAILY_SMA10",
                )
            return DownDailyDecision(facts, signal, tuple(events), self._active_setup)

        setup = self._active_setup
        if setup is not None:
            if bar.trade_date <= setup.origin_trade_date:
                raise DownStrategyValidationError(
                    "setup evaluation time went backwards"
                )
            setup = SurgePullbackSetup(
                setup.setup_id,
                setup.stock_code,
                setup.origin_trade_date,
                setup.sessions_elapsed + 1,
            )
            self._active_setup = setup

        initial_surge = facts.origin_context_satisfied
        active_setup_supersede = (
            setup is not None and facts.down_trend and facts.sma10_breakout
        )
        if (
            entry_allowed
            and (initial_surge or active_setup_supersede)
            and facts.rise_branch is DownRiseBranch.ABOVE_TEN
        ):
            if setup is not None:
                events.append(
                    SurgeSetupEvent(
                        SurgeSetupEventType.SUPERSEDED,
                        setup.setup_id,
                        bar.trade_date,
                        setup.sessions_elapsed,
                    )
                )
            setup = SurgePullbackSetup(
                stable_id("down_surge_setup", bar.stock_code, bar.trade_date),
                bar.stock_code,
                bar.trade_date,
                0,
            )
            self._active_setup = setup
            events.append(
                SurgeSetupEvent(
                    SurgeSetupEventType.CREATED,
                    setup.setup_id,
                    bar.trade_date,
                    0,
                )
            )
            return DownDailyDecision(facts, None, tuple(events), setup)

        if setup is not None and 1 <= setup.sessions_elapsed <= 10:
            if touches_sma10_pullback(bar, point, self.config):
                events.append(
                    SurgeSetupEvent(
                        SurgeSetupEventType.TOUCHED,
                        setup.setup_id,
                        bar.trade_date,
                        setup.sessions_elapsed,
                        facts.block_reasons,
                    )
                )
                if entry_allowed and facts.down_trend and not facts.block_reasons:
                    signal = self._signal(
                        bar,
                        point,
                        stock_full_weight,
                        DailyCoreSignalType.ENTER,
                        DownEntryBranch.SURGE_PULLBACK,
                        "DOWN_SURGE_PULLBACK_TOUCH_AND_FILTERS_PASSED",
                    )
                    events.append(
                        SurgeSetupEvent(
                            SurgeSetupEventType.ENTRY,
                            setup.setup_id,
                            bar.trade_date,
                            setup.sessions_elapsed,
                        )
                    )
                    self._active_setup = None
                    return DownDailyDecision(facts, signal, tuple(events), None)
            if setup.sessions_elapsed == self.config.surge_valid_sessions:
                events.append(
                    SurgeSetupEvent(
                        SurgeSetupEventType.EXPIRED,
                        setup.setup_id,
                        bar.trade_date,
                        setup.sessions_elapsed,
                    )
                )
                self._active_setup = None

        if not entry_allowed or not facts.origin_context_satisfied:
            return DownDailyDecision(facts, None, tuple(events), self._active_setup)
        entry_branch = None
        if facts.rise_branch is DownRiseBranch.FIVE_TO_TEN:
            entry_branch = DownEntryBranch.REVERSAL
        elif (
            facts.rise_branch is DownRiseBranch.BELOW_FIVE and facts.red_three_soldiers
        ):
            entry_branch = DownEntryBranch.RED_THREE_SOLDIERS
        if entry_branch is None or facts.block_reasons:
            return DownDailyDecision(facts, None, tuple(events), self._active_setup)
        self._active_setup = None
        signal = self._signal(
            bar,
            point,
            stock_full_weight,
            DailyCoreSignalType.ENTER,
            entry_branch,
            f"DOWN_{entry_branch.value}_AND_FILTERS_PASSED",
        )
        return DownDailyDecision(facts, signal, tuple(events), None)

    def _signal(
        self,
        bar: DailyBar,
        point: DailyIndicatorPoint,
        stock_full_weight: Decimal,
        signal_type: DailyCoreSignalType,
        entry_branch: DownEntryBranch | None,
        reason: str,
    ) -> DownDailySignal:
        if point.sma10 is None:
            raise DownStrategyValidationError("DOWN signal requires SMA10")
        target = (
            core_target_weight(stock_full_weight, self.config.core_fraction_of_full)
            if signal_type is DailyCoreSignalType.ENTER
            else Decimal(0)
        )
        generated_at = datetime.combine(bar.trade_date, time(15, 30), tzinfo=KOREA_TZ)
        activation = self._calendar.next_trading_day(bar.trade_date)
        signal_id = stable_id(
            "down_daily_core_signal",
            bar.stock_code,
            bar.trade_date,
            signal_type.value,
            entry_branch.value if entry_branch else "",
            bar.signal.low,
            bar.signal.close,
            point.sma10,
            target,
        )
        return DownDailySignal(
            signal_id,
            bar.stock_code,
            signal_type,
            OrderSide.BUY
            if signal_type is DailyCoreSignalType.ENTER
            else OrderSide.SELL,
            generated_at,
            generated_at,
            bar.trade_date,
            activation,
            stock_full_weight,
            target,
            DailyTrendClassifier().classify(point),
            reason,
            bar.signal.low,
            bar.signal.close,
            point.sma10,
            entry_branch,
        )


def _is_resistance(
    high: Decimal,
    close: Decimal,
    moving_average: Decimal | None,
    config: DownStrategyConfig,
) -> bool:
    if moving_average is None:
        return False
    ratio = config.resistance_band_pct / Decimal(100)
    lower = moving_average * (Decimal(1) - ratio)
    upper = moving_average * (Decimal(1) + ratio)
    return lower <= high <= upper and close < moving_average
