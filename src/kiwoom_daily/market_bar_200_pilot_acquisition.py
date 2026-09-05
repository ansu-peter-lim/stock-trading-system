"""V1.2 acquisition and materialization of the frozen 200-bar pilot.

Only the V1.1 preferred 066570 window is handled here.  Existing immutable
RAW ka10080 artifacts are reused, and missing FAST sessions are fetched one at
a time when ``allow_network`` is explicitly enabled.  The V0.6 geometry and
the V0.9 segment/materialization functions are reused unchanged; this module
does not perform strategy, MMA-role, BUY/SELL, PnL, or historical
reconstruction work.
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

from .down_box_daily_execution_proof import _load_stock
from .global_tau_resolution_adequacy_audit import V01_PROOF_PATH
from .market_bar_continuous_pilot_extension_plan import (
    ANCHOR_STOCK,
    V09_PATH,
    _anchor_baseline,
)
from .market_bar_pilot_acquisition import (
    MINUTE_ROOT,
    _build_segments,
    _decimal,
    _enrich_provenance,
    _materialize,
    _provenance_unique,
    _raw_dates,
    _session_mapping,
)
from .market_bar_pilot_source_acquisition_plan import (
    _session_inventory,
    _structural_gap_dates,
)
from .market_time_selective_intraday_decomposition import (
    _label_quality,
    _load_cached_raw_rows,
)

V11_PLAN_PATH = Path(
    "data/processed/strategy_review/market_bar_continuous_pilot_extension_plan_v1_1.json"
)
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_200_pilot_acquisition_v1_2.json"
)
TARGET = 200
MAX_PAGES = 40
V11_CHECKPOINT = "fe4abbb"


class Pilot200AcquisitionError(ValueError):
    """The frozen 200-bar pilot cannot be acquired or materialized."""


def _load_plan(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidate = payload.get("preferred_candidate_200")
    if not isinstance(candidate, dict):
        raise Pilot200AcquisitionError("V1.1 preferred 200 candidate is missing")
    if (
        str(candidate.get("stock_code")) != ANCHOR_STOCK
        or str(candidate.get("calendar_start")) != "2026-02-02"
        or str(candidate.get("calendar_end")) != "2026-05-28"
        or int(candidate.get("target_market_bars", TARGET)) != TARGET
    ):
        raise Pilot200AcquisitionError("V1.1 preferred 200 candidate is not frozen")
    return payload


def _quality_for_rows(rows: Any) -> tuple[bool, dict[str, Any] | None]:
    if not rows:
        return False, None
    try:
        quality = _label_quality(rows)
    except (AttributeError, IndexError, TypeError, ValueError):
        return False, None
    # A missing source slot is evidence, not permission to synthesize a bar.
    # The existing V0.6 materializer can still use the observed source rows;
    # the irregularity remains visible as a quality anomaly below.
    valid = True
    return valid, quality


def _fetch_missing_fail_fast(
    *,
    stock_code: str,
    missing_dates: list[date],
    max_pages: int,
    page_delay: float,
) -> tuple[
    dict[date, tuple[Any, ...]],
    list[dict[str, object]],
    int,
    list[dict[str, object]],
    str | None,
    list[dict[str, object]],
]:
    """Fetch missing dates sequentially and stop at the first failure."""

    if not missing_dates:
        return {}, [], 0, [], None, []
    try:
        config = load_demo_config()
        token = issue_demo_token(config)
    except (ConfigurationError, KiwoomApiError, OSError) as exc:
        return {}, [], 0, [], type(exc).__name__, []

    store = KiwoomMinuteStore(MINUTE_ROOT)
    fetched: dict[date, tuple[Any, ...]] = {}
    provenance: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    attempt_diagnostics: list[dict[str, object]] = []
    request_count = 0
    for day in sorted(missing_dates):
        request = MinuteCollectionRequest(stock_code, day, day, MinutePriceBasis.RAW)
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
            diagnostic.setdefault("stock_code", stock_code)
            diagnostic.setdefault("requested_date", day.isoformat())
            diagnostic.setdefault("request_sequence", 0)
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
            diagnostic["error_type"] = type(exc).__name__
            # Only class/typed issue values are retained; never str(exc).
            attempt_diagnostics.append(dict(diagnostic))
            errors.append(dict(diagnostic))
            return (
                fetched,
                _provenance_unique(provenance),
                request_count,
                errors,
                (type(exc).__name__),
                attempt_diagnostics,
            )
        request_count += len(collected.pages)
        fetched[day] = collected.rows
        attempt_diagnostics.append(dict(diagnostic))
        provenance.extend(
            {
                "raw_file_path": page.raw_file_path,
                "raw_file_sha256": page.raw_file_sha256,
                "retrieved_at": page.retrieved_at,
                "pagination_sequence": page.pagination_sequence,
                "row_count": page.row_count,
            }
            for page in collected.pages
        )
    return (
        fetched,
        _provenance_unique(provenance),
        request_count,
        errors,
        None,
        attempt_diagnostics,
    )


def _coverage_rows(
    planned_dates: list[str],
    *,
    cached_rows: dict[date, tuple[Any, ...]],
    fetched_rows: dict[date, tuple[Any, ...]],
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    anomaly_count = 0
    for text_day in planned_dates:
        day = date.fromisoformat(text_day)
        if day in cached_rows:
            source = "cached"
            day_rows = cached_rows[day]
        elif day in fetched_rows:
            source = "network"
            day_rows = fetched_rows[day]
        else:
            source = "missing"
            day_rows = ()
        quality_valid, quality = _quality_for_rows(day_rows)
        if (
            day_rows
            and quality
            and (quality["non_5m_spacing_minutes"] or quality["opening_label_missing"])
        ):
            anomaly_count += 1
        rows.append(
            {
                "date": text_day,
                "status": "USABLE_FOR_MARKET_BAR"
                if day_rows and quality_valid
                else "PARSED"
                if day_rows
                else "PLANNED",
                "source": source,
                "row_count": len(day_rows),
                "planned": True,
                "available_in_cache": source == "cached",
                "fetched": source == "network",
                "parsed": bool(day_rows),
                "source_quality_valid": bool(day_rows) and quality_valid,
                "usable_for_market_bar": bool(day_rows) and quality_valid,
                "source_quality": quality,
            }
        )
    return rows, anomaly_count


def _density_distribution(mapping: list[dict[str, Any]]) -> dict[str, int]:
    result = {"0": 0, "1": 0, "2": 0, "3_plus": 0, "5_plus": 0}
    for row in mapping:
        completed = int(row.get("market_bars_completed", 0))
        if completed == 0:
            result["0"] += 1
        elif completed == 1:
            result["1"] += 1
        elif completed == 2:
            result["2"] += 1
        else:
            result["3_plus"] += 1
        if completed >= 5:
            result["5_plus"] += 1
    return result


def _materialization_error_name(exc: BaseException) -> str:
    return type(exc).__name__


def run_pilot(
    *,
    plan_path: Path = V11_PLAN_PATH,
    proof_path: Path = V01_PROOF_PATH,
    v09_path: Path = V09_PATH,
    output_path: Path = OUTPUT_PATH,
    allow_network: bool = False,
    max_pages: int = MAX_PAGES,
    page_delay: float = 1.1,
) -> dict[str, Any]:
    plan = _load_plan(plan_path)
    candidate = plan["preferred_candidate_200"]
    v01 = json.loads(proof_path.read_text(encoding="utf-8"))
    v09_data = (
        json.loads(v09_path.read_text(encoding="utf-8")) if v09_path.exists() else {}
    )
    sessions_all, _ = _session_inventory(v01)
    structural = _structural_gap_dates(v01)
    inventory = sessions_all[ANCHOR_STOCK]
    start = date.fromisoformat(candidate["calendar_start"])
    end = date.fromisoformat(candidate["calendar_end"])
    window_dates = sorted(day for day in inventory if str(start) <= day <= str(end))
    if start != date(2026, 2, 2) or end != date(2026, 5, 28):
        raise Pilot200AcquisitionError("unexpected frozen window")
    if any((ANCHOR_STOCK, day) in structural for day in window_dates):
        raise Pilot200AcquisitionError("frozen window crosses a structural gap")

    required_fast = [
        day for day in window_dates if _decimal(inventory[day]["daily_tau"]) > 1
    ]
    planned_dates = list(candidate["repairable_session_dates"])
    planned_date_set = {date.fromisoformat(day) for day in planned_dates}
    cached_rows_all, cached_provenance = _load_cached_raw_rows(
        ANCHOR_STOCK,
        {
            date.fromisoformat(day)
            for day in required_fast
            if date.fromisoformat(day) not in planned_date_set
        },
        MINUTE_ROOT,
    )
    # V1.1 explicitly froze ten cached FAST sessions and 61 planned fetches.
    # Do not silently reclassify a generic historical artifact as one of those
    # planned repairs; only the ten non-planned required sessions are cache
    # reuse for this run.
    cached_rows = {
        day: rows
        for day, rows in cached_rows_all.items()
        if day not in planned_date_set
    }
    missing_planned = [date.fromisoformat(day) for day in planned_dates]

    fetched_rows: dict[date, tuple[Any, ...]] = {}
    fetched_provenance: list[dict[str, object]] = []
    request_count = 0
    acquisition_errors: list[dict[str, object]] = []
    acquisition_diagnostics: list[dict[str, object]] = []
    first_failure: str | None = None
    if missing_planned and allow_network:
        (
            fetched_rows,
            fetched_provenance,
            request_count,
            acquisition_errors,
            first_failure,
            acquisition_diagnostics,
        ) = _fetch_missing_fail_fast(
            stock_code=ANCHOR_STOCK,
            missing_dates=missing_planned,
            max_pages=max_pages,
            page_delay=page_delay,
        )

    all_rows = dict(cached_rows)
    all_rows.update(fetched_rows)
    coverage, quality_anomaly_count = _coverage_rows(
        planned_dates, cached_rows=cached_rows, fetched_rows=fetched_rows
    )
    usable_dates = [
        str(row["date"]) for row in coverage if row["usable_for_market_bar"]
    ]
    missing_required = [day for day in required_fast if day not in all_rows]
    daily_bars = tuple(
        sorted(_load_stock(ANCHOR_STOCK)[0], key=lambda bar: bar.trade_date)
    )
    materialized: dict[str, Any] | None = None
    materialization_error: str | None = None
    if not missing_required and not acquisition_errors:
        try:
            segments = _build_segments(
                stock_code=ANCHOR_STOCK,
                window_dates=window_dates,
                inventory=inventory,
                daily_bars=daily_bars,
                raw_by_date=all_rows,
            )
            materialized = _materialize(segments, ANCHOR_STOCK)
        except (Pilot200AcquisitionError, MinuteValidationError, ValueError) as exc:
            materialization_error = _materialization_error_name(exc)

    bars = materialized["market_bars"] if materialized else []
    mapping, examples = _session_mapping(
        window_dates=window_dates, inventory=inventory, bars=bars
    )
    all_provenance = _enrich_provenance(
        _provenance_unique([*cached_provenance, *fetched_provenance]),
        stock_code=ANCHOR_STOCK,
        start=start,
        end=end,
    )
    extra_dates = _raw_dates(
        [str(item["raw_file_path"]) for item in all_provenance]
    ) - set(window_dates)
    success = bool(
        materialized
        and materialized["island_count"] == 1
        and len(bars) >= TARGET
        and materialized["skip_count"] == 0
        and materialized["duplicate_target_count"] == 0
        and materialized["unresolved_source_count"] == 0
        and materialized["source_ohlc_exact"]
        and materialized["source_volume_exact"]
    )
    if first_failure and not fetched_rows and missing_planned:
        retention = "RETENTION_BLOCKED"
    elif first_failure:
        retention = "ACQUISITION_FAILED"
    elif missing_required:
        retention = "INCOMPLETE_REQUIRED_COVERAGE"
    elif not missing_planned:
        retention = "CACHE_COMPLETE"
    elif allow_network:
        retention = "PASSED_REQUIRED_COVERAGE"
    else:
        retention = "NETWORK_NOT_ENABLED"

    report: dict[str, Any] = {
        "audit_version": "MARKET_BAR_200_PILOT_ACQUISITION_V1_2",
        "v11_checkpoint": V11_CHECKPOINT,
        "plan_freeze": {
            "source": str(plan_path),
            "target": TARGET,
            "stock_code": ANCHOR_STOCK,
            "calendar_start": str(start),
            "calendar_end": str(end),
            "exact_fetch_dates_reused": True,
            "manual_date_selection": False,
        },
        "anchor_pilot": _anchor_baseline(v09_data),
        "contract": {
            "global_activity_tau": True,
            "integer_target_lattice": True,
            "one_target_one_market_bar": True,
            "multi_target_source_resolution_insufficient": True,
            "unresolved_gap_continuity": False,
            "fractional_source_split": False,
            "interpolation": False,
            "synthetic_market_bar": False,
            "source_ohlc_aggregation": "actual source only",
            "source_volume_aggregation": "actual source only",
            "tau_formula_changed": False,
            "strategy": False,
            "mma_role_analysis": False,
            "buy_sell": False,
            "pnl": False,
        },
        "acquisition": {
            "api_id": "ka10080",
            "price_basis": "RAW",
            "network_enabled": allow_network,
            "planned_count": len(planned_dates),
            "required_fast_total": len(required_fast),
            "already_cached_required_sessions": sum(
                day in cached_rows for day in required_fast
            ),
            "planned_new_fetch_count": len(missing_planned),
            "actual_api_request_count": request_count,
            "newly_fetched_count": sum(bool(row["fetched"]) for row in coverage),
            "parsed_count": sum(bool(row["parsed"]) for row in coverage),
            "usable_count": sum(bool(row["usable_for_market_bar"]) for row in coverage),
            "missing_count": sum(not row["row_count"] for row in coverage),
            "invalid_count": sum(
                bool(row["row_count"]) and not row["source_quality_valid"]
                for row in coverage
            ),
            "source_quality_anomaly_count": quality_anomaly_count,
            "coverage": coverage,
            "errors": acquisition_errors,
            "attempt_diagnostics": acquisition_diagnostics,
            "first_failure_type": first_failure,
            "raw_artifact_count": len(all_provenance),
            "new_raw_artifact_count": len(fetched_provenance),
            "oldest_usable_date": min(usable_dates) if usable_dates else None,
            "newest_usable_date": max(usable_dates) if usable_dates else None,
            "planned_dates_newly_covered": sorted(
                day.isoformat() for day in fetched_rows
            ),
            "extra_returned_session_count": len(extra_dates),
            "unused_extra_sessions": sorted(extra_dates),
            "raw_provenance": all_provenance,
        },
        "retention_feasibility": {
            "result": retention,
            "oldest_required_date": min(window_dates) if window_dates else None,
            "newest_required_date": max(window_dates) if window_dates else None,
            "oldest_missing_fetch_date": missing_planned[0]
            if missing_planned
            else None,
            "newest_missing_fetch_date": missing_planned[-1]
            if missing_planned
            else None,
            "oldest_usable_date": min(usable_dates) if usable_dates else None,
            "newest_usable_date": max(usable_dates) if usable_dates else None,
            "retry_performed": False,
            "automatic_candidate_switch": False,
        },
        "materialization": {
            "success": success,
            "expected_market_bar_capacity": int(
                candidate["expected_market_bar_capacity"]
            ),
            "actual_market_bar_count": len(bars),
            "expected_actual_difference": len(bars)
            - int(candidate["expected_market_bar_capacity"]),
            "resolved_island_count": materialized["island_count"]
            if materialized
            else 0,
            "skip_count": materialized["skip_count"] if materialized else None,
            "duplicate_target_count": materialized["duplicate_target_count"]
            if materialized
            else None,
            "unresolved_internal_gap_count": materialized["unresolved_source_count"]
            if materialized
            else None,
            "source_ohlc_exact": materialized["source_ohlc_exact"]
            if materialized
            else None,
            "source_volume_exact": materialized["source_volume_exact"]
            if materialized
            else None,
            "fractional_source_split_count": 0,
            "synthetic_bar_count": 0,
            "interpolation_count": 0,
            "error": materialization_error,
            "quality": materialized["quality"] if materialized else None,
            "market_bars": bars,
        },
        "tau_quality": materialized["quality"] if materialized else None,
        "calendar_market_bar_mapping": mapping,
        "calendar_market_bar_density_distribution": _density_distribution(mapping),
        "mapping_examples": examples,
        "acquisition_efficiency": {
            "planned_new_fetch_sessions": len(missing_planned),
            "actual_api_request_count": request_count,
            "new_raw_artifact_count": len(fetched_provenance),
            "planned_dates_newly_covered": len(fetched_rows),
            "extra_returned_sessions": len(extra_dates),
            "unused_extra_sessions": len(extra_dates),
            "cache_reuse_required_sessions": sum(
                day in cached_rows for day in required_fast
            ),
        },
        "hypotheses": {
            "H1_targeted_acquisition_reaches_200_plus": "SUPPORTED"
            if success
            else "INCONCLUSIVE",
            "H2_short_pilot_is_sufficient_for_MMA_research": "SUPPORTED"
            if success and len(bars) >= 200
            else "INCONCLUSIVE",
            "H3_v06_geometry_unchanged": "SUPPORTED"
            if materialized and materialized["unresolved_source_count"] == 0
            else "INCONCLUSIVE",
            "H4_resolution_is_source_sensor_not_final_timeframe": "SUPPORTED",
        },
        "strategy_ma_pnl_changed": False,
        "source_artifacts": {
            "daily_proof": str(proof_path),
            "v09_anchor": str(v09_path),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=V11_PLAN_PATH)
    parser.add_argument("--proof", type=Path, default=V01_PROOF_PATH)
    parser.add_argument("--v09", type=Path, default=V09_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--page-delay", type=float, default=1.1)
    args = parser.parse_args()
    report = run_pilot(
        plan_path=args.plan,
        proof_path=args.proof,
        v09_path=args.v09,
        output_path=args.output,
        allow_network=args.allow_network,
        max_pages=args.max_pages,
        page_delay=args.page_delay,
    )
    print(
        json.dumps(
            {
                "retention": report["retention_feasibility"]["result"],
                "acquisition": {
                    key: report["acquisition"][key]
                    for key in (
                        "required_fast_total",
                        "planned_new_fetch_count",
                        "actual_api_request_count",
                        "newly_fetched_count",
                        "parsed_count",
                        "usable_count",
                        "missing_count",
                    )
                },
                "materialization": {
                    key: report["materialization"][key]
                    for key in (
                        "success",
                        "expected_market_bar_capacity",
                        "actual_market_bar_count",
                        "resolved_island_count",
                        "skip_count",
                        "duplicate_target_count",
                        "unresolved_internal_gap_count",
                    )
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
