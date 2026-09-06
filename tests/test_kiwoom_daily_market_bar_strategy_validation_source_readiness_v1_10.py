import json
from datetime import date

from src.kiwoom_daily.market_bar_strategy_validation_source_readiness_v1_10 import (
    run_source_readiness,
)


def _report(tmp_path):
    return run_source_readiness(
        output_path=tmp_path / "source-readiness.json",
    )


def test_source_readiness_is_strategy_blind_and_offline(tmp_path):
    report = _report(tmp_path)

    assert report["scope"]["network_calls"] == 0
    assert report["scope"]["strategy_outputs_inspected"] is False
    assert report["scope"]["strategy_signals"] is False
    assert report["scope"]["future_returns"] is False
    assert report["scope"]["pnl"] is False
    assert report["scope"]["market_bar_materialization"] is False
    assert (
        report["frozen_documents"]["registry_verification"]["expected_roles_found"]
        is True
    )


def test_candidate_population_and_role_selection_are_frozen(tmp_path):
    report = _report(tmp_path)
    population = report["candidate_population"]

    assert population["per_stock"] == {
        "005380": {"candidate_count_160": 415, "candidate_count_200": 368},
        "068270": {"candidate_count_160": 404, "candidate_count_200": 362},
    }
    assert population["candidate_count_160"] == 819
    assert population["candidate_count_200"] == 730

    assert (
        report["candidate_tables"]["005380"]["best_200_candidate"]["candidate_id"]
        == "005380:2026-01-06:2026-05-08:200"
    )
    assert (
        report["candidate_tables"]["068270"]["best_200_candidate"]["candidate_id"]
        == "068270:2026-01-02:2026-06-12:200"
    )

    primary = report["preferred_selections"]["005380"]
    assert primary["target_market_bars"] == 200
    assert primary["candidate"]["candidate_id"] == "005380:2025-06-12:2026-01-20:200"
    assert primary["new_fetch_required_count"] == 24

    stress = report["preferred_selections"]["068270"]
    assert stress["target_market_bars"] == 200
    assert stress["candidate"]["candidate_id"] == "068270:2024-12-27:2025-10-31:200"
    assert stress["new_fetch_required_count"] == 48


def test_exact_fetch_dates_are_sorted_and_not_cached(tmp_path):
    report = _report(tmp_path)
    for stock in ("005380", "068270"):
        rows = report["exact_fetch_dates"][stock]
        dates = [row["date"] for row in rows]
        assert dates == sorted(dates)
        assert all(row["cached_minute_exists"] is False for row in rows)
        assert all(row["planned_fetch_required"] is True for row in rows)
        assert all(row["reason"] == "MARKET_BAR_RESOLUTION_REQUIRED" for row in rows)
        assert (
            len(rows)
            == report["preferred_selections"][stock]["new_fetch_required_count"]
        )


def test_local_source_inventory_is_clean_and_reproducible(tmp_path):
    first = _report(tmp_path / "first")
    second = _report(tmp_path / "second")

    for stock in ("005380", "068270"):
        daily = first["source_coverage"]["daily"][stock]
        minute = first["source_coverage"]["minute_raw"][stock]
        assert daily["raw_adjusted_date_parity"] is True
        assert daily["raw"]["source_quality_anomalies"] == []
        assert daily["adjusted"]["source_quality_anomalies"] == []
        assert minute["source_quality_anomalies"] == []
        assert minute["duplicate_timestamp_count"] == 0
        assert minute["covered_date_start"] == date(2025, 9, 1)
        assert minute["covered_date_end"] == date(2026, 8, 28)

    first.pop("source_coverage")
    second.pop("source_coverage")
    assert first == second


def test_report_is_valid_json_and_has_materialization_invariants(tmp_path):
    output = tmp_path / "source-readiness.json"
    _report(tmp_path)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["required_market_bar_invariants"]
    assert payload["materialization_plan"]["current_phase"].startswith("A planning")
