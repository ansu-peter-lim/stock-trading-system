from decimal import Decimal

from src.kiwoom_daily.market_bar_pilot_acquisition import (
    _materialize,
    _raw_dates,
)


def _segment(source_id: str, start: str, end: str, value: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "source_resolution": "TEST_SOURCE",
        "source_tau_start": start,
        "source_tau_end": end,
        "calendar_start_datetime": "2026-01-02T09:00:00+09:00",
        "calendar_end_datetime": "2026-01-02T15:30:00+09:00",
        "open": value,
        "high": value,
        "low": value,
        "close": value,
        "volume": "10",
    }


def test_materialize_uses_integer_targets_without_synthetic_prices():
    result = _materialize(
        [
            _segment("a", "0", "0.6", "100"),
            _segment("b", "0.6", "1.0", "101"),
            _segment("c", "1.0", "1.4", "102"),
            _segment("d", "1.4", "2.0", "103"),
        ],
        "000001",
    )
    assert result["island_count"] == 1
    assert len(result["market_bars"]) == 2
    assert result["skip_count"] == 0
    assert result["duplicate_target_count"] == 0
    assert result["unresolved_source_count"] == 0
    assert result["source_ohlc_exact"] is True
    assert result["source_volume_exact"] is True


def test_raw_dates_are_normalized_to_iso_dates(tmp_path):
    path = tmp_path / "page.json"
    path.write_text(
        '{"stk_min_pole_chart_qry": [{"cntr_tm": "20260422090000"}]}',
        encoding="utf-8",
    )
    assert _raw_dates([str(path)]) == {"2026-04-22"}


def test_materialize_reports_decimal_tau_quality():
    result = _materialize([_segment("a", "0", "1", "100")], "000001")
    assert result["quality"]["total_source_tau"] == "1"
    assert result["quality"]["tau_length"]["count"] == 1
    assert isinstance(result["quality"]["tau_length"]["median"], Decimal)
