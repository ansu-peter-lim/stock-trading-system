from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from src.backtest_engine.down_box_strategy import (
    BoxDayDecision,
    BoxEventType,
    BoxOriginAnalysis,
    BoxReentryTerminalOutcome,
    BoxSetup,
    BoxSetupIssue,
    BoxSetupState,
    BoxSignal,
    BoxSignalType,
    BoxTerminalOutcome,
    DownBoxStrategyConfig,
    DownBoxValidationError,
    _create_reentry_setup,
    _select_floor_and_upper,
    analyze_box_origin,
    evaluate_box_position,
    evaluate_breakout_reentry,
    evaluate_reversal_wait,
    run_down_box_signal_proof,
)
from src.backtest_engine.indicators import (
    DailyIndicatorPoint,
    DailyPivotCandidate,
    PivotKind,
)
from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.validation import KOREA_TZ


def _bars(
    closes: list[int | str], *, lows: list[int | str] | None = None
) -> tuple[DailyBar, ...]:
    values = [Decimal(str(value)) for value in closes]
    low_values = [
        Decimal(str(value)) for value in (lows or [value - 2 for value in values])
    ]
    rows = []
    start = date(2024, 1, 2)
    for index, (close, low) in enumerate(zip(values, low_values, strict=True)):
        high = max(close + 2, low + 2)
        open_price = min(max(close - 1, low), high)
        rows.append(
            DailyBar(
                "005930",
                start + timedelta(days=index),
                Ohlcv(open_price, high, low, close, 1000),
                Ohlcv(open_price, high, low, close, 1000),
            )
        )
    return tuple(rows)


def _points(
    bars: tuple[DailyBar, ...], *, slope: int = -1
) -> tuple[DailyIndicatorPoint, ...]:
    return tuple(
        DailyIndicatorPoint(
            bar.stock_code,
            bar.trade_date,
            Decimal(1),
            Decimal(100),
            Decimal(100),
            Decimal(100),
            Decimal(slope),
            Decimal(slope),
        )
        for bar in bars
    )


def _pivot(
    bars: tuple[DailyBar, ...], index: int, kind: PivotKind, price: int
) -> DailyPivotCandidate:
    return DailyPivotCandidate(
        "005930",
        bars[index].trade_date,
        kind,
        Decimal(price),
        datetime.combine(
            bars[index + 2].trade_date, datetime.min.time(), tzinfo=KOREA_TZ
        ),
    )


def _setup(
    origin: date | None = None, *, state: BoxSetupState = BoxSetupState.REVERSAL_WAIT
) -> BoxSetup:
    return BoxSetup(
        "setup-1",
        "005930",
        origin or date(2024, 1, 2),
        Decimal(80),
        Decimal(150),
        date(2024, 1, 1),
        date(2023, 12, 1),
        state=state,
    )


def test_floor_is_latest_confirmed_low_and_upper_is_nearest_price() -> None:
    bars = _bars([100] * 30)
    pivots = (
        _pivot(bars, 5, PivotKind.HIGH, 180),
        _pivot(bars, 10, PivotKind.HIGH, 160),
        _pivot(bars, 17, PivotKind.LOW, 90),
        _pivot(bars, 20, PivotKind.LOW, 80),
        _pivot(bars, 21, PivotKind.HIGH, 155),
    )
    floor, upper = _select_floor_and_upper(bars, 25, DownBoxStrategyConfig(), pivots)
    assert floor is not None and floor.price == Decimal(80)
    assert upper is not None and upper.price == Decimal(160)


def test_upper_boundary_is_t_minus_ten_and_t_minus_nine_is_rejected() -> None:
    bars = _bars([100] * 30)
    at_boundary = _pivot(bars, 15, PivotKind.HIGH, 150)
    too_recent = _pivot(bars, 16, PivotKind.HIGH, 140)
    floor_pivot = _pivot(bars, 20, PivotKind.LOW, 80)
    floor, upper = _select_floor_and_upper(
        bars, 25, DownBoxStrategyConfig(), (at_boundary, too_recent, floor_pivot)
    )
    assert floor == floor_pivot
    assert upper == at_boundary


