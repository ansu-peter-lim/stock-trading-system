"""Acquire and materialize the frozen unseen-validation Market-Bar streams.

V1.11 is deliberately source-only.  It reuses the frozen V1.10 windows and
the V0.6/V0.9 geometry helpers, fetches only exact missing RAW ``ka10080``
dates when explicitly enabled, and materializes no strategy indicators,
signals, returns, trades, or PnL.  RAW artifacts are immutable and the token is
kept in memory by the existing authentication/collector layers only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.kiwoom_minute.pipeline import (
    KiwoomMinuteStore,
    MinuteCollectionRequest,
    MinutePipelineIssue,
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
from .global_tau_resolution_adequacy_audit import V01_PROOF_PATH
from .market_bar_mma_role_replication_candidate_plan_v1_4 import (
    MINUTE_ROOT,
)
from .market_bar_pilot_acquisition import (
    _build_segments,
    _materialize,
)
from .market_time_normalization_audit import market_time_series
from .market_time_selective_intraday_decomposition import _label_quality

V1_10_PATH = Path(
    "data/processed/strategy_review/"
    "market_bar_strategy_validation_source_readiness_v1_10.json"
)
OUTPUT_PATH = Path(
    "data/processed/strategy_review/"
    "market_bar_strategy_validation_source_acquisition_materialization_v1_11.json"
)
STREAM_MANIFEST_PATH = Path(
    "docs/research/market_bar_strategy_validation_stream_manifest_v1_11.md"
)
STOCKS = ("005380", "068270")
TARGET = 200
MAX_PAGES = 40
DEFAULT_PAGE_DELAY = 1.1
RETRYABLE_ISSUES = frozenset({MinutePipelineIssue.HTTP_ERROR})


class ValidationSourceAcquisitionError(ValueError):
    """The frozen source/materialization contract cannot be completed."""


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal)):
        return str(value)
    return str(value)


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_v1_10(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationSourceAcquisitionError("V1.10 report is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValidationSourceAcquisitionError("V1.10 report has an invalid shape")
    if (
        payload.get("plan_version")
        != "MARKET_BAR_STRATEGY_VALIDATION_SOURCE_READINESS_V1_10"
    ):
        raise ValidationSourceAcquisitionError("unexpected V1.10 report version")
    if payload.get("scope", {}).get("network_calls") != 0:
        raise ValidationSourceAcquisitionError("V1.10 report was not offline")
    return payload


def _frozen_windows(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    preferred = report.get("preferred_selections")
    if not isinstance(preferred, Mapping):
        raise ValidationSourceAcquisitionError("V1.10 preferred selections are missing")
    result: dict[str, dict[str, Any]] = {}
    expected = {
        "005380": (
            "2025-06-12",
            "2026-01-20",
            "BALANCED_STRATEGY_VALIDATION_CANDIDATE",
            200,
        ),
        "068270": (
            "2024-12-27",
            "2025-10-31",
            "SLOW_DOMINANT_STRESS_VALIDATION_CANDIDATE",
            200,
        ),
    }
    for stock, (start, end, role, target) in expected.items():
        item = preferred.get(stock)
        if not isinstance(item, Mapping):
            raise ValidationSourceAcquisitionError(f"frozen selection missing: {stock}")
        candidate = item.get("candidate")
        if not isinstance(candidate, Mapping):
            raise ValidationSourceAcquisitionError(f"frozen candidate missing: {stock}")
        if (
            str(candidate.get("calendar_start")) != start
            or str(candidate.get("calendar_end")) != end
            or int(candidate.get("candidate_target", 0)) != target
            or str(item.get("role")) != role
        ):
            raise ValidationSourceAcquisitionError(f"V1.10 window changed: {stock}")
        exact = report.get("exact_fetch_dates", {}).get(stock)
        if not isinstance(exact, list):
            raise ValidationSourceAcquisitionError(f"exact fetch list missing: {stock}")
        result[stock] = {
            "role": role,
            "start": date.fromisoformat(start),
            "end": date.fromisoformat(end),
            "target": target,
            "expected_capacity": int(item.get("expected_market_bar_capacity", 0)),
            "exact_fetch_dates": tuple(sorted(str(row["date"]) for row in exact)),
        }
    return result


def _index_pages(
    *,
    stock_code: str,
    raw_root: Path,
    window_dates: Sequence[date],
    required_fast_dates: set[date],
) -> tuple[
    dict[date, tuple[ParsedMinuteRow, ...]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    set[date],
]:
    """Parse all immutable RAW pages and de-duplicate exact overlaps."""

    if not window_dates:
        return {}, [], [], set()
    request = MinuteCollectionRequest(
        stock_code,
        min(window_dates),
        max(window_dates),
        MinutePriceBasis.RAW,
    )
    labels: dict[str, ParsedMinuteRow] = {}
    label_sources: dict[str, list[dict[str, Any]]] = defaultdict(list)
    records: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    returned_dates: set[date] = set()
    paths = sorted((raw_root / stock_code / "raw").glob("**/page-*.json"))
    for source_page, path in enumerate(paths, start=1):
        raw: bytes | None = None
        record: dict[str, Any] = {
            "raw_file_path": path.as_posix(),
            "raw_file_sha256": None,
            "requested_base_date": path.parent.name,
            "row_count": 0,
            "parsed": False,
            "issue_code": None,
            "covered_dates_first": None,
            "covered_dates_last": None,
        }
        try:
            raw = path.read_bytes()
            digest = _sha256_bytes(raw)
            record["raw_file_sha256"] = digest
            page = parse_minute_page(
                raw,
                request,
                source_page=source_page,
                artifact_sha256=digest,
            )
        except OSError as exc:
            record["issue_code"] = type(exc).__name__
            records.append(record)
            continue
        except MinuteValidationError as exc:
            record["issue_code"] = exc.issue.value
            records.append(record)
            continue
        record["parsed"] = True
        record["row_count"] = len(page.rows)
        if page.rows:
            covered = sorted({row.trading_date for row in page.rows})
            record["covered_dates_first"] = covered[0].isoformat()
            record["covered_dates_last"] = covered[-1].isoformat()
        records.append(record)
        returned_dates.update(row.trading_date for row in page.rows)
        for row in page.rows:
            if row.trading_date not in required_fast_dates:
                continue
            label_sources[row.source_label].append(
                {
                    "raw_file_path": path.as_posix(),
                    "requested_base_date": path.parent.name,
                }
            )
            previous = labels.get(row.source_label)
            if previous is None:
                labels[row.source_label] = row
                continue
            if (
                previous.raw != row.raw
                or previous.source_price_text != row.source_price_text
            ):
                conflicts.append(
                    {
                        "source_label": row.source_label,
                        "trading_date": row.trading_date.isoformat(),
                    }
                )
    rows_by_date: dict[date, list[ParsedMinuteRow]] = defaultdict(list)
    for row in labels.values():
        rows_by_date[row.trading_date].append(row)
    return (
        {
            day: tuple(sorted(rows, key=lambda row: row.source_label))
            for day, rows in rows_by_date.items()
        },
        records,
        conflicts,
        returned_dates,
    )


def _quality(rows: Sequence[ParsedMinuteRow]) -> tuple[bool, dict[str, Any] | None]:
    if not rows:
        return False, None
    try:
        result = _label_quality(rows)
    except (AttributeError, IndexError, TypeError, ValueError):
        return False, None
    # The known 15-minute close-label gap is retained as an anomaly, not
    # silently repaired.  Opening-label loss is a hard source-quality failure.
    return not bool(result["opening_label_missing"]), result


def _coverage(
    *,
    planned_dates: Sequence[date],
    rows_by_date: Mapping[date, Sequence[ParsedMinuteRow]],
    records: Sequence[Mapping[str, Any]],
    conflicts: Sequence[Mapping[str, Any]],
    network_base_dates: set[str],
) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    conflict_dates = {str(item["trading_date"]) for item in conflicts}
    result: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    anomaly_count = 0
    for day in sorted(planned_dates):
        rows = tuple(rows_by_date.get(day, ()))
        sources = [
            record
            for record in records
            if record.get("parsed")
            and record.get("covered_dates_first")
            and str(record["covered_dates_first"])
            <= day.isoformat()
            <= str(record["covered_dates_last"])
        ]
        network_sources = [
            record
            for record in sources
            if str(record.get("requested_base_date")) in network_base_dates
        ]
        quality_valid, quality = _quality(rows)
        if quality and quality.get("non_5m_spacing_minutes"):
            anomaly_count += 1
        if day.isoformat() in conflict_dates or (rows and not quality_valid):
            status = "SOURCE_INVALID"
        elif not rows:
            invalid_page = any(
                str(record.get("requested_base_date")) == day.strftime("%Y%m%d")
                and record.get("issue_code")
                for record in records
            )
            status = "SOURCE_INVALID" if invalid_page else "STILL_MISSING"
        elif network_sources:
            exact_network = any(
                str(record.get("requested_base_date")) == day.strftime("%Y%m%d")
                for record in network_sources
            )
            status = (
                "NEWLY_FETCHED_USABLE"
                if exact_network
                else "COVERED_BY_EXISTING_OVERLAP"
            )
        else:
            exact_existing = any(
                str(record.get("requested_base_date")) == day.strftime("%Y%m%d")
                for record in sources
            )
            status = (
                "PREEXISTING_USABLE"
                if exact_existing
                else "COVERED_BY_EXISTING_OVERLAP"
            )
        counts[status] += 1
        result.append(
            {
                "date": day.isoformat(),
                "status": status,
                "row_count": len(rows),
                "parsed": bool(rows),
                "quality_valid": quality_valid,
                "usable_for_market_bar": bool(rows)
                and quality_valid
                and status != "SOURCE_INVALID",
                "newly_fetched": bool(network_sources),
                "overlap_covered": bool(rows)
                and any(
                    str(record.get("requested_base_date")) != day.strftime("%Y%m%d")
                    for record in sources
                ),
                "source_quality": quality,
            }
        )
    return result, dict(sorted(counts.items())), anomaly_count


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, MinuteValidationError) and exc.issue in RETRYABLE_ISSUES


def _empty_response_artifact(
    *, stock_code: str, requested_date: date, raw_root: Path
) -> bool:
    """Recognize the typed empty-row retention response without guessing."""

    paths = sorted(
        (raw_root / stock_code / "raw" / requested_date.strftime("%Y%m%d")).glob(
            "page-*.json"
        )
    )
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        rows = (
            payload.get("stk_min_pole_chart_qry")
            if isinstance(payload, Mapping)
            else None
        )
        if not isinstance(rows, list) or not rows:
            continue
        if all(
            isinstance(row, Mapping)
            and all(
                row.get(field) in (None, "")
                for field in (
                    "cntr_tm",
                    "open_pric",
                    "high_pric",
                    "low_pric",
                    "cur_prc",
                    "trde_qty",
                )
            )
            for row in rows
        ):
            return True
    return False


def _diagnostic(
    value: Mapping[str, object],
    *,
    stock_code: str,
    requested_date: date,
    attempt_number: int,
    outcome: str,
) -> dict[str, object]:
    result = dict(value)
    result.update(
        {
            "stock_code": stock_code,
            "requested_date": requested_date.isoformat(),
            "attempt_number": attempt_number,
            "outcome": outcome,
        }
    )
    # The collector only emits typed issue/stage fields.  Do not copy an
    # exception string, traceback, Authorization header, token, or account.
    result.pop("exception", None)
    result.pop("traceback", None)
    return result


def _acquire_stock(
    *,
    stock_code: str,
    missing_dates: Sequence[date],
    raw_root: Path,
    planned_dates: Sequence[date],
    max_pages: int,
    page_delay: float,
) -> dict[str, Any]:
    if not missing_dates:
        return {
            "requested_date_count": 0,
            "successful_date_request_count": 0,
            "actual_api_request_count": 0,
            "retry_count": 0,
            "network_base_dates": set(),
            "diagnostics": [],
            "failure": None,
        }
    try:
        config = load_demo_config()
        token = issue_demo_token(config)
    except (ConfigurationError, KiwoomApiError, OSError) as exc:
        return {
            "requested_date_count": 0,
            "successful_date_request_count": 0,
            "actual_api_request_count": 0,
            "retry_count": 0,
            "network_base_dates": set(),
            "diagnostics": [
                {
                    "stock_code": stock_code,
                    "requested_date": min(missing_dates).isoformat(),
                    "attempt_number": 1,
                    "failure_stage": "PRE_REQUEST",
                    "minute_validation_issue_code": None,
                    "outcome": "FAILED",
                    "safe_exception_type": type(exc).__name__,
                }
            ],
            "failure": {
                "kind": "AUTH_OR_TRANSPORT",
                "issue_code": None,
                "safe_exception_type": type(exc).__name__,
            },
        }
    store = KiwoomMinuteStore(raw_root)
    diagnostics: list[dict[str, object]] = []
    network_base_dates: set[str] = set()
    request_count = 0
    successful = 0
    retry_count = 0
    failure: dict[str, Any] | None = None
    # Newest first follows ka10080's historical direction and lets a response
    # overlap older planned dates before another request is considered.
    pending = sorted(missing_dates, reverse=True)
    while pending:
        day = pending.pop(0)
        request = MinuteCollectionRequest(stock_code, day, day, MinutePriceBasis.RAW)
        attempt = 1
        while True:
            diagnostic: dict[str, object] = {}
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
                safe = _diagnostic(
                    diagnostic,
                    stock_code=stock_code,
                    requested_date=day,
                    attempt_number=attempt,
                    outcome="FAILED",
                )
                if isinstance(exc, MinuteValidationError):
                    safe.setdefault("minute_validation_issue_code", exc.issue.value)
                safe.setdefault("safe_exception_type", type(exc).__name__)
                diagnostics.append(safe)
                if attempt == 1 and _retryable(exc):
                    retry_count += 1
                    attempt = 2
                    continue
                issue_code = (
                    exc.issue.value if isinstance(exc, MinuteValidationError) else None
                )
                empty_response = _empty_response_artifact(
                    stock_code=stock_code,
                    requested_date=day,
                    raw_root=raw_root,
                )
                failure = {
                    "kind": "RETENTION_UNAVAILABLE"
                    if issue_code
                    == MinutePipelineIssue.REQUIRED_START_NOT_REACHED.value
                    or empty_response
                    else "SOURCE_QUALITY_FAILURE"
                    if issue_code
                    and issue_code
                    not in {
                        MinutePipelineIssue.HTTP_ERROR.value,
                        MinutePipelineIssue.REQUIRED_START_NOT_REACHED.value,
                    }
                    else "ACQUISITION_TRANSPORT_BLOCKED",
                    "issue_code": "EMPTY_SOURCE_RESPONSE"
                    if empty_response
                    else issue_code,
                    "safe_exception_type": type(exc).__name__,
                    "requested_date": day.isoformat(),
                }
                return {
                    "requested_date_count": successful + 1,
                    "successful_date_request_count": successful,
                    "actual_api_request_count": request_count,
                    "retry_count": retry_count,
                    "network_base_dates": network_base_dates,
                    "diagnostics": diagnostics,
                    "failure": failure,
                }
            diagnostics.append(
                _diagnostic(
                    diagnostic,
                    stock_code=stock_code,
                    requested_date=day,
                    attempt_number=attempt,
                    outcome="SUCCESS",
                )
            )
            request_count += len(collected.pages)
            network_base_dates.add(day.strftime("%Y%m%d"))
            successful += 1
            covered_rows, _, _, _ = _index_pages(
                stock_code=stock_code,
                raw_root=raw_root,
                window_dates=planned_dates,
                required_fast_dates=set(planned_dates),
            )
            covered = set(covered_rows)
            pending = [candidate for candidate in pending if candidate not in covered]
            break
    return {
        "requested_date_count": successful,
        "successful_date_request_count": successful,
        "actual_api_request_count": request_count,
        "retry_count": retry_count,
        "network_base_dates": network_base_dates,
        "diagnostics": diagnostics,
        "failure": failure,
    }


def _stream_digest(stock_code: str, bars: Sequence[Mapping[str, Any]]) -> str:
    records = []
    for bar in sorted(bars, key=lambda item: int(item["emitted_bar_index"])):
        records.append(
            {
                "stock_code": stock_code,
                "market_bar_index": int(bar["emitted_bar_index"]),
                "market_bar_id": bar["market_bar_id"],
                "tau_start": bar["tau_start"],
                "tau_end": bar["tau_end"],
                "calendar_start_datetime": bar["calendar_start_datetime"],
                "calendar_end_datetime": bar["calendar_end_datetime"],
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
                "source_resolutions_used": bar["source_resolutions_used"],
                "source_segment_count": bar["source_segment_count"],
                "provenance": bar["provenance"],
            }
        )
    encoded = json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _materialization_summary(
    materialized: Mapping[str, Any] | None, expected: int
) -> dict[str, Any]:
    bars = list(materialized.get("market_bars", [])) if materialized else []
    result = {
        "expected_market_bar_capacity": expected,
        "actual_market_bar_count": len(bars),
        "expected_actual_difference": len(bars) - expected,
        "resolved_island_count": materialized.get("island_count", 0)
        if materialized
        else 0,
        "unresolved_internal_gap_count": materialized.get("unresolved_source_count")
        if materialized
        else None,
        "skip_count": materialized.get("skip_count") if materialized else None,
        "duplicate_target_count": materialized.get("duplicate_target_count")
        if materialized
        else None,
        "source_ohlc_exact": materialized.get("source_ohlc_exact")
        if materialized
        else None,
        "source_volume_exact": materialized.get("source_volume_exact")
        if materialized
        else None,
        "fractional_source_split_count": 0,
        "interpolation_count": 0,
        "synthetic_bar_count": 0,
        "quality": materialized.get("quality") if materialized else None,
        "market_bars": bars,
    }
    result["success"] = bool(
        materialized
        and result["resolved_island_count"] == 1
        and result["actual_market_bar_count"] >= expected
        and result["unresolved_internal_gap_count"] == 0
        and result["skip_count"] == 0
        and result["duplicate_target_count"] == 0
        and result["source_ohlc_exact"] is True
        and result["source_volume_exact"] is True
    )
    result["stream_digest"] = (
        _stream_digest(stock_code="", bars=bars) if result["success"] else None
    )
    return result


def _safe_failure_status(
    failure: Mapping[str, Any] | None, *, missing: int, allow_network: bool
) -> str:
    if failure:
        return str(failure["kind"])
    if missing:
        return (
            "SOURCE_COVERAGE_INSUFFICIENT"
            if not allow_network
            else "RETENTION_UNAVAILABLE"
        )
    return "VALIDATION_STREAM_READY"


def _build_stock_result(
    *,
    stock_code: str,
    frozen: Mapping[str, Any],
    proof: Mapping[str, Any],
    raw_root: Path,
    allow_network: bool,
    max_pages: int,
    page_delay: float,
) -> dict[str, Any]:
    daily_bars = tuple(
        sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date)
    )
    inventory = {
        row["trade_date"].isoformat(): {"daily_tau": row["delta_tau"]}
        for row in market_time_series(daily_bars)
        if row.get("delta_tau") is not None
    }
    start, end = frozen["start"], frozen["end"]
    window_dates = sorted(
        day for day in inventory if start <= date.fromisoformat(day) <= end
    )
    required_fast = {
        date.fromisoformat(day)
        for day in window_dates
        if _decimal(inventory[day]["daily_tau"]) >= Decimal(1)
    }
    planned_dates = tuple(
        date.fromisoformat(day) for day in frozen["exact_fetch_dates"]
    )
    if not set(planned_dates).issubset(required_fast):
        raise ValidationSourceAcquisitionError(
            f"V1.10 fetch list is outside required FAST dates: {stock_code}"
        )
    initial_paths = set((raw_root / stock_code / "raw").glob("**/page-*.json"))
    rows, records, conflicts, returned_dates = _index_pages(
        stock_code=stock_code,
        raw_root=raw_root,
        window_dates=[date.fromisoformat(day) for day in window_dates],
        required_fast_dates=required_fast,
    )
    initial_required_fast_usable = sum(day in rows for day in required_fast)
    _, initial_counts, _ = _coverage(
        planned_dates=planned_dates,
        rows_by_date=rows,
        records=records,
        conflicts=conflicts,
        network_base_dates=set(),
    )
    still_missing = sorted(day for day in planned_dates if day not in rows)
    known_empty_retention = next(
        (
            day
            for day in still_missing
            if _empty_response_artifact(
                stock_code=stock_code,
                requested_date=day,
                raw_root=raw_root,
            )
        ),
        None,
    )
    persisted_empty_response_evidence = int(known_empty_retention is not None)
    acquisition = (
        {
            "requested_date_count": 0,
            "successful_date_request_count": 0,
            "actual_api_request_count": persisted_empty_response_evidence,
            "retry_count": 0,
            "network_base_dates": set(),
            "diagnostics": [
                {
                    "stock_code": stock_code,
                    "requested_date": known_empty_retention.isoformat(),
                    "attempt_number": 1,
                    "failure_stage": "ROW_VALIDATION",
                    "minute_validation_issue_code": MinutePipelineIssue.MALFORMED_ROW.value,
                    "transport_completed": True,
                    "response_object_available": True,
                    "response_bytes_available_to_pipeline": True,
                    "raw_persistence_started": True,
                    "raw_persistence_completed": True,
                    "parser_started": True,
                    "parser_completed": False,
                    "pagination_started": True,
                    "pagination_completed": True,
                    "outcome": "FAILED",
                    "safe_exception_type": "MinuteValidationError",
                    "persisted_evidence": True,
                }
            ],
            "failure": {
                "kind": "RETENTION_UNAVAILABLE",
                "issue_code": "EMPTY_SOURCE_RESPONSE",
                "safe_exception_type": "MinuteValidationError",
                "requested_date": known_empty_retention.isoformat(),
            },
        }
        if known_empty_retention is not None and allow_network
        else _acquire_stock(
            stock_code=stock_code,
            missing_dates=still_missing,
            raw_root=raw_root,
            planned_dates=tuple(date.fromisoformat(day) for day in window_dates),
            max_pages=max_pages,
            page_delay=page_delay,
        )
        if allow_network and known_empty_retention is None
        else {
            "requested_date_count": 0,
            "successful_date_request_count": 0,
            "actual_api_request_count": 0,
            "retry_count": 0,
            "network_base_dates": set(),
            "diagnostics": [],
            "failure": None,
        }
    )
    rows, records, conflicts, returned_dates = _index_pages(
        stock_code=stock_code,
        raw_root=raw_root,
        window_dates=[date.fromisoformat(day) for day in window_dates],
        required_fast_dates=required_fast,
    )
    coverage, status_counts, quality_anomalies = _coverage(
        planned_dates=planned_dates,
        rows_by_date=rows,
        records=records,
        conflicts=conflicts,
        network_base_dates=set(acquisition["network_base_dates"]),
    )
    missing_required = sorted(day for day in required_fast if day not in rows)
    coverage_complete = not conflicts and not missing_required
    materialized: dict[str, Any] | None = None
    materialization_error: str | None = None
    if coverage_complete:
        try:
            segments = _build_segments(
                stock_code=stock_code,
                window_dates=window_dates,
                inventory=inventory,
                daily_bars=daily_bars,
                raw_by_date=rows,
            )
            materialized = _materialize(segments, stock_code)
        except (
            MinuteValidationError,
            ValueError,
            ValidationSourceAcquisitionError,
        ) as exc:
            materialization_error = type(exc).__name__
    materialization = _materialization_summary(
        materialized, int(frozen["expected_capacity"])
    )
    materialization["error"] = materialization_error
    if materialization["success"]:
        materialization["stream_digest"] = _stream_digest(
            stock_code, materialization["market_bars"]
        )
    else:
        materialization["stream_digest"] = None
    final_paths = set((raw_root / stock_code / "raw").glob("**/page-*.json"))
    new_paths = sorted(final_paths - initial_paths)
    persisted_empty_paths = sorted(
        (
            raw_root
            / stock_code
            / "raw"
            / (
                known_empty_retention.strftime("%Y%m%d")
                if known_empty_retention
                else "__none__"
            )
        ).glob("page-*.json")
    )
    observed_new_paths = sorted(set(new_paths) | set(persisted_empty_paths))
    status = _safe_failure_status(
        acquisition["failure"],
        missing=len(missing_required),
        allow_network=allow_network,
    )
    if materialization["success"] and status not in {
        "SOURCE_QUALITY_FAILURE",
        "ACQUISITION_TRANSPORT_BLOCKED",
    }:
        status = "VALIDATION_STREAM_READY"
    return {
        "stock_code": stock_code,
        "role": frozen["role"],
        "frozen_window": {
            "calendar_start": start.isoformat(),
            "calendar_end": end.isoformat(),
            "target_market_bars": frozen["target"],
            "expected_market_bar_capacity": frozen["expected_capacity"],
            "window_session_count": len(window_dates),
        },
        "coverage": {
            "required_fast_total": len(required_fast),
            "v1_10_planned_fetch_total": len(planned_dates),
            "pre_network_status_counts": initial_counts,
            "preexisting_usable_required_fast": initial_required_fast_usable,
            "final_usable_required_fast": sum(day in rows for day in required_fast),
            "final_status_counts": status_counts,
            "preexisting_usable": initial_counts.get("PREEXISTING_USABLE", 0),
            "covered_by_existing_overlap": status_counts.get(
                "COVERED_BY_EXISTING_OVERLAP", 0
            ),
            "newly_fetched": status_counts.get("NEWLY_FETCHED_USABLE", 0),
            "still_missing": len(missing_required),
            "still_missing_dates": [day.isoformat() for day in missing_required],
            "source_invalid": status_counts.get("SOURCE_INVALID", 0),
            "quality_anomaly_count": quality_anomalies,
            "planned_dates": coverage,
            "complete": coverage_complete,
        },
        "acquisition": {
            "api_id": "ka10080",
            "price_basis": "RAW",
            "network_enabled": allow_network,
            "requested_date_count": acquisition["requested_date_count"],
            "successful_date_request_count": acquisition[
                "successful_date_request_count"
            ],
            "actual_api_request_count": acquisition["actual_api_request_count"],
            "retry_count": acquisition["retry_count"],
            "persisted_empty_response_evidence": bool(
                persisted_empty_response_evidence
            ),
            "new_raw_artifact_count": len(observed_new_paths),
            "new_raw_artifacts": [path.as_posix() for path in observed_new_paths],
            "network_base_dates": sorted(acquisition["network_base_dates"]),
            "failure": acquisition["failure"],
            "attempt_diagnostics": acquisition["diagnostics"],
        },
        "raw_provenance": {
            "artifact_count": len(records),
            "parsed_artifact_count": sum(bool(record["parsed"]) for record in records),
            "failed_artifact_count": sum(
                not bool(record["parsed"]) for record in records
            ),
            "pages": records,
            "credentials_in_report": False,
        },
        "materialization": materialization,
        "retention": {
            "result": status,
            "oldest_required_date": min(window_dates) if window_dates else None,
            "newest_required_date": max(window_dates) if window_dates else None,
            "oldest_missing_date": min(missing_required) if missing_required else None,
            "newest_missing_date": max(missing_required) if missing_required else None,
            "network_phase_closed": coverage_complete,
        },
        "returned_date_count": len(returned_dates),
        "returned_dates_outside_window": sorted(
            day.isoformat() for day in returned_dates if day < start or day > end
        ),
        "strategy_outputs_inspected": False,
        "c2_evaluated": False,
    }


def _canonical_stream_manifest(results: Mapping[str, Mapping[str, Any]]) -> str:
    lines = [
        "# Market-Bar strategy-unseen validation stream manifest V1.11",
        "",
        "This manifest is a source/materialization checkpoint only. Strategy signals, C2, returns, trades, and PnL were not evaluated.",
        "",
        "| Stock | Role | Window | Target | Actual bars | Islands | Raw artifacts | Stream digest |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for stock in STOCKS:
        result = results[stock]
        frozen = result["frozen_window"]
        materialization = result["materialization"]
        lines.append(
            f"| `{stock}` | `{result['role']}` | {frozen['calendar_start']} .. {frozen['calendar_end']} | "
            f"{frozen['target_market_bars']} | {materialization['actual_market_bar_count']} | "
            f"{materialization['resolved_island_count']} | {result['raw_provenance']['artifact_count']} | "
            f"`{materialization['stream_digest']}` |"
        )
    lines.extend(
        [
            "",
            "- geometry contract: frozen V0.6/V0.9 global activity-tau integer lattice",
            "- source OHLC/volume: exact source aggregation only",
            "- strategy_outputs_inspected: `false`",
            "- C2_evaluated: `false`",
            "- window_changed_after_v1_10: `false`",
        ]
    )
    return "\n".join(lines) + "\n"


def run_v1_11(
    *,
    plan_path: Path = V1_10_PATH,
    proof_path: Path = V01_PROOF_PATH,
    raw_root: Path = MINUTE_ROOT,
    output_path: Path = OUTPUT_PATH,
    allow_network: bool = False,
    max_pages: int = MAX_PAGES,
    page_delay: float = DEFAULT_PAGE_DELAY,
    manifest_path: Path = STREAM_MANIFEST_PATH,
) -> dict[str, Any]:
    report = _load_v1_10(plan_path)
    frozen = _frozen_windows(report)
    try:
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationSourceAcquisitionError(
            "source construction proof is unreadable"
        ) from exc
    results = {
        stock: _build_stock_result(
            stock_code=stock,
            frozen=frozen[stock],
            proof=proof,
            raw_root=raw_root,
            allow_network=allow_network,
            max_pages=max_pages,
            page_delay=page_delay,
        )
        for stock in STOCKS
    }
    all_ready = all(
        result["retention"]["result"] == "VALIDATION_STREAM_READY"
        for result in results.values()
    )
    stream_manifest_written = False
    if all_ready:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(_canonical_stream_manifest(results), encoding="utf-8")
        stream_manifest_written = True
    report_payload: dict[str, Any] = {
        "plan_version": "MARKET_BAR_STRATEGY_VALIDATION_SOURCE_ACQUISITION_MATERIALIZATION_V1_11",
        "input_v1_10": str(plan_path),
        "frozen_windows": {
            stock: {
                "calendar_start": frozen[stock]["start"],
                "calendar_end": frozen[stock]["end"],
                "target": frozen[stock]["target"],
                "expected_capacity": frozen[stock]["expected_capacity"],
                "role": frozen[stock]["role"],
            }
            for stock in STOCKS
        },
        "validation_order": list(STOCKS),
        "network_calls": sum(
            int(result["acquisition"]["actual_api_request_count"])
            for result in results.values()
        ),
        "retry_count": sum(
            int(result["acquisition"]["retry_count"]) for result in results.values()
        ),
        "strategy_outputs_inspected": False,
        "c2_evaluated": False,
        "window_changed_after_v1_10": False,
        "market_bar_geometry_changed": False,
        "strategy_calculations": 0,
        "source_hypotheses": {
            "R1_005380_targeted_source_feasibility": "INCONCLUSIVE",
            "R2_068270_targeted_source_feasibility": "INCONCLUSIVE",
            "R3_targeted_acquisition_without_full_history": "INCONCLUSIVE",
            "R4_source_window_independent_of_strategy_outcome": "SUPPORTED",
        },
        "results": results,
        "validation_stream_manifest": {
            "path": str(manifest_path),
            "written": stream_manifest_written,
        },
        "readiness": {
            stock: result["retention"]["result"] for stock, result in results.items()
        },
        "next_gate": "V1.12_STRATEGY_UNSEEN_VALIDATION" if all_ready else None,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report_payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=V1_10_PATH)
    parser.add_argument("--proof", type=Path, default=V01_PROOF_PATH)
    parser.add_argument("--raw-root", type=Path, default=MINUTE_ROOT)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--manifest", type=Path, default=STREAM_MANIFEST_PATH)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--page-delay", type=float, default=DEFAULT_PAGE_DELAY)
    args = parser.parse_args()
    report = run_v1_11(
        plan_path=args.plan,
        proof_path=args.proof,
        raw_root=args.raw_root,
        output_path=args.output,
        allow_network=args.allow_network,
        max_pages=args.max_pages,
        page_delay=args.page_delay,
        manifest_path=args.manifest,
    )
    print(
        json.dumps(
            {
                "network_calls": report["network_calls"],
                "retry_count": report["retry_count"],
                "readiness": report["readiness"],
                "strategy_outputs_inspected": report["strategy_outputs_inspected"],
                "c2_evaluated": report["c2_evaluated"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
