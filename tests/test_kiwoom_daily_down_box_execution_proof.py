from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.backtest_engine.down_box_strategy import (
    BoxSetup,
    BoxSetupState,
    BoxSignal,
    BoxSignalType,
)
from src.backtest_engine.indicators import DailyIndicatorPoint
from src.backtest_engine.models import DailyBar, Ohlcv
from src.kiwoom_daily.down_box_daily_execution_proof import (
    DailyProofAction,
    ZeroCostDailyAccount,
    _future_return,
    _make_intent,
    _position_signal,
    _sma20_persistent,
    _sma60_flatness,
    _three_day_support,
    audit_floor_reclaim,
    audit_upper_transition,
)


def setup(state: BoxSetupState = BoxSetupState.REVERSAL_WAIT) -> BoxSetup:
    return BoxSetup(
        "daily-setup",
        "005930",
        date(2026, 1, 2),
        Decimal(90),
        Decimal(120),
        date(2025, 12, 1),
        date(2025, 11, 1),
        state=state,
        parent_setup_id="parent"
        if state is BoxSetupState.BREAKOUT_REENTRY_WAIT
        else None,
        breakout_date=date(2026, 1, 2)
        if state is BoxSetupState.BREAKOUT_REENTRY_WAIT
        else None,
    )


def bar(day: date, raw_open: str, signal_close: str) -> DailyBar:
    raw = Ohlcv(
        Decimal(raw_open), Decimal(raw_open), Decimal(raw_open), Decimal(raw_open), 1
    )
    signal = Ohlcv(
        Decimal(signal_close),
        Decimal(signal_close),
        Decimal(signal_close),
        Decimal(signal_close),
        1,
    )
    return DailyBar("005930", day, raw, signal)


def signal(
    action: DailyProofAction, day: date, item: BoxSetup | None = None
) -> BoxSignal:
    return BoxSignal(
        f"signal-{action.value}-{day}",
        {
            DailyProofAction.INITIAL_ENTRY: BoxSignalType.ENTRY_CANDIDATE_MA5_TURN,
            DailyProofAction.HALF_EXIT: BoxSignalType.HALF_EXIT_SIGNAL,
            DailyProofAction.FULL_EXIT_FLOOR_BREAK: BoxSignalType.FULL_EXIT_FLOOR_BREAK,
            DailyProofAction.FULL_TAKE_PROFIT_UPPER: BoxSignalType.FULL_TAKE_PROFIT_UPPER,
            DailyProofAction.BREAKOUT_REENTRY: BoxSignalType.BREAKOUT_REENTRY_CANDIDATE,
        }[action],
        "005930",
        day,
        (item or setup()).setup_id,
        action.value,
    )


def intent_for(
    action: DailyProofAction,
    signal_day: date,
    activation_day: date,
    item: BoxSetup | None = None,
    *,
    reentry: BoxSetup | None = None,
) -> object:
    return _make_intent(
        signal=signal(action, signal_day, item),
        action=action,
        setup=item or setup(),
        activation_date=activation_day,
        equity_at_decision=Decimal(100000000)
        if action in {DailyProofAction.INITIAL_ENTRY, DailyProofAction.BREAKOUT_REENTRY}
        else None,
        entry_type="MA5_TURN" if action is DailyProofAction.INITIAL_ENTRY else None,
        breakout_reentry_setup=reentry,
        metadata={"entry_signal_close": Decimal(100)},
    )


def account() -> ZeroCostDailyAccount:
    return ZeroCostDailyAccount(
        stock_code="005930",
        initial_capital=Decimal(100000000),
        stock_full_weight=Decimal("0.10"),
    )


def open_position(value: ZeroCostDailyAccount) -> BoxSetup:
    item = setup()
    value.schedule(
        intent_for(
            DailyProofAction.INITIAL_ENTRY, date(2026, 1, 2), date(2026, 1, 5), item
        )
    )
    outcome = value.fill_daily_open(
        date(2026, 1, 5), bar(date(2026, 1, 5), "100", "100"), known_days=()
    )
    assert outcome is not None and outcome.next_setup is not None
    return outcome.next_setup


def test_daily_entry_uses_t_plus_one_raw_open_and_no_same_day_fill() -> None:
    value = account()
    value.schedule(
        intent_for(DailyProofAction.INITIAL_ENTRY, date(2026, 1, 2), date(2026, 1, 5))
    )
    assert (
        value.fill_daily_open(
            date(2026, 1, 2), bar(date(2026, 1, 2), "100", "999"), known_days=()
        )
        is None
    )
    value.fill_daily_open(
        date(2026, 1, 5), bar(date(2026, 1, 5), "101", "999"), known_days=()
    )
    assert value.fills[0]["raw_price"] == Decimal(101)
    assert value.quantity == 99009


