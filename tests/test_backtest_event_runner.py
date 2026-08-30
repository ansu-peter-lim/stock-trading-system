from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from src.backtest_engine.event_runner import (
    DeterministicEventRunner,
    EventRunnerError,
    EventType,
)
from src.backtest_engine.events import (
    CANONICAL_TIMEZONE_ID,
    EventPhase,
    assign_bar_identities,
)
from src.backtest_engine.execution import (
    IntentSubmission,
    IntentType,
    OrderSide,
    StrategyIntent,
)
from src.backtest_engine.ledgers import OrderStatus, SignalStatus
from src.backtest_engine.models import FiveMinuteBar, Ohlcv, TimestampSemantics
from src.backtest_engine.validation import KOREA_TZ

D = Decimal
KST = KOREA_TZ


def minute_bar(
    start: datetime,
    *,
    stock_code: str = "005930",
    raw_open: str = "100",
) -> FiveMinuteBar:
    raw_value = D(raw_open)
    raw = Ohlcv(
        raw_value,
        raw_value + D("1"),
        raw_value - D("1"),
        raw_value,
        100,
    )
    signal_value = D("500")
    signal = Ohlcv(
        signal_value,
        signal_value + D("1"),
        signal_value - D("1"),
        signal_value,
        100,
    )
    return FiveMinuteBar.from_source_timestamp(
        stock_code=stock_code,
        source_timestamp=start,
        source_timestamp_semantics=TimestampSemantics.START,
        raw=raw,
        signal=signal,
    )


def two_bars(stock_code: str = "005930") -> list[FiveMinuteBar]:
    start = datetime(2024, 1, 2, 9, 0, tzinfo=KST)
    return [
        minute_bar(start, stock_code=stock_code, raw_open="100"),
        minute_bar(start + timedelta(minutes=5), stock_code=stock_code, raw_open="105"),
    ]


def submission_for(bars: list[FiveMinuteBar], quantity: int = 3) -> IntentSubmission:
    source = assign_bar_identities(bars)[0]
    available = source.bar.signal_available_at
    intent = StrategyIntent(
        intent_id=f"signal-{source.stock_code}",
        stock_code=source.stock_code,
        intent_type=IntentType.UNIT_DELTA,
        side=OrderSide.BUY,
        signal_generated_at=available,
        signal_available_at=available,
        execution_signal_at=available,
        reason="synthetic boundary signal",
        strategy_state="phase-2-fixture",
        signal_source_bar_id=source.bar_id,
        signal_source_bar_sequence=source.bar_sequence,
        unit_delta=1,
    )
    return IntentSubmission(intent, quantity)