def test_upper_price_tie_uses_latest_pivot_deterministically() -> None:
    bars = _bars([100] * 30)
    older = _pivot(bars, 10, PivotKind.HIGH, 160)
    latest = _pivot(bars, 12, PivotKind.HIGH, 160)
    floor = _pivot(bars, 20, PivotKind.LOW, 80)
    _, upper = _select_floor_and_upper(
        bars, 25, DownBoxStrategyConfig(), (older, latest, floor)
    )
    assert upper == latest


def test_unconfirmed_pivot_is_not_available_at_origin() -> None:
    bars = _bars([100] * 30)
    future = DailyPivotCandidate(
        "005930",
        bars[10].trade_date,
        PivotKind.HIGH,
        Decimal(160),
        datetime.combine(bars[26].trade_date, datetime.min.time(), tzinfo=KOREA_TZ),
    )
    floor = _pivot(bars, 20, PivotKind.LOW, 80)
    selected_floor, selected_upper = _select_floor_and_upper(
        bars, 25, DownBoxStrategyConfig(), (future, floor)
    )
    assert selected_floor == floor
    assert selected_upper is None


def test_no_valid_upper_is_explicit() -> None:
    bars = _bars([100] * 30)
    floor = _pivot(bars, 20, PivotKind.LOW, 80)
    with patch(
        "src.backtest_engine.down_box_strategy.detect_daily_pivots",
        return_value=(floor,),
    ):
        analysis = analyze_box_origin(bars, _points(bars), 25)
    assert analysis.issue is BoxSetupIssue.NO_VALID_BOX_UPPER


def test_origin_requires_down_and_first_sma10_breakout_with_t_excluded_context() -> (
    None
):
    closes = [90] * 10 + [110] + [100] * 15
    bars = _bars(closes, lows=[81] + [88] * 9 + [108] + [98] * 15)
    points = _points(bars)
    floor = _pivot(bars, 8, PivotKind.LOW, 80)
    upper = _pivot(bars, 0, PivotKind.HIGH, 150)
    with patch(
        "src.backtest_engine.down_box_strategy._select_floor_and_upper",
        return_value=(floor, upper),
    ):
        analysis = analyze_box_origin(bars, points, 10)
    assert analysis.setup is not None
    assert analysis.issue is None


def test_origin_without_first_breakout_is_rejected() -> None:
    bars = _bars([90] * 26, lows=[81] + [88] * 25)
    with patch(
        "src.backtest_engine.down_box_strategy._select_floor_and_upper",
        return_value=(
            _pivot(bars, 8, PivotKind.LOW, 80),
            _pivot(bars, 0, PivotKind.HIGH, 150),
        ),
    ):
        analysis = analyze_box_origin(bars, _points(bars), 10)
    assert analysis.issue is BoxSetupIssue.NO_FIRST_SMA10_BREAKOUT


def test_origin_upper_sell_proximity_rejects_setup() -> None:
    closes = [90] * 10 + [147] + [100] * 2
    bars = _bars(closes, lows=[81] + [88] * 9 + [145] + [98] * 2)
    with patch(
        "src.backtest_engine.down_box_strategy._select_floor_and_upper",
        return_value=(
            _pivot(bars, 8, PivotKind.LOW, 80),
            _pivot(bars, 0, PivotKind.HIGH, 150),
        ),
    ):
        analysis = analyze_box_origin(bars, _points(bars), 10)
    assert analysis.issue is BoxSetupIssue.ORIGIN_UPPER_SELL_ZONE


@pytest.mark.parametrize("value", ["80", "82.4"])
def test_lower_zone_floor_and_three_percent_edges_are_inclusive(value: str) -> None:
    bars = _bars(
        [100] * 10 + [110] + [100] * 2, lows=[80] * 10 + [Decimal(value), 98, 98]
    )
    points = _points(bars)
    setup = _setup(bars[9].trade_date)
    decision = evaluate_reversal_wait(setup, bars, points, 10)
    assert any(
        event.event_type.value == "LOWER_ZONE_TOUCH" for event in decision.events
    )


def test_reversal_wait_d1_candidate_and_d11_expiry() -> None:
    bars = _bars([100] * 11 + [101] + [100] * 11)
    points = _points(bars)
    setup = _setup(bars[10].trade_date)
    day_one = evaluate_reversal_wait(setup, bars, points, 11)
    assert day_one.signals
    assert day_one.signals[0].signal_type is BoxSignalType.ENTRY_CANDIDATE_MA5_TURN
    expired = evaluate_reversal_wait(setup, bars, points, 21)
    assert expired.setup_after.state is BoxSetupState.EXPIRED


