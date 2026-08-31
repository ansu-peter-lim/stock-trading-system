from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

from src.backtest_engine.core_actions import (
    ACTIVE_CORE_ACTION_STATUSES,
    CoreActionStatus,
    CoreActionType,
)
from src.backtest_engine.core_mvp import (
    CoreExecutionCondition,
    CoreMvpEngine,
    core_entry_condition,
)
from src.backtest_engine.core_strategy import DailyCoreSignalType
from src.backtest_engine.execution import ScheduleStatus
from src.backtest_engine.indicators import IntradayIndicatorPoint
from src.backtest_engine.models import (
    DailyBar,
    FiveMinuteBar,
    Ohlcv,
    TimestampSemantics,
)
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.backtest_engine.validation import KOREA_TZ
from src.backtest_engine.zero_cost_accounting import PositionStatus

D = Decimal
KST = KOREA_TZ
DEFAULT_RAW_OPEN = D("100")


def daily_fixture() -> tuple[list[DailyBar], list[date]]:
    start = date(2024, 1, 1)
    days = [start + timedelta(days=index) for index in range(69)]
    bars: list[DailyBar] = []
    for index in range(67):
        close = D(100 + index) if index <= 65 else D("100")
        low = D("155.5") if index == 65 else close - D(1)
        value = Ohlcv(close, close + D(1), low, close, 100)
        bars.append(DailyBar("005930", days[index], value, value))
    return bars, days


def intraday_bar(
    start: datetime,
    *,
    signal_close: Decimal,
    raw_open: Decimal = DEFAULT_RAW_OPEN,
    raw_volume: int = 100,
) -> FiveMinuteBar:
    signal = Ohlcv(
        signal_close,
        signal_close + D(1),
        signal_close - D(1),
        signal_close,
        100,
    )
    raw = Ohlcv(
        raw_open,
        raw_open + D(1),
        raw_open - D(1),
        raw_open,
        raw_volume,
    )
    return FiveMinuteBar.from_source_timestamp(
        stock_code="005930",
        source_timestamp=start,
        source_timestamp_semantics=TimestampSemantics.START,
        raw=raw,
        signal=signal,
    )


def intraday_fixture(
    days: list[date],
    *,
    entry_next_bar: bool = True,
    exit_values: tuple[str, ...] = ("50", "51"),
    later_exit_values: tuple[str, ...] = (),
    exit_second_raw_volume: int = 100,
) -> list[FiveMinuteBar]:
    bars: list[FiveMinuteBar] = []
    warmup_start = datetime.combine(days[65], datetime.min.time(), tzinfo=KST).replace(
        hour=9
    )
    for index in range(60):
        bars.append(
            intraday_bar(
                warmup_start + timedelta(minutes=5 * index),
                signal_close=D(100 + index),
            )
        )
    entry_start = datetime.combine(days[66], datetime.min.time(), tzinfo=KST).replace(
        hour=9
    )
    bars.append(intraday_bar(entry_start, signal_close=D("160")))
    if entry_next_bar:
        bars.append(
            intraday_bar(
                entry_start + timedelta(minutes=5),
                signal_close=D("161"),
                raw_open=D("100"),
            )
        )
    exit_start = datetime.combine(days[67], datetime.min.time(), tzinfo=KST).replace(
        hour=9
    )
    for index, value in enumerate(exit_values):
        bars.append(
            intraday_bar(
                exit_start + timedelta(minutes=5 * index),
                signal_close=D(value),
                raw_open=D("90") if index == 1 else D("100"),
                raw_volume=exit_second_raw_volume if index == 1 else 100,
            )
        )
    later_exit_start = datetime.combine(
        days[68], datetime.min.time(), tzinfo=KST
    ).replace(hour=9)
    for index, value in enumerate(later_exit_values):
        bars.append(
            intraday_bar(
                later_exit_start + timedelta(minutes=5 * index),
                signal_close=D(value),
                raw_open=D("85") if index == 1 else D("100"),
            )
        )
    return bars


