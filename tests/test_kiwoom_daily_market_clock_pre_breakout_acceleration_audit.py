from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.backtest_engine.models import DailyBar, Ohlcv
from src.kiwoom_daily.market_clock_pre_breakout_acceleration_audit import (
    OUTCOME_FAILED,
    OUTCOME_GOOD,
    OUTCOME_UNAVAILABLE,
    _chart_metadata_complete,
    _decorate_events,
    _event_bar_metrics,
    _metric_features,
    _outcome_label,
    _quartile_evidence,
)


def _bar(day: int, *, open_: int, high: int, low: int, close: int) -> DailyBar:
    ohlcv = Ohlcv(Decimal(open_), Decimal(high), Decimal(low), Decimal(close), 100)
    return DailyBar("005930", date(2024, 1, day), ohlcv, ohlcv)


def test_event_time_features_do_not_read_future_rows() -> None:
    rows = {index: {"range_speed": Decimal(index)} for index in range(1, 8)}
    baseline = _metric_features(rows, 5, "range_speed", "range_speed")
    rows[6]["range_speed"] = Decimal(999)
    rows[7]["range_speed"] = Decimal(999)
    assert _metric_features(rows, 5, "range_speed", "range_speed") == baseline
    assert baseline["range_speed_delta_3"] == Decimal(3)
    assert baseline["range_speed_ratio_pre3"] == Decimal(5) / Decimal(3)


def test_up_and_down_event_bar_metrics_are_direction_aligned() -> None:
    previous = _bar(1, open_=100, high=101, low=99, close=100)
    up = _bar(2, open_=101, high=112, low=100, close=110)
    down = _bar(3, open_=109, high=110, low=98, close=100)
    up_metrics = _event_bar_metrics(
        bar=up,
        previous_bar=previous,
        row={
            "atr20": Decimal(10),
            "ma_band_high": Decimal(105),
            "ma_band_low": Decimal(95),
        },
        direction=1,
    )
    down_metrics = _event_bar_metrics(
        bar=down,
        previous_bar=up,
        row={
            "atr20": Decimal(10),
            "ma_band_high": Decimal(105),
            "ma_band_low": Decimal(105),
        },
        direction=-1,
    )
    assert up_metrics["breakout_clearance_atr"] == Decimal("0.5")
    assert up_metrics["event_body_atr"] == Decimal("0.9")
    assert down_metrics["breakout_clearance_atr"] == Decimal("0.5")
    assert down_metrics["event_body_atr"] == Decimal("0.9")
    assert up_metrics["event_return_pct"] == Decimal(10)
    assert down_metrics["event_return_pct"] == (
        Decimal(-1) * (Decimal(100) / Decimal(110) - Decimal(1)) * Decimal(100)
    )


def test_outcome_label_is_evaluation_only_and_handles_horizon() -> None:
    assert _outcome_label({"aligned_return_10_pct": Decimal("0.01")}) == OUTCOME_GOOD
    assert _outcome_label({"aligned_return_10_pct": Decimal(0)}) == OUTCOME_FAILED
    assert _outcome_label({"aligned_return_10_pct": None}) == OUTCOME_UNAVAILABLE


def test_quartile_evidence_is_deterministic_under_input_permutation() -> None:
    rows = [
        {
            "range_delta_3": Decimal(index),
            "outcome_label": OUTCOME_GOOD if index % 2 else OUTCOME_FAILED,
            "aligned_return_10_pct": Decimal(index),
        }
        for index in range(1, 9)
    ]
    assert _quartile_evidence(rows, "range_delta_3") == _quartile_evidence(
        list(reversed(rows)), "range_delta_3"
    )


def test_decorated_band_event_keeps_future_return_out_of_features() -> None:
    bars = tuple(
        _bar(index + 1, open_=100, high=102, low=98, close=100 + index)
        for index in range(6)
    )
    rows = []
    for index, bar in enumerate(bars):
        rows.append(
            {
                "stock_code": "005930",
                "trade_date": bar.trade_date,
                "_index": index,
                "range_speed": Decimal(index + 1),
                "efficiency_10": Decimal(index + 1),
                "flow_speed": Decimal(index + 1),
                "atr20": Decimal(2),
                "sma5": Decimal(100),
                "sma10": Decimal(100),
                "sma20": Decimal(100),
                "ma_band_high": Decimal(100),
                "ma_band_low": Decimal(100),
                "ma_cluster_width_atr": Decimal(1),
                "compression_quartile": "C1",
                "range_speed_quartile": "Q1",
                "compression_duration_sessions": index + 1,
            }
        )
    report = {
        "rows": rows,
        "events": [
            {
                "event_type": "BAND_EXIT",
                "stock_code": "005930",
                "event_date": bars[5].trade_date,
                "direction": 1,
                "aligned_return_3_pct": Decimal(3),
                "aligned_return_5_pct": Decimal(5),
                "aligned_return_10_pct": Decimal(10),
            }
        ],
    }
    record = _decorate_events(report, {"005930": bars})[0]
    assert record["range_delta_3"] == Decimal(3)
    assert record["outcome_label"] == OUTCOME_GOOD
    assert not any("plus" in key for key in record)


def test_chart_selection_requires_complete_event_time_metadata() -> None:
    base = {
        "ma_cluster_width_atr_t": Decimal(1),
        "range_speed_t": Decimal(1),
        "range_delta_3": Decimal(1),
        "efficiency_10_t": Decimal(1),
        "eff_delta_3": Decimal(1),
        "width_delta_3": Decimal(1),
        "flow_delta_3": Decimal(1),
        "breakout_clearance_atr": Decimal(1),
    }
    assert _chart_metadata_complete(base) is True
    assert _chart_metadata_complete({**base, "flow_delta_3": None}) is False