def test_ma5_turn_without_ma5_band_touch_is_not_a_candidate() -> None:
    closes = [100] * 11 + [120] + [100] * 2
    bars = _bars(closes, lows=[98] * 11 + [110, 98, 98])
    decision = evaluate_reversal_wait(
        _setup(bars[10].trade_date), bars, _points(bars), 11
    )
    assert any(event.event_type.value == "MA5_TURN" for event in decision.events)
    assert decision.signals == ()


def test_floor_close_break_invalidates_but_wick_below_does_not() -> None:
    bars = _bars([100] * 11 + [79, 100], lows=[98] * 11 + [78, 79])
    points = _points(bars)
    setup = _setup(bars[10].trade_date)
    broken = evaluate_reversal_wait(setup, bars, points, 11)
    assert broken.setup_after.state is BoxSetupState.INVALIDATED

    wick_bars = _bars([100] * 11 + [100, 100], lows=[98] * 11 + [79, 98])
    intact = evaluate_reversal_wait(setup, wick_bars, _points(wick_bars), 11)
    assert intact.setup_after.state is BoxSetupState.REVERSAL_WAIT


def test_sma10_rebreak_requires_a_prior_armed_close() -> None:
    bars = _bars([100] * 11 + [90, 110], lows=[98] * 11 + [88, 108])
    setup = _setup(bars[10].trade_date)
    first = evaluate_reversal_wait(setup, bars, _points(bars), 11)
    assert first.setup_after.rebreak_armed is True
    second = evaluate_reversal_wait(first.setup_after, bars, _points(bars), 12)
    assert any(event.event_type.value == "SMA10_REBREAK" for event in second.events)
    assert any(
        signal.signal_type is BoxSignalType.ENTRY_CANDIDATE_SMA10_REBREAK
        for signal in second.signals
    )


def test_first_breakout_day_is_not_a_rebreak() -> None:
    bars = _bars([100] * 11 + [110], lows=[98] * 11 + [108])
    decision = evaluate_reversal_wait(
        _setup(bars[10].trade_date), bars, _points(bars), 11
    )
    assert all(event.event_type.value != "SMA10_REBREAK" for event in decision.events)


def test_box_position_full_exit_priority_suppresses_half_exit() -> None:
    bars = _bars([100, 99], lows=[98, 98])
    points = list(_points(bars))
    points[0] = DailyIndicatorPoint(
        "005930",
        bars[0].trade_date,
        None,
        Decimal(100),
        Decimal(100),
        Decimal(100),
        -1,
        -1,
    )
    points[1] = DailyIndicatorPoint(
        "005930",
        bars[1].trade_date,
        None,
        Decimal(100),
        Decimal(100),
        Decimal(100),
        -1,
        -1,
    )
    bars = tuple(
        DailyBar(
            bar.stock_code,
            bar.trade_date,
            bar.raw
            if index == 0
            else Ohlcv(Decimal(100), Decimal(147), Decimal(98), Decimal(99), 1000),
            bar.signal
            if index == 0
            else Ohlcv(Decimal(100), Decimal(147), Decimal(98), Decimal(99), 1000),
        )
        for index, bar in enumerate(bars)
    )
    decision = evaluate_box_position(
        _setup(bars[0].trade_date, state=BoxSetupState.BOX_POSITION),
        bars,
        tuple(points),
        1,
    )
    assert [signal.signal_type for signal in decision.signals] == [
        BoxSignalType.FULL_TAKE_PROFIT_UPPER
    ]


def test_half_exit_is_emitted_only_once() -> None:
    bars = _bars([100, 99, 98], lows=[98, 97, 96])
    points = tuple(_points(bars))
    setup = _setup(bars[0].trade_date, state=BoxSetupState.BOX_POSITION)
    first = evaluate_box_position(setup, bars, points, 1)
    assert first.signals[0].signal_type is BoxSignalType.HALF_EXIT_SIGNAL
    second = evaluate_box_position(first.setup_after, bars, points, 2)
    assert second.signals == ()


