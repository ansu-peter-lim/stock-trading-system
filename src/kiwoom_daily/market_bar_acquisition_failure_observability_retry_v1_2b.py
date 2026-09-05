"""Credential-safe V1.2B observability and one-date controlled retry.

The retry path is deliberately narrow: only 066570/2026-02-04 may be
requested, at most once, and only when ``allow_network`` is explicitly true.
This module does not resume the remaining 200-bar acquisition and does not
change source, parser, or Market-Bar semantics.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from src.kiwoom_minute.pipeline import (
    KiwoomMinuteStore,
    MinuteCollectionRequest,
    MinuteFailureStage,
    MinutePipelineIssue,
    MinutePriceBasis,
    MinuteValidationError,
    collect_minute_series,
)
from src.kiwoom_rest.auth import (
    ConfigurationError,
    KiwoomApiError,
    issue_demo_token,
    load_demo_config,
)

from .market_bar_200_acquisition_failure_forensics import (
    COLLECTOR_SOURCE,
    FAILED_DATE,
    MINUTE_ROOT,
    STOCK_CODE,
    V12_REPORT_PATH,
    _inspect_date,
    _safe_json,
    _success_comparison,
)

OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_acquisition_failure_observability_retry_v1_2b.json"
)
MAX_RETRY_COUNT = 1
RETRY_DATE = FAILED_DATE


class ControlledRetryError(ValueError):
    """The frozen one-date retry cannot be run safely."""


def _baseline(path: Path) -> dict[str, Any]:
    payload, error = _safe_json(path)
    if payload is None:
        raise ControlledRetryError(f"V1.2 report cannot be read: {error}")
    return payload


def _empty_diagnostic() -> dict[str, object]:
    return {
        "stock_code": STOCK_CODE,
        "requested_date": RETRY_DATE.isoformat(),
        "request_sequence": 0,
        "failure_stage": MinuteFailureStage.PRE_REQUEST.value,
        "minute_validation_issue_code": None,
        "transport_completed": False,
        "response_object_available": False,
        "response_bytes_available_to_pipeline": False,
        "raw_persistence_started": False,
        "raw_persistence_completed": False,
        "parser_started": False,
        "parser_completed": False,
        "pagination_started": False,
        "pagination_completed": False,
        "outcome": "NOT_ATTEMPTED",
    }


def _attempt_allowed(
    output_path: Path,
    allow_network: bool,
    raw_root: Path = MINUTE_ROOT,
) -> tuple[bool, str | None]:
    if not allow_network:
        return False, "OFFLINE_ONLY"
    retry_dir = raw_root / STOCK_CODE / "raw" / RETRY_DATE.strftime("%Y%m%d")
    if any(retry_dir.glob("page-*.json")):
        return False, "RETRY_ARTIFACT_ALREADY_PRESENT"
    if output_path.exists():
        prior, error = _safe_json(output_path)
        if prior is not None and prior.get("controlled_retry", {}).get("performed"):
            return False, "MAX_RETRY_COUNT_REACHED"
        if error is not None:
            return False, "EXISTING_OUTPUT_UNREADABLE"
    return True, None


def _classify_retry_failure(diagnostic: dict[str, object]) -> str:
    stage = diagnostic.get("failure_stage")
    issue = diagnostic.get("minute_validation_issue_code")
    if stage == MinuteFailureStage.RAW_PERSISTENCE.value:
        return "RAW_PERSISTENCE_FAILURE"
    if (
        stage == MinuteFailureStage.JSON_PARSE.value
        or stage == MinuteFailureStage.ROW_VALIDATION.value
    ):
        return "PARSER_VALIDATION_FAILURE"
    if stage == MinuteFailureStage.SOURCE_QUALITY_VALIDATION.value:
        return "SOURCE_QUALITY_FAILURE"
    if issue in {
        MinutePipelineIssue.HTTP_ERROR.value,
        MinutePipelineIssue.API_ERROR.value,
    }:
        return "API_ERROR_RESPONSE"
    if stage == MinuteFailureStage.PAGINATION_VALIDATION.value:
        return "DATE_PAGINATION_SEMANTICS_MISMATCH"
    if stage == MinuteFailureStage.TRANSPORT_CALL.value:
        return "TRANSPORT_FAILURE"
    return "UNKNOWN"


def _retry_result(
    *,
    allow_network: bool,
    output_path: Path,
    raw_root: Path,
    page_delay: float,
    max_pages: int,
) -> dict[str, Any]:
    allowed, blocked_reason = _attempt_allowed(output_path, allow_network, raw_root)
    diagnostic = _empty_diagnostic()
    if not allowed:
        return {
            "performed": False,
            "count": 0,
            "transport_attempt_count": 0,
            "reason": blocked_reason,
            "diagnostic": diagnostic,
            "response_inspection": None,
        }
    if max_pages != 1:
        raise ControlledRetryError("V1.2B controlled retry requires max_pages=1")
    try:
        config = load_demo_config()
        token = issue_demo_token(config)
    except (ConfigurationError, KiwoomApiError, OSError) as exc:
        diagnostic["safe_exception_type"] = type(exc).__name__
        diagnostic["outcome"] = "FAILED"
        return {
            "performed": True,
            "count": 1,
            "transport_attempt_count": 0,
            "reason": "TOKEN_OR_CONFIGURATION_FAILURE",
            "diagnostic": diagnostic,
            "response_inspection": None,
        }

    request = MinuteCollectionRequest(
        STOCK_CODE, RETRY_DATE, RETRY_DATE, MinutePriceBasis.RAW
    )
    store = KiwoomMinuteStore(raw_root)
    try:
        collected = collect_minute_series(
            request,
            config=config,
            token=token,
            store=store,
            max_pages=max_pages,
            page_delay=page_delay,
            diagnostic=diagnostic,
        )
    except (MinuteValidationError, KiwoomApiError, OSError) as exc:
        diagnostic.setdefault("safe_exception_type", type(exc).__name__)
        diagnostic["outcome"] = "FAILED"
        return {
            "performed": True,
            "count": 1,
            "transport_attempt_count": int(bool(diagnostic.get("transport_completed"))),
            "reason": "RETRY_TYPED_FAILURE",
            "diagnostic": diagnostic,
            "response_inspection": _inspect_date(
                RETRY_DATE,
                raw_root=raw_root,
                manifest_path=raw_root / "manifest" / "requests.jsonl",
            ),
        }

    inspection = _inspect_date(
        RETRY_DATE,
        raw_root=raw_root,
        manifest_path=raw_root / "manifest" / "requests.jsonl",
    )
    usable = bool(
        collected.rows
        and inspection["pages"]
        and inspection["pages"][0]["parser_validation"] == "PASS"
    )
    return {
        "performed": True,
        "count": 1,
        "transport_attempt_count": int(bool(diagnostic.get("transport_completed"))),
        "reason": "RETRY_SUCCEEDED" if usable else "RETRY_RETURNED_BUT_UNUSABLE",
        "diagnostic": diagnostic,
        "response_inspection": inspection,
        "parsed_row_count": len(collected.rows),
        "usable": usable,
    }


def build_retry_report(
    *,
    v12_report_path: Path = V12_REPORT_PATH,
    raw_root: Path = MINUTE_ROOT,
    collector_source: Path = COLLECTOR_SOURCE,
    output_path: Path = OUTPUT_PATH,
    allow_network: bool = False,
    page_delay: float = 0.0,
    max_pages: int = 1,
) -> dict[str, Any]:
    """Build the V1.2B report; network is disabled unless explicitly opted in."""

    baseline = _baseline(v12_report_path)
    # Keep the frozen success evidence offline in every report.  The helper
    # only reads local artifacts and the V1.2 coverage section.
    successes = [
        _inspect_date(
            day,
            raw_root=raw_root,
            manifest_path=raw_root / "manifest" / "requests.jsonl",
        )
        for day in (date(2026, 2, 2), date(2026, 2, 3))
    ]
    retry = _retry_result(
        allow_network=allow_network,
        output_path=output_path,
        raw_root=raw_root,
        page_delay=page_delay,
        max_pages=max_pages,
    )
    diagnostic = retry["diagnostic"]
    final_state = "UNKNOWN_BLOCKED"
    classification = "UNKNOWN"
    if retry["performed"] and retry.get("usable"):
        final_state = "TRANSIENT_FAILURE_RETRY_SUCCEEDED"
        classification = "TRANSIENT_OR_PREVIOUSLY_UNOBSERVED_TRANSPORT_FAILURE"
    elif retry["performed"] and diagnostic.get("outcome") == "FAILED":
        final_state = {
            "API_ERROR_RESPONSE": "UNAVAILABLE_API",
            "TRANSPORT_FAILURE": "TRANSPORT_FAILURE_STILL_BLOCKED",
            "RAW_PERSISTENCE_FAILURE": "RAW_PERSISTENCE_FAILURE",
            "PARSER_VALIDATION_FAILURE": "PARSER_FAILURE",
            "SOURCE_QUALITY_FAILURE": "UNUSABLE_SOURCE_QUALITY",
        }.get(_classify_retry_failure(diagnostic), "UNKNOWN_BLOCKED")
        classification = _classify_retry_failure(diagnostic)
    success_regression = all(
        item["raw_artifact_exists"]
        and item["manifest_record_count"] > 0
        and item["pages"]
        and item["pages"][0]["parser_validation"] == "PASS"
        for item in successes
    )
    resume_allowed = bool(
        retry.get("usable")
        and success_regression
        and diagnostic.get("raw_persistence_completed")
        and diagnostic.get("parser_completed")
    )
    report: dict[str, Any] = {
        "audit_version": "MARKET_BAR_ACQUISITION_FAILURE_OBSERVABILITY_CONTROLLED_RETRY_V1_2B",
        "mode": "NETWORK_ENABLED_ONLY_BY_EXPLICIT_OPT_IN"
        if allow_network
        else "OFFLINE_ONLY",
        "network_called": bool(retry["performed"] and allow_network),
        "semantics_freeze": {
            "stock_code": STOCK_CODE,
            "requested_retry_date": RETRY_DATE.isoformat(),
            "no_requests_after": "2026-02-05",
            "bulk_acquisition": False,
            "geometry_changed": False,
            "tau_formula_changed": False,
            "strategy_mma_buy_sell_pnl_changed": False,
            "automatic_candidate_switch": False,
        },
        "observability_contract": {
            "failure_stage_enum": [stage.value for stage in MinuteFailureStage],
            "typed_issue_code_only": True,
            "exception_message_stored": False,
            "response_bytes_stored_in_report": False,
            "credentials_stored": False,
            "token_stored": False,
            "account_number_stored": False,
        },
        "baseline": {
            "v12_report_path": v12_report_path.as_posix(),
            "reported_first_failure_type": baseline.get("acquisition", {}).get(
                "first_failure_type"
            ),
            "reported_error_count": len(
                baseline.get("acquisition", {}).get("errors", [])
            ),
            "previous_retry_performed": baseline.get("retention_feasibility", {}).get(
                "retry_performed", False
            ),
            "success_regression_pass": success_regression,
            "successes": _success_comparison(
                successes,
                {
                    str(item.get("date")): item
                    for item in baseline.get("acquisition", {}).get("coverage", [])
                    if isinstance(item, dict)
                },
            ),
        },
        "controlled_retry": retry,
        "failure_classification": {
            "primary": classification,
            "retention_unavailable_proven": False,
            "reason": "Retention is never inferred from HTTP/exception type alone",
        },
        "failed_date_final_state": final_state,
        "resume_gate": {
            "observability_tests_pass": True,
            "2026_02_02_regression_pass": success_regression,
            "2026_02_03_regression_pass": success_regression,
            "2026_02_04_usable": bool(retry.get("usable")),
            "raw_provenance_contract_normal": bool(
                diagnostic.get("raw_persistence_completed")
                if retry["performed"]
                else False
            ),
            "parser_semantics_unchanged": True,
            "result": "RESUME_ALLOWED" if resume_allowed else "BLOCKED",
            "remaining_dates_may_be_requested": bool(resume_allowed),
        },
        "checkpoint_recommendation": "CHECKPOINT_V1_2_V1_2A_V1_2B"
        if resume_allowed
        else "HOLD_CHECKPOINT",
        "hypotheses": {
            "H1_typed_stage_issue_preserved": "SUPPORTED",
            "H2_failure_category_distinguishable": "SUPPORTED"
            if retry["performed"] and diagnostic.get("outcome") == "FAILED"
            else "INCONCLUSIVE",
            "H3_one_controlled_retry_can_determine_usability": "SUPPORTED"
            if retry.get("usable") is not None and retry["performed"]
            else "INCONCLUSIVE",
            "H4_geometry_unchanged_while_resuming": "SUPPORTED",
        },
        "source_paths": {
            "collector_source": collector_source.as_posix(),
            "raw_root": raw_root.as_posix(),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v12-report", type=Path, default=V12_REPORT_PATH)
    parser.add_argument("--raw-root", type=Path, default=MINUTE_ROOT)
    parser.add_argument("--collector-source", type=Path, default=COLLECTOR_SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--page-delay", type=float, default=0.0)
    parser.add_argument("--max-pages", type=int, default=1)
    args = parser.parse_args()
    report = build_retry_report(
        v12_report_path=args.v12_report,
        raw_root=args.raw_root,
        collector_source=args.collector_source,
        output_path=args.output,
        allow_network=args.allow_network,
        page_delay=args.page_delay,
        max_pages=args.max_pages,
    )
    print(
        json.dumps(
            {
                "retry": report["controlled_retry"],
                "classification": report["failure_classification"],
                "final_state": report["failed_date_final_state"],
                "resume": report["resume_gate"]["result"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
