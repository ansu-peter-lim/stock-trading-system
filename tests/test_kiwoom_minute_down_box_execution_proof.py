from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.backtest_engine.down_box_strategy import (
    BoxSetup,
    BoxSetupState,
    BoxSignal,
    BoxSignalType,
)
from src.backtest_engine.models import Ohlcv
from src.kiwoom_minute.down_box_execution_proof import (
    BoxProofAction,
    BoxProofPositionState,
    ZeroCostBoxProofAccount,
    _make_intent,
    _position_action,
)
from src.kiwoom_minute.pipeline import ASSUMPTION_ID, MinuteSourceBar

KST = timezone(timedelta(hours=9))


def setup(state: BoxSetupState = BoxSetupState.REVERSAL_WAIT) -> BoxSetup:
    parent = "parent" if state is BoxSetupState.BREAKOUT_REENTRY_WAIT else None
    breakout = date(2026, 1, 2) if parent else None
    return BoxSetup(
        "setup-1",
        "005930",
        date(2026, 1, 2),
        Decimal(90),
        Decimal(120),
        date(2025, 12, 1),
        date(2025, 11, 1),
        state=state,
        parent_setup_id=parent,
        breakout_date=breakout,
    )


def source_bar(
    day: date,
    sequence: int,
    *,
    label_time: str = "090000",
    raw_open: str = "100",
    signal_open: str = "999",
) -> MinuteSourceBar:
    label = f"{day:%Y%m%d}{label_time}"
    at = datetime.strptime(label, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    raw = Ohlcv(
        Decimal(raw_open),
        Decimal(raw_open),
        Decimal(raw_open),
        Decimal(raw_open),
        1,
    )
    signal = Ohlcv(
        Decimal(signal_open),
        Decimal(signal_open),
        Decimal(signal_open),
        Decimal(signal_open),
        1,
    )
    return MinuteSourceBar(
        "005930",
        label,
        at,
        day,
        sequence,
        f"bar-{sequence}",
        raw,
        signal,
        ASSUMPTION_ID,
    )


def account() -> ZeroCostBoxProofAccount:
    return ZeroCostBoxProofAccount(
        stock_code="005930",
        initial_capital=Decimal(100000000),
        stock_full_weight=Decimal("0.10"),
    )


def intent(
    action: BoxProofAction,
    *,
    signal_day: date = date(2026, 1, 2),
    activation_day: date = date(2026, 1, 5),
    item: BoxSetup | None = None,
    reentry: BoxSetup | None = None,
) -> object:
    return _make_intent(
        stock_code="005930",
        action=action,
        signal_date=signal_day,
        activation_date=activation_day,
        setup=item or setup(),
        signal_id=f"signal-{action.value}-{signal_day}",
        equity_at_decision=(
            Decimal(100000000)
            if action in {BoxProofAction.INITIAL_ENTRY, BoxProofAction.BREAKOUT_REENTRY}
            else None
        ),
        entry_type="MA5_TURN" if action is BoxProofAction.INITIAL_ENTRY else None,
        breakout_reentry_setup=reentry,
        metadata={"box_position_close": Decimal("0.25")},
    )


def fill_entry(
    value: ZeroCostBoxProofAccount,
    *,
    signal_day: date = date(2026, 1, 2),
    fill_day: date = date(2026, 1, 5),
    raw_open: str = "100",
) -> BoxSetup:
    value.schedule(
        intent(
            BoxProofAction.INITIAL_ENTRY, signal_day=signal_day, activation_day=fill_day
        )
    )
    outcome = value.fill_session_open(
        fill_day,
        source_bar(fill_day, 0, raw_open=raw_open),
        known_days=(signal_day, fill_day),
    )
    assert outcome is not None and outcome.next_setup is not None
    return outcome.next_setup


def test_entry_fills_t_plus_one_first_included_raw_open_at_full_weight() -> None:
    value = account()
    next_setup = fill_entry(value)
    assert value.quantity == 100000
    assert value.cash == Decimal(90000000)
    assert value.fills[0]["raw_price"] == Decimal(100)
    assert value.fills[0]["filled_at_source_label"] == "20260105090000"
    assert next_setup.state is BoxSetupState.BOX_POSITION


def test_signal_day_bar_cannot_fill_t_plus_one_intent() -> None:
    value = account()
    value.schedule(intent(BoxProofAction.INITIAL_ENTRY))
    assert (
        value.fill_session_open(
            date(2026, 1, 2),
            source_bar(date(2026, 1, 2), 0),
            known_days=(date(2026, 1, 2), date(2026, 1, 5)),
        )
        is None
    )
    assert value.fills == []


def test_entry_has_no_overnight_retry() -> None:
    value = account()
    value.schedule(intent(BoxProofAction.INITIAL_ENTRY))
    value.expire_if_due_without_row(date(2026, 1, 5))
    assert value.pending is None
    assert len(value.expirations) == 1
    assert (
        value.fill_session_open(
            date(2026, 1, 6),
            source_bar(date(2026, 1, 6), 0),
            known_days=(),
        )
        is None
    )


def test_raw_and_adjusted_prices_remain_separate() -> None:
    value = account()
    value.schedule(intent(BoxProofAction.INITIAL_ENTRY))
    value.fill_session_open(
        date(2026, 1, 5),
        source_bar(date(2026, 1, 5), 0, raw_open="101", signal_open="999"),
        known_days=(),
    )
    assert value.fills[0]["raw_price"] == Decimal(101)
    assert value.fills[0]["raw_price"] != Decimal(999)


def test_half_exit_sells_floor_half_of_initial_quantity_only_once() -> None:
    value = account()
    position_setup = fill_entry(value, raw_open="3000000")
    assert value.quantity == 3
    value.schedule(
        intent(
            BoxProofAction.HALF_EXIT,
            signal_day=date(2026, 1, 5),
            activation_day=date(2026, 1, 6),
            item=position_setup,
        )
    )
    value.fill_session_open(
        date(2026, 1, 6),
        source_bar(date(2026, 1, 6), 1, raw_open="3300000"),
        known_days=(),
    )
    assert value.quantity == 2
    assert value.state is BoxProofPositionState.HALF_POSITION
    assert value.half_exit_done
    with pytest.raises(ValueError, match="only once"):
        value.schedule(
            intent(
                BoxProofAction.HALF_EXIT,
                signal_day=date(2026, 1, 7),
                activation_day=date(2026, 1, 8),
                item=position_setup,
            )
        )


def test_half_exit_expires_without_retry_and_consumes_lifecycle_signal() -> None:
    value = account()
    position_setup = fill_entry(value)
    value.schedule(
        intent(
            BoxProofAction.HALF_EXIT,
            signal_day=date(2026, 1, 5),
            activation_day=date(2026, 1, 6),
            item=position_setup,
        )
    )
    value.expire_if_due_without_row(date(2026, 1, 6))
    assert value.state is BoxProofPositionState.FULL_POSITION
    with pytest.raises(ValueError, match="only once"):
        value.schedule(
            intent(
                BoxProofAction.HALF_EXIT,
                signal_day=date(2026, 1, 7),
                activation_day=date(2026, 1, 8),
                item=position_setup,
            )
        )


@pytest.mark.parametrize(
    "action",
    [BoxProofAction.FULL_EXIT_FLOOR_BREAK, BoxProofAction.FULL_TAKE_PROFIT_UPPER],
)
def test_full_exit_closes_all_remaining_quantity(action: BoxProofAction) -> None:
    value = account()
    position_setup = fill_entry(value)
    value.schedule(
        intent(
            action,
            signal_day=date(2026, 1, 5),
            activation_day=date(2026, 1, 6),
            item=position_setup,
        )
    )
    value.fill_session_open(
        date(2026, 1, 6),
        source_bar(date(2026, 1, 6), 1, raw_open="110"),
        known_days=(date(2026, 1, 5), date(2026, 1, 6)),
    )
    assert value.quantity == 0
    assert value.state is BoxProofPositionState.FLAT
    assert value.completed_trades[0]["exit_action"] == action.value


def test_half_then_full_exit_pnl_uses_both_realized_legs() -> None:
    value = account()
    position_setup = fill_entry(value)
    value.schedule(
        intent(
            BoxProofAction.HALF_EXIT,
            signal_day=date(2026, 1, 5),
            activation_day=date(2026, 1, 6),
            item=position_setup,
        )
    )
    value.fill_session_open(
        date(2026, 1, 6),
        source_bar(date(2026, 1, 6), 1, raw_open="120"),
        known_days=(),
    )
    value.schedule(
        intent(
            BoxProofAction.FULL_EXIT_FLOOR_BREAK,
            signal_day=date(2026, 1, 6),
            activation_day=date(2026, 1, 7),
            item=position_setup,
        )
    )
    value.fill_session_open(
        date(2026, 1, 7),
        source_bar(date(2026, 1, 7), 2, raw_open="80"),
        known_days=(date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)),
    )
    trade = value.completed_trades[0]
    assert trade["pnl_amount"] == Decimal(0)
    assert trade["pnl_pct"] == Decimal(0)


