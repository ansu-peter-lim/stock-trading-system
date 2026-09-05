import json
from decimal import Decimal

from src.kiwoom_daily.market_bar_mma_visual_proof import (
    V09_PATH,
    _market_rows,
    _mmas,
    _values_rows,
    run_proof,
)


def _bar(index: int) -> dict[str, object]:
    value = str(100 + index)
    return {
        "market_bar_id": f"000001:V06:{index:06d}",
        "open": value,
        "high": value,
        "low": value,
        "close": value,
        "volume": "1",
        "tau_length": "1",
        "boundary_error": "0",
        "calendar_start_datetime": "2026-01-02T09:00:00+09:00",
        "calendar_end_datetime": "2026-01-02T15:30:00+09:00",
        "calendar_start_date": "2026-01-02",
        "calendar_end_date": "2026-01-02",
        "source_segment_count": 1,
        "source_resolutions_used": ["DAILY_SIGNAL_ADJUSTED"],
    }


def test_mma_warmup_is_period_minus_one():
    result = _mmas([Decimal(index) for index in range(1, 81)])
    assert sum(value is not None for value in result[5]) == 76
    assert sum(value is not None for value in result[10]) == 71
    assert sum(value is not None for value in result[20]) == 61
    assert sum(value is not None for value in result[60]) == 21
    assert result[5][4] == Decimal(3)
    assert result[60][59] == Decimal("30.5")


def test_value_rows_keep_market_bar_index_and_calendar_metadata():
    rows = _market_rows(
        {"materialization": {"market_bars": [_bar(index) for index in range(3)]}}
    )
    result = _values_rows(rows)
    assert [row["market_bar_index"] for row in result] == [1, 2, 3]
    assert result[0]["calendar_start_datetime"].endswith("+09:00")
    assert result[0]["MMA5"] is None


def test_full_v10_proof_writes_three_charts_and_summary(tmp_path):
    summary = run_proof(input_path=V09_PATH, output_root=tmp_path)
    assert summary["pilot"]["market_bar_count"] == 80
    assert summary["mma_valid_counts"] == {
        "MMA5": 76,
        "MMA10": 71,
        "MMA20": 61,
        "MMA60": 21,
    }
    assert summary["market_bar_chart"]["x_axis_tick_indexes_zero_based"] == [
        0,
        9,
        19,
        29,
        39,
        49,
        59,
        69,
        79,
    ]
    assert summary["market_bar_chart"]["x_axis_tick_labels"] == [
        "1",
        "10",
        "20",
        "30",
        "40",
        "50",
        "60",
        "70",
        "80",
    ]
    assert (tmp_path / "CALENDAR_REFERENCE.png").exists()
    assert (tmp_path / "MARKET_BAR_MMA.png").exists()
    assert (tmp_path / "MARKET_BAR_MMA_MB50_80.png").exists()
    persisted = json.loads(
        (tmp_path / "market_bar_mma_visual_proof_v1_0.json").read_text(encoding="utf-8")
    )
    assert persisted["contract"]["strategy"] is False
