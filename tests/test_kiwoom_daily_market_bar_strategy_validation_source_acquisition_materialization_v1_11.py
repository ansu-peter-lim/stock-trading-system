from src.kiwoom_daily.market_bar_strategy_validation_source_acquisition_materialization_v1_11 import (
    run_v1_11,
)


def _report(tmp_path):
    return run_v1_11(
        output_path=tmp_path / "v1-11.json",
        manifest_path=tmp_path / "stream-manifest.md",
        allow_network=False,
    )


def test_offline_run_keeps_strategy_gate_closed(tmp_path):
    report = _report(tmp_path)

    assert report["network_calls"] == 0
    assert report["strategy_outputs_inspected"] is False
    assert report["c2_evaluated"] is False
    assert report["strategy_calculations"] == 0
    assert report["validation_stream_manifest"]["written"] is False


def test_frozen_windows_and_validation_order_are_unchanged(tmp_path):
    report = _report(tmp_path)

    assert report["validation_order"] == ["005380", "068270"]
    assert {
        stock: {
            **{
                key: str(value)
                for key, value in window.items()
                if key.startswith("calendar_")
            },
            **{
                key: value
                for key, value in window.items()
                if not key.startswith("calendar_")
            },
        }
        for stock, window in report["frozen_windows"].items()
    } == {
        "005380": {
            "calendar_start": "2025-06-12",
            "calendar_end": "2026-01-20",
            "target": 200,
            "expected_capacity": 200,
            "role": "BALANCED_STRATEGY_VALIDATION_CANDIDATE",
        },
        "068270": {
            "calendar_start": "2024-12-27",
            "calendar_end": "2025-10-31",
            "target": 200,
            "expected_capacity": 201,
            "role": "SLOW_DOMINANT_STRESS_VALIDATION_CANDIDATE",
        },
    }
    assert report["results"]["005380"]["frozen_window"]["window_session_count"] == 150
    assert report["results"]["068270"]["frozen_window"]["window_session_count"] == 203


def test_offline_coverage_reports_exact_unresolved_dates(tmp_path):
    report = _report(tmp_path)

    assert report["readiness"] == {
        "005380": "SOURCE_COVERAGE_INSUFFICIENT",
        "068270": "SOURCE_COVERAGE_INSUFFICIENT",
    }
    assert report["results"]["005380"]["coverage"]["still_missing"] == 24
    assert report["results"]["068270"]["coverage"]["still_missing"] == 48
    assert report["results"]["005380"]["acquisition"]["actual_api_request_count"] == 0
    assert report["results"]["068270"]["acquisition"]["actual_api_request_count"] == 0


def test_materialization_is_not_started_until_required_fast_coverage_exists(tmp_path):
    report = _report(tmp_path)

    for result in report["results"].values():
        assert result["coverage"]["complete"] is False
        assert result["materialization"]["actual_market_bar_count"] == 0
        assert result["materialization"]["stream_digest"] is None
        assert result["retention"]["network_phase_closed"] is False