class EventRunnerBoundaryTests(unittest.TestCase):
    def test_equal_wall_clock_boundary_fills_on_next_bar_raw_open(self) -> None:
        bars = two_bars()
        submission = submission_for(bars)
        result = DeterministicEventRunner().run(bars, [submission])

        fill = result.fill_ledger[0]
        signal_created = result.signal_ledger[0]
        order_created = result.order_ledger[0]

        self.assertEqual(submission.intent.signal_available_at, fill.filled_at)
        self.assertEqual(D("105"), fill.raw_price)
        self.assertGreater(fill.fill_event_key, signal_created.signal_event_key)
        self.assertGreater(fill.fill_event_key, order_created.created_event_key)
        self.assertGreater(
            fill.fill_bar_sequence, signal_created.signal_source_bar_sequence
        )
        self.assertNotEqual(fill.fill_bar_id, signal_created.signal_source_bar_id)

    def test_equal_timestamp_uses_documented_event_phases(self) -> None:
        bars = two_bars()
        boundary = bars[0].bar_end_at
        result = DeterministicEventRunner().run(bars, [submission_for(bars)])
        phases = [
            row.event_key.event_phase
            for row in result.event_trace
            if row.event_key.timestamp == boundary
        ]

        self.assertEqual(
            [
                EventPhase.PREVIOUS_BAR_CLOSE_AVAILABLE,
                EventPhase.SIGNAL_EVALUATION,
                EventPhase.ORDER_CREATED_OR_SCHEDULED,
                EventPhase.NEXT_BAR_OPEN_FILL,
            ],
            phases,
        )

    def test_signal_cannot_be_used_before_source_bar_availability(self) -> None:
        bars = two_bars()
        valid = submission_for(bars)
        too_early = valid.intent.signal_available_at - timedelta(minutes=1)
        intent = StrategyIntent(
            intent_id=valid.intent.intent_id,
            stock_code=valid.intent.stock_code,
            intent_type=valid.intent.intent_type,
            side=valid.intent.side,
            signal_generated_at=too_early,
            signal_available_at=too_early,
            execution_signal_at=valid.intent.execution_signal_at,
            reason=valid.intent.reason,
            strategy_state=valid.intent.strategy_state,
            signal_source_bar_id=valid.intent.signal_source_bar_id,
            signal_source_bar_sequence=valid.intent.signal_source_bar_sequence,
            unit_delta=1,
        )

        with self.assertRaisesRegex(EventRunnerError, "precedes source bar"):
            DeterministicEventRunner().run(bars, [IntentSubmission(intent, 1)])

    def test_no_next_bar_records_result_without_synthetic_fill(self) -> None:
        bars = two_bars()[:1]
        result = DeterministicEventRunner().run(bars, [submission_for(bars)])

        self.assertEqual([], list(result.fill_ledger))
        order = result.order_ledger[-1]
        self.assertEqual(OrderStatus.NO_NEXT_BAR, order.status)
        self.assertIsNone(order.eligible_at)
        self.assertIsNone(order.eligible_bar_id)
        self.assertIsNone(order.eligible_bar_sequence)
        self.assertEqual(SignalStatus.NO_NEXT_BAR, result.signal_ledger[-1].status)

    def test_ledgers_are_append_only_and_fully_traceable(self) -> None:
        bars = two_bars()
        result = DeterministicEventRunner().run(bars, [submission_for(bars)])

        self.assertEqual(
            [
                SignalStatus.CREATED,
                SignalStatus.ORDER_SCHEDULED,
                SignalStatus.FILLED,
            ],
            [row.status for row in result.signal_ledger],
        )
        self.assertEqual(
            [OrderStatus.SCHEDULED, OrderStatus.FILLED],
            [row.status for row in result.order_ledger],
        )
        self.assertIsNone(result.signal_ledger[0].previous_status)
        self.assertEqual(
            SignalStatus.ORDER_SCHEDULED,
            result.signal_ledger[-1].previous_status,
        )
        fill = result.fill_ledger[0]
        order = next(
            row for row in result.order_ledger if row.order_id == fill.order_id
        )
        signal = next(
            row for row in result.signal_ledger if row.signal_id == order.signal_id
        )
        self.assertEqual("005930", signal.stock_code)

    def test_timestamps_remain_aware_and_timezone_metadata_is_canonical(self) -> None:
        bars = two_bars()
        result = DeterministicEventRunner().run(bars, [submission_for(bars)])

        self.assertEqual(CANONICAL_TIMEZONE_ID, result.timezone_identifier)
        for trace in result.event_trace:
            self.assertIsNotNone(trace.event_key.timestamp.utcoffset())
        self.assertIsNotNone(result.fill_ledger[0].filled_at.utcoffset())


class EventRunnerDeterminismTests(unittest.TestCase):
    def test_repeated_and_shuffled_inputs_have_identical_canonical_results(
        self,
    ) -> None:
        samsung = two_bars("005930")
        hynix = two_bars("000660")
        bars = samsung + hynix
        submissions = [submission_for(samsung), submission_for(hynix)]
        runner = DeterministicEventRunner()

        first = runner.run(bars, submissions)
        repeated = runner.run(bars, submissions)
        shuffled = runner.run(list(reversed(bars)), list(reversed(submissions)))

        self.assertEqual(first, repeated)
        self.assertEqual(first, shuffled)
        same_time_signals = [
            row
            for row in first.event_trace
            if row.event_type is EventType.SIGNAL
            and row.event_key.timestamp == submissions[0].intent.execution_signal_at
        ]
        self.assertEqual(
            ["000660", "005930"],
            [
                row.event_key.deterministic_tie_break.stock_code
                for row in same_time_signals
            ],
        )

    def test_duplicate_canonical_signal_event_key_is_rejected(self) -> None:
        bars = two_bars()
        first = submission_for(bars)
        duplicate_intent = StrategyIntent(
            intent_id="different-display-id",
            stock_code=first.intent.stock_code,
            intent_type=first.intent.intent_type,
            side=first.intent.side,
            signal_generated_at=first.intent.signal_generated_at,
            signal_available_at=first.intent.signal_available_at,
            execution_signal_at=first.intent.execution_signal_at,
            reason=first.intent.reason,
            strategy_state=first.intent.strategy_state,
            signal_source_bar_id=first.intent.signal_source_bar_id,
            signal_source_bar_sequence=first.intent.signal_source_bar_sequence,
            unit_delta=1,
        )

        with self.assertRaisesRegex(EventRunnerError, "duplicate EventKey"):
            DeterministicEventRunner().run(
                bars, [first, IntentSubmission(duplicate_intent, 3)]
            )


if __name__ == "__main__":
    unittest.main()
