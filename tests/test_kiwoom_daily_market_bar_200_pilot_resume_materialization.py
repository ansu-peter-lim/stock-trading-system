import json
from datetime import date
from pathlib import Path

from src.kiwoom_daily.market_bar_200_pilot_resume_materialization import (
    _artifact_status,
    build_report,
)


def test_offline_resume_reuses_overlap_and_materializes_frozen_pilot(tmp_path):
    report = build_report(output_path=tmp_path / "resume.json", allow_network=False)

    assert report["network_called"] is False
    assert report["coverage"]["required_fast_total"] == 71
    assert report["coverage"]["still_missing_count"] == 0
    assert report["coverage"]["complete"] is True
    assert report["coverage"]["status_counts"]["SOURCE_INVALID"] == 0
    assert report["coverage"]["status_counts"]["ALREADY_CACHED_BEFORE_V1_2"] == 58
    assert report["coverage"]["status_counts"]["FETCHED_V1_2"] == 2
    assert report["coverage"]["status_counts"]["FETCHED_V1_2B_RETRY"] == 1
    assert report["coverage"]["planned_dates_covered_by_overlap_count"] == 61
    assert report["materialization"]["success"] is True
    assert report["materialization"]["market_bar_count"] == 201
    assert report["materialization"]["resolved_island_count"] == 1
    assert report["materialization"]["skip_count"] == 0
    assert report["materialization"]["duplicate_target_count"] == 0
    assert report["materialization"]["unresolved_internal_gap_count"] == 0
    assert report["materialization"]["source_ohlc_exact"] is True
    assert report["materialization"]["source_volume_exact"] is True
    assert report["acquisition_efficiency"]["v12c_actual_api_request_count"] == 0

    persisted = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
    assert persisted["result"] == "MARKET_BAR_200_STREAM_READY"
    assert persisted["network_called"] is False


def test_coverage_report_does_not_expose_artifact_hashes(tmp_path):
    report = build_report(output_path=tmp_path / "resume.json", allow_network=False)
    report_text = json.dumps(report, ensure_ascii=False, default=str)

    # Page paths are intentionally redacted, and raw digests remain provenance-only.
    assert 'content_hashes_in_report": false' in report_text
    assert not any(
        token in report_text.lower()
        for token in ("app_key", "secret_key", "access_token", "bearer ")
    )
    assert not any(
        part.startswith("page-") and len(part.split("-")[-1].split(".")[0]) == 64
        for part in report_text.split("/")
    )


def test_artifact_status_preserves_frozen_attempt_classes():
    planned = {date(2026, 2, 2), date(2026, 2, 3), date(2026, 2, 4), date(2026, 2, 5)}

    assert (
        _artifact_status(Path("raw/20260202/page-001-a.json"), planned)
        == "FETCHED_V1_2"
    )
    assert (
        _artifact_status(Path("raw/20260204/page-001-a.json"), planned)
        == "FETCHED_V1_2B_RETRY"
    )
    assert (
        _artifact_status(Path("raw/20260205/page-001-a.json"), planned)
        == "FETCHED_V1_2C"
    )
    assert (
        _artifact_status(Path("raw/20260828/page-001-a.json"), planned)
        == "ALREADY_CACHED_BEFORE_V1_2"
    )
