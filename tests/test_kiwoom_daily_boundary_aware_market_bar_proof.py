from src.kiwoom_daily.boundary_aware_market_bar_proof import _materialize, audit_proof


def _s(source_id, start, end, resolution="DAILY_SIGNAL_ADJUSTED"):
    return {
        "source_id": source_id,
        "source_tau_start": str(start),
        "source_tau_end": str(end),
        "calendar_start_datetime": "2024-01-02T09:00:00+09:00",
        "calendar_end_datetime": "2024-01-02T15:30:00+09:00",
        "source_resolution": resolution,
        "open": "10",
        "high": "12",
        "low": "9",
        "close": "11",
        "volume": "100",
    }


def test_closes_at_actual_boundary_without_split_or_prorated_volume():
    bars = _materialize(
        [_s("000001:2024-01-02:0", 0, "0.6"), _s("000001:2024-01-02:1", "0.6", "1.2")],
        "000001",
    )
    assert len(bars) == 1
    assert bars[0]["tau_length"] == "1.2"
    assert bars[0]["overshoot_tau"] == "0.2"
    assert bars[0]["volume"] == "200"
    assert bars[0]["boundary_source_quality"] == "ACTUAL_BOUNDARY_WITH_COARSE_TAU"
    assert bars[0]["source_segment_count"] == 2


def test_audit_reuses_same_selected_source_provenance():
    v01 = {
        "market_bars": [
            {
                "source_segments": [
                    _s("000001:2024-01-02:0", 0, "0.6"),
                    _s("000001:2024-01-02:1", "0.6", "1.2"),
                ]
            },
            {"source_segments": [_s("000001:2024-01-02:2", "1.2", "2.2")]},
        ]
    }
    v03 = {
        "diagnostics": {},
        "selected_runs": [
            {
                "stock_code": "000001",
                "run_index": 1,
                "run_start": "2024-01-02",
                "run_end": "2024-01-02",
                "source_ids": [
                    "000001:2024-01-02:0",
                    "000001:2024-01-02:1",
                    "000001:2024-01-02:2",
                ],
                "market_bar_count": 2,
            }
        ],
    }
    report = audit_proof(v01, v03)
    assert report["population"]["boundary_aware_market_bar_count"] == 2
    assert report["contract"]["overshoot_carry"] is False
