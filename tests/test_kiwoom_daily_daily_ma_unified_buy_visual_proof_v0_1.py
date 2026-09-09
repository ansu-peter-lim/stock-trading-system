from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.backtest_engine.models import DailyBar, Ohlcv
from src.kiwoom_daily.daily_ma_unified_buy_visual_proof_v0_1 import (
    BuyType,
    DailyMaPoint,
    _summary,
    assert_no_future_outcome_fields,
    calculate_daily_ma_points,
    confirmed_breakdown,
    confirmed_breakout,
    consecutive_below_runs,
    detect_buy_candidates,
    select_signal_windows,
    slope_state,
    upward_inflection,
)
from src.strategy_review.chart import ChartType, prepare_review_chart


def d(value: str | int) -> Decimal:
    return Decimal(str(value))


def _bar(index: int, close: Decimal | None = None) -> DailyBar:
    close = close if close is not None else d(100 + index)
    return DailyBar(
        "005930",
        date(2024, 1, 1) + timedelta(days=index),
        Ohlcv(close, close + 1, close - 1, close, 100),
        Ohlcv(close, close + 1, close - 1, close, 100),
    )


def _point(
    index: int,
    *,
    ma5_inflection: bool = False,
    ma10_inflection: bool = False,
    ma10_breakout: bool = False,
    ma20_breakout: bool = False,
    ma10_breakdown: bool = False,
    run10: int = 0,
    run20: int = 0,
    slope: str = "0",
) -> DailyMaPoint:
    return DailyMaPoint(
        trade_date=date(2024, 1, 1) + timedelta(days=index),
        close=d(102),
        ma5=d(100),
        ma10=d(100),
        ma20=d(100),
        ma60=d(100),
        ma120=d(100),
        ma10_slope10_pct=d(slope),
        ma20_slope10_pct=d(0),
        ma60_slope10_pct=d(0),
        ma10_below_run=run10,
        ma20_below_run=run20,
        ma5_upward_inflection=ma5_inflection,
        ma10_upward_inflection=ma10_inflection,
        ma10_breakout=ma10_breakout,
        ma20_breakout=ma20_breakout,
        ma10_breakdown=ma10_breakdown,
    )


def test_ma_calculation_slope_and_ma120_alignment() -> None:
    points = calculate_daily_ma_points(tuple(_bar(index) for index in range(130)))
    assert points[4].ma5 == d(102)
    assert points[9].ma10 == d("104.5")
    assert points[119].ma120 == d("159.5")
    assert points[20].ma10_slope10_pct == (d("115.5") / d("105.5") - 1) * 100
    assert slope_state(d(3)) == "FLAT"
    assert slope_state(d("3.01")) == "UP"
    assert slope_state(d("-3.01")) == "DOWN"


def test_below_run_breakout_breakdown_and_inflection_primitives() -> None:
    assert consecutive_below_runs((d(99), d(98), d(101), d(99)), (d(100),) * 4) == (
        1,
        2,
        0,
        1,
    )
    assert confirmed_breakout(d(102), d(100), d("101.99"), d(100))
    assert not confirmed_breakout(d(102), d(100), d(102), d(100))
    assert confirmed_breakdown(d(98), d(100), d("98.01"), d(100))
    assert not confirmed_breakdown(d(98), d(100), d(98), d(100))
    assert upward_inflection((d(10), d(9), d(9), d(10)), 3)
    assert not upward_inflection((d(10), d(9), d(9)), 2)


def test_buy_a_and_c_require_inflection_before_breakout_within_below_run() -> None:
    points_a = [_point(index, run10=index + 1) for index in range(11)]
    points_a[9] = _point(9, ma5_inflection=True, run10=10)
    points_a.append(_point(11, ma10_breakout=True))
    result_a = detect_buy_candidates("005930", points_a)
    assert [candidate.buy_type for candidate in result_a] == [BuyType.A]
    assert result_a[0].ma5_inflection_date == points_a[9].trade_date
    assert result_a[0].elapsed_bars_inflection_to_breakout == 2

    points_c = [_point(index, run20=index + 1) for index in range(16)]
    points_c[13] = _point(13, ma10_inflection=True, run20=14)
    points_c.append(_point(16, ma20_breakout=True))
    result_c = detect_buy_candidates("005930", points_c)
    assert [candidate.buy_type for candidate in result_c] == [BuyType.C]
    assert result_c[0].ma10_inflection_date == points_c[13].trade_date


def test_buy_b_inclusive_one_to_ten_completed_bar_boundary() -> None:
    one = [_point(0, ma10_breakdown=True), _point(1, ma10_breakout=True)]
    ten = [
        _point(0, ma10_breakdown=True),
        *(_point(index) for index in range(1, 10)),
        _point(10, ma10_breakout=True),
    ]
    eleven = [
        _point(0, ma10_breakdown=True),
        *(_point(index) for index in range(1, 11)),
        _point(11, ma10_breakout=True),
    ]
    assert detect_buy_candidates("005930", one)[0].bars_since_breakdown == 1
    assert detect_buy_candidates("005930", ten)[0].bars_since_breakdown == 10
    assert detect_buy_candidates("005930", eleven) == ()


def test_overlap_is_preserved_and_slope_reject_is_not_deleted() -> None:
    points = [_point(index, run10=index + 1, run20=index + 1) for index in range(16)]
    points[1] = _point(1, ma10_breakdown=True, run10=2, run20=2)
    points[13] = _point(
        13, ma5_inflection=True, ma10_inflection=True, run10=14, run20=14
    )
    points.append(
        _point(
            16,
            ma10_breakout=True,
            ma20_breakout=True,
            slope="-21",
        )
    )
    candidates = detect_buy_candidates("005930", points)
    assert {candidate.buy_type for candidate in candidates} == {BuyType.A, BuyType.C}
    assert all(not candidate.restriction_pass for candidate in candidates)
    windows = select_signal_windows(candidates)
    assert len(windows) == 1
    assert [candidate.buy_type for candidate in windows[0][2]] == [BuyType.A, BuyType.C]


def test_input_order_is_deterministic_and_metadata_has_no_outcomes() -> None:
    points = [_point(index, run10=index + 1) for index in range(11)]
    points[9] = _point(9, ma5_inflection=True, run10=10)
    points.append(_point(11, ma10_breakout=True))
    first = detect_buy_candidates("005930", points)
    second = detect_buy_candidates("005930", tuple(reversed(points)))
    assert first == second
    summary = _summary(first)
    serialized = str(summary).casefold()
    for field in ("future_return", "pnl", "win", "loss", "mfe", "mae"):
        assert field not in serialized
    assert summary["orders_created"] is False
    assert summary["network_calls"] == 0
    assert_no_future_outcome_fields(summary)
    with pytest.raises(ValueError, match="future-outcome"):
        assert_no_future_outcome_fields({"pnl": 1})


def test_renderer_can_include_ma120_without_changing_daily_bar_coordinates() -> None:
    bars = tuple(_bar(index) for index in range(130))
    normal = prepare_review_chart(
        bars, chart_type=ChartType.EVENT_REVIEW, focus_date=bars[120].trade_date
    )
    with_ma120 = prepare_review_chart(
        bars,
        chart_type=ChartType.EVENT_REVIEW,
        focus_date=bars[120].trade_date,
        show_sma120=True,
    )
    assert with_ma120.show_sma120 is True
    assert with_ma120.sma120[-1] is not None
    assert normal.window == with_ma120.window
