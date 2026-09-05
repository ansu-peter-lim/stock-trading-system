from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.indicators import DailyIndicatorPoint
from src.kiwoom_daily.upper_transition_audit import (
    _sma20_structure,
    build_funnel,
    build_report,
    select_chart_rows,
)


def _point(index: int, sma20: Decimal) -> DailyIndicatorPoint:
    return DailyIndicatorPoint(
        stock_code="005930",
        trade_date=date(2024, 1, 1) + timedelta(days=index),
        daily_return=None,
        sma10=Decimal(100),
        sma20=sma20,
        sma60=Decimal(100),
        ma20_slope_5=None,
        ma60_slope_5=None,
    )


def _row(
    stock_code: str,
    setup_id: str,
    future20: Decimal | None,
    *,
    d1: bool = False,
    d2: bool = False,
    d3: bool = False,
    persistent: bool = False,
) -> dict[str, object]:
    return {
        "stock_code": stock_code,
        "setup_id": setup_id,
        "upper_exit_fill_date": date(2024, 1, 10),
        "future_20_session_return": future20,
        "sma5_d1_pass": d1,
        "sma5_d2_pass": d2,
        "sma5_d3_pass": d3,
        "holds_sma5_all_3": d1 and d2 and d3,
        "sma20_recent_up": persistent,
        "sma20_prior_up": persistent,
        "sma20_persistent_up": persistent,
        "structural_up_transition": persistent and d1 and d2 and d3,
    }


def test_sma20_structure_uses_recent_and_prior_windows() -> None:
    points = tuple(_point(index, Decimal(100 + index)) for index in range(11))
    recent, prior, persistent, recent_change, prior_change = _sma20_structure(
        points, 10
    )
    assert recent is True
    assert prior is True
    assert persistent is True
    assert recent_change == Decimal(110) / Decimal(105) - Decimal(1)
    assert prior_change == Decimal(105) / Decimal(100) - Decimal(1)


def test_sma20_structure_is_false_without_ten_session_lookback() -> None:
    points = tuple(_point(index, Decimal(100)) for index in range(10))
    assert _sma20_structure(points, 9) == (False, False, False, None, None)


def test_funnel_counts_are_report_only_and_deterministic() -> None:
    rows = (
        _row("005930", "a", Decimal("0.2"), d1=True, d2=True, d3=True, persistent=True),
        _row("000660", "b", Decimal("-0.1"), d1=True),
    )
    assert build_funnel(rows) == {
        "upper_exit_total": 2,
        "sma5_d1": 2,
        "sma5_d1_d2": 1,
        "sma5_all3": 1,
        "sma20_recent_up": 1,
        "sma20_prior_up": 1,
        "sma20_persistent_up": 1,
        "sma5_all3_and_sma20_persistent": 1,
    }


def test_top_bottom_and_anchor_selection_is_stable() -> None:
    rows = (
        _row("005930", "low", Decimal("-0.2")),
        _row("000660", "high", Decimal("0.3")),
        _row("012450", "anchor", Decimal("0.1")),
        _row("035420", "middle", None),
    )
    first = select_chart_rows(rows)
    second = select_chart_rows(tuple(reversed(rows)))
    assert first == second
    assert {category for category, _ in first} == {
        "TOP5_FUTURE20",
        "BOTTOM5_FUTURE20",
    }
    assert any(row["stock_code"] == "012450" for _, row in first)


def test_build_report_keeps_future_values_report_only() -> None:
    rows = (_row("005930", "a", Decimal("0.2")),)
    report = build_report(rows)
    assert report["network_calls"] == 0
    assert report["upper_exit_count"] == 1
    assert report["accounting_contract"]["upper_transition_future_returns"] == (
        "RESEARCH_ONLY_NO_PNL"
    )