def test_box_breakout_freezes_old_upper_and_reentry_failure_is_explicit() -> None:
    bars = _bars([100, 160, 140], lows=[98, 150, 138])
    points = tuple(_points(bars, slope=1))
    setup = _setup(bars[0].trade_date, state=BoxSetupState.BOX_POSITION)
    breakout = evaluate_box_position(setup, bars, points, 1)
    assert breakout.setup_after.state is BoxSetupState.BREAKOUT_REENTRY_WAIT
    assert breakout.setup_after.box_upper == Decimal(150)
    failed = evaluate_breakout_reentry(breakout.setup_after, bars, points, 2)
    assert failed.setup_after.state is BoxSetupState.INVALIDATED


def test_breakout_reentry_requires_old_upper_and_up_trend() -> None:
    bars = _bars([100] * 10 + [150], lows=[98] * 10 + [105])
    points = list(_points(bars, slope=1))
    decision = evaluate_breakout_reentry(
        _create_reentry_setup(_setup(bars[0].trade_date), bars[0].trade_date),
        bars,
        tuple(points),
        10,
    )
    assert decision.signals[0].signal_type is BoxSignalType.BREAKOUT_REENTRY_CANDIDATE


def test_input_order_is_canonicalized_for_origin_analysis() -> None:
    bars = _bars([90] * 10 + [110] + [100] * 5, lows=[81] + [88] * 9 + [108] + [98] * 5)
    points = _points(bars)
    floor, upper = (
        _pivot(bars, 8, PivotKind.LOW, 80),
        _pivot(bars, 0, PivotKind.HIGH, 150),
    )
    with patch(
        "src.backtest_engine.down_box_strategy._select_floor_and_upper",
        return_value=(floor, upper),
    ):
        forward = analyze_box_origin(bars, points, 10)
        reverse = analyze_box_origin(tuple(reversed(bars)), tuple(reversed(points)), 10)
    assert forward.setup is not None and reverse.setup is not None
    assert forward.setup.setup_id == reverse.setup.setup_id


def test_signal_proof_stops_reversal_wait_after_first_buy_candidate() -> None:
    bars = _bars([100, 101, 102])
    setup = _setup(bars[0].trade_date)
    origin = BoxOriginAnalysis(
        stock_code=setup.stock_code,
        trade_date=setup.setup_origin_date,
        setup=setup,
        issue=None,
        box_floor=setup.box_floor,
        box_upper=setup.box_upper,
        floor_pivot_date=setup.floor_pivot_date,
        upper_pivot_date=setup.upper_pivot_date,
        lower_zone_touched=True,
        floor_integrity=True,
        down_trend=True,
    )
    no_origin = BoxOriginAnalysis(
        stock_code=setup.stock_code,
        trade_date=bars[2].trade_date,
        setup=None,
        issue=BoxSetupIssue.NOT_DOWN_TREND,
        box_floor=None,
        box_upper=None,
        floor_pivot_date=None,
        upper_pivot_date=None,
        lower_zone_touched=False,
        floor_integrity=False,
        down_trend=False,
    )
    signal = BoxSignal(
        signal_id="signal-1",
        signal_type=BoxSignalType.ENTRY_CANDIDATE_MA5_TURN,
        stock_code=setup.stock_code,
        signal_date=bars[1].trade_date,
        setup_id=setup.setup_id,
        reason="MA5_TURN",
    )
    decision = BoxDayDecision(
        stock_code=setup.stock_code,
        trade_date=bars[1].trade_date,
        setup_before=setup,
        setup_after=replace(setup, sessions_elapsed=1),
        events=(),
        signals=(signal,),
    )
    with (
        patch(
            "src.backtest_engine.down_box_strategy.analyze_box_origin",
            side_effect=[origin, no_origin, no_origin],
        ),
        patch(
            "src.backtest_engine.down_box_strategy.evaluate_reversal_wait",
            return_value=decision,
        ) as evaluate_wait,
    ):
        result = run_down_box_signal_proof(bars)

    assert evaluate_wait.call_count == 1
    assert result["setups_created"] == 1
    assert result["entry_candidate_count"] == 1
    assert len(result["signals"]) == 1
    entry_events = [
        event
        for event in result["events"]
        if event.event_type is BoxEventType.ENTRY_SIGNALLED
    ]
    assert len(entry_events) == 1
    assert entry_events[0].event_date == bars[1].trade_date
    assert result["setup_lifecycle"] == (
        {
            "setup_id": setup.setup_id,
            "stock_code": setup.stock_code,
            "setup_origin_date": setup.setup_origin_date,
            "terminal_outcome": BoxTerminalOutcome.ENTRY_SIGNALLED.value,
            "terminal_date": bars[1].trade_date,
            "terminal_reason": "FIRST_BUY_CANDIDATE_TERMINAL",
        },
    )


