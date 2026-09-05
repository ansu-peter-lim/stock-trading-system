import json

from src.kiwoom_daily.market_bar_continuous_pilot_extension_plan import (
    ANCHOR_END,
    ANCHOR_START,
    OUTPUT_PATH,
    _candidate_sort_key,
    _extension_direction,
    run_plan,
)


def test_extension_direction_is_deterministic():
    assert (
        _extension_direction(
            {"calendar_start": "2026-03-01", "calendar_end": "2026-05-26"}
        )
        == "BACKWARD_ONLY"
    )
    assert (
        _extension_direction(
            {"calendar_start": "2026-04-20", "calendar_end": "2026-06-01"}
        )
        == "FORWARD_ONLY"
    )
    assert (
        _extension_direction(
            {"calendar_start": "2026-03-01", "calendar_end": "2026-06-01"}
        )
        == "BOTH_SIDES"
    )


def test_candidate_sort_key_prefers_lower_fetch_cost_then_earlier_start():
    common = {
        "structural_gap_count": 0,
        "anchor_included": True,
        "calendar_session_count": 100,
        "expected_market_bar_capacity": 170,
        "source_quality_anomaly_count": 0,
        "calendar_end": "2026-05-26",
        "stock_code": "066570",
    }
    expensive = {
        **common,
        "new_fetch_required_session_count": 12,
        "calendar_start": "2026-01-02",
    }
    cheap = {
        **common,
        "new_fetch_required_session_count": 8,
        "calendar_start": "2026-02-02",
    }
    assert min((expensive, cheap), key=_candidate_sort_key) is cheap


def test_v11_plan_is_offline_and_keeps_anchor_contract(tmp_path):
    output = tmp_path / OUTPUT_PATH.name
    report = run_plan(output_path=output)
    population = report["planning_population"]
    assert population["stock_code"] == "066570"
    assert population["target_160_candidate_count"] > 0
    assert population["target_200_candidate_count"] > 0
    assert report["anchor_pilot"]["market_bar_count"] == 80
    assert report["contract"]["network_calls"] == 0
    assert report["contract"]["strategy"] is False
    assert report["contract"]["buy_sell"] is False
    assert report["contract"]["pnl"] is False
    for target in (160, 200):
        candidate = report[f"preferred_candidate_{target}"]
        assert candidate["anchor_included"] is True
        assert candidate["calendar_start"] <= ANCHOR_START.isoformat()
        assert candidate["calendar_end"] >= ANCHOR_END.isoformat()
        assert candidate["expected_market_bar_capacity"] >= target
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["contract"]["network_calls"] == 0
    assert not any(
        secret in output.read_text(encoding="utf-8")
        for secret in ("APP_KEY", "SECRET_KEY", "ACCESS_TOKEN")
    )
