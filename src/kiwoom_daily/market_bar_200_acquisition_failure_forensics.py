"""Offline forensics for the frozen V1.2 200-bar acquisition failure.

This module deliberately does not call Kiwoom, retry the failed date, alter a
raw artifact, or change the V0.6 market-bar geometry.  It inspects the existing
V1.2 report, the two locally preserved successful responses, and the collector
source order so that a later retry can be made with a known evidence contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

from src.kiwoom_minute.pipeline import (
    MinuteCollectionRequest,
    MinutePriceBasis,
    MinuteValidationError,
    parse_minute_page,
)

V12_REPORT_PATH = Path(
    "data/processed/strategy_review/market_bar_200_pilot_acquisition_v1_2.json"
)
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_200_acquisition_failure_forensics_v1_2a.json"
)
MINUTE_ROOT = Path("data/raw/kiwoom/minute")
COLLECTOR_SOURCE = Path("src/kiwoom_minute/pipeline.py")
STOCK_CODE = "066570"
SUCCESS_DATES = (date(2026, 2, 2), date(2026, 2, 3))
FAILED_DATE = date(2026, 2, 4)

ROW_FIELDS = (
    "cur_prc",
    "trde_qty",
    "cntr_tm",
    "open_pric",
    "high_pric",
    "low_pric",
    "acc_trde_qty",
    "pred_pre",
    "pred_pre_sig",
)
PRICE_FIELDS = ("open_pric", "high_pric", "low_pric", "cur_prc")
SIGNED_INTEGER = re.compile(r"^[+-]?\d+$")
UNSIGNED_INTEGER = re.compile(r"^\d+$")


def _iso_day(value: date) -> str:
    return value.isoformat()


def _safe_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__
    if not isinstance(payload, dict):
        return None, "NON_OBJECT_JSON"
    return payload, None


def _parse_label(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) != 14 or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _price_magnitude(value: object) -> int | None:
    if not isinstance(value, str) or SIGNED_INTEGER.fullmatch(value) is None:
        return None
    magnitude = value[1:] if value[:1] in "+-" else value
    parsed = int(magnitude)
    return parsed if parsed > 0 else None


def _spacing_minutes(labels: list[str]) -> list[int]:
    parsed = [_parse_label(label) for label in labels]
    times = sorted(value for value in parsed if value is not None)
    return [
        int((right - left).total_seconds() // 60) for left, right in pairwise(times)
    ]


def inspect_raw_page(path: Path, requested_date: date) -> dict[str, Any]:
    """Inspect one immutable response without writing or normalizing it."""

    # Artifact names contain a content digest.  Forensics needs the bytes, but
    # the report does not need to repeat that digest (or expose a raw filename).
    page_match = re.match(r"(page-\d{3})-[0-9a-f]+\.json$", path.name)
    report_path = (
        str(path.parent / (page_match.group(1) + ".json"))
        if page_match
        else str(path.parent / "<page>.json")
    )
    result: dict[str, Any] = {
        "requested_date": _iso_day(requested_date),
        "path": Path(report_path).as_posix(),
        "exists": path.exists(),
        "raw_bytes_received": None,
        "raw_artifact_preserved": path.exists(),
        "byte_count": None,
        "json_valid": False,
        "envelope_valid": False,
        "top_level_fields": [],
        "row_count": 0,
        "required_row_fields_present": False,
        "field_presence": {},
        "first_source_label": None,
        "last_source_label": None,
        "source_labels_descending": None,
        "duplicate_source_label_count": 0,
        "date_coverage": {"first": None, "last": None, "requested_row_count": 0},
        "spacing_minutes": [],
        "non_five_minute_spacing": [],
        "signed_price_counts": {
            "positive": 0,
            "negative": 0,
            "unsigned": 0,
            "malformed": 0,
        },
        "volume_invalid_count": 0,
        "zero_ohlc_count": 0,
        "ohlc_relationship_invalid_count": 0,
        "parser_validation": "NOT_RUN",
        "parser_issue": None,
        "pagination_metadata_in_body": False,
    }
    if not path.exists():
        return result
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        result["raw_bytes_received"] = "UNKNOWN"
        result["read_error"] = type(exc).__name__
        return result
    result["raw_bytes_received"] = True
    result["byte_count"] = len(raw_bytes)
    payload, error = _safe_json(path)
    if error or payload is None:
        result["parse_error"] = error
        return result
    result["json_valid"] = True
    result["top_level_fields"] = sorted(payload)
    result["envelope_valid"] = (
        payload.get("return_code") == 0
        and payload.get("stk_cd") == STOCK_CODE
        and isinstance(payload.get("stk_min_pole_chart_qry"), list)
    )
    result["pagination_metadata_in_body"] = any(
        key in payload for key in ("cont-yn", "next-key", "cont_yn", "next_key")
    )
    source_rows = payload.get("stk_min_pole_chart_qry")
    if not isinstance(source_rows, list):
        return result
    result["row_count"] = len(source_rows)
    field_presence = {
        field: sum(isinstance(row, dict) and field in row for row in source_rows)
        for field in ROW_FIELDS
    }
    result["field_presence"] = field_presence
    result["required_row_fields_present"] = all(
        field_presence[field] == len(source_rows) for field in ROW_FIELDS
    )
    labels = [row.get("cntr_tm") for row in source_rows if isinstance(row, dict)]
    valid_labels = [label for label in labels if _parse_label(label) is not None]
    result["first_source_label"] = labels[0] if labels else None
    result["last_source_label"] = labels[-1] if labels else None
    result["source_labels_descending"] = labels == sorted(labels, reverse=True)
    result["duplicate_source_label_count"] = len(valid_labels) - len(set(valid_labels))
    days = sorted({label[:8] for label in valid_labels})
    requested_text = requested_date.strftime("%Y%m%d")
    result["date_coverage"] = {
        "first": days[0] if days else None,
        "last": days[-1] if days else None,
        "requested_row_count": sum(
            label.startswith(requested_text) for label in valid_labels
        ),
    }
    spacing = _spacing_minutes(valid_labels)
    result["spacing_minutes"] = {
        str(key): value for key, value in sorted(Counter(spacing).items())
    }
    result["non_five_minute_spacing"] = sorted(
        {value for value in spacing if value != 5}
    )

    counts = result["signed_price_counts"]
    for row in source_rows:
        if not isinstance(row, dict):
            counts["malformed"] += len(PRICE_FIELDS)
            result["volume_invalid_count"] += 1
            continue
        magnitudes: list[int] = []
        for field in PRICE_FIELDS:
            value = row.get(field)
            if not isinstance(value, str) or SIGNED_INTEGER.fullmatch(value) is None:
                counts["malformed"] += 1
                continue
            if value.startswith("+"):
                counts["positive"] += 1
            elif value.startswith("-"):
                counts["negative"] += 1
            else:
                counts["unsigned"] += 1
            magnitude = _price_magnitude(value)
            if magnitude is None:
                counts["malformed"] += 1
            else:
                magnitudes.append(magnitude)
        if len(magnitudes) == len(PRICE_FIELDS):
            open_price, high_price, low_price, close_price = magnitudes
            if min(magnitudes) == 0:
                result["zero_ohlc_count"] += 1
            if high_price < max(open_price, close_price) or low_price > min(
                open_price, close_price
            ):
                result["ohlc_relationship_invalid_count"] += 1
        volume = row.get("trde_qty")
        if not isinstance(volume, str) or UNSIGNED_INTEGER.fullmatch(volume) is None:
            result["volume_invalid_count"] += 1

    digest = hashlib.sha256(raw_bytes).hexdigest()
    request = MinuteCollectionRequest(
        STOCK_CODE, requested_date, requested_date, MinutePriceBasis.RAW
    )
    try:
        parse_minute_page(raw_bytes, request, source_page=1, artifact_sha256=digest)
    except MinuteValidationError as exc:
        result["parser_validation"] = "FAIL"
        result["parser_issue"] = exc.issue.value
    else:
        result["parser_validation"] = "PASS"
    return result


def _manifest_records(
    manifest_path: Path, requested_date: date
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not manifest_path.exists():
        return records
    try:
        lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    expected = requested_date.strftime("%Y%m%d")
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(record, dict)
            and record.get("stock_code") == STOCK_CODE
            and record.get("base_date") == expected
            and record.get("price_basis") == "RAW"
        ):
            records.append(record)
    return records


def _inspect_date(
    requested_date: date, *, raw_root: Path, manifest_path: Path
) -> dict[str, Any]:
    directory = raw_root / STOCK_CODE / "raw" / requested_date.strftime("%Y%m%d")
    paths = sorted(directory.glob("page-*.json")) if directory.exists() else []
    pages = [inspect_raw_page(path, requested_date) for path in paths]
    records = _manifest_records(manifest_path, requested_date)
    return {
        "date": _iso_day(requested_date),
        "raw_directory": directory.as_posix(),
        "raw_artifact_exists": bool(paths),
        "raw_artifact_count": len(paths),
        "manifest_record_count": len(records),
        "manifest_raw_artifact_present": bool(records),
        "pages": pages,
    }


def _flow_evidence(source_path: Path) -> dict[str, Any]:
    """Capture line-number evidence for the immutable raw-first order."""

    needles = {
        "transport_call": "response = transport(",
        "raw_store": "path, digest = store.store_page(",
        "pagination_parse": "cont_yn, response_key = parse_pagination_headers(",
        "page_parse": "page = parse_minute_page(",
        "transport_exception": "except KiwoomApiError as exc:",
    }
    found: dict[str, int | None] = {key: None for key in needles}
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for number, line in enumerate(lines, start=1):
        for key, needle in needles.items():
            if found[key] is None and needle in line:
                found[key] = number
    order_keys = ("transport_call", "raw_store", "pagination_parse", "page_parse")
    order = [key for key in order_keys if found[key] is not None]
    positions = [found[key] for key in order]
    return {
        "source_path": source_path.as_posix(),
        "line_numbers": found,
        "observed_order": order,
        "raw_store_precedes_validation": bool(
            found["raw_store"] is not None
            and found["page_parse"] is not None
            and found["raw_store"] < found["page_parse"]
        ),
        "transport_error_is_wrapped_before_store": bool(
            found["transport_exception"] is not None
            and found["transport_call"] is not None
            and found["transport_exception"] < found["raw_store"]
            if found["raw_store"] is not None
            else False
        ),
        "line_order_complete": len(positions) == len(order_keys)
        and positions == sorted(positions),
    }


def _success_comparison(
    inspections: list[dict[str, Any]],
    v12_coverage: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = []
    for item in inspections:
        page = item["pages"][0] if item["pages"] else {}
        fields.append(
            {
                "date": item["date"],
                "artifact_available": item["raw_artifact_exists"],
                "page_count": item["raw_artifact_count"],
                "row_count": page.get("row_count", 0),
                "top_level_fields": page.get("top_level_fields", []),
                "required_fields_present": page.get(
                    "required_row_fields_present", False
                ),
                "envelope_valid": page.get("envelope_valid", False),
                "parser_validation": page.get("parser_validation"),
                "first_source_label": page.get("first_source_label"),
                "last_source_label": page.get("last_source_label"),
                "requested_row_count": page.get("date_coverage", {}).get(
                    "requested_row_count", 0
                ),
                "date_coverage": page.get("date_coverage"),
                "spacing_minutes": page.get("spacing_minutes"),
                "non_five_minute_spacing": page.get("non_five_minute_spacing"),
                "duplicate_source_label_count": page.get(
                    "duplicate_source_label_count"
                ),
                "volume_invalid_count": page.get("volume_invalid_count"),
                "ohlc_relationship_invalid_count": page.get(
                    "ohlc_relationship_invalid_count"
                ),
                "pagination_metadata_in_body": page.get("pagination_metadata_in_body"),
                "v12_coverage": {
                    key: v12_coverage.get(item["date"], {}).get(key)
                    for key in (
                        "status",
                        "source",
                        "row_count",
                        "source_quality",
                    )
                },
            }
        )
    return {
        "dates": fields,
        "same_schema": len({tuple(row["top_level_fields"]) for row in fields}) <= 1,
    }


def build_forensics_report(
    *,
    v12_report_path: Path = V12_REPORT_PATH,
    raw_root: Path = MINUTE_ROOT,
    manifest_path: Path | None = None,
    collector_source: Path = COLLECTOR_SOURCE,
) -> dict[str, Any]:
    """Build an offline report from the frozen V1.2 state."""

    report_payload, error = _safe_json(v12_report_path)
    if report_payload is None:
        raise ValueError(f"V1.2 report cannot be read: {error}")
    manifest = manifest_path or raw_root / "manifest" / "requests.jsonl"
    successes = [
        _inspect_date(day, raw_root=raw_root, manifest_path=manifest)
        for day in SUCCESS_DATES
    ]
    failed = _inspect_date(FAILED_DATE, raw_root=raw_root, manifest_path=manifest)
    errors = report_payload.get("acquisition", {}).get("errors", [])
    failure_record = next(
        (
            item
            for item in errors
            if isinstance(item, dict) and item.get("date") == _iso_day(FAILED_DATE)
        ),
        None,
    )
    flow = _flow_evidence(collector_source)
    no_failed_artifact = (
        not failed["raw_artifact_exists"] and not failed["manifest_record_count"]
    )
    likely_pre_capture = no_failed_artifact and failure_record is not None
    failure_stage = (
        "HTTP/API RESPONSE (pre-raw capture boundary; exact subtype unavailable)"
        if likely_pre_capture
        else "UNKNOWN"
    )
    response_received = (
        "UNKNOWN" if likely_pre_capture else bool(failed["raw_artifact_exists"])
    )
    primary = "UNKNOWN"
    secondary = (
        ["API_ERROR_RESPONSE", "RAW_CAPTURE_ORDER_BUG"] if likely_pre_capture else []
    )
    coverage_records = report_payload.get("acquisition", {}).get("coverage", [])
    v12_coverage = {
        str(item.get("date")): item
        for item in coverage_records
        if isinstance(item, dict) and item.get("date")
    }
    report: dict[str, Any] = {
        "audit_version": "MARKET_BAR_200_ACQUISITION_FAILURE_FORENSICS_V1_2A",
        "mode": "OFFLINE_ONLY",
        "network_called": False,
        "new_dates_requested": False,
        "geometry_changed": False,
        "strategy_mma_buy_sell_pnl_changed": False,
        "freeze": {
            "stock_code": STOCK_CODE,
            "successful_dates": [_iso_day(day) for day in SUCCESS_DATES],
            "failed_date": _iso_day(FAILED_DATE),
            "no_acquisition_after": "2026-02-05",
            "v12_report_path": v12_report_path.as_posix(),
        },
        "failure": {
            "reported_error": failure_record,
            "reported_error_type": report_payload.get("acquisition", {}).get(
                "first_failure_type"
            ),
            "stage": failure_stage,
            "known_failure_boundary": "BEFORE_RAW_ARTIFACT_PERSISTENCE"
            if likely_pre_capture
            else "UNKNOWN",
            "most_likely_stage": "HTTP/API RESPONSE"
            if likely_pre_capture
            else "UNKNOWN",
            "stage_confidence": "BOUNDARY_IDENTIFIED_SUBTYPE_UNKNOWN"
            if likely_pre_capture
            else "UNRESOLVED",
            "stage_candidates": [
                "transport/API response failure",
                "raw artifact write failure",
            ],
            "response_bytes_received": response_received,
            "raw_artifact_exists": failed["raw_artifact_exists"],
            "manifest_record_exists": bool(failed["manifest_record_count"]),
            "raw_provenance_classification": "UNKNOWN"
            if likely_pre_capture
            else "RESPONSE_RECEIVED_AND_PRESERVED"
            if failed["raw_artifact_exists"]
            else "UNKNOWN",
            "exception_issue_code_available": False,
            "exception_cause_is_not_recoverable_from_v12_report": True,
        },
        "collector_flow_evidence": flow,
        "raw_first_contract": {
            "implementation_order": "transport -> immutable raw store -> pagination/envelope/row parse",
            "contract_order_is_normal": flow["raw_store_precedes_validation"],
            "failed_request_provenance_verifiable": False,
            "observability_gap": "V1.2 records exception type but not MinuteValidationError.issue or failure stage",
        },
        "failure_payload_analysis": {
            "performed": False,
            "reason": "No 2026-02-04 raw artifact or manifest record exists; no payload was fabricated",
            "failed_date": failed,
        },
        "success_comparison": _success_comparison(successes, v12_coverage),
        "date_comparison": {
            "successes": successes,
            "failure": failed,
            "success_regression_pass": all(
                item["raw_artifact_exists"]
                and item["manifest_record_count"] > 0
                and item["pages"]
                and item["pages"][0]["parser_validation"] == "PASS"
                for item in successes
            ),
        },
        "classification": {
            "primary": primary,
            "secondary_candidates": secondary,
            "retention_judgment": "UNPROVEN",
            "reason": "Absent artifact is insufficient to distinguish transport/API failure from a pre-validation capture failure; V1.2 discarded the typed issue",
            "source_quality": "UNDETERMINED",
        },
        "required_fix": {
            "needed": True,
            "scope": "observability only",
            "action": "Preserve a safe typed issue/stage code in failure metadata before any controlled retry",
            "parser_semantics_change": False,
            "geometry_change": False,
            "silent_relaxation": False,
        },
        "controlled_retry": {
            "performed": False,
            "count": 0,
            "network": False,
            "reason": "V1.2A is offline-only; no retry was attempted",
        },
        "failed_date_usability": "UNDETERMINED_NOT_USABLE",
        "resume_gate": {
            "failure_primary_identified": False,
            "raw_provenance_safe": False,
            "parser_validator_justified": True,
            "failed_date_usable_determined": False,
            "success_regression_pass": all(
                item["raw_artifact_exists"]
                and item["pages"]
                and item["pages"][0]["parser_validation"] == "PASS"
                for item in successes
            ),
            "resume_200_bar_acquisition": "BLOCKED",
        },
        "checkpoint_recommendation": "DO_NOT_CHECKPOINT_YET",
        "hypotheses": {
            "H1_failure_boundary_identifiable": "PARTIALLY_SUPPORTED",
            "H2_retention_vs_api_source_distinguishable": "INCONCLUSIVE",
            "H3_returned_response_would_be_preserved_before_parse": "SUPPORTED_BY_CODE_PATH",
            "H4_fix_without_geometry_change": "INCONCLUSIVE",
        },
        "security": {
            "credentials_included": False,
            "tokens_included": False,
            "account_numbers_included": False,
            "response_bytes_included": False,
            "content_hashes_included": False,
        },
    }
    return report


def run_forensics(
    *,
    output_path: Path = OUTPUT_PATH,
    v12_report_path: Path = V12_REPORT_PATH,
    raw_root: Path = MINUTE_ROOT,
    manifest_path: Path | None = None,
    collector_source: Path = COLLECTOR_SOURCE,
) -> dict[str, Any]:
    report = build_forensics_report(
        v12_report_path=v12_report_path,
        raw_root=raw_root,
        manifest_path=manifest_path,
        collector_source=collector_source,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v12-report", type=Path, default=V12_REPORT_PATH)
    parser.add_argument("--raw-root", type=Path, default=MINUTE_ROOT)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--collector-source", type=Path, default=COLLECTOR_SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = run_forensics(
        output_path=args.output,
        v12_report_path=args.v12_report,
        raw_root=args.raw_root,
        manifest_path=args.manifest,
        collector_source=args.collector_source,
    )
    print(
        json.dumps(
            {
                "failure_stage": report["failure"]["stage"],
                "primary_classification": report["classification"]["primary"],
                "success_regression_pass": report["date_comparison"][
                    "success_regression_pass"
                ],
                "failed_raw_artifact_exists": report["failure"]["raw_artifact_exists"],
                "controlled_retry_count": report["controlled_retry"]["count"],
                "resume_200_bar_acquisition": report["resume_gate"][
                    "resume_200_bar_acquisition"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
