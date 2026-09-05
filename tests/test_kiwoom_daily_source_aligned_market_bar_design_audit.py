from src.kiwoom_daily.source_aligned_market_bar_design_audit import (
    _materialize_source_aligned,
    audit_proof,
)


def _source(source_id, start, end, open_, high, low, close, volume="10"):
    return {
        "source_id": source_id,
        "source_tau_start": str(start),
        "source_tau_end": str(end),
        "calendar_start_datetime": "2024-01-02T09:00:00+09:00",
        "calendar_end_datetime": "2024-01-02T15:30:00+09:00",
        "source_resolution": "DAILY_SIGNAL_ADJUSTED",
        "open": str(open_),
        "high": str(high),
        "low": str(low),
        "close": str(close),
        "volume": volume,
    }


def test_source_boundary_closes_with_overshoot_and_no_carry():
    run = [
        _source("000001:2024-01-02:0", 0, "0.6", 10, 12, 9, 11),
        _source("000001:2024-01-02:1", "0.6", "1.2", 11, 14, 10, 13),
        _source("000001:2024-01-03:0", 0, "0.8", 13, 15, 12, 14),
        _source("000001:2024-01-03:1", "0.8", "1.4", 14, 16, 13, 15),
    ]
    bars = _materialize_source_aligned(run, "000001")
    assert len(bars) == 2
    assert bars[0]["tau_length"] == "1.2"
    assert bars[0]["overshoot_tau"] == "0.2"
    assert bars[0]["open"] == "10" and bars[0]["close"] == "13"
    assert bars[0]["volume"] == "20"
    assert bars[1]["tau_length"] == "1.4"
    assert bars[1]["provenance"][0]["source_id"].endswith(":0")


def test_audit_reports_selected_fast_and_slow_islands():
    data = {
        "market_bars": [
            {"source_segments": [_source("000001:2024-01-02:0", 0, "0.6", 1, 2, 1, 2)]},
            {
                "source_segments": [
                    _source("000001:2024-01-02:1", "0.6", "1.2", 2, 3, 1, 2)
                ]
            },
            {
                "source_segments": [
                    _source("000001:2024-01-02:2", "1.2", "1.8", 2, 3, 1, 2)
                ]
            },
            {
                "source_segments": [
                    _source("000001:2024-01-02:3", "1.8", "2.4", 2, 3, 1, 2)
                ]
            },
            {"source_segments": [_source("000001:2024-01-03:0", 0, "1.1", 2, 3, 1, 2)]},
            {
                "source_segments": [
                    _source("000001:2024-01-03:1", "1.1", "2.2", 2, 3, 1, 2)
                ]
            },
        ]
    }
    report = audit_proof(data, selected_run_count=2)
    assert report["population"]["selected_run_count"] == 2
    assert report["contract"]["fractional_source_split"] is False
    assert report["contract"]["volume_proration"] is False
    assert report["population"]["source_aligned_market_bar_count"] >= 2
    assert "tau_coordinate_drift" in report["exact_tau_comparison"]