def test_reversal_wait_breakout_beats_upper_invalidation_and_entry() -> None:
    bars = _bars([100] * 11 + [151], lows=[98] * 11 + [110])
    decision = evaluate_reversal_wait(
        _setup(bars[10].trade_date), bars, _points(bars), 11
    )
    assert decision.setup_after.state is BoxSetupState.BREAKOUT_REENTRY_WAIT
    assert any(
        event.event_type is BoxEventType.BOX_BREAKOUT_CONFIRMED
        for event in decision.events
    )
    assert not any(
        event.event_type is BoxEventType.SETUP_INVALIDATED for event in decision.events
    )
    assert decision.signals == ()


def test_reversal_wait_close_equal_upper_is_upper_invalidation() -> None:
    bars = _bars([100] * 11 + [150], lows=[98] * 11 + [145])
    decision = evaluate_reversal_wait(
        _setup(bars[10].trade_date), bars, _points(bars), 11
    )
    assert decision.setup_after.state is BoxSetupState.INVALIDATED
    assert not any(
        event.event_type is BoxEventType.BOX_BREAKOUT_CONFIRMED
        for event in decision.events
    )
    assert any(event.reason == "HIGH_IN_UPPER_SELL_ZONE" for event in decision.events)


def test_reversal_wait_upper_zone_without_breakout_is_invalidated() -> None:
    bars = _bars([100] * 11 + [120], lows=[98] * 11 + [115])
    last = bars[-1]
    bars = (
        *bars[:-1],
        replace(
            last,
            raw=replace(last.raw, high=Decimal(147)),
            signal=replace(last.signal, high=Decimal(147)),
        ),
    )
    decision = evaluate_reversal_wait(
        _setup(bars[10].trade_date), bars, _points(bars), 11
    )
    assert decision.setup_after.state is BoxSetupState.INVALIDATED
    assert any(event.reason == "HIGH_IN_UPPER_SELL_ZONE" for event in decision.events)


def test_reversal_wait_floor_break_has_highest_priority() -> None:
    bars = _bars([100] * 11 + [79], lows=[98] * 11 + [78])
    last = bars[-1]
    bars = (
        *bars[:-1],
        replace(
            last,
            raw=replace(last.raw, high=Decimal(160)),
            signal=replace(last.signal, high=Decimal(160)),
        ),
    )
    decision = evaluate_reversal_wait(
        _setup(bars[10].trade_date), bars, _points(bars), 11
    )
    assert decision.setup_after.state is BoxSetupState.INVALIDATED
    assert any(
        event.event_type is BoxEventType.FLOOR_BREAK for event in decision.events
    )
    assert not any(
        event.event_type is BoxEventType.BOX_BREAKOUT_CONFIRMED
        for event in decision.events
    )


def test_breakout_creates_distinct_reentry_setup_with_frozen_upper() -> None:
    parent = _setup()
    reentry = _create_reentry_setup(parent, date(2024, 1, 3))
    repeated = _create_reentry_setup(parent, date(2024, 1, 3))
    assert reentry.setup_id != parent.setup_id
    assert reentry.setup_id == repeated.setup_id
    assert reentry.parent_setup_id == parent.setup_id
    assert reentry.box_upper == parent.box_upper
    assert reentry.breakout_date == date(2024, 1, 3)


def test_breakout_reentry_cannot_be_evaluated_on_breakout_session() -> None:
    bars = _bars([100] * 12)
    setup = _create_reentry_setup(_setup(), bars[10].trade_date)
    with pytest.raises(DownBoxValidationError, match="starts after"):
        evaluate_breakout_reentry(setup, bars, _points(bars, slope=1), 10)


