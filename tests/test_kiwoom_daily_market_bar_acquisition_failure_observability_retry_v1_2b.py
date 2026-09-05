import json

from src.kiwoom_daily.market_bar_acquisition_failure_observability_retry_v1_2b import (
    _attempt_allowed,
    _classify_retry_failure,
    build_retry_report,
)
from src.kiwoom_minute.pipeline import MinuteFailureStage


def test_offline_default_never_attempts_retry(tmp_path):
    report = build_retry_report(output_path=tmp_path / "retry.json")

    assert report["mode"] == "OFFLINE_ONLY"
    assert report["network_called"] is False
    assert report["controlled_retry"]["performed"] is False
    assert report["controlled_retry"]["count"] == 0
    assert report["resume_gate"]["result"] == "BLOCKED"


def test_retry_guard_rejects_a_second_performed_attempt(tmp_path):
    output = tmp_path / "retry.json"
    output.write_text(
        json.dumps({"controlled_retry": {"performed": True}}), encoding="utf-8"
    )

    assert _attempt_allowed(output, True, tmp_path / "raw") == (
        False,
        "MAX_RETRY_COUNT_REACHED",
    )


def test_retry_guard_rejects_existing_failed_date_artifact(tmp_path):
    output = tmp_path / "retry.json"
    raw_page = tmp_path / "raw" / "066570" / "raw" / "20260204" / "page-001-a.json"
    raw_page.parent.mkdir(parents=True)
    raw_page.write_bytes(b"{}")

    assert _attempt_allowed(output, True, tmp_path / "raw") == (
        False,
        "RETRY_ARTIFACT_ALREADY_PRESENT",
    )


def test_retry_classification_uses_structured_stage_and_issue_only():
    assert (
        _classify_retry_failure(
            {"failure_stage": MinuteFailureStage.RAW_PERSISTENCE.value}
        )
        == "RAW_PERSISTENCE_FAILURE"
    )
    assert (
        _classify_retry_failure(
            {
                "failure_stage": MinuteFailureStage.TRANSPORT_CALL.value,
                "minute_validation_issue_code": "HTTP_ERROR",
            }
        )
        == "API_ERROR_RESPONSE"
    )
    assert (
        _classify_retry_failure(
            {
                "failure_stage": MinuteFailureStage.ROW_VALIDATION.value,
                "minute_validation_issue_code": "MALFORMED_ROW",
            }
        )
        == "PARSER_VALIDATION_FAILURE"
    )
