from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal

from src.backtest_engine.core_actions import (
    CoreActionStatus,
    CoreActionType,
    CoreActionValidationError,
    PendingCoreActionLedger,
)
from src.backtest_engine.core_strategy import (
    DailyCoreSignal,
    DailyCoreSignalType,
    DailyTrendState,
)
from src.backtest_engine.events import (
    DeterministicTieBreak,
    EntityKindRank,
    EventKey,
    EventPhase,
)
from src.backtest_engine.execution import OrderSide
from src.backtest_engine.ledgers import FillLedgerEntry
from src.backtest_engine.validation import KOREA_TZ
from src.backtest_engine.zero_cost_accounting import (
    PositionStatus,
    ZeroCostSingleStockAccount,
)

D = Decimal
KST = KOREA_TZ


def daily_signal(
    signal_type: DailyCoreSignalType,
    *,
    generated_day: date = date(2024, 1, 2),
) -> DailyCoreSignal:
    generated_at = datetime.combine(
        generated_day, datetime.min.time().replace(hour=15, minute=30), tzinfo=KST
    )
    is_entry = signal_type is DailyCoreSignalType.ENTER
    return DailyCoreSignal(
        f"daily-{signal_type.value}",
        "005930",
        signal_type,
        OrderSide.BUY if is_entry else OrderSide.SELL,
        generated_at,
        generated_at,
        generated_day,
        generated_day + timedelta(days=1),
        D("0.10"),
        D("0.09") if is_entry else D(0),
        DailyTrendState.UP,
        "fixture",
        D("100"),
        D("100"),
        D("100"),
    )


def fill(
    *,
    fill_id: str,
    order_id: str,
    price: str,
    quantity: int,
    sequence: int,
) -> FillLedgerEntry:
    at = datetime(2024, 1, 3, 9, 5 + sequence * 5, tzinfo=KST)
    key = EventKey(
        at,
        EventPhase.NEXT_BAR_OPEN_FILL,
        DeterministicTieBreak("005930", sequence, EntityKindRank.FILL, fill_id),
    )
    return FillLedgerEntry(
        sequence,
        key,
        fill_id,
        order_id,
        at,
        key,
        f"bar-{sequence}",
        sequence,
        D(price),
        quantity,
    )


class PendingCoreActionTests(unittest.TestCase):
    def test_entry_is_pending_on_t_and_armed_only_on_t_plus_one(self) -> None:
        signal = daily_signal(DailyCoreSignalType.ENTER)
        ledger = PendingCoreActionLedger()
        pending = ledger.create_from_signal(
            signal, portfolio_equity_at_decision=D("100000")
        )
        self.assertEqual(CoreActionStatus.PENDING, pending.status)
        with self.assertRaisesRegex(CoreActionValidationError, "activation_trade_date"):
            ledger.transition(
                pending.action_id,
                CoreActionStatus.ARMED,
                transition_at=signal.signal_available_at,
                transition_reason="too early",
            )

        armed = ledger.transition(
            pending.action_id,
            CoreActionStatus.ARMED,
            transition_at=datetime(2024, 1, 3, 9, 0, tzinfo=KST),
            transition_reason="T+1 regular session",
        )
        self.assertEqual(signal.activation_trade_date, armed.activation_trade_date)
        self.assertEqual(CoreActionStatus.ARMED, armed.status)

    def test_full_exit_supersedes_active_enter(self) -> None:
        ledger = PendingCoreActionLedger()
        enter = ledger.create_from_signal(
            daily_signal(DailyCoreSignalType.ENTER),
            portfolio_equity_at_decision=D("100000"),
        )
        exit_action = ledger.create_from_signal(
            daily_signal(DailyCoreSignalType.FULL_EXIT),
            portfolio_equity_at_decision=D("100000"),
        )

        cancelled = ledger.latest(enter.action_id)
        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(CoreActionStatus.CANCELLED, cancelled.status)
        self.assertEqual(exit_action.action_id, cancelled.superseded_by)
        self.assertEqual(CoreActionType.FULL_EXIT, exit_action.action)
        self.assertEqual(exit_action, ledger.active_for_stock("005930"))


class ZeroCostAccountingTests(unittest.TestCase):
    def test_full_internal_weight_integer_floor_and_full_liquidation(self) -> None:
        account = ZeroCostSingleStockAccount(
            stock_code="005930",
            stock_full_weight=D("0.10"),
            initial_capital=D("100000"),
        )
        quantity = account.entry_quantity(
            portfolio_equity_at_decision=D("100000"),
            core_fraction_of_full=D("0.90"),
            raw_fill_price=D("100"),
        )
        self.assertEqual(90, quantity)

        entry_action = PendingCoreActionLedger().create_from_signal(
            daily_signal(DailyCoreSignalType.ENTER),
            portfolio_equity_at_decision=D("100000"),
        )
        account.apply_fill(
            action=entry_action,
            fill=fill(
                fill_id="entry-fill",
                order_id="entry-order",
                price="100",
                quantity=quantity,
                sequence=1,
            ),
            execution_signal_id="entry-execution",
        )
        self.assertEqual(PositionStatus.CORE, account.position.position_status)
        self.assertEqual(D("0.09"), account.position.core_target_weight)
        self.assertEqual(90, account.position.actual_quantity)
        self.assertEqual(D("100"), account.position.average_cost)
        self.assertEqual(D("91000"), account.cash)

        exit_action = PendingCoreActionLedger().create_from_signal(
            daily_signal(DailyCoreSignalType.FULL_EXIT),
            portfolio_equity_at_decision=D("99100"),
        )
        account.apply_fill(
            action=exit_action,
            fill=fill(
                fill_id="exit-fill",
                order_id="exit-order",
                price="90",
                quantity=90,
                sequence=2,
            ),
            execution_signal_id="exit-execution",
        )
        self.assertEqual(PositionStatus.FLAT, account.position.position_status)
        self.assertEqual(0, account.position.actual_quantity)
        self.assertEqual(D("99100"), account.cash)
        self.assertTrue(
            all(
                row.commission == row.tax == row.slippage == 0
                for row in account.entries
            )
        )
        self.assertEqual(
            [PositionStatus.CORE, PositionStatus.FLAT],
            [row.status for row in account.entries],
        )


if __name__ == "__main__":
    unittest.main()
