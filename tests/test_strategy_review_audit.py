from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.indicators import DailyIndicatorPoint
from src.strategy_review.audit import (
    CrossPoint,
    CrossType,
    calculate_ma10_direction,
    cross_review_events,
    detect_ma10_ma20_crosses,
    down_h1_metrics,
    recent_cross_metrics,
    summarize_down,
)


def _points(
    count: int,
    *,
    sma10: list[Decimal | None] | None = None,
    sma20: list[Decimal | None] | None = None,
) -> tuple[DailyIndicatorPoint, ...]:
    values10 = sma10 or [Decimal(100)] * count
    values20 = sma20 or [Decimal(100)] * count
    return tuple(
        DailyIndicatorPoint(
            "005930",
            date(2026, 1, 1) + timedelta(days=index),
            Decimal(1),
            values10[index],
            values20[index],
            Decimal(100),
            Decimal(1),
            Decimal(1),
        )
        for index in range(count)
    )


def test_golden_cross_detection() -> None:
    points = _points(3, sma10=[Decimal(100), Decimal(100), Decimal(101)])
    assert detect_ma10_ma20_crosses(points)[0].cross_type is CrossType.GOLDEN


def test_death_cross_detection() -> None:
    points = _points(3, sma10=[Decimal(100), Decimal(100), Decimal(99)])
    assert detect_ma10_ma20_crosses(points)[0].cross_type is CrossType.DEATH


def test_cross_equality_boundaries_are_inclusive_on_previous_value() -> None:
    golden = _points(2, sma10=[Decimal(100), Decimal(101)])
    death = _points(2, sma10=[Decimal(100), Decimal(99)])
    assert detect_ma10_ma20_crosses(golden)[0].cross_type is CrossType.GOLDEN
    assert detect_ma10_ma20_crosses(death)[0].cross_type is CrossType.DEATH


def test_recent_cross_includes_t_but_excludes_t_minus_10() -> None:
    points = _points(12, sma10=[Decimal(100)] * 12)
    crosses = [
        CrossPoint(points[1].trade_date, 1, CrossType.GOLDEN, Decimal(101)),
        CrossPoint(points[11].trade_date, 11, CrossType.DEATH, Decimal(99)),
    ]
    metrics = recent_cross_metrics(points, crosses, 11)
    assert metrics["recent_cross_10d"] is True
    assert metrics["recent_cross_count_10d"] == 1
    assert metrics["most_recent_cross_type"] == "DEATH"
    assert metrics["sessions_since_cross"] == 0


def test_recent_cross_or_semantics_and_no_cross() -> None:
    points = _points(10)
    golden = CrossPoint(points[5].trade_date, 5, CrossType.GOLDEN, Decimal(101))
    death = CrossPoint(points[6].trade_date, 6, CrossType.DEATH, Decimal(99))
    assert recent_cross_metrics(points, [golden], 7)["recent_cross_10d"]
    assert recent_cross_metrics(points, [death], 7)["recent_cross_10d"]
    assert not recent_cross_metrics(points, [], 7)["recent_cross_10d"]


def test_most_recent_cross_is_deterministic() -> None:
    points = _points(10)
    crosses = [
        CrossPoint(points[3].trade_date, 3, CrossType.GOLDEN, Decimal(101)),
        CrossPoint(points[8].trade_date, 8, CrossType.DEATH, Decimal(99)),
    ]
    metrics = recent_cross_metrics(points, list(reversed(crosses)), 9)
    assert metrics["most_recent_cross_date"] == points[8].trade_date
    assert metrics["most_recent_cross_type"] == "DEATH"


def test_ma10_current_and_1_3_5_session_changes() -> None:
    values = [Decimal(100)] * 6
    values[0] = Decimal(90)
    values[1] = Decimal(100)
    values[3] = Decimal(110)
    values[5] = Decimal(120)
    result = calculate_ma10_direction(_points(6, sma10=values))[5]
    assert result["ma10_current"] == Decimal(120)
    assert result["ma10_change_1"] == Decimal(20)
    assert result["ma10_slope_3"] == Decimal(20)
    assert result["ma10_slope_5"] == Decimal("33.33333333333333333333333330")


def test_ma10_direction_data_shortage_is_none() -> None:
    result = calculate_ma10_direction(_points(3))[0]
    assert result["ma10_current"] == Decimal(100)
    assert result["ma10_change_1"] is None
    assert result["ma10_slope_3"] is None
    assert result["ma10_slope_5"] is None


def test_down_h1_deceleration_pass() -> None:
    values = [Decimal(100)] * 11
    values[3], values[6], values[9] = Decimal(100), Decimal(94), Decimal("89.3")
    result = down_h1_metrics(_points(11, sma10=values), 10)
    assert result["deceleration_status"] == "PASS"
    assert result["prior_slope_3"] < result["recent_slope_3"] < 0


def test_down_h1_accelerating_decline_fails() -> None:
    values = [Decimal(100)] * 11
    values[3], values[6], values[9] = Decimal(100), Decimal(97), Decimal("90.21")
    assert (
        down_h1_metrics(_points(11, sma10=values), 10)["deceleration_status"] == "FAIL"
    )


def test_down_h1_flat_or_positive_recent_slope_fails() -> None:
    flat = [Decimal(100)] * 11
    flat[6], flat[9] = Decimal(94), Decimal(94)
    positive = [Decimal(100)] * 11
    positive[6], positive[9] = Decimal(94), Decimal(95)
    assert down_h1_metrics(_points(11, sma10=flat), 10)["deceleration_status"] == "FAIL"
    assert (
        down_h1_metrics(_points(11, sma10=positive), 10)["deceleration_status"]
        == "FAIL"
    )


def test_down_h1_uses_no_future_t_value() -> None:
    values = [Decimal(100)] * 11
    values[3], values[6], values[9], values[10] = (
        Decimal(100),
        Decimal(94),
        Decimal("89.3"),
        Decimal(1000),
    )
    points = _points(11, sma10=values)
    first = down_h1_metrics(points, 10)
    values[10] = Decimal(1)
    second = down_h1_metrics(_points(11, sma10=values), 10)
    assert first == second


def test_down_h1_short_history_is_insufficient() -> None:
    assert down_h1_metrics(_points(7), 6)["deceleration_status"] == "INSUFFICIENT_DATA"


def test_cross_review_event_mapping_uses_signal_price() -> None:
    point = CrossPoint(date(2026, 1, 3), 2, CrossType.GOLDEN, Decimal(105))
    event = cross_review_events([point])[0]
    assert event.event_type.value == "GOLDEN_CROSS"
    assert event.label == "GC"
    assert event.adjusted_plot_price == Decimal(105)


def test_down_summary_separates_pass_and_fail() -> None:
    rows = [
        {
            "deceleration_status": "PASS",
            "candidate": True,
            "actual_trade": True,
            "block_reasons": [],
            "rise_branch": "RISE_FIVE_TO_TEN_INCLUSIVE",
            "pnl_pct": Decimal(2),
        },
        {
            "deceleration_status": "FAIL",
            "candidate": False,
            "actual_trade": False,
            "block_reasons": ["STEEP"],
            "rise_branch": "RISE_BELOW_FIVE",
            "pnl_pct": None,
        },
    ]
    result = summarize_down(rows)
    assert result["DECELERATION_PASS"]["actual_trade_count"] == 1
    assert result["DECELERATION_FAIL"]["blocked_count"] == 1
