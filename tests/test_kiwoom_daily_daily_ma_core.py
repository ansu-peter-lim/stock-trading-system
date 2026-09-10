from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.kiwoom_daily.daily_ma.core import (
    confirmed_breakdown,
    confirmed_breakout,
    consecutive_below_ma_runs,
    ma_percentage_change,
    moving_averages,
    structural_upward_inflection_events,
    structural_upward_inflection_state,
)
from src.kiwoom_daily.daily_ma.data import load_local_daily_bars
from src.kiwoom_daily.daily_ma_unified_buy_visual_proof_v0_1 import (
    calculate_daily_ma_points,
)

STOCKS = (
    "000660",
    "005380",
    "005930",
    "012450",
    "034020",
    "035420",
    "035720",
    "066570",
    "068270",
    "105560",
    "207940",
)


def d(value: str | int) -> Decimal:
    return Decimal(str(value))


def test_ma_slope_and_warmup() -> None:
    values = tuple(d(value) for value in range(1, 21))
    ma = moving_averages(values)[5]
    assert ma[3] is None and ma[4] == d(3)
    assert ma_percentage_change(ma, 9) is None
    assert ma_percentage_change(ma, 14) == (d(13) / d(3) - 1) * 100


def test_below_runs_and_confirmation_boundaries() -> None:
    assert consecutive_below_ma_runs((d(99), d(98), d(101)), (d(100),) * 3) == (1, 2, 0)
    assert confirmed_breakout(d(102), d(100), d("101.99"), d(100))
    assert not confirmed_breakout(d(102), d(100), d(102), d(100))
    assert confirmed_breakdown(d(98), d(100), d("98.01"), d(100))
    assert not confirmed_breakdown(d(98), d(100), d(98), d(100))


def test_structural_inflection_uses_recent_equal_low_and_event_transition() -> None:
    values = (d(10), d(9), d(9), d(10), d(11))
    assert not structural_upward_inflection_state(values, 2, lookback=4)
    assert structural_upward_inflection_state(values, 3, lookback=4)
    assert structural_upward_inflection_state(values, 4, lookback=4)
    assert structural_upward_inflection_events(values, lookback=4) == (
        False,
        False,
        False,
        True,
        False,
    )


def test_structural_inflection_rejects_higher_intermediate_and_incomplete_data() -> (
    None
):
    assert not structural_upward_inflection_state(
        (d(10), d(8), d(11), d(10)), 3, lookback=4
    )
    assert not structural_upward_inflection_state((d(9), d(10)), 1, lookback=5)
    assert not structural_upward_inflection_state(
        (d(9), None, d(9), d(10)), 3, lookback=4
    )


def test_structural_inflection_lookback_period_is_explicit() -> None:
    values = (d(12), d(11), d(10), d(10), d(11))
    assert structural_upward_inflection_state(values, 4, lookback=5)
    assert not structural_upward_inflection_state(values, 4, lookback=10)


@pytest.mark.parametrize("stock_code", STOCKS)
def test_core_matches_v0_1_stable_primitives(stock_code: str) -> None:
    bars = load_local_daily_bars(stock_code, date(2023, 6, 1), date(2026, 8, 31))
    old = calculate_daily_ma_points(bars)
    closes = tuple(bar.signal.close for bar in bars)
    ma = moving_averages(closes)
    assert tuple(point.ma5 for point in old) == ma[5]
    assert tuple(point.ma10 for point in old) == ma[10]
    assert tuple(point.ma20 for point in old) == ma[20]
    assert tuple(point.ma60 for point in old) == ma[60]
    assert tuple(point.ma120 for point in old) == ma[120]
    assert tuple(point.ma10_slope10_pct for point in old) == tuple(
        ma_percentage_change(ma[10], index) for index in range(len(bars))
    )
    assert tuple(point.ma10_below_run for point in old) == consecutive_below_ma_runs(
        closes, ma[10]
    )
    assert tuple(point.ma20_below_run for point in old) == consecutive_below_ma_runs(
        closes, ma[20]
    )
    for index, point in enumerate(old):
        previous_close = closes[index - 1] if index else None
        previous_ma10 = ma[10][index - 1] if index else None
        previous_ma20 = ma[20][index - 1] if index else None
        assert point.ma10_breakout == confirmed_breakout(
            closes[index], ma[10][index], previous_close, previous_ma10
        )
        assert point.ma20_breakout == confirmed_breakout(
            closes[index], ma[20][index], previous_close, previous_ma20
        )
        assert point.ma10_breakdown == confirmed_breakdown(
            closes[index], ma[10][index], previous_close, previous_ma10
        )
