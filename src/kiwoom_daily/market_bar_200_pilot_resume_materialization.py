"""Coverage-aware V1.2C resume and frozen 200-bar materialization.

The V1.1 date list remains the source of truth, but a preserved ka10080 page
can cover more than the date in its request directory.  This module indexes
those immutable pages first, requests only genuinely missing planned dates,
and materializes the unchanged V0.6 geometry only after coverage is complete.
No MMA, strategy, order, or PnL calculation is performed here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.kiwoom_minute.pipeline import (
    KiwoomMinuteStore,
    MinuteCollectionRequest,
    MinutePriceBasis,
    MinuteValidationError,
    ParsedMinuteRow,
    collect_minute_series,
    parse_minute_page,
)
from src.kiwoom_rest.auth import (
    ConfigurationError,
    KiwoomApiError,
    issue_demo_token,
    load_demo_config,
)

from .down_box_daily_execution_proof import _load_stock
from .market_bar_200_pilot_acquisition import (
    TARGET,
    V11_CHECKPOINT,
    V11_PLAN_PATH,
    _density_distribution,
    _load_plan,
    _quality_for_rows,
    _session_mapping,
)
from .market_bar_pilot_acquisition import (
    MINUTE_ROOT,
    _build_segments,
    _materialize,
)
from .market_bar_pilot_source_acquisition_plan import _session_inventory

V12B_CHECKPOINT = "9e772dd"
V12A_FORENSICS_PATH = Path(
    "data/processed/strategy_review/market_bar_200_acquisition_failure_forensics_v1_2a.json"
)
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_200_pilot_resume_materialization_v1_2c.json"
)
ANCHOR_STOCK = "066570"
WINDOW_START = date(2026, 2, 2)
WINDOW_END = date(2026, 5, 28)
RETRY_DATES = {date(2026, 2, 2), date(2026, 2, 3), date(2026, 2, 4)}

STATUS_PRIORITY = {
    "ALREADY_CACHED_BEFORE_V1_2": 1,
    "FETCHED_V1_2": 2,
    "FETCHED_V1_2B_RETRY": 3,
    "FETCHED_V1_2C": 4,
}


class ResumeMaterializationError(ValueError):
    """The frozen V1.2C coverage/materialization contract cannot be met."""


def _artifact_status(path: Path, planned_dates: set[date]) -> str:
    try:
        base = date.fromisoformat(path.parent.name)
    except ValueError:
        return "ALREADY_CACHED_BEFORE_V1_2"
    if base == date(2026, 2, 4):
        return "FETCHED_V1_2B_RETRY"
    if base in RETRY_DATES:
        return "FETCHED_V1_2"
    if base in planned_dates:
        return "FETCHED_V1_2C"
    return "ALREADY_CACHED_BEFORE_V1_2"


def _redacted_page_path(path: Path) -> str:
    """Return a page ordinal path without the digest in the filename."""

    name = path.name
    if name.startswith("page-") and "-" in name[5:]:
        ordinal = name[5:].split("-", 1)[0]
        return (path.parent / f"page-{ordinal}.json").as_posix()
    return (path.parent / "<page>.json").as_posix()


def _page_record(
    path: Path,
    *,
    request: MinuteCollectionRequest,
    status: str,
    source_page: int,
) -> tuple[dict[str, Any], tuple[ParsedMinuteRow, ...] | None]:
    """Parse a preserved page and return report-safe metadata plus rows."""

    record: dict[str, Any] = {
        "path": _redacted_page_path(path),
        "base_date": path.parent.name,
        "status": status,
        "raw_artifact_exists": path.exists(),
        "byte_count": None,
        "row_count": 0,
        "parsed": False,
        "first_source_label": None,
        "last_source_label": None,
        "covered_dates_first": None,
        "covered_dates_last": None,
        "issue_code": None,
    }
    try:
        raw = path.read_bytes()
    except OSError as exc:
        record["issue_code"] = type(exc).__name__
        return record, None
    record["byte_count"] = len(raw)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        page = parse_minute_page(
            raw,
            request,
            source_page=source_page,
            artifact_sha256=digest,
        )
    except MinuteValidationError as exc:
        record["issue_code"] = exc.issue.value
        return record, None
    record["parsed"] = True
    record["row_count"] = len(page.rows)
    if page.rows:
        labels = [row.source_label for row in page.rows]
        record["first_source_label"] = labels[0]
        record["last_source_label"] = labels[-1]
        days = sorted({row.trading_date.isoformat() for row in page.rows})
        record["covered_dates_first"] = days[0]
        record["covered_dates_last"] = days[-1]
    return record, page.rows


def _load_coverage(
    *,
    raw_root: Path,
    stock_code: str,
    window_dates: list[str],
    required_fast_dates: set[date],
) -> tuple[
    dict[date, tuple[ParsedMinuteRow, ...]],
    list[dict[str, Any]],
    dict[date, list[dict[str, Any]]],
    list[dict[str, Any]],
    set[date],
]:
    """Index all local RAW pages, deduplicating exact overlapping labels."""

    planned_dates = {date.fromisoformat(day) for day in window_dates}
    request = MinuteCollectionRequest(
        stock_code,
        min(planned_dates),
        max(planned_dates),
        MinutePriceBasis.RAW,
    )
    page_paths = sorted((raw_root / stock_code / "raw").glob("**/page-*.json"))
    labels: dict[str, tuple[ParsedMinuteRow, str, int]] = {}
    label_sources: dict[str, list[dict[str, Any]]] = defaultdict(list)
    page_records: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    returned_dates: set[date] = set()
    for source_page, path in enumerate(page_paths, start=1):
        status = _artifact_status(path, planned_dates)
        record, rows = _page_record(
            path,
            request=request,
            status=status,
            source_page=source_page,
        )
        page_records.append(record)
        if rows is None:
            continue
        returned_dates.update(row.trading_date for row in rows)
        for row in rows:
            if row.trading_date not in required_fast_dates:
                continue
            label_sources[row.source_label].append(
                {"status": status, "path": _redacted_page_path(path)}
            )
            current = labels.get(row.source_label)
            if current is None:
                labels[row.source_label] = (row, status, source_page)
                continue
            old_row, old_status, old_page = current
            same_source_value = (
                old_row.raw == row.raw
                and old_row.source_price_text == row.source_price_text
            )
            if not same_source_value:
                conflicts.append(
                    {
                        "source_label": row.source_label,
                        "trading_date": row.trading_date.isoformat(),
                        "existing_status": old_status,
                        "incoming_status": status,
                        "existing_page": old_page,
                        "incoming_page": source_page,
                    }
                )
                continue
            if STATUS_PRIORITY.get(status, 0) > STATUS_PRIORITY.get(old_status, 0):
                labels[row.source_label] = (row, status, source_page)

    rows_by_date: dict[date, list[ParsedMinuteRow]] = defaultdict(list)
    for row, _, _ in labels.values():
        rows_by_date[row.trading_date].append(row)
    ordered_rows = {
        day: tuple(sorted(rows, key=lambda row: row.source_label))
        for day, rows in rows_by_date.items()
    }
    return ordered_rows, page_records, label_sources, conflicts, returned_dates


def _coverage_rows(
    *,
    target_dates: list[str],
    required_fast_dates: set[date],
    rows_by_date: dict[date, tuple[ParsedMinuteRow, ...]],
    page_records: list[dict[str, Any]],
    label_sources: dict[date, list[dict[str, Any]]] | dict[str, list[dict[str, Any]]],
    conflicts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], int, int]:
    """Build deterministic per-date coverage and status counts."""

    conflict_dates = {item["trading_date"] for item in conflicts}
    coverage: list[dict[str, Any]] = []
    counts = {
        "ALREADY_CACHED_BEFORE_V1_2": 0,
        "FETCHED_V1_2": 0,
        "FETCHED_V1_2B_RETRY": 0,
        "FETCHED_V1_2C": 0,
        "STILL_MISSING": 0,
        "SOURCE_INVALID": 0,
    }
    overlap_count = 0
    anomaly_count = 0
    path_records = [record for record in page_records if record["parsed"]]
    for text_day in target_dates:
        day = date.fromisoformat(text_day)
        rows = rows_by_date.get(day, ())
        date_overlap = False
        if day.isoformat() in conflict_dates:
            status = "SOURCE_INVALID"
        elif day not in required_fast_dates:
            continue
        elif not rows:
            status = "STILL_MISSING"
        else:
            candidates = []
            for record in path_records:
                first = record.get("covered_dates_first")
                last = record.get("covered_dates_last")
                if first and last and first <= text_day <= last:
                    candidates.append(record)
            # Report the frozen request provenance for a planned day when an
            # exact request-date artifact exists.  A later page can overlap
            # that day and still be used for label de-duplication, but must
            # not relabel the original V1.2/V1.2B acquisition state.
            exact_candidates = [
                record
                for record in candidates
                if record["base_date"] == day.strftime("%Y%m%d")
            ]
            status_candidates = exact_candidates or candidates
            status = max(
                (record["status"] for record in status_candidates),
                key=lambda value: STATUS_PRIORITY.get(value, 0),
                default="ALREADY_CACHED_BEFORE_V1_2",
            )
            date_overlap = any(
                record["base_date"] != day.strftime("%Y%m%d") for record in candidates
            )
            if date_overlap:
                overlap_count += 1
        quality_valid, quality = _quality_for_rows(rows)
        if (
            rows
            and quality
            and (quality["non_5m_spacing_minutes"] or quality["opening_label_missing"])
        ):
            anomaly_count += 1
        if status not in counts:
            status = "SOURCE_INVALID"
        counts[status] += 1
        coverage.append(
            {
                "date": text_day,
                "status": status,
                "row_count": len(rows),
                "parsed": bool(rows),
                "usable": bool(rows) and quality_valid and status != "SOURCE_INVALID",
                "source_quality": quality,
                "overlap_covered": bool(rows)
                and status not in {"STILL_MISSING", "SOURCE_INVALID"}
                and date_overlap,
                "conflict": day.isoformat() in conflict_dates,
            }
        )
    return coverage, counts, overlap_count, anomaly_count


def _fetch_remaining(
    *,
    missing_dates: list[date],
    raw_root: Path,
    max_pages: int,
    page_delay: float,
) -> tuple[list[dict[str, Any]], int, int]:
    """Fetch missing dates in order, stopping at the first typed failure."""

    if not missing_dates:
        return [], 0, 0
    try:
        config = load_demo_config()
        token = issue_demo_token(config)
    except (ConfigurationError, KiwoomApiError, OSError) as exc:
        return (
            [
                {
                    "stock_code": ANCHOR_STOCK,
                    "requested_date": missing_dates[0].isoformat(),
                    "request_sequence": 0,
                    "attempt_number": 1,
                    "failure_stage": "PRE_REQUEST",
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
                    "outcome": "FAILED",
                    "safe_exception_type": type(exc).__name__,
                }
            ],
            0,
            0,
        )
    store = KiwoomMinuteStore(raw_root)
    diagnostics: list[dict[str, Any]] = []
    successful_dates = 0
    request_count = 0
    for day in sorted(missing_dates):
        diagnostic: dict[str, object] = {}
        request = MinuteCollectionRequest(ANCHOR_STOCK, day, day, MinutePriceBasis.RAW)
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
            diagnostic.setdefault("requested_date", day.isoformat())
            diagnostic.setdefault("request_sequence", 0)
            diagnostic.setdefault("attempt_number", 1)
            diagnostic.setdefault("failure_stage", "PRE_REQUEST")
            diagnostic.setdefault("minute_validation_issue_code", None)
            diagnostic.setdefault("transport_completed", False)
            diagnostic.setdefault("response_object_available", False)
            diagnostic.setdefault("response_bytes_available_to_pipeline", False)
            diagnostic.setdefault("raw_persistence_started", False)
            diagnostic.setdefault("raw_persistence_completed", False)
            diagnostic.setdefault("parser_started", False)
            diagnostic.setdefault("parser_completed", False)
            diagnostic.setdefault("pagination_started", False)
            diagnostic.setdefault("pagination_completed", False)
            diagnostic.setdefault("outcome", "FAILED")
            diagnostic["safe_exception_type"] = type(exc).__name__
            diagnostics.append(dict(diagnostic))
            break
        diagnostic["attempt_number"] = 1
        diagnostics.append(dict(diagnostic))
        successful_dates += 1
        request_count += len(collected.pages)
    return diagnostics, request_count, successful_dates


def _all_required_usable(
    required_fast_dates: set[date],
    rows_by_date: dict[date, tuple[ParsedMinuteRow, ...]],
    conflicts: list[dict[str, Any]],
) -> bool:
    return not conflicts and all(
        day in rows_by_date and rows_by_date[day] for day in required_fast_dates
    )


def build_report(
    *,
    plan_path: Path = V11_PLAN_PATH,
    raw_root: Path = MINUTE_ROOT,
    output_path: Path = OUTPUT_PATH,
    allow_network: bool = False,
    max_pages: int = 40,
    page_delay: float = 1.1,
) -> dict[str, Any]:
    plan = _load_plan(plan_path)
    candidate = plan["preferred_candidate_200"]
    if (
        candidate["stock_code"] != ANCHOR_STOCK
        or candidate["calendar_start"] != WINDOW_START.isoformat()
        or candidate["calendar_end"] != WINDOW_END.isoformat()
        or int(candidate["target_market_bars"]) != TARGET
    ):
        raise ResumeMaterializationError("V1.1 preferred candidate is not frozen")
    v01_path = Path(
        "data/processed/strategy_review/market_bar_construction_proof_v0_1.json"
    )
    v01 = json.loads(v01_path.read_text(encoding="utf-8"))
    sessions_all, _ = _session_inventory(v01)
    inventory = sessions_all[ANCHOR_STOCK]
    window_dates = sorted(
        day
        for day in inventory
        if WINDOW_START <= date.fromisoformat(day) <= WINDOW_END
    )
    required_fast_dates = {
        date.fromisoformat(day)
        for day in window_dates
        if str(inventory[day]["daily_tau"])
        and Decimal(str(inventory[day]["daily_tau"])) > Decimal(1)
    }
    planned_dates = [str(day) for day in candidate["repairable_session_dates"]]
    (
        rows_by_date,
        page_records,
        label_sources,
        conflicts,
        returned_dates,
    ) = _load_coverage(
        raw_root=raw_root,
        stock_code=ANCHOR_STOCK,
        window_dates=planned_dates,
        required_fast_dates=required_fast_dates,
    )
    coverage, status_counts, overlap_count, quality_anomaly_count = _coverage_rows(
        target_dates=planned_dates,
        required_fast_dates=required_fast_dates,
        rows_by_date=rows_by_date,
        page_records=page_records,
        label_sources=label_sources,
        conflicts=conflicts,
    )
    still_missing_dates = sorted(
        day for day in required_fast_dates if day not in rows_by_date
    )
    network_enabled = False
    retry_diagnostics: list[dict[str, Any]] = []
    request_count = 0
    v12c_successful = 0
    if still_missing_dates and allow_network:
        network_enabled = True
        retry_diagnostics, request_count, v12c_successful = _fetch_remaining(
            missing_dates=still_missing_dates,
            raw_root=raw_root,
            max_pages=max_pages,
            page_delay=page_delay,
        )
        (
            rows_by_date,
            page_records,
            label_sources,
            conflicts,
            returned_dates,
        ) = _load_coverage(
            raw_root=raw_root,
            stock_code=ANCHOR_STOCK,
            window_dates=planned_dates,
            required_fast_dates=required_fast_dates,
        )
        coverage, status_counts, overlap_count, quality_anomaly_count = _coverage_rows(
            target_dates=planned_dates,
            required_fast_dates=required_fast_dates,
            rows_by_date=rows_by_date,
            page_records=page_records,
            label_sources=label_sources,
            conflicts=conflicts,
        )
        still_missing_dates = sorted(
            day for day in required_fast_dates if day not in rows_by_date
        )
    coverage_complete = _all_required_usable(
        required_fast_dates, rows_by_date, conflicts
    )
    materialized: dict[str, Any] | None = None
    materialization_error: str | None = None
    if coverage_complete:
        daily_bars = tuple(
            sorted(_load_stock(ANCHOR_STOCK)[0], key=lambda bar: bar.trade_date)
        )
        try:
            segments = _build_segments(
                stock_code=ANCHOR_STOCK,
                window_dates=window_dates,
                inventory=inventory,
                daily_bars=daily_bars,
                raw_by_date=rows_by_date,
            )
            materialized = _materialize(segments, ANCHOR_STOCK)
        except (MinuteValidationError, ValueError, ResumeMaterializationError) as exc:
            materialization_error = type(exc).__name__
    bars = materialized["market_bars"] if materialized else []
    mapping, examples = _session_mapping(
        window_dates=window_dates,
        inventory=inventory,
        bars=bars,
    )
    status_counts["SOURCE_INVALID"] = max(
        status_counts["SOURCE_INVALID"],
        len({item["trading_date"] for item in conflicts}),
    )
    completed = bool(
        materialized
        and materialized["island_count"] == 1
        and len(bars) >= TARGET
        and materialized["skip_count"] == 0
        and materialized["duplicate_target_count"] == 0
        and materialized["unresolved_source_count"] == 0
        and materialized["source_ohlc_exact"]
        and materialized["source_volume_exact"]
    )
    report: dict[str, Any] = {
        "audit_version": "MARKET_BAR_200_PILOT_RESUME_MATERIALIZATION_V1_2C",
        "mode": "NETWORK_ENABLED_ONLY_WHEN_STILL_MISSING_AND_EXPLICITLY_ALLOWED"
        if allow_network
        else "OFFLINE_COVERAGE_SCAN",
        "network_called": network_enabled,
        "checkpoint": {
            "v11": V11_CHECKPOINT,
            "v12b": V12B_CHECKPOINT,
            "v12a_forensics": str(V12A_FORENSICS_PATH),
        },
        "plan_freeze": {
            "stock_code": ANCHOR_STOCK,
            "calendar_start": WINDOW_START.isoformat(),
            "calendar_end": WINDOW_END.isoformat(),
            "target_market_bars": TARGET,
            "expected_market_bar_capacity": int(
                candidate["expected_market_bar_capacity"]
            ),
            "exact_v11_fetch_dates_source_of_truth": True,
            "manual_date_selection": False,
            "automatic_candidate_switch": False,
        },
        "contract": {
            "global_activity_tau": True,
            "integer_target_lattice": True,
            "one_target_one_market_bar": True,
            "first_actual_source_endpoint_after_crossing": True,
            "multi_target_source_resolution_insufficient": True,
            "unresolved_gap_continuity": False,
            "fractional_source_split": False,
            "interpolation": False,
            "synthetic_market_bar": False,
            "source_ohlc_exact_only": True,
            "source_volume_exact_only": True,
            "tau_formula_changed": False,
            "mma": False,
            "strategy": False,
            "buy_sell": False,
            "pnl": False,
        },
        "coverage": {
            "required_fast_total": len(required_fast_dates),
            "v11_planned_new_fetch_total": len(planned_dates),
            "status_counts": status_counts,
            "still_missing_count": len(still_missing_dates),
            "still_missing_dates": [day.isoformat() for day in still_missing_dates],
            "source_invalid_count": len(conflicts),
            "source_quality_anomaly_count": quality_anomaly_count,
            "planned_dates_covered_by_overlap_count": overlap_count,
            "planned_dates": coverage,
            "complete": coverage_complete,
        },
        "acquisition_efficiency": {
            "v11_planned_new_fetch_dates": len(planned_dates),
            "already_covered_before_v1_2": status_counts["ALREADY_CACHED_BEFORE_V1_2"],
            "v12_acquired": status_counts["FETCHED_V1_2"],
            "v12b_retry_acquired": status_counts["FETCHED_V1_2B_RETRY"],
            "v12c_actual_api_request_count": request_count,
            "v12c_successful_request_count": v12c_successful,
            "v12c_retry_count": 0,
            "new_raw_artifact_count": sum(
                record["status"] == "FETCHED_V1_2C" for record in page_records
            ),
            "planned_dates_newly_covered_by_v12c": status_counts["FETCHED_V1_2C"],
            "extra_non_planned_returned_date_count": len(
                returned_dates - {date.fromisoformat(day) for day in planned_dates}
            ),
            "extra_non_planned_returned_dates": sorted(
                day.isoformat()
                for day in returned_dates
                - {date.fromisoformat(day) for day in planned_dates}
            ),
            "unused_extra_sessions": True,
        },
        "attempt_diagnostics": retry_diagnostics,
        "raw_provenance": {
            "artifact_count": len(page_records),
            "parsed_artifact_count": sum(record["parsed"] for record in page_records),
            "failed_artifact_count": sum(
                not record["parsed"] for record in page_records
            ),
            "pages": page_records,
            "content_hashes_in_report": False,
            "credentials_in_report": False,
        },
        "materialization": {
            "started": coverage_complete,
            "success": completed,
            "resolved_island_count": materialized["island_count"]
            if materialized
            else 0,
            "market_bar_count": len(bars),
            "expected_market_bar_capacity": int(
                candidate["expected_market_bar_capacity"]
            ),
            "expected_actual_difference": len(bars)
            - int(candidate["expected_market_bar_capacity"]),
            "unresolved_internal_gap_count": materialized["unresolved_source_count"]
            if materialized
            else None,
            "skip_count": materialized["skip_count"] if materialized else None,
            "duplicate_target_count": materialized["duplicate_target_count"]
            if materialized
            else None,
            "source_ohlc_exact": materialized["source_ohlc_exact"]
            if materialized
            else None,
            "source_volume_exact": materialized["source_volume_exact"]
            if materialized
            else None,
            "fractional_source_split_count": 0,
            "interpolation_count": 0,
            "synthetic_bar_count": 0,
            "error": materialization_error,
            "quality": materialized["quality"] if materialized else None,
            "market_bars": bars,
        },
        "tau_quality": materialized["quality"] if materialized else None,
        "calendar_market_bar_mapping": mapping,
        "calendar_market_bar_density_distribution": _density_distribution(mapping),
        "fast_normal_slow_examples": examples,
        "result": "MARKET_BAR_200_STREAM_READY"
        if completed
        else "INSUFFICIENT_ACTUAL_MARKET_BARS",
        "resume_gate": {
            "coverage_complete": coverage_complete,
            "source_invalid_zero": not conflicts,
            "materialization_success": completed,
            "network_phase_closed": coverage_complete,
            "next_phase": "V1.3_MARKET_BAR_MMA_ROLE_STABILITY_AUDIT"
            if completed
            else None,
        },
        "hypotheses": {
            "H1_targeted_acquisition_reaches_200_plus": "SUPPORTED"
            if completed
            else "INCONCLUSIVE",
            "H2_v06_geometry_unchanged": "SUPPORTED" if completed else "INCONCLUSIVE",
            "H3_coverage_aware_overlap_reduces_requests": "SUPPORTED"
            if overlap_count > 0
            else "INCONCLUSIVE",
            "H4_source_resolution_is_not_final_timeframe": "SUPPORTED",
        },
        "strategy_ma_pnl_changed": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=V11_PLAN_PATH)
    parser.add_argument("--raw-root", type=Path, default=MINUTE_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--max-pages", type=int, default=40)
    parser.add_argument("--page-delay", type=float, default=1.1)
    args = parser.parse_args()
    report = build_report(
        plan_path=args.plan,
        raw_root=args.raw_root,
        output_path=args.output,
        allow_network=args.allow_network,
        max_pages=args.max_pages,
        page_delay=args.page_delay,
    )
    print(
        json.dumps(
            {
                "result": report["result"],
                "network_called": report["network_called"],
                "coverage": report["coverage"],
                "materialization": {
                    key: report["materialization"][key]
                    for key in (
                        "started",
                        "success",
                        "resolved_island_count",
                        "market_bar_count",
                        "expected_market_bar_capacity",
                    )
                },
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
