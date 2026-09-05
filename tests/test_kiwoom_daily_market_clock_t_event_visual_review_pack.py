from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from src.backtest_engine.models import DailyBar, Ohlcv
from src.kiwoom_daily.market_clock_t_event_visual_review_pack import (
    FAILED,
    GOOD,
    _blind_metadata,
    _case_observation,
    _load_records,
    select_matched_cases,
)
from src.strategy_review.chart import ChartType, prepare_review_chart


def _record(
    code: str,
    day: int,
    *,
    outcome: str,
    direction: int = 1,
    offset: int = 0,
) -> dict[str, object]:
    return {
        "stock_code": code,
        "event_date": date(2024, 1, day),
        "direction": direction,
        "direction_label": "UP" if direction > 0 else "DOWN",
        "outcome_label": outcome,
        "compression_quartile": "C1",
        "range_speed_t": Decimal(10 + offset),
        "ma_cluster_width_atr_t": Decimal(1) + Decimal(offset) / Decimal(100),
        "breakout_clearance_atr": Decimal(1) + Decimal(offset) / Decimal(100),
        "event_body_atr": Decimal(1) + Decimal(offset) / Decimal(100),
    }


def _bars() -> tuple[DailyBar, ...]:
    first = DailyBar(
        "005930",
        date(2024, 1, 1),
        Ohlcv(Decimal(99), Decimal(102), Decimal(98), Decimal(100), 1),
        Ohlcv(Decimal(99), Decimal(102), Decimal(98), Decimal(100), 1),
    )
    second = DailyBar(
        "005930",
        date(2024, 1, 2),
        Ohlcv(Decimal(100), Decimal(103), Decimal(99), Decimal(101), 1),
        Ohlcv(Decimal(100), Decimal(103), Decimal(99), Decimal(101), 1),
    )
    return first, second


def test_matched_selection_is_same_direction_deterministic_and_interleaved() -> None:
    rows = [
        _record("000001", 1, outcome=GOOD, offset=0),
        _record("000002", 2, outcome=FAILED, offset=1),
        _record("000003", 3, outcome=GOOD, offset=10),
        _record("000004", 4, outcome=FAILED, offset=11),
        _record("000005", 5, outcome=GOOD, direction=-1, offset=20),
        _record("000006", 6, outcome=FAILED, direction=-1, offset=21),
    ]
    first = select_matched_cases(rows)
    second = select_matched_cases(list(reversed(rows)))
    assert first == second
    assert [case["record"]["outcome_label"] for case in first] == [
        GOOD,
        FAILED,
        GOOD,
        FAILED,
        GOOD,
        FAILED,
    ]
    for index in range(0, 6, 2):
        assert (
            first[index]["record"]["direction"]
            == first[index + 1]["record"]["direction"]
        )


def test_case_observation_captures_t_and_t_minus_five_gap_changes() -> None:
    record = _record("005930", 6, outcome=GOOD)
    case = {
        "case_id": "CASE_01",
        "matching_distance": Decimal(0),
        "selection_reason": "test",
        "record": record,
    }
    state_now = {
        "stock_code": "005930",
        "trade_date": date(2024, 1, 6),
        "_index": 10,
        "sma5": Decimal(105),
        "sma10": Decimal(100),
        "sma20": Decimal(95),
        "sma60": Decimal(90),
        "ma5_10_gap_atr": Decimal(1),
        "ma10_20_gap_atr": Decimal(2),
        "ma20_60_gap_atr": Decimal(3),
    }
    state_prev = {
        **state_now,
        "_index": 5,
        "ma5_10_gap_atr": Decimal("0.5"),
        "ma10_20_gap_atr": Decimal(1),
        "ma20_60_gap_atr": Decimal(4),
    }
    observation = _case_observation(
        case,
        {("005930", 10): state_now, ("005930", 5): state_prev},
        {("005930", date(2024, 1, 6)): state_now},
    )
    assert observation["ma5_10_gap_atr_delta_5"] == Decimal("0.5")
    assert observation["ma10_20_gap_atr_delta_5"] == Decimal(1)
    assert observation["ma20_60_gap_atr_delta_5"] == Decimal(-1)


def test_blind_metadata_masks_identity_outcome_and_future_return() -> None:
    bars = _bars()
    prepared = prepare_review_chart(bars, chart_type=ChartType.STOCK_OVERVIEW)
    case = {
        "case_id": "CASE_01",
        "record": {
            **_record("005930", 1, outcome=GOOD),
            "range_delta_3": Decimal(1),
            "efficiency_10_t": Decimal(1),
            "eff_delta_3": Decimal(1),
            "width_delta_3": Decimal(1),
            "flow_speed_t": Decimal(1),
            "flow_delta_3": Decimal(1),
        },
    }
    encoded = json.dumps(
        _blind_metadata(case, prepared, "test"), default=str
    ).casefold()
    assert "005930" not in encoded
    assert "good" not in encoded
    assert "failed" not in encoded
    assert "aligned_return" not in encoded


def test_record_loader_restores_cluster_levels_as_decimal(tmp_path) -> None:
    path = tmp_path / "v03.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "stock_code": "005930",
                        "event_date": "2024-01-02",
                        "ma_cluster_high": "101.5",
                        "ma_cluster_low": "99.5",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    record = _load_records(path)[0]
    assert record["ma_cluster_high"] == Decimal("101.5")
    assert record["ma_cluster_low"] == Decimal("99.5")
