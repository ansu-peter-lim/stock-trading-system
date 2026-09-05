from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.models import DailyBar, Ohlcv
from src.kiwoom_daily.market_time_normalization_audit import (
    _cross,
    _prior_valid_median,
    _tau_weighted_mean,
    market_time_series,
)


def _bar(index: int, *, close: int, high: int, low: int) -> DailyBar:
    ohlcv = Ohlcv(Decimal(close), Decimal(high), Decimal(low), Decimal(close), 1)
    return DailyBar("005930", date(2023, 1, 1) + timedelta(days=index), ohlcv, ohlcv)


def test_reference_uses_exactly_252_prior_valid_values_and_excludes_current() -> None:
    values = [Decimal(index) for index in range(260)]
    assert _prior_valid_median(values, 252) == Decimal("125.5")
    assert _prior_valid_median(values, 253) == Decimal("126.5")


def test_tau_weighted_ma_uses_fractional_boundary_overlap_without_synthetic_bars() -> (
    None
):
    result = _tau_weighted_mean(
        [Decimal(10), Decimal(20)],
        [Decimal(1), Decimal(4)],
        [Decimal(1), Decimal(3)],
        1,
        2,
    )
    assert result["mtma"] == Decimal(20)
    assert result["calendar_equiv_sessions"] == Decimal(2) / Decimal(3)
    assert result["max_single_day_tau_share"] == Decimal(1)


def test_tau_weighted_ma_requires_sufficient_cumulative_tau_history() -> None:
    result = _tau_weighted_mean([Decimal(10)], [Decimal(1)], [Decimal(1)], 0, 5)
    assert result["mtma"] is None


def test_market_time_series_current_tr_does_not_affect_its_reference() -> None:
    bars = tuple(
        _bar(index, close=100 + index, high=102 + index, low=98 + index)
        for index in range(255)
    )
    baseline = market_time_series(bars)[254]
    changed = list(bars)
    changed[254] = _bar(254, close=354, high=1000, low=1)
    after = market_time_series(changed)[254]
    assert after["reference_tr"] == baseline["reference_tr"]
    assert after["delta_tau"] != baseline["delta_tau"]


def test_future_daily_bar_cannot_change_prior_tau_or_mtma() -> None:
    bars = tuple(
        _bar(index, close=100 + index, high=102 + index, low=98 + index)
        for index in range(256)
    )
    baseline = market_time_series(bars)[254]
    changed = list(bars)
    changed[255] = _bar(255, close=999, high=1000, low=1)
    after = market_time_series(changed)[254]
    assert after == baseline


def test_calendar_and_market_time_cross_semantics_are_exact() -> None:
    assert _cross(Decimal(101), Decimal(100), Decimal(100), Decimal(100)) == 1
    assert _cross(Decimal(99), Decimal(100), Decimal(100), Decimal(100)) == -1
    assert _cross(Decimal(100), Decimal(100), Decimal(100), Decimal(100)) is None
