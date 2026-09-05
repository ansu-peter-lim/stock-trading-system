from src.kiwoom_daily.market_bar_pilot_source_acquisition_plan import (
    _candidate_sort_key,
    audit_plan,
)


def test_empty_cache_is_safe_and_network_free():
    report = audit_plan({"market_bars": [], "unresolved_sessions": [], "runs": []})
    assert report["contract"]["network_calls"] == 0
    assert report["population"]["candidate_window_count_80"] == 0
    assert report["preferred_pilot"] is None


def test_candidate_ranking_uses_missing_fast_before_span_and_capacity():
    def candidate(*, missing: int, sessions: int, capacity: int, stock: str = "000001"):
        return {
            "structural_gap_count": 0,
            "missing_fast_session_count": missing,
            "calendar_session_count": sessions,
            "expected_market_bar_capacity": capacity,
            "source_quality_anomaly_count": 0,
            "stock_code": stock,
        }

    a = candidate(missing=16, sessions=24, capacity=80)
    b = candidate(missing=15, sessions=23, capacity=82)
    c = candidate(missing=15, sessions=23, capacity=81)
    assert sorted((a, b, c), key=_candidate_sort_key) == [b, c, a]