def test_daily_entry_missing_t_plus_one_expires_without_retry() -> None:
    value = account()
    value.schedule(
        intent_for(DailyProofAction.INITIAL_ENTRY, date(2026, 1, 2), date(2026, 1, 5))
    )
    value.expire_if_due_without_bar(date(2026, 1, 5))
    assert value.pending is None
    assert len(value.expirations) == 1
    assert (
        value.fill_daily_open(
            date(2026, 1, 6), bar(date(2026, 1, 6), "100", "100"), known_days=()
        )
        is None
    )


def test_half_exit_is_floor_half_once_then_full_exit_remaining() -> None:
    value = account()
    item = open_position(value)
    value.schedule(
        intent_for(DailyProofAction.HALF_EXIT, date(2026, 1, 5), date(2026, 1, 6), item)
    )
    value.fill_daily_open(
        date(2026, 1, 6), bar(date(2026, 1, 6), "120", "120"), known_days=()
    )
    assert value.quantity == 50000
    with pytest.raises(ValueError, match="only once"):
        value.schedule(
            intent_for(
                DailyProofAction.HALF_EXIT, date(2026, 1, 7), date(2026, 1, 8), item
            )
        )
    value.schedule(
        intent_for(
            DailyProofAction.FULL_EXIT_FLOOR_BREAK,
            date(2026, 1, 6),
            date(2026, 1, 7),
            item,
        )
    )
    value.fill_daily_open(
        date(2026, 1, 7), bar(date(2026, 1, 7), "110", "110"), known_days=()
    )
    assert value.quantity == 0
    assert len(value.completed_trades) == 1


def test_persistent_full_exit_carries_until_available_daily_bar() -> None:
    value = account()
    item = open_position(value)
    value.schedule(
        intent_for(
            DailyProofAction.FULL_EXIT_FLOOR_BREAK,
            date(2026, 1, 5),
            date(2026, 1, 6),
            item,
        )
    )
    value.expire_if_due_without_bar(date(2026, 1, 6))
    assert value.pending is not None
    value.fill_daily_open(
        date(2026, 1, 7), bar(date(2026, 1, 7), "90", "90"), known_days=()
    )
    assert value.pending is None and value.quantity == 0


def test_raw_adjusted_separation_is_preserved() -> None:
    value = account()
    value.schedule(
        intent_for(DailyProofAction.INITIAL_ENTRY, date(2026, 1, 2), date(2026, 1, 5))
    )
    value.fill_daily_open(
        date(2026, 1, 5), bar(date(2026, 1, 5), "101", "999"), known_days=()
    )
    assert value.fills[0]["raw_price"] == Decimal(101)
    assert value.fills[0]["raw_price"] != Decimal(999)


def test_corporate_action_ambiguous_trade_is_excluded_not_adjusted() -> None:
    value = ZeroCostDailyAccount(
        stock_code="005930",
        initial_capital=Decimal(100000000),
        stock_full_weight=Decimal("0.10"),
        corporate_action_ambiguous=("daily-setup",),
    )
    item = open_position(value)
    value.schedule(
        intent_for(
            DailyProofAction.FULL_EXIT_FLOOR_BREAK,
            date(2026, 1, 5),
            date(2026, 1, 6),
            item,
        )
    )
    value.fill_daily_open(
        date(2026, 1, 6), bar(date(2026, 1, 6), "90", "90"), known_days=()
    )
    assert value.completed_trades == []
    assert (
        value.corporate_action_exclusions[0]["reason"] == "CORPORATE_ACTION_AMBIGUOUS"
    )


def point(
    day: date, sma5: Decimal | None, sma20: Decimal | None, sma60: Decimal | None
) -> DailyIndicatorPoint:
    return DailyIndicatorPoint("005930", day, None, None, sma20, sma60, None, None)


def test_sma20_persistence_uses_strict_order() -> None:
    days = [date(2026, 1, n) for n in range(1, 12)]
    points = [point(day, None, Decimal(100), Decimal(100)) for day in days]
    points[1] = point(days[1], None, Decimal(101), Decimal(100))
    points[5] = point(days[5], None, Decimal(105), Decimal(100))
    points[10] = point(days[10], None, Decimal(110), Decimal(100))
    persistent, change5, change10 = _sma20_persistent(points, 10)
    assert persistent is True
    assert change5 == Decimal(110) / Decimal(105) - Decimal(1)
    assert change10 == Decimal("0.1")
    points[10] = point(days[10], None, Decimal(105), Decimal(100))
    assert _sma20_persistent(points, 10)[0] is False


