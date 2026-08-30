from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from src.backtest_engine.events import (
    DeterministicTieBreak,
    EntityKindRank,
    EventKey,
    EventPhase,
    assign_bar_identities,
)
from src.backtest_engine.execution import (
    ExecutionModelError,
    IntentType,
    OrderSide,
    ScheduleResult,
    ScheduleStatus,
    StrategyIntent,
    signal_event_key,
)
from src.backtest_engine.ledgers import (
    FillLedger,
    LedgerValidationError,
    OrderLedger,
    SignalLedger,
    SignalStatus,
)
from src.backtest_engine.models import FiveMinuteBar, Ohlcv, TimestampSemantics
from src.backtest_engine.validation import KOREA_TZ

D = Decimal
KST = KOREA_TZ


def minute_bar(start: datetime, stock_code: str = "005930") -> FiveMinuteBar:
    price = D("100")
    value = Ohlcv(price, price + D("1"), price - D("1"), price, 100)
    return FiveMinuteBar.from_source_timestamp(
        stock_code=stock_code,
        source_timestamp=start,
        source_timestamp_semantics=TimestampSemantics.START,
        raw=value,
        signal=value,
    )


def intent_for(source, *, available_at: datetime | None = None) -> StrategyIntent:
    available = available_at or source.bar.signal_available_at
    return StrategyIntent(
        intent_id=f"signal-{source.stock_code}",
        stock_code=source.stock_code,
        intent_type=IntentType.UNIT_DELTA,
        side=OrderSide.BUY,
        signal_generated_at=available,
        signal_available_at=available,
        execution_signal_at=available,
        reason="synthetic phase-2 signal",
        strategy_state="fixture-state",
        signal_source_bar_id=source.bar_id,
        signal_source_bar_sequence=source.bar_sequence,
        unit_delta=1,
    )


class EventIdentityTests(unittest.TestCase):
    def test_bar_sequence_is_stable_and_does_not_reset_at_date_boundary(self) -> None:
        day_one = minute_bar(datetime(2024, 1, 2, 15, 25, tzinfo=KST))
        day_two = minute_bar(datetime(2024, 1, 3, 9, 0, tzinfo=KST))

        forward = assign_bar_identities([day_one, day_two])
        reversed_input = assign_bar_identities([day_two, day_one])

        self.assertEqual(forward, reversed_input)
        self.assertEqual([0, 1], [row.bar_sequence for row in forward])
        self.assertEqual(2, len({row.bar_id for row in forward}))

    def test_strategy_intent_accepts_equal_causal_timestamps(self) -> None:
        source = assign_bar_identities(
            [minute_bar(datetime(2024, 1, 2, 9, 0, tzinfo=KST))]
        )[0]
        intent = intent_for(source)

        self.assertEqual(intent.signal_generated_at, intent.execution_signal_at)

    def test_strategy_intent_rejects_naive_or_reversed_timestamps(self) -> None:
        source = assign_bar_identities(
            [minute_bar(datetime(2024, 1, 2, 9, 0, tzinfo=KST))]
        )[0]
        valid = intent_for(source)
        with self.assertRaises(ExecutionModelError):
            replace(
                valid,
                signal_generated_at=datetime(2024, 1, 2, 9, 5),  # noqa: DTZ001
            )
        with self.assertRaises(ExecutionModelError):
            replace(
                valid,
                signal_generated_at=valid.signal_available_at + timedelta(minutes=1),
            )

    def test_strategy_intent_payload_is_strictly_discriminated(self) -> None:
        source = assign_bar_identities(
            [minute_bar(datetime(2024, 1, 2, 9, 0, tzinfo=KST))]
        )[0]
        unit_intent = intent_for(source)
        target_intent = replace(
            unit_intent,
            intent_type=IntentType.TARGET_WEIGHT,
            unit_delta=None,
            target_weight=D("0.10"),
        )

        self.assertIsNone(unit_intent.target_weight)
        self.assertIsNotNone(unit_intent.unit_delta)
        self.assertIsNotNone(target_intent.target_weight)
        self.assertIsNone(target_intent.unit_delta)

        invalid_payloads = (
            (unit_intent, {"target_weight": D("0.10")}),
            (unit_intent, {"unit_delta": None}),
            (target_intent, {"unit_delta": 1}),
            (target_intent, {"target_weight": None}),
        )
        for base, changes in invalid_payloads:
            with (
                self.subTest(intent_type=base.intent_type, changes=changes),
                self.assertRaises(ExecutionModelError),
            ):
                replace(base, **changes)


class LedgerIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = assign_bar_identities(
            [minute_bar(datetime(2024, 1, 2, 9, 0, tzinfo=KST))]
        )[0]
        self.intent = intent_for(self.source)
        self.signal_key = signal_event_key(self.intent)

    def _created_signal_ledger(self) -> SignalLedger:
        ledger = SignalLedger()
        ledger.append_transition(
            self.intent,
            status=SignalStatus.CREATED,
            event_key=self.signal_key,
            root_signal_event_key=self.signal_key,
        )
        return ledger

    def _created_key(self, order_id: str = "order-test") -> EventKey:
        return EventKey(
            self.intent.execution_signal_at,
            EventPhase.ORDER_CREATED_OR_SCHEDULED,
            DeterministicTieBreak(
                self.intent.stock_code,
                self.intent.signal_source_bar_sequence,
                EntityKindRank.ORDER,
                order_id,
            ),
        )

    def test_order_without_signal_is_rejected(self) -> None:
        signals = SignalLedger()
        orders = OrderLedger(signals)
        result = ScheduleResult(
            "order-test",
            self.intent.intent_id,
            self.intent.stock_code,
            self.intent.execution_signal_at,
            self._created_key(),
            None,
            None,
            None,
            self.intent.side,
            1,
            ScheduleStatus.NO_NEXT_BAR,
            "NO_NEXT_BAR",
        )
        with self.assertRaisesRegex(LedgerValidationError, "without a signal"):
            orders.append_schedule(result)

    def test_same_source_bar_cannot_be_an_eligible_fill_bar(self) -> None:
        signals = self._created_signal_ledger()
        orders = OrderLedger(signals)
        result = ScheduleResult(
            "order-test",
            self.intent.intent_id,
            self.intent.stock_code,
            self.intent.execution_signal_at,
            self._created_key(),
            self.source.bar.bar_start_at,
            self.source.bar_id,
            self.source.bar_sequence,
            self.intent.side,
            1,
            ScheduleStatus.SCHEDULED,
            "invalid same-bar fixture",
        )
        with self.assertRaisesRegex(LedgerValidationError, "later than"):
            orders.append_schedule(result)

    def test_fill_without_order_is_rejected(self) -> None:
        signals = self._created_signal_ledger()
        orders = OrderLedger(signals)
        fills = FillLedger(orders, signals)
        fill_key = EventKey(
            self.intent.execution_signal_at,
            EventPhase.NEXT_BAR_OPEN_FILL,
            DeterministicTieBreak(
                self.intent.stock_code,
                self.intent.signal_source_bar_sequence,
                EntityKindRank.FILL,
                "fill-test",
            ),
        )
        with self.assertRaisesRegex(LedgerValidationError, "without an order"):
            fills.append(
                fill_id="fill-test",
                order_id="missing-order",
                filled_at=self.intent.execution_signal_at,
                fill_event_key=fill_key,
                fill_bar_id="later-bar",
                fill_bar_sequence=1,
                raw_price=D("100"),
                quantity=1,
            )

    def test_same_source_bar_fill_is_rejected_even_at_equal_timestamp(self) -> None:
        signals = self._created_signal_ledger()
        orders = OrderLedger(signals)
        created_key = self._created_key()
        result = ScheduleResult(
            "order-test",
            self.intent.intent_id,
            self.intent.stock_code,
            self.intent.execution_signal_at,
            created_key,
            self.intent.execution_signal_at,
            "next-bar",
            1,
            self.intent.side,
            1,
            ScheduleStatus.SCHEDULED,
            "NEXT_REGULAR_BAR_RAW_OPEN",
        )
        orders.append_schedule(result)
        signals.append_transition(
            self.intent,
            status=SignalStatus.ORDER_SCHEDULED,
            event_key=created_key,
        )
        fills = FillLedger(orders, signals)
        fill_key = EventKey(
            self.intent.execution_signal_at,
            EventPhase.NEXT_BAR_OPEN_FILL,
            DeterministicTieBreak(
                self.intent.stock_code,
                self.intent.signal_source_bar_sequence,
                EntityKindRank.FILL,
                "fill-test",
            ),
        )
        with self.assertRaisesRegex(LedgerValidationError, "later, different bar"):
            fills.append(
                fill_id="fill-test",
                order_id="order-test",
                filled_at=self.intent.execution_signal_at,
                fill_event_key=fill_key,
                fill_bar_id=self.source.bar_id,
                fill_bar_sequence=self.source.bar_sequence,
                raw_price=D("100"),
                quantity=1,
            )

    def test_fill_must_match_authoritative_eligible_bar_identity(self) -> None:
        signals = self._created_signal_ledger()
        orders = OrderLedger(signals)
        created_key = self._created_key()
        result = ScheduleResult(
            "order-test",
            self.intent.intent_id,
            self.intent.stock_code,
            self.intent.execution_signal_at,
            created_key,
            self.intent.execution_signal_at,
            "eligible-bar",
            1,
            self.intent.side,
            1,
            ScheduleStatus.SCHEDULED,
            "NEXT_REGULAR_BAR_RAW_OPEN",
        )
        orders.append_schedule(result)
        signals.append_transition(
            self.intent,
            status=SignalStatus.ORDER_SCHEDULED,
            event_key=created_key,
        )
        fills = FillLedger(orders, signals)
        fill_key = EventKey(
            self.intent.execution_signal_at,
            EventPhase.NEXT_BAR_OPEN_FILL,
            DeterministicTieBreak(
                self.intent.stock_code,
                self.intent.signal_source_bar_sequence,
                EntityKindRank.FILL,
                "fill-test",
            ),
        )
        with self.assertRaisesRegex(LedgerValidationError, "authoritative eligible"):
            fills.append(
                fill_id="fill-test",
                order_id="order-test",
                filled_at=self.intent.execution_signal_at,
                fill_event_key=fill_key,
                fill_bar_id="different-bar",
                fill_bar_sequence=1,
                raw_price=D("100"),
                quantity=1,
            )


if __name__ == "__main__":
    unittest.main()