def test_full_exit_persists_to_next_tradable_session() -> None:
    value = account()
    position_setup = fill_entry(value)
    value.schedule(
        intent(
            BoxProofAction.FULL_EXIT_FLOOR_BREAK,
            signal_day=date(2026, 1, 5),
            activation_day=date(2026, 1, 6),
            item=position_setup,
        )
    )
    value.expire_if_due_without_row(date(2026, 1, 6))
    assert value.pending is not None
    outcome = value.fill_session_open(
        date(2026, 1, 7),
        source_bar(date(2026, 1, 7), 2),
        known_days=(),
    )
    assert outcome is not None
    assert value.quantity == 0


def test_floor_full_exit_has_priority_over_upper_and_half() -> None:
    item = setup(BoxSetupState.BOX_POSITION)
    signals = (
        BoxSignal(
            "half",
            BoxSignalType.HALF_EXIT_SIGNAL,
            "005930",
            date(2026, 1, 5),
            item.setup_id,
            "half",
        ),
        BoxSignal(
            "upper",
            BoxSignalType.FULL_TAKE_PROFIT_UPPER,
            "005930",
            date(2026, 1, 5),
            item.setup_id,
            "upper",
        ),
        BoxSignal(
            "floor",
            BoxSignalType.FULL_EXIT_FLOOR_BREAK,
            "005930",
            date(2026, 1, 5),
            item.setup_id,
            "floor",
        ),
    )
    assert _position_action(signals).signal_type is BoxSignalType.FULL_EXIT_FLOOR_BREAK


