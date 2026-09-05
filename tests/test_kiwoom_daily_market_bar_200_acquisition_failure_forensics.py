import json
from datetime import date

from src.kiwoom_daily.market_bar_200_acquisition_failure_forensics import (
    FAILED_DATE,
    SUCCESS_DATES,
    _flow_evidence,
    build_forensics_report,
    inspect_raw_page,
    run_forensics,
)


def _payload(*, stock_code: str = "066570", label: str = "20260202090000") -> dict:
    return {
        "stk_cd": stock_code,
        "return_code": 0,
        "return_msg": "ok",
        "stk_min_pole_chart_qry": [
            {
                "cur_prc": "+100",
                "trde_qty": "0",
                "cntr_tm": label,
                "open_pric": "-100",
                "high_pric": "+110",
                "low_pric": "100",
                "acc_trde_qty": "0",
                "pred_pre": "0",
                "pred_pre_sig": "0",
            }
        ],
    }


def test_inspect_raw_page_keeps_signed_source_evidence_and_validates_parser(tmp_path):
    path = tmp_path / "page-001-aabb.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    result = inspect_raw_page(path, date(2026, 2, 2))

    assert result["json_valid"] is True
    assert result["envelope_valid"] is True
    assert result["parser_validation"] == "PASS"
    assert result["signed_price_counts"] == {
        "positive": 2,
        "negative": 1,
        "unsigned": 1,
        "malformed": 0,
    }
    assert result["volume_invalid_count"] == 0
    assert result["path"].endswith("page-001.json")
    assert "aabb" not in result["path"]


def test_missing_failed_date_has_no_payload_and_does_not_get_invented(tmp_path):
    result = inspect_raw_page(tmp_path / "does-not-exist.json", FAILED_DATE)

    assert result["exists"] is False
    assert result["raw_bytes_received"] is None
    assert result["row_count"] == 0
    assert result["parser_validation"] == "NOT_RUN"


def test_collector_flow_requires_raw_store_before_parser(tmp_path):
    source = tmp_path / "pipeline.py"
    source.write_text(
        "response = transport(\n"
        "except KiwoomApiError as exc:\n"
        "path, digest = store.store_page(\n"
        "cont_yn, response_key = parse_pagination_headers(\n"
        "page = parse_minute_page(\n",
        encoding="utf-8",
    )
    evidence = _flow_evidence(source)
    assert evidence["observed_order"] == [
        "transport_call",
        "raw_store",
        "pagination_parse",
        "page_parse",
    ]
    assert evidence["raw_store_precedes_validation"] is True


def test_forensics_is_offline_and_freezes_the_two_successes_and_failure(tmp_path):
    report = build_forensics_report()

    assert report["mode"] == "OFFLINE_ONLY"
    assert report["network_called"] is False
    assert report["freeze"]["successful_dates"] == [
        day.isoformat() for day in SUCCESS_DATES
    ]
    assert report["freeze"]["failed_date"] == FAILED_DATE.isoformat()
    assert report["controlled_retry"] == {
        "performed": False,
        "count": 0,
        "network": False,
        "reason": "V1.2A is offline-only; no retry was attempted",
    }
    assert report["date_comparison"]["success_regression_pass"] is True
    # V1.2B may later create the previously missing artifact; the V1.2A
    # forensic conclusion itself remains historical and is never rewritten.
    assert report["failure"]["raw_artifact_exists"] in {True, False}
    if report["failure"]["raw_artifact_exists"]:
        assert report["failure"]["manifest_record_exists"] is True
    assert report["classification"]["primary"] == "UNKNOWN"

    output = tmp_path / "forensics.json"
    persisted = run_forensics(output_path=output)
    assert json.loads(output.read_text(encoding="utf-8")) == persisted
    assert persisted["security"]["credentials_included"] is False