def test_sma60_flatness_is_relative_and_sign_symmetric() -> None:
    points = [
        point(date(2026, 1, n), None, Decimal(100), Decimal(100)) for n in range(1, 7)
    ]
    points[0] = point(date(2026, 1, 1), None, Decimal(100), Decimal(90))
    points[5] = point(date(2026, 1, 6), None, Decimal(100), Decimal(110))
    flat, signed = _sma60_flatness(points, 5)
    assert flat == Decimal(110) / Decimal(90) - Decimal(1)
    assert signed > 0
    points[5] = point(date(2026, 1, 6), None, Decimal(100), Decimal(70))
    assert _sma60_flatness(points, 5)[0] == Decimal(20) / Decimal(90)


def test_three_day_sma5_support_is_close_inclusive_and_wick_tolerant() -> None:
    bars = [bar(date(2026, 1, n), "100", "100") for n in range(1, 8)]
    points = [
        point(item.trade_date, Decimal(100), Decimal(100), Decimal(100))
        for item in bars
    ]
    assert _three_day_support(bars, points, (4, 5, 6)) == (True, True, True)
    bars[5] = DailyBar(
        "005930",
        bars[5].trade_date,
        bars[5].raw,
        Ohlcv(Decimal(90), Decimal(110), Decimal(80), Decimal(99), 1),
    )
    assert _three_day_support(bars, points, (4, 5, 6)) == (True, False, True)


def test_future_return_and_d3_out_of_window() -> None:
    closes = [Decimal(100), Decimal(101), Decimal(102)]
    assert _future_return(closes, 0, 2) == Decimal("0.02")
    assert _future_return(closes, 1, 2) is None


def test_floor_reclaim_uses_original_entry_signal_close_and_only_d1_to_d3() -> None:
    days = [date(2026, 1, n) for n in range(1, 9)]
    closes = [100, 80, 95, 100, 101, 102, 103, 104]
    bars = [
        bar(day, "100", str(close)) for day, close in zip(days, closes, strict=True)
    ]
    points = [point(day, Decimal(100), Decimal(100), Decimal(100)) for day in days]
    trade = {
        "stock_code": "005930",
        "setup_id": "daily-setup",
        "box_floor": Decimal(90),
        "entry_daily_signal_close": Decimal(100),
        "entry_daily_signal_date": days[0],
        "exit_daily_signal_date": days[1],
        "exit_fill_date": days[1],
    }
    audit = audit_floor_reclaim(trade, bars, points)
    assert audit["fast_reclaim_observed"] is True
    assert audit["fast_reclaim"]["reclaim_date"] == days[3]
    assert len(audit["d1_d3"]) == 3


def test_upper_audit_runups_and_structural_candidate_are_report_only() -> None:
    days = [date(2026, 1, n) for n in range(1, 22)]
    bars = [bar(day, "100", str(100 + n)) for n, day in enumerate(days)]
    points = [
        point(day, Decimal(100), Decimal(100 + n), Decimal(100))
        for n, day in enumerate(days)
    ]
    trade = {
        "stock_code": "005930",
        "setup_id": "daily-setup",
        "entry_type": "MA5_TURN",
        "entry_daily_signal_date": days[0],
        "exit_daily_signal_date": days[10],
        "exit_fill_date": days[10],
        "box_floor": Decimal(90),
        "box_upper": Decimal(120),
        "_exit_fill_raw_open": Decimal(100),
    }
    audit = audit_upper_transition(trade, bars, points)
    assert audit["d3_runup_from_exit_signal"] is not None
    assert audit["d3_runup_from_exit_fill"] is not None
    assert audit["structural_up_transition"] is True
    assert audit["d3_distance_from_old_box_upper"] < 0


def test_position_signal_priority_is_floor_upper_then_half() -> None:
    item = setup(BoxSetupState.BOX_POSITION)
    signals = (
        signal(DailyProofAction.HALF_EXIT, date(2026, 1, 5), item),
        signal(DailyProofAction.FULL_TAKE_PROFIT_UPPER, date(2026, 1, 5), item),
        signal(DailyProofAction.FULL_EXIT_FLOOR_BREAK, date(2026, 1, 5), item),
    )
    assert _position_signal(signals).signal_type is BoxSignalType.FULL_EXIT_FLOOR_BREAK