def run_fixture(
    *,
    entry_next_bar: bool = True,
    exit_values: tuple[str, ...] = ("50", "51"),
    later_exit_values: tuple[str, ...] = (),
    exit_second_raw_volume: int = 100,
    reverse_input: bool = False,
):
    daily, days = daily_fixture()
    intraday = intraday_fixture(
        days,
        entry_next_bar=entry_next_bar,
        exit_values=exit_values,
        later_exit_values=later_exit_values,
        exit_second_raw_volume=exit_second_raw_volume,
    )
    if reverse_input:
        daily.reverse()
        intraday.reverse()
    calendar = ExplicitTradingCalendar(days)
    return CoreMvpEngine(calendar).run(
        daily_bars=daily,
        intraday_bars=intraday,
        stock_full_weight=D("0.10"),
        initial_capital=D("100000"),
    )


class IntradayCoreConditionTests(unittest.TestCase):
    def test_first_bar_failure_waits_for_later_new_golden_cross(self) -> None:
        at = datetime(2024, 1, 2, 9, 5, tzinfo=KST)
        first = IntradayIndicatorPoint(
            "005930",
            at - timedelta(minutes=5),
            at,
            at,
            None,
            D("90"),
            D("100"),
            False,
            False,
        )
        later = IntradayIndicatorPoint(
            "005930",
            at,
            at + timedelta(minutes=5),
            at + timedelta(minutes=5),
            None,
            D("101"),
            D("100"),
            True,
            False,
        )
        self.assertIsNone(core_entry_condition(first, is_first_completed_bar=True))
        self.assertEqual(
            CoreExecutionCondition.NEW_MA20_MA60_GOLDEN_CROSS,
            core_entry_condition(later, is_first_completed_bar=False),
        )


