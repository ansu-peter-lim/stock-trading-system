"""Deterministic ZERO_COST single-stock Core entry/Exit-C MVP."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum

from .core_actions import (
    CoreActionStatus,
    CoreActionType,
    PendingCoreAction,
    PendingCoreActionLedger,
)
from .core_strategy import (
    DailyCoreSignal,
    DailyCoreSignalGenerator,
    StrategyConfig,
)
from .event_runner import EventTraceEntry, EventType
from .events import (
    CANONICAL_TIMEZONE_ID,
    BarIdentity,
    DeterministicTieBreak,
    EntityKindRank,
    EventKey,
    EventPhase,
    assign_bar_identities,
    stable_id,
)
from .execution import (
    IntentSubmission,
    IntentType,
    NextBarScheduler,
    OrderSide,
    ScheduleResult,
    ScheduleStatus,
    StrategyIntent,
    signal_event_key,
)
from .indicators import (
    IntradayIndicatorPoint,
    calculate_daily_indicators,
    calculate_intraday_indicators,
)
from .ledgers import (
    FillLedger,
    FillLedgerEntry,
    OrderLedger,
    OrderLedgerEntry,
    SignalLedger,
    SignalLedgerEntry,
    SignalStatus,
)
from .models import DailyBar, FiveMinuteBar
from .trading_calendar import CalendarRangeError, TradingCalendar
from .validation import KOREA_TZ, validate_daily_bars
from .zero_cost_accounting import (
    CorePosition,
    PositionTransition,
    ZeroCostSingleStockAccount,
)


class CoreMvpValidationError(ValueError):
    """The phase-3 single-stock run violates its deliberately narrow scope."""


class CostProfile(str, Enum):
    ZERO_COST = "ZERO_COST"


class ExitPolicy(str, Enum):
    C = "C"


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    timezone: str = CANONICAL_TIMEZONE_ID
    cost_profile: CostProfile = CostProfile.ZERO_COST
    exit_policy: ExitPolicy = ExitPolicy.C

    def __post_init__(self) -> None:
        if self.timezone != CANONICAL_TIMEZONE_ID:
            raise CoreMvpValidationError("timezone must be Asia/Seoul")
        if self.cost_profile is not CostProfile.ZERO_COST:
            raise CoreMvpValidationError("phase 3 supports ZERO_COST only")
        if self.exit_policy is not ExitPolicy.C:
            raise CoreMvpValidationError("phase 3 supports ExitPolicy C only")


class CoreExecutionCondition(str, Enum):
    FIRST_BAR_MA20_ABOVE_MA60 = "FIRST_BAR_MA20_ABOVE_MA60"
    NEW_MA20_MA60_GOLDEN_CROSS = "NEW_MA20_MA60_GOLDEN_CROSS"
    EXIT_C_CLOSE_BELOW_MA60 = "EXIT_C_CLOSE_BELOW_MA60"


@dataclass(frozen=True, slots=True)
class CoreExecutionRecord:
    action_id: str
    daily_signal_id: str
    execution_signal_id: str
    order_id: str
    fill_id: str | None
    condition: CoreExecutionCondition
    schedule_status: ScheduleStatus


@dataclass(frozen=True, slots=True)
class CoreMvpResult:
    strategy_config: StrategyConfig
    execution_config: ExecutionConfig
    daily_signals: tuple[DailyCoreSignal, ...]
    pending_core_ledger: tuple[PendingCoreAction, ...]
    execution_records: tuple[CoreExecutionRecord, ...]
    event_trace: tuple[EventTraceEntry, ...]
    signal_ledger: tuple[SignalLedgerEntry, ...]
    order_ledger: tuple[OrderLedgerEntry, ...]
    fill_ledger: tuple[FillLedgerEntry, ...]
    position_ledger: tuple[PositionTransition, ...]
    final_position: CorePosition
    final_cash: Decimal


@dataclass(frozen=True, slots=True)
class _ScheduledFill:
    action_id: str
    intent: StrategyIntent
    schedule: ScheduleResult
    fill_id: str
    fill_event_key: EventKey


def core_entry_condition(
    point: IntradayIndicatorPoint,
    *,
    is_first_completed_bar: bool,
) -> CoreExecutionCondition | None:
    if point.sma20 is None or point.sma60 is None:
        return None
    if is_first_completed_bar:
        if point.sma20 > point.sma60:
            return CoreExecutionCondition.FIRST_BAR_MA20_ABOVE_MA60
        return None
    if point.ma20_ma60_golden_cross:
        return CoreExecutionCondition.NEW_MA20_MA60_GOLDEN_CROSS
    return None


def exit_policy_c_condition(
    bar: FiveMinuteBar,
    point: IntradayIndicatorPoint,
) -> bool:
    return point.sma60 is not None and bar.signal.close < point.sma60


class CoreMvpEngine:
    """Chronological phase-3 runner; no Tactical, costs, or multi-stock logic."""

    def __init__(
        self,
        calendar: TradingCalendar,
        *,
        strategy_config: StrategyConfig | None = None,
        execution_config: ExecutionConfig | None = None,
        scheduler: NextBarScheduler | None = None,
    ) -> None:
        self._calendar = calendar
        self.strategy_config = strategy_config or StrategyConfig()
        self.execution_config = execution_config or ExecutionConfig()
        self._scheduler = scheduler or NextBarScheduler()

    def run(
        self,
        *,
        daily_bars: Sequence[DailyBar],
        intraday_bars: Sequence[FiveMinuteBar],
        stock_full_weight: Decimal,
        initial_capital: Decimal,
    ) -> CoreMvpResult:
        canonical_daily = tuple(
            sorted(daily_bars, key=lambda bar: (bar.stock_code, bar.trade_date))
        )
        validate_daily_bars(canonical_daily, self._calendar)
        if not canonical_daily:
            raise CoreMvpValidationError("daily_bars must not be empty")
        stock_codes = {bar.stock_code for bar in canonical_daily}
        stock_codes.update(bar.stock_code for bar in intraday_bars)
        if len(stock_codes) != 1:
            raise CoreMvpValidationError("phase 3 supports exactly one stock")
        stock_code = next(iter(stock_codes))

        identities = assign_bar_identities(intraday_bars)
        canonical_intraday = tuple(identity.bar for identity in identities)
        intraday_points = calculate_intraday_indicators(canonical_intraday)
        daily_points = calculate_daily_indicators(canonical_daily, self._calendar)
        daily_by_date = {bar.trade_date: bar for bar in canonical_daily}
        daily_point_by_date = {point.trade_date: point for point in daily_points}
        identity_by_id = {identity.bar_id: identity for identity in identities}

        intraday_by_date: dict[
            date, list[tuple[BarIdentity, IntradayIndicatorPoint]]
        ] = {}
        for identity, point in zip(identities, intraday_points, strict=True):
            local_day = identity.bar.bar_start_at.astimezone(KOREA_TZ).date()
            intraday_by_date.setdefault(local_day, []).append((identity, point))

        event_days = set(daily_by_date) | set(intraday_by_date)
        for day in daily_by_date:
            try:
                event_days.add(self._calendar.next_trading_day(day))
            except CalendarRangeError:
                pass

        signal_generator = DailyCoreSignalGenerator(
            self._calendar, self.strategy_config
        )
        action_ledger = PendingCoreActionLedger()
        account = ZeroCostSingleStockAccount(
            stock_code=stock_code,
            stock_full_weight=stock_full_weight,
            initial_capital=initial_capital,
        )
        signals = SignalLedger()
        orders = OrderLedger(signals)
        fills = FillLedger(orders, signals)
        daily_signals: list[DailyCoreSignal] = []
        executions: list[CoreExecutionRecord] = []
        scheduled_by_bar: dict[str, _ScheduledFill] = {}
        trace_facts: list[tuple[EventKey, EventType, str]] = []
        trace_keys: set[EventKey] = set()

        def add_trace(key: EventKey, event_type: EventType, reference_id: str) -> None:
            if key in trace_keys:
                raise CoreMvpValidationError("duplicate canonical event key")
            trace_keys.add(key)
            trace_facts.append((key, event_type, reference_id))

        for day in sorted(event_days):
            active = action_ledger.active_for_stock(stock_code)
            if (
                active is not None
                and active.status is CoreActionStatus.PENDING
                and active.activation_trade_date == day
            ):
                action_ledger.transition(
                    active.action_id,
                    CoreActionStatus.ARMED,
                    transition_at=datetime.combine(day, time(9, 0), tzinfo=KOREA_TZ),
                    transition_reason="ACTIVATION_TRADE_DATE_REGULAR_SESSION",
                )

            day_bars = intraday_by_date.get(day, [])
            for day_index, (identity, point) in enumerate(day_bars):
                scheduled_fill = scheduled_by_bar.pop(identity.bar_id, None)
                if scheduled_fill is not None:
                    current = action_ledger.latest(scheduled_fill.action_id)
                    if current is None:
                        raise CoreMvpValidationError("scheduled fill lost its action")
                    fill = fills.append(
                        fill_id=scheduled_fill.fill_id,
                        order_id=scheduled_fill.schedule.order_id,
                        filled_at=identity.bar.bar_start_at,
                        fill_event_key=scheduled_fill.fill_event_key,
                        fill_bar_id=identity.bar_id,
                        fill_bar_sequence=identity.bar_sequence,
                        raw_price=identity.bar.raw.open,
                        quantity=scheduled_fill.schedule.requested_quantity,
                    )
                    orders.append_filled(
                        scheduled_fill.schedule.order_id,
                        event_key=scheduled_fill.fill_event_key,
                    )
                    signals.append_transition(
                        scheduled_fill.intent,
                        status=SignalStatus.FILLED,
                        event_key=scheduled_fill.fill_event_key,
                        executed_at=identity.bar.bar_start_at,
                    )
                    action_ledger.transition(
                        current.action_id,
                        CoreActionStatus.FILLED,
                        transition_at=identity.bar.bar_start_at,
                        transition_reason="ZERO_COST_RAW_OPEN_FILL_COMPLETED",
                        execution_signal_id=scheduled_fill.intent.intent_id,
                        order_id=scheduled_fill.schedule.order_id,
                        fill_id=fill.fill_id,
                    )
                    account.apply_fill(
                        action=current,
                        fill=fill,
                        execution_signal_id=scheduled_fill.intent.intent_id,
                    )
                    add_trace(
                        scheduled_fill.fill_event_key,
                        EventType.FILL,
                        fill.fill_id,
                    )

                bar_available_key = EventKey(
                    identity.bar.signal_available_at,
                    EventPhase.PREVIOUS_BAR_CLOSE_AVAILABLE,
                    DeterministicTieBreak(
                        stock_code,
                        identity.bar_sequence,
                        EntityKindRank.BAR,
                        identity.bar_id,
                    ),
                )
                add_trace(bar_available_key, EventType.BAR_AVAILABLE, identity.bar_id)

                active = action_ledger.active_for_stock(stock_code)
                if active is None or active.status is not CoreActionStatus.ARMED:
                    continue
                condition: CoreExecutionCondition | None = None
                if active.action is CoreActionType.ENTER:
                    if day != active.activation_trade_date:
                        continue
                    condition = core_entry_condition(
                        point, is_first_completed_bar=day_index == 0
                    )
                elif (
                    active.action is CoreActionType.FULL_EXIT
                    and day >= active.activation_trade_date
                    and exit_policy_c_condition(identity.bar, point)
                ):
                    condition = CoreExecutionCondition.EXIT_C_CLOSE_BELOW_MA60
                if condition is None:
                    continue
                self._schedule_execution(
                    action=active,
                    condition=condition,
                    source=identity,
                    same_day_bars=tuple(item[0] for item in day_bars),
                    all_bars=identities,
                    identity_by_id=identity_by_id,
                    account=account,
                    action_ledger=action_ledger,
                    signals=signals,
                    orders=orders,
                    scheduled_by_bar=scheduled_by_bar,
                    executions=executions,
                    add_trace=add_trace,
                )

            active = action_ledger.active_for_stock(stock_code)
            if (
                active is not None
                and active.action is CoreActionType.ENTER
                and active.activation_trade_date == day
                and active.status
                in {
                    CoreActionStatus.ARMED,
                    CoreActionStatus.ORDER_SCHEDULED,
                }
            ):
                latest_execution = next(
                    (
                        record
                        for record in reversed(executions)
                        if record.action_id == active.action_id
                    ),
                    None,
                )
                action_ledger.transition(
                    active.action_id,
                    CoreActionStatus.EXPIRED,
                    transition_at=datetime.combine(day, time(15, 30), tzinfo=KOREA_TZ),
                    transition_reason="ENTER_EXPIRED_AT_ACTIVATION_SESSION_END",
                    execution_signal_id=(
                        latest_execution.execution_signal_id
                        if latest_execution is not None
                        else active.execution_signal_id
                    ),
                    order_id=(
                        latest_execution.order_id
                        if latest_execution is not None
                        else active.order_id
                    ),
                )

            daily_bar = daily_by_date.get(day)
            daily_point = daily_point_by_date.get(day)
            if daily_bar is None or daily_point is None:
                continue
            if action_ledger.active_for_stock(stock_code) is not None:
                continue
            daily_signal = signal_generator.evaluate(
                daily_bar,
                daily_point,
                holding_core=account.holding_core,
                stock_full_weight=stock_full_weight,
            )
            if daily_signal is not None:
                daily_signals.append(daily_signal)
                action_ledger.create_from_signal(
                    daily_signal,
                    portfolio_equity_at_decision=account.cash,
                )

        if scheduled_by_bar:
            raise CoreMvpValidationError("scheduled fill target was not processed")

        ordered_trace = sorted(trace_facts, key=lambda item: item[0])
        event_trace = tuple(
            EventTraceEntry(index, key, event_type, reference_id)
            for index, (key, event_type, reference_id) in enumerate(ordered_trace)
        )
        return CoreMvpResult(
            self.strategy_config,
            self.execution_config,
            tuple(daily_signals),
            action_ledger.entries,
            tuple(executions),
            event_trace,
            signals.entries,
            orders.entries,
            fills.entries,
            account.entries,
            account.position,
            account.cash,
        )

    def _schedule_execution(
        self,
        *,
        action: PendingCoreAction,
        condition: CoreExecutionCondition,
        source: BarIdentity,
        same_day_bars: tuple[BarIdentity, ...],
        all_bars: tuple[BarIdentity, ...],
        identity_by_id: dict[str, BarIdentity],
        account: ZeroCostSingleStockAccount,
        action_ledger: PendingCoreActionLedger,
        signals: SignalLedger,
        orders: OrderLedger,
        scheduled_by_bar: dict[str, _ScheduledFill],
        executions: list[CoreExecutionRecord],
        add_trace,
    ) -> None:
        side = (
            OrderSide.BUY if action.action is CoreActionType.ENTER else OrderSide.SELL
        )
        target_weight = (
            action.target_core_weight
            if action.action is CoreActionType.ENTER
            else Decimal(0)
        )
        intent_id = stable_id(
            "core_execution_signal",
            action.action_id,
            condition.value,
            source.bar_id,
        )
        intent = StrategyIntent(
            intent_id=intent_id,
            stock_code=action.stock_code,
            intent_type=IntentType.TARGET_WEIGHT,
            side=side,
            signal_generated_at=source.bar.bar_end_at,
            signal_available_at=source.bar.signal_available_at,
            execution_signal_at=source.bar.signal_available_at,
            reason=condition.value,
            strategy_state=f"{action.action.value}:{action.status.value}:{action.action_id}",
            signal_source_bar_id=source.bar_id,
            signal_source_bar_sequence=source.bar_sequence,
            target_weight=target_weight,
        )
        source_signal_key = signal_event_key(intent)
        signals.append_transition(
            intent,
            status=SignalStatus.CREATED,
            event_key=source_signal_key,
            root_signal_event_key=source_signal_key,
        )
        add_trace(source_signal_key, EventType.SIGNAL, intent.intent_id)

        candidates = (
            same_day_bars if action.action is CoreActionType.ENTER else all_bars
        )
        preview = self._scheduler.schedule(IntentSubmission(intent, 1), candidates)
        if preview.status is ScheduleStatus.SCHEDULED:
            target = identity_by_id.get(preview.eligible_bar_id or "")
            if target is None:
                raise CoreMvpValidationError("scheduler selected unknown bar")
            if action.action is CoreActionType.ENTER:
                quantity = account.entry_quantity(
                    portfolio_equity_at_decision=(action.portfolio_equity_at_decision),
                    core_fraction_of_full=self.strategy_config.core_fraction_of_full,
                    raw_fill_price=target.bar.raw.open,
                )
            else:
                quantity = account.position.actual_quantity
            if quantity <= 0:
                raise CoreMvpValidationError(
                    "target amount cannot purchase one integer share"
                )
            schedule = self._scheduler.schedule(
                IntentSubmission(intent, quantity), candidates
            )
        else:
            schedule = preview

        orders.append_schedule(schedule)
        add_trace(schedule.created_event_key, EventType.ORDER, schedule.order_id)
        if schedule.status is ScheduleStatus.NO_NEXT_BAR:
            signals.append_transition(
                intent,
                status=SignalStatus.NO_NEXT_BAR,
                event_key=schedule.created_event_key,
            )
            executions.append(
                CoreExecutionRecord(
                    action.action_id,
                    action.daily_signal_id,
                    intent.intent_id,
                    schedule.order_id,
                    None,
                    condition,
                    schedule.status,
                )
            )
            return

        signals.append_transition(
            intent,
            status=SignalStatus.ORDER_SCHEDULED,
            event_key=schedule.created_event_key,
        )
        action_ledger.transition(
            action.action_id,
            CoreActionStatus.ORDER_SCHEDULED,
            transition_at=schedule.created_at,
            transition_reason="NEXT_REGULAR_BAR_RAW_OPEN_SCHEDULED",
            execution_signal_id=intent.intent_id,
            order_id=schedule.order_id,
        )
        target = identity_by_id.get(schedule.eligible_bar_id or "")
        if target is None:
            raise CoreMvpValidationError("eligible fill bar is missing")
        fill_id = stable_id("fill", schedule.order_id, target.bar_id)
        fill_key = EventKey(
            target.bar.bar_start_at,
            EventPhase.NEXT_BAR_OPEN_FILL,
            DeterministicTieBreak(
                target.stock_code,
                intent.signal_source_bar_sequence,
                EntityKindRank.FILL,
                fill_id,
            ),
        )
        if target.bar_id in scheduled_by_bar:
            raise CoreMvpValidationError("multiple fills target the same bar")
        scheduled_by_bar[target.bar_id] = _ScheduledFill(
            action.action_id, intent, schedule, fill_id, fill_key
        )
        executions.append(
            CoreExecutionRecord(
                action.action_id,
                action.daily_signal_id,
                intent.intent_id,
                schedule.order_id,
                fill_id,
                condition,
                schedule.status,
            )
        )