def test_upper_full_exit_suppresses_half_exit() -> None:
    item = setup(BoxSetupState.BOX_POSITION)
    signals = (
        BoxSignal(
            "half",
            BoxSignalType.HALF_EXIT_SIGNAL,
            "005930",
            date(2026, 1, 5),
            item.setup_id,
            "half",
        ),
        BoxSignal(
            "upper",
            BoxSignalType.FULL_TAKE_PROFIT_UPPER,
            "005930",
            date(2026, 1, 5),
            item.setup_id,
            "upper",
        ),
    )
    assert _position_action(signals).signal_type is BoxSignalType.FULL_TAKE_PROFIT_UPPER


def test_breakout_take_profit_exit_activates_wait_only_after_fill() -> None:
    value = account()
    position_setup = fill_entry(value)
    reentry = BoxSetup(
        "reentry-1",
        "005930",
        date(2026, 1, 5),
        position_setup.box_floor,
        position_setup.box_upper,
        position_setup.floor_pivot_date,
        position_setup.upper_pivot_date,
        state=BoxSetupState.BREAKOUT_REENTRY_WAIT,
        parent_setup_id=position_setup.setup_id,
        breakout_date=date(2026, 1, 5),
    )
    value.schedule(
        intent(
            BoxProofAction.FULL_TAKE_PROFIT_UPPER,
            signal_day=date(2026, 1, 5),
            activation_day=date(2026, 1, 6),
            item=position_setup,
            reentry=reentry,
        )
    )
    assert value.state is BoxProofPositionState.FULL_POSITION
    outcome = value.fill_session_open(
        date(2026, 1, 6),
        source_bar(date(2026, 1, 6), 1),
        known_days=(),
    )
    assert outcome is not None and outcome.next_setup is reentry
    assert value.state is BoxProofPositionState.BREAKOUT_REENTRY_WAIT


def test_breakout_reentry_fills_once_then_hands_off_without_pnl() -> None:
    value = account()
    reentry = setup(BoxSetupState.BREAKOUT_REENTRY_WAIT)
    value.state = BoxProofPositionState.BREAKOUT_REENTRY_WAIT
    value.schedule(
        intent(
            BoxProofAction.BREAKOUT_REENTRY,
            signal_day=date(2026, 1, 5),
            activation_day=date(2026, 1, 6),
            item=reentry,
        )
    )
    value.fill_session_open(
        date(2026, 1, 6),
        source_bar(date(2026, 1, 6), 1),
        known_days=(),
    )
    assert value.state is BoxProofPositionState.HANDOFF_TO_UP_FILLED
    assert value.handoffs[0]["post_handoff_pnl_calculated"] is False
    with pytest.raises(ValueError, match="matching flat state"):
        value.schedule(
            intent(
                BoxProofAction.BREAKOUT_REENTRY,
                signal_day=date(2026, 1, 7),
                activation_day=date(2026, 1, 8),
                item=reentry,
            )
        )


def test_decimal_accounting_has_no_float_values() -> None:
    value = account()
    fill_entry(value, raw_open="101")
    assert isinstance(value.cash, Decimal)
    assert isinstance(value.current_trade["initial_invested_capital"], Decimal)
    assert all(isinstance(row["raw_price"], Decimal) for row in value.fills)


def test_replay_is_deterministic() -> None:
    first = account()
    second = account()
    fill_entry(first)
    fill_entry(second)
    assert first.fills == second.fills
    assert first.transitions == second.transitions


def test_future_bar_does_not_change_prior_fill() -> None:
    first = account()
    second = account()
    fill_entry(first)
    fill_entry(second)
    future = source_bar(date(2026, 1, 6), 1, raw_open="999")
    assert future.source_label > first.fills[0]["filled_at_source_label"]
    assert first.fills == second.fills