class CoreMvpIntegrationTests(unittest.TestCase):
    def test_complete_entry_to_exit_c_pipeline(self) -> None:
        result = run_fixture()
        self.assertEqual(
            [DailyCoreSignalType.ENTER, DailyCoreSignalType.FULL_EXIT],
            [signal.signal_type for signal in result.daily_signals],
        )
        self.assertEqual(
            [
                CoreExecutionCondition.FIRST_BAR_MA20_ABOVE_MA60,
                CoreExecutionCondition.EXIT_C_CLOSE_BELOW_MA60,
            ],
            [execution.condition for execution in result.execution_records],
        )
        self.assertEqual(
            [D("100"), D("90")], [row.raw_price for row in result.fill_ledger]
        )
        self.assertEqual([90, 90], [row.quantity for row in result.fill_ledger])
        self.assertEqual(PositionStatus.FLAT, result.final_position.position_status)
        self.assertEqual(0, result.final_position.actual_quantity)
        self.assertEqual(D("99100"), result.final_cash)
        self.assertEqual(
            [PositionStatus.CORE, PositionStatus.FLAT],
            [row.status for row in result.position_ledger],
        )

    def test_daily_signal_activates_and_executes_only_on_t_plus_one(self) -> None:
        result = run_fixture()
        entry_signal = result.daily_signals[0]
        entry_fill = result.fill_ledger[0]
        self.assertEqual(
            entry_signal.activation_trade_date,
            entry_fill.filled_at.astimezone(KST).date(),
        )
        self.assertGreater(
            entry_fill.filled_at.astimezone(KST).date(),
            entry_signal.generated_trade_date,
        )
        execution_signal = next(
            row
            for row in result.signal_ledger
            if row.signal_id == result.execution_records[0].execution_signal_id
            and row.status.value == "CREATED"
        )
        self.assertEqual(execution_signal.signal_available_at, entry_fill.filled_at)
        self.assertGreater(entry_fill.fill_event_key, execution_signal.signal_event_key)
        self.assertGreater(
            entry_fill.fill_bar_sequence,
            execution_signal.signal_source_bar_sequence,
        )

    def test_last_bar_entry_signal_records_no_next_bar_and_expires(self) -> None:
        result = run_fixture(entry_next_bar=False, exit_values=())
        self.assertEqual([], list(result.fill_ledger))
        self.assertEqual(
            ScheduleStatus.NO_NEXT_BAR, result.execution_records[0].schedule_status
        )
        entry_action_id = result.execution_records[0].action_id
        latest = next(
            row
            for row in reversed(result.pending_core_ledger)
            if row.action_id == entry_action_id
        )
        self.assertEqual(CoreActionStatus.EXPIRED, latest.status)
        order = result.order_ledger[-1]
        self.assertIsNone(order.eligible_at)
        self.assertIsNone(order.eligible_bar_id)
        self.assertIsNone(order.eligible_bar_sequence)

    def test_exit_c_without_trigger_remains_active_on_later_day(self) -> None:
        result = run_fixture(exit_values=("200", "201"))
        self.assertEqual(1, len(result.fill_ledger))
        self.assertEqual(PositionStatus.CORE, result.final_position.position_status)
        active_rows = [
            row
            for row in result.pending_core_ledger
            if row.action is CoreActionType.FULL_EXIT
            and row.status in ACTIVE_CORE_ACTION_STATUSES
        ]
        self.assertEqual(CoreActionStatus.ARMED, active_rows[-1].status)

    def test_exit_c_can_trigger_on_first_later_trading_day(self) -> None:
        result = run_fixture(exit_values=("200", "201"), later_exit_values=("50", "51"))
        self.assertEqual(2, len(result.fill_ledger))
        self.assertEqual(D("85"), result.fill_ledger[-1].raw_price)
        self.assertEqual(PositionStatus.FLAT, result.final_position.position_status)
        self.assertEqual(
            CoreExecutionCondition.EXIT_C_CLOSE_BELOW_MA60,
            result.execution_records[-1].condition,
        )

    def test_last_bar_exit_c_no_next_bar_does_not_expire_full_exit(self) -> None:
        result = run_fixture(exit_values=("50",))
        self.assertEqual(1, len(result.fill_ledger))
        self.assertEqual(
            ScheduleStatus.NO_NEXT_BAR, result.execution_records[-1].schedule_status
        )
        exit_action_id = result.execution_records[-1].action_id
        latest = next(
            row
            for row in reversed(result.pending_core_ledger)
            if row.action_id == exit_action_id
        )
        self.assertEqual(CoreActionStatus.ARMED, latest.status)
        self.assertEqual(PositionStatus.CORE, result.final_position.position_status)

    def test_zero_volume_does_not_override_raw_open_fill_contract(self) -> None:
        result = run_fixture(exit_second_raw_volume=0)
        self.assertEqual(2, len(result.fill_ledger))
        self.assertEqual(D("90"), result.fill_ledger[-1].raw_price)
        self.assertEqual(PositionStatus.FLAT, result.final_position.position_status)

    def test_entry_quantity_uses_equity_snapshot_stored_at_daily_decision(self) -> None:
        result = run_fixture()
        entry_action = next(
            row
            for row in result.pending_core_ledger
            if row.action is CoreActionType.ENTER
        )
        self.assertEqual(D("100000"), entry_action.portfolio_equity_at_decision)
        self.assertTrue(
            all(
                row.portfolio_equity_at_decision
                == entry_action.portfolio_equity_at_decision
                for row in result.pending_core_ledger
                if row.action_id == entry_action.action_id
            )
        )
        self.assertEqual(90, result.fill_ledger[0].quantity)

    def test_audit_chain_is_complete_and_append_only(self) -> None:
        result = run_fixture()
        for position in result.position_ledger:
            execution = next(
                row
                for row in result.execution_records
                if row.fill_id == position.fill_id
            )
            order = next(
                row for row in result.order_ledger if row.order_id == execution.order_id
            )
            signal = next(
                row for row in result.signal_ledger if row.signal_id == order.signal_id
            )
            action = next(
                row
                for row in result.pending_core_ledger
                if row.action_id == execution.action_id
            )
            daily = next(
                row
                for row in result.daily_signals
                if row.signal_id == action.daily_signal_id
            )
            self.assertEqual(execution.execution_signal_id, signal.signal_id)
            self.assertEqual(execution.daily_signal_id, daily.signal_id)
        self.assertEqual(
            list(range(len(result.pending_core_ledger))),
            [row.ledger_sequence for row in result.pending_core_ledger],
        )

    def test_input_permutation_does_not_change_result(self) -> None:
        self.assertEqual(run_fixture(), run_fixture(reverse_input=True))


if __name__ == "__main__":
    unittest.main()
