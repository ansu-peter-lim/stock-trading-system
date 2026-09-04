from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from src.backtest_engine.core_strategy import (
    DailyCoreSignal,
    DailyCoreSignalType,
    DailyTrendState,
)
from src.backtest_engine.execution import OrderSide
from src.backtest_engine.indicators import DailyIndicatorPoint
from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.validation import KOREA_TZ
from src.kiwoom_minute.proof import UpEntryPolicy, _apply_entry_policy
from src.strategy_review.h1_ab import _candidate_funnel, _metric_comparison


def _signal() -> DailyCoreSignal:
    at = datetime(2026, 1, 5, 15, 30, tzinfo=KOREA_TZ)
    return DailyCoreSignal(
        "signal-1",
        "005930",
        DailyCoreSignalType.ENTER,
        OrderSide.BUY,
        at,
        at,
        date(2026, 1, 5),
        date(2026, 1, 6),
        Decimal("0.10"),
        Decimal("0.09"),
        DailyTrendState.UP,
        "fixture",
        Decimal(97),
        Decimal(100),
        Decimal(100),
    )


def _bar() -> DailyBar:
    value = Ohlcv(Decimal(100), Decimal(105), Decimal(97), Decimal(100), 100)
    return DailyBar("005930", date(2026, 1, 5), value, value)


def _point() -> DailyIndicatorPoint:
    return DailyIndicatorPoint(
        "005930",
        date(2026, 1, 5),
        Decimal(1),
        Decimal(100),
        Decimal(100),
        Decimal(100),
        Decimal(1),
        Decimal(1),
    )


def test_h1_three_session_policy_blocks_only_negative_slope() -> None:
    for slope in (Decimal(0), Decimal("1.25")):
        assert (
            _apply_entry_policy(
                _signal(),
                _bar(),
                _point(),
                UpEntryPolicy.LOW_REQUIRED_MA10_3D_NON_DOWN,
                Decimal(3),
                slope,
            )
            is not None
        )
    assert (
        _apply_entry_policy(
            _signal(),
            _bar(),
            _point(),
            UpEntryPolicy.LOW_REQUIRED_MA10_3D_NON_DOWN,
            Decimal(3),
            Decimal("-0.01"),
        )
        is None
    )


def test_h1_three_session_policy_requires_known_slope() -> None:
    assert (
        _apply_entry_policy(
            _signal(),
            _bar(),
            _point(),
            UpEntryPolicy.LOW_REQUIRED_MA10_3D_NON_DOWN,
            Decimal(3),
            None,
        )
        is None
    )


def test_metric_comparison_is_deterministic_and_directional() -> None:
    a = {
        "cumulative_return_pct": Decimal(1),
        "mdd_pct": Decimal(-5),
        "win_rate_pct": Decimal(30),
        "profit_factor": Decimal(1),
    }
    b = {
        "cumulative_return_pct": Decimal(2),
        "mdd_pct": Decimal(-4),
        "win_rate_pct": Decimal(35),
        "profit_factor": Decimal("1.5"),
    }
    result = _metric_comparison(a, b)
    assert result["overall"] == "BETTER"
    assert result["mdd_pct"]["direction"] == "BETTER"


def test_candidate_funnel_separates_three_session_slope_states() -> None:
    result = _candidate_funnel(
        {
            "candidates": [
                {"entry_classification": "LOW_ONLY", "ma10_slope_3": Decimal(-1)},
                {"entry_classification": "LOW_AND_CLOSE", "ma10_slope_3": Decimal(0)},
                {"entry_classification": "LOW_ONLY", "ma10_slope_3": None},
                {"entry_classification": "CLOSE_ONLY", "ma10_slope_3": Decimal(-2)},
            ]
        }
    )
    assert result["low_required_candidates"] == 3
    assert result["ma10_slope_3_distribution"] == {
        "DOWN": 1,
        "INSUFFICIENT": 1,
        "NON_DOWN": 1,
    }
