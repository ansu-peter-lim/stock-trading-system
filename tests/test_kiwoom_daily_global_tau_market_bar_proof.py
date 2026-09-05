from src.kiwoom_daily.global_tau_market_bar_proof import _materialize_global


def _s(source_id, start, end):
    return {
        "source_id": source_id,
        "source_tau_start": str(start),
        "source_tau_end": str(end),
        "calendar_start_datetime": "2024-01-02T09:00:00+09:00",
        "calendar_end_datetime": "2024-01-02T15:30:00+09:00",
        "source_resolution": "5M_RAW_ACTIVITY_SIGNAL_ANCHORED",
        "open": "10",
        "high": "12",
        "low": "9",
        "close": "11",
        "volume": "10",
    }


def test_global_integer_lattice_does_not_carry_overshoot():
    bars, targets = _materialize_global(
        [
            _s("000001:2024-01-02:0", 0, "0.6"),
            _s("000001:2024-01-02:1", "0.6", "1.14"),
            _s("000001:2024-01-02:2", "1.14", "1.9"),
            _s("000001:2024-01-02:3", "1.9", "2.14"),
        ],
        "000001",
    )
    assert [x["target_tau"] for x in targets] == ["1", "2"]
    assert bars[0]["tau_end"] == "1.14"
    assert bars[1]["tau_end"] == "2.14"
    assert bars[1]["boundary_error"] == "0.14"
    assert bars[0]["source_segment_count"] == 2


def test_global_target_is_forward_snapped_only():
    bars, targets = _materialize_global([_s("000001:2024-01-02:0", 0, "1.8")], "000001")
    assert len(bars) == 1
    assert targets[0]["snapped_tau"] == "1.8"
    assert targets[0]["skipped_target_count"] == 0
