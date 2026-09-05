from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.backtest_engine.models import DailyBar, Ohlcv
from src.kiwoom_daily.market_clock_market_phase_audit import (
    _descriptive_phase_groups,
    _matrix,
    _phase_record,
    _quartile_evidence,
    _window_phase_metrics,
)


def _bar(day: int, *, low: int, high: int, close: int) -> DailyBar:
    ohlcv = Ohlcv(Decimal(close), Decimal(high), Decimal(low), Decimal(close), 1)
    return DailyBar("005930", date(2024, 1, day), ohlcv, ohlcv)


def test_directional_range_room_and_move_are_direction_aligned() -> None:
    bars = (_bar(1, low=90, high=110, close=100), _bar(2, low=80, high=120, close=110))
    up = _window_phase_metrics(bars, 1, 1, Decimal(10))
    down = _window_phase_metrics(bars, 1, -1, Decimal(10))
    assert up["dir_position_20"] == Decimal("0.75")
    assert down["dir_position_20"] == Decimal("0.25")
    assert up["directional_room_20_atr"] == Decimal(1)
    assert down["directional_room_20_atr"] == Decimal(3)
    assert up["directional_move_20_atr"] == Decimal(3)
    assert down["directional_move_20_atr"] == Decimal(1)


def test_most_recent_tied_extreme_is_used() -> None:
    bars = (
        _bar(1, low=90, high=110, close=100),
        _bar(2, low=80, high=115, close=100),
        _bar(3, low=80, high=120, close=110),
    )
    values = _window_phase_metrics(bars, 2, 1, Decimal(10))
    assert values["sessions_since_recent_extreme_20"] == 0


def test_phase_record_cannot_read_future_daily_prices() -> None:
    bars = tuple(
        _bar(day, low=90 + day, high=110 + day, close=100 + day) for day in range(1, 8)
    )
    rows = [
        {
            "trade_date": bar.trade_date,
            "_index": index,
            "atr20": Decimal(2),
            "sma20": Decimal(100 + index),
            "sma60": Decimal(100 + index),
        }
        for index, bar in enumerate(bars)
    ]
    record = {
        "stock_code": "005930",
        "event_date": bars[5].trade_date,
        "direction": 1,
        "outcome_label": "FAILED",
    }
    baseline = _phase_record(record, rows, bars)
    changed = list(bars)
    changed[6] = _bar(7, low=1, high=999, close=1)
    assert _phase_record(record, rows, changed) == baseline


def test_oppositely_sloped_ma_has_zero_same_direction_age() -> None:
    bars = tuple(
        _bar(day, low=90 + day, high=110 + day, close=100 + day) for day in range(1, 8)
    )
    rows = [
        {
            "trade_date": bar.trade_date,
            "_index": index,
            "atr20": Decimal(2),
            "sma20": Decimal(200 - index),
            "sma60": Decimal(200 - index),
        }
        for index, bar in enumerate(bars)
    ]
    record = _phase_record(
        {"stock_code": "005930", "event_date": bars[5].trade_date, "direction": 1},
        rows,
        bars,
    )
    assert record["ma20_same_direction_age"] == 0
    assert record["ma60_same_direction_age"] == 0


def test_quartiles_and_clock_phase_matrix_are_permutation_independent() -> None:
    rows = [
        {
            "directional_room_40_atr": Decimal(index),
            "range_delta_3": Decimal(index),
            "outcome_label": "GOOD_DIRECTIONAL" if index % 2 else "FAILED",
            "aligned_return_10_pct": Decimal(index),
        }
        for index in range(1, 9)
    ]
    assert _quartile_evidence(rows, "directional_room_40_atr") == _quartile_evidence(
        list(reversed(rows)), "directional_room_40_atr"
    )
    assert _matrix(rows, "range_delta_3", "directional_room_40_atr") == _matrix(
        list(reversed(rows)), "range_delta_3", "directional_room_40_atr"
    )


def test_descriptive_phase_groups_do_not_depend_on_input_order() -> None:
    rows = [
        {
            "directional_move_40_atr": Decimal(index),
            "outcome_label": "GOOD_DIRECTIONAL" if index % 2 else "FAILED",
        }
        for index in range(1, 9)
    ]
    assert _descriptive_phase_groups(rows) == _descriptive_phase_groups(
        list(reversed(rows))
    )