def test_breakout_reentry_close_equal_upper_is_not_failure() -> None:
    bars = _bars([100] * 11 + [150], lows=[98] * 11 + [145])
    setup = _create_reentry_setup(_setup(bars[10].trade_date), bars[10].trade_date)
    decision = evaluate_breakout_reentry(setup, bars, _points(bars), 11)
    assert decision.setup_after.state is BoxSetupState.BREAKOUT_REENTRY_WAIT
    assert not any(
        event.event_type is BoxEventType.BREAKOUT_FAILED for event in decision.events
    )


@pytest.mark.parametrize(
    ("closes", "lows"),
    [
        ([100] * 12, [98] * 11 + [97]),
        ([897] * 11 + [927], [895] * 11 + [880]),
    ],
)
def test_breakout_reentry_sma10_band_edges_are_inclusive(
    closes: list[int], lows: list[int]
) -> None:
    bars = _bars(closes, lows=lows)
    setup = _create_reentry_setup(
        replace(_setup(bars[10].trade_date), box_upper=Decimal(90)),
        bars[10].trade_date,
    )
    decision = evaluate_breakout_reentry(setup, bars, _points(bars, slope=1), 11)
    assert decision.setup_after.state is BoxSetupState.REENTRY_SIGNALLED
    assert decision.signals[0].signal_type is BoxSignalType.BREAKOUT_REENTRY_CANDIDATE


def test_breakout_reentry_requires_close_above_upper_and_up_trend() -> None:
    bars = _bars([100] * 12, lows=[98] * 11 + [97])
    failed = evaluate_breakout_reentry(
        _create_reentry_setup(
            replace(_setup(bars[10].trade_date), box_upper=Decimal(101)),
            bars[10].trade_date,
        ),
        bars,
        _points(bars, slope=1),
        11,
    )
    assert failed.setup_after.state is BoxSetupState.INVALIDATED
    no_up = evaluate_breakout_reentry(
        _create_reentry_setup(
            replace(_setup(bars[10].trade_date), box_upper=Decimal(90)),
            bars[10].trade_date,
        ),
        bars,
        _points(bars, slope=-1),
        11,
    )
    assert no_up.setup_after.state is BoxSetupState.BREAKOUT_REENTRY_WAIT
    assert no_up.signals == ()


def test_reentry_wait_has_no_arbitrary_expiry() -> None:
    bars = _bars([100] * 30, lows=[80] * 30)
    setup = _create_reentry_setup(
        replace(_setup(bars[0].trade_date), box_upper=Decimal(90)),
        bars[0].trade_date,
    )
    decision = evaluate_breakout_reentry(setup, bars, _points(bars), 29)
    assert decision.setup_after.state is BoxSetupState.BREAKOUT_REENTRY_WAIT
    assert not any(
        event.event_type is BoxEventType.SETUP_EXPIRED for event in decision.events
    )


def test_signal_proof_tracks_unique_breakout_and_reentry_terminal_outcomes() -> None:
    bars = _bars([100, 151, 151], lows=[98, 149, 149])
    setup = _setup(bars[0].trade_date)
    origin = BoxOriginAnalysis(
        stock_code=setup.stock_code,
        trade_date=setup.setup_origin_date,
        setup=setup,
        issue=None,
        box_floor=setup.box_floor,
        box_upper=setup.box_upper,
        floor_pivot_date=setup.floor_pivot_date,
        upper_pivot_date=setup.upper_pivot_date,
        lower_zone_touched=True,
        floor_integrity=True,
        down_trend=True,
    )
    with patch(
        "src.backtest_engine.down_box_strategy.analyze_box_origin",
        return_value=origin,
    ):
        result = run_down_box_signal_proof(bars)

    assert result["setup_lifecycle"][0]["terminal_outcome"] == (
        BoxTerminalOutcome.BOX_BREAKOUT_CONFIRMED.value
    )
    assert result["breakout_reentry_lifecycle"][0]["terminal_outcome"] == (
        BoxReentryTerminalOutcome.END_OF_DATA_ACTIVE.value
    )
    assert len({row["setup_id"] for row in result["setup_lifecycle"]}) == 1
    assert (
        len({row["reentry_setup_id"] for row in result["breakout_reentry_lifecycle"]})
        == 1
    )
