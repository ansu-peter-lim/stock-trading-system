from src.kiwoom_daily.global_tau_resolution_adequacy_audit import (
    _cross_count,
    _resolve_islands,
)


def _s(source_id, start, end, resolution="5M_RAW_ACTIVITY_SIGNAL_ANCHORED"):
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
        "volume": "10",
    }


def test_integer_cross_count_excludes_exact_start_target():
    assert _cross_count(0, 0.9) == 0
    assert _cross_count(0.9, 1.1) == 1
    assert _cross_count(1, 1.2) == 0


def test_multi_target_segment_is_unresolved_and_does_not_make_duplicate_bars():
    sources = [
        _s("000001:2024-01-02:0", 0, "0.8"),
        _s("000001:2024-01-02:1", "0.8", "3.0"),
        _s("000001:2024-01-03:0", 0, "0.7"),
        _s("000001:2024-01-03:1", "0.7", "1.2"),
    ]
    islands, unresolved, bars = _resolve_islands(sources, "000001")
    assert len(unresolved) == 1
    assert unresolved[0]["targets_crossed_count"] == 3
    assert unresolved[0]["skipped_target_count"] == 2
    assert len(islands) == 1
    assert len(bars) == 1
    assert bars[0]["actual_integer_target"] == 1
