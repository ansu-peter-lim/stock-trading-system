from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.indicators import PivotKind
from src.backtest_engine.models import DailyBar, Ohlcv
from src.kiwoom_daily.market_clock_reaction_audit import (
    _decorate_pivot_rows,
    _direction_efficiency_group_rows,
    _reaction_signatures,
    _role_period_summary,
    _signed_distance,
)


def _bars(count: int = 5) -> tuple[DailyBar, ...]:
    result: list[DailyBar] = []
    for index in range(count):
        close = Decimal(100 + index)
        ohlcv = Ohlcv(close - 1, close + 2, close - 2, close, 100)
        result.append(
            DailyBar("005930", date(2024, 1, 1) + timedelta(days=index), ohlcv, ohlcv)
        )
    return tuple(result)


def test_signed_distance_preserves_direction() -> None:
    assert _signed_distance(Decimal(98), Decimal(100), Decimal(2)) == Decimal(-1)
    assert _signed_distance(Decimal(102), Decimal(100), Decimal(2)) == Decimal(1)
    assert _signed_distance(Decimal(100), None, Decimal(2)) is None


def test_low_and_high_reaction_labels_are_parameter_free() -> None:
    low = _reaction_signatures(
        kind=PivotKind.LOW,
        low=Decimal(99),
        high=Decimal(103),
        close=Decimal(101),
        ma=Decimal(100),
    )
    assert low["wick_reclaim"] is True
    assert low["full_hold_above"] is False
    assert low["close_below"] is False
    high = _reaction_signatures(
        kind=PivotKind.HIGH,
        low=Decimal(97),
        high=Decimal(101),
        close=Decimal(99),
        ma=Decimal(100),
    )
    assert high["wick_rejection"] is True
    assert high["full_hold_below"] is False
    assert high["close_above"] is False


def test_decorated_pivot_has_second_nearest_and_p2_follow_through() -> None:
    bars = _bars()
    rows = _decorate_pivot_rows(
        bars,
        [
            {
                "stock_code": "005930",
                "pivot_kind": "LOW",
                "pivot_trade_date": bars[1].trade_date,
                "confirmed_at": "2024-01-03T15:30:00+09:00",
                "pivot_price": Decimal(98),
                "atr20": Decimal(2),
                "sma5": Decimal(100),
                "sma10": Decimal(101),
                "sma20": Decimal(104),
                "sma60": Decimal(110),
                "low_to_ma5_atr": Decimal(1),
                "low_to_ma10_atr": Decimal(2),
                "low_to_ma20_atr": Decimal(3),
                "low_to_ma60_atr": Decimal(4),
                "high_to_ma5_atr": Decimal(1),
                "high_to_ma10_atr": Decimal(2),
                "high_to_ma20_atr": Decimal(3),
                "high_to_ma60_atr": Decimal(4),
                "ma5_10_gap_atr": Decimal(-1),
                "ma10_20_gap_atr": Decimal(-2),
                "ma20_60_gap_atr": Decimal(-3),
            }
        ],
    )
    row = rows[0]
    assert row["nearest_ma"] == "MA5"
    assert row["second_nearest_ma"] == "MA10"
    assert row["nearest_margin_atr"] == Decimal(1)
    assert row["low_signed_dist_ma5_atr"] == Decimal("-0.5")
    assert row["follow_through_p2_pct"] == (
        bars[3].signal.close / bars[1].signal.close - Decimal(1)
    ) * Decimal(100)
    assert row["low_wick_reclaim_ma5"] is True


def test_direction_efficiency_groups_are_disjoint_and_period_summary_works() -> None:
    base = {
        "nearest_ma": "MA10",
        "range_speed_quartile": "Q4",
        "efficiency_10_quartile": "Q4",
        "net_move_atr_10": Decimal(1),
    }
    negative = {**base, "net_move_atr_10": Decimal(-1)}
    slow = {**base, "range_speed_quartile": "Q1", "nearest_ma": "MA20"}
    groups = _direction_efficiency_group_rows([base, negative, slow])
    assert groups["FAST_POSITIVE_HIGH_EFFICIENCY"] == [base]
    assert groups["FAST_NEGATIVE_HIGH_EFFICIENCY"] == [negative]
    assert groups["SLOW"] == [slow]
    summary = _role_period_summary([base, negative, slow])
    assert summary["ALL"]["median_nearest_period"] == Decimal(10)
    assert summary["FAST_POSITIVE_HIGH_EFFICIENCY"]["mean_nearest_period"] == Decimal(
        10
    )
