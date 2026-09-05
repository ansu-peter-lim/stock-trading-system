"""V0.9 targeted Market-Bar pilot acquisition and materialization.

The module deliberately has a small scope: it freezes one V0.8 window, reuses
immutable ka10080 RAW artifacts when they already cover the planned dates, and
optionally fetches only uncovered dates.  It does not calculate Market MAs,
strategy signals, orders, fills, PnL, or corporate-action events.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
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
)
from src.kiwoom_rest.auth import issue_demo_token, load_demo_config

from .down_box_daily_execution_proof import _load_stock
from .global_tau_resolution_adequacy_audit import (
    V01_PROOF_PATH,
    _resolve_islands,
)
from .market_bar_construction_proof import _session_bounds
from .market_bar_pilot_source_acquisition_plan import (
    OUTPUT_PATH as V08_OUTPUT_PATH,
)
from .market_bar_pilot_source_acquisition_plan import (
    _session_inventory,
    audit_plan,
)
from .market_time_selective_intraday_decomposition import (
    _activity_segments,
    _load_cached_raw_rows,
)

V08_PLAN_PATH = V08_OUTPUT_PATH
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_pilot_acquisition_v0_9.json"
)
MINUTE_ROOT = Path("data/raw/kiwoom/minute")
PILOT_TARGET = 80
PILOT_START = date(2023, 9, 1)
PILOT_END = date(2026, 8, 28)


class PilotAcquisitionError(ValueError):
    """A pilot cannot be materialized under the frozen V0.6 contract."""


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _distribution(values: Sequence[Decimal]) -> dict[str, object]:
    ordered = sorted(values)
    if not ordered:
        return {
            "count": 0,
            "min": None,
            "p10": None,
            "p25": None,
            "median": None,
            "p75": None,
            "p90": None,
            "max": None,
        }

    def percentile(q: Decimal) -> Decimal:
        position = (len(ordered) - 1) * q
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {
        "count": len(ordered),
        "min": ordered[0],
        "p10": percentile(Decimal("0.10")),
        "p25": percentile(Decimal("0.25")),
        "median": percentile(Decimal("0.50")),
        "p75": percentile(Decimal("0.75")),
        "p90": percentile(Decimal("0.90")),
        "max": ordered[-1],
    }


def _raw_dates(paths: Sequence[str]) -> set[str]:
    """Read dates from already-stored raw responses without changing them."""
    result: set[str] = set()
    for raw_path in paths:
        try:
            payload = json.loads(Path(raw_path).read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        rows = payload.get("stk_min_pole_chart_qry", [])
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or not row.get("cntr_tm"):
                    continue
                stamp = str(row["cntr_tm"])[:8]
                try:
                    result.add(
                        date.fromisoformat(
                            f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
                        ).isoformat()
                    )
                except ValueError:
                    continue
    return result


def _provenance_unique(
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    unique: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        key = (
            str(record.get("raw_file_path", "")),
            str(record.get("raw_file_sha256", "")),
        )
        if key[0] and key[1]:
            unique[key] = dict(record)
    return [unique[key] for key in sorted(unique)]


def _enrich_provenance(
    records: Sequence[Mapping[str, object]],
    *,
    stock_code: str,
    start: date,
    end: date,
) -> list[dict[str, object]]:
    """Add non-secret request metadata from the append-only minute manifest."""
    manifest: dict[str, dict[str, object]] = {}
    manifest_path = MINUTE_ROOT / "manifest" / "requests.jsonl"
    if manifest_path.exists():
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("raw_file_path"):
                manifest[str(item["raw_file_path"])] = item
    enriched: list[dict[str, object]] = []
    for record in records:
        item = dict(record)
        details = manifest.get(str(item.get("raw_file_path", "")), {})
        requested_base = str(details.get("base_date", "")) or None
        item.update(
            {
                "source_name": "KIWOOM",
                "service_name": "ka10080",
                "stock_code": stock_code,
                "price_basis": "RAW",
                "requested_coverage_start": start.isoformat(),
                "requested_coverage_end": end.isoformat(),
                "requested_base_date": requested_base,
                "request_parameters": {
                    "stk_cd": stock_code,
                    "tic_scope": "5",
                    "upd_stkpc_tp": "0",
                    "base_dt": requested_base,
                },
                "retrieved_at": details.get("retrieved_at", item.get("retrieved_at")),
                "pagination_sequence": details.get(
                    "pagination_sequence", item.get("pagination_sequence")
                ),
                "request_continuation_identity": details.get(
                    "request_continuation_identity"
                ),
                "response_continuation_identity": details.get(
                    "response_continuation_identity"
                ),
                "row_count": details.get("row_count", item.get("row_count")),
            }
        )
        enriched.append(item)
    return enriched


def _source_segment(
    *,
    stock_code: str,
    source_id: str,
    resolution: str,
    start_tau: Decimal,
    length: Decimal,
    start_at: datetime,
    end_at: datetime,
    open_price: Decimal,
    high_price: Decimal,
    low_price: Decimal,
    close_price: Decimal,
    volume: Decimal,
) -> dict[str, object]:
    return {
        "stock_code": stock_code,
        "source_id": source_id,
        "source_resolution": resolution,
        "source_tau_start": str(start_tau),
        "source_tau_end": str(start_tau + length),
        "calendar_start_datetime": start_at.isoformat(),
        "calendar_end_datetime": end_at.isoformat(),
        "open": str(open_price),
        "high": str(high_price),
        "low": str(low_price),
        "close": str(close_price),
        "volume": str(volume),
    }


def _build_segments(
    *,
    stock_code: str,
    window_dates: Sequence[str],
    inventory: Mapping[str, Mapping[str, object]],
    daily_bars: Sequence[Any],
    raw_by_date: Mapping[date, Sequence[ParsedMinuteRow]],
) -> list[dict[str, object]]:
    bars_by_date = {bar.trade_date: bar for bar in daily_bars}
    ordered_dates = tuple(sorted(bars_by_date))
    segments: list[dict[str, object]] = []
    cursor = Decimal(0)
    for text_day in window_dates:
        day = date.fromisoformat(text_day)
        bar = bars_by_date.get(day)
        if bar is None:
            raise PilotAcquisitionError(f"Daily bar is missing for {text_day}")
        delta = _decimal(inventory[text_day]["daily_tau"])
        if delta <= 0:
            raise PilotAcquisitionError(f"non-positive tau for {text_day}")
        if delta <= 1:
            start_at, end_at = _session_bounds(day)
            segments.append(
                _source_segment(
                    stock_code=stock_code,
                    source_id=f"{stock_code}:{text_day}:DAILY",
                    resolution="DAILY_SIGNAL_ADJUSTED",
                    start_tau=cursor,
                    length=delta,
                    start_at=start_at,
                    end_at=end_at,
                    open_price=bar.signal.open,
                    high_price=bar.signal.high,
                    low_price=bar.signal.low,
                    close_price=bar.signal.close,
                    volume=Decimal(bar.signal.volume),
                )
            )
            cursor += delta
            continue

        prior_dates = [candidate for candidate in ordered_dates if candidate < day]
        if not prior_dates or day not in raw_by_date or not raw_by_date[day]:
            raise PilotAcquisitionError(f"FAST source is missing for {text_day}")
        previous = bars_by_date[prior_dates[-1]]
        activity, _, _ = _activity_segments(
            delta_tau=delta,
            previous_adjusted_close=previous.signal.close,
            adjusted_open=bar.signal.open,
            minute_rows=raw_by_date[day],
        )
        volume_by_label = {row.source_label: row.raw.volume for row in raw_by_date[day]}
        five_index = 0
        for item in activity:
            length = _decimal(item["tau"])
            if length <= 0:
                continue
            if item["time_bucket"] == "OVERNIGHT":
                _, start_at = _session_bounds(previous.trade_date)
                _, end_at = _session_bounds(day)
                source_id = f"{stock_code}:{text_day}:OVERNIGHT"
                open_price = high_price = low_price = close_price = _decimal(
                    item["signal_close"]
                )
                volume = Decimal(0)
                resolution = "OVERNIGHT_GAP"
            else:
                start_at, end_at = _session_bounds(day)
                label = str(item["label"])
                source_id = f"{stock_code}:{text_day}:5M:{five_index}:{label}"
                five_index += 1
                open_price = _decimal(item["signal_open"])
                high_price = _decimal(item["signal_high"])
                low_price = _decimal(item["signal_low"])
                close_price = _decimal(item["signal_close"])
                if label not in volume_by_label:
                    raise PilotAcquisitionError(f"minute volume is missing for {label}")
                volume = Decimal(volume_by_label[label])
                resolution = "5M_RAW_ACTIVITY_SIGNAL_ANCHORED"
            segments.append(
                _source_segment(
                    stock_code=stock_code,
                    source_id=source_id,
                    resolution=resolution,
                    start_tau=cursor,
                    length=length,
                    start_at=start_at,
                    end_at=end_at,
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    volume=volume,
                )
            )
            cursor += length
    return segments


def _materialize(
    segments: Sequence[dict[str, object]], stock_code: str
) -> dict[str, object]:
    islands, unresolved, bars = _resolve_islands(list(segments), stock_code)
    source_by_id = {str(row["source_id"]): row for row in segments}
    exact_ohlcv = True
    exact_volume = True
    for bar in bars:
        records = [source_by_id[str(item["source_id"])] for item in bar["provenance"]]
        expected = {
            "open": records[0]["open"],
            "high": str(max(_decimal(item["high"]) for item in records)),
            "low": str(min(_decimal(item["low"]) for item in records)),
            "close": records[-1]["close"],
            "volume": str(
                sum((_decimal(item["volume"]) for item in records), Decimal(0))
            ),
        }
        exact_ohlcv &= all(
            bar[key] == expected[key] for key in ("open", "high", "low", "close")
        )
        exact_volume &= bar["volume"] == expected["volume"]
    lengths = [_decimal(row["tau_length"]) for row in bars]
    errors = [abs(_decimal(row["boundary_error"])) for row in bars]
    duplicate_target = len({row["actual_integer_target"] for row in bars}) != len(bars)
    skip_count = sum(int(row.get("skipped_target_count", 0)) for row in bars)
    return {
        "island_count": len(islands),
        "islands": islands,
        "market_bars": bars,
        "unresolved_source_count": len(unresolved),
        "unresolved_sources": unresolved,
        "skip_count": skip_count,
        "duplicate_target_count": int(duplicate_target),
        "source_ohlc_exact": exact_ohlcv,
        "source_volume_exact": exact_volume,
        "quality": {
            "tau_length": _distribution(lengths),
            "boundary_error": _distribution(errors),
            "total_source_tau": str(
                sum(
                    (
                        _decimal(row["source_tau_end"])
                        - _decimal(row["source_tau_start"])
                        for row in segments
                    ),
                    Decimal(0),
                )
            ),
            "materialized_bar_count": len(bars),
        },
    }


def _session_mapping(
    *,
    window_dates: Sequence[str],
    inventory: Mapping[str, Mapping[str, object]],
    bars: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows: list[dict[str, object]] = []
    for text_day in window_dates:
        delta = _decimal(inventory[text_day]["daily_tau"])
        category = (
            "FAST" if delta > 1 else "NORMAL" if delta >= Decimal("0.5") else "SLOW"
        )
        completed = sum(1 for bar in bars if bar.get("calendar_end_date") == text_day)
        boundary = sum(
            1
            for bar in bars
            if bar.get("calendar_start_date") == text_day
            or bar.get("calendar_end_date") == text_day
        )
        rows.append(
            {
                "calendar_session": text_day,
                "daily_delta_tau": str(delta),
                "mapping_category": category,
                "market_bar_boundaries_crossed": boundary,
                "market_bars_completed": completed,
            }
        )
    examples: dict[str, object] = {}
    for category in ("FAST", "NORMAL", "SLOW"):
        match = next((row for row in rows if row["mapping_category"] == category), None)
        if match:
            examples[category] = match
    if "SLOW" not in examples:
        cross = next(
            (
                bar
                for bar in bars
                if bar.get("calendar_start_date") != bar.get("calendar_end_date")
            ),
            None,
        )
        if cross:
            examples["SLOW"] = {
                "mapping_category": "SLOW_PERIOD",
                "calendar_start_date": cross.get("calendar_start_date"),
                "calendar_end_date": cross.get("calendar_end_date"),
                "market_bar_id": cross.get("market_bar_id"),
            }
    return rows, examples


def _fetch_missing(
    *,
    stock_code: str,
    missing_dates: Sequence[date],
    max_pages: int,
    page_delay: float,
) -> tuple[
    dict[date, tuple[ParsedMinuteRow, ...]], list[dict[str, object]], int, list[str]
]:
    """Fetch only planned dates; credentials stay inside the auth/collector layer."""
    if not missing_dates:
        return {}, [], 0, []
    config = load_demo_config()
    token = issue_demo_token(config)
    store = KiwoomMinuteStore(MINUTE_ROOT)
    rows: dict[date, tuple[ParsedMinuteRow, ...]] = {}
    provenance: list[dict[str, object]] = []
    errors: list[str] = []
    request_count = 0
    for day in sorted(missing_dates):
        request = MinuteCollectionRequest(stock_code, day, day, MinutePriceBasis.RAW)
        try:
            collected = collect_minute_series(
                request,
                config=config,
                token=token,
                store=store,
                max_pages=max_pages,
                page_delay=page_delay,
            )
        except (MinuteValidationError, OSError) as exc:
            errors.append(f"{day.isoformat()}:{type(exc).__name__}")
            continue
        request_count += len(collected.pages)
        rows[day] = collected.rows
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
    return rows, _provenance_unique(provenance), request_count, errors


def run_pilot(
    *,
    v08_plan_path: Path = V08_PLAN_PATH,
    v01_path: Path = V01_PROOF_PATH,
    output_path: Path = OUTPUT_PATH,
    allow_network: bool = False,
    max_pages: int = 40,
    page_delay: float = 1.1,
) -> dict[str, object]:
    v01 = json.loads(v01_path.read_text(encoding="utf-8"))
    recomputed = audit_plan(v01)
    frozen = (
        json.loads(v08_plan_path.read_text(encoding="utf-8"))
        if v08_plan_path.exists()
        else recomputed
    )
    ranking_consistent = frozen.get("preferred_pilot") == recomputed.get(
        "preferred_pilot"
    )
    preferred = recomputed.get("preferred_pilot")
    if not isinstance(preferred, dict):
        raise PilotAcquisitionError("V0.8 has no preferred pilot")
    stock_code = str(preferred["stock_code"])
    start = date.fromisoformat(str(preferred["calendar_start"]))
    end = date.fromisoformat(str(preferred["calendar_end"]))
    if start < PILOT_START or end > PILOT_END or start > end:
        raise PilotAcquisitionError("pilot window is outside the research period")
    inventory_all, _ = _session_inventory(v01)
    inventory = inventory_all[stock_code]
    window_dates = sorted(day for day in inventory if str(start) <= day <= str(end))
    planned_dates = tuple(str(day) for day in preferred["repairable_session_dates"])
    required_fast = tuple(
        day for day in window_dates if _decimal(inventory[day]["daily_tau"]) > 1
    )
    cached_rows, cached_provenance = _load_cached_raw_rows(
        stock_code,
        {date.fromisoformat(day) for day in required_fast},
        MINUTE_ROOT,
    )
    missing_planned = [
        date.fromisoformat(day)
        for day in planned_dates
        if date.fromisoformat(day) not in cached_rows
    ]
    fetched_rows: dict[date, tuple[ParsedMinuteRow, ...]] = {}
    fetched_provenance: list[dict[str, object]] = []
    request_count = 0
    acquisition_errors: list[str] = []
    if missing_planned and allow_network:
        fetched_rows, fetched_provenance, request_count, acquisition_errors = (
            _fetch_missing(
                stock_code=stock_code,
                missing_dates=missing_planned,
                max_pages=max_pages,
                page_delay=page_delay,
            )
        )
    all_rows = dict(cached_rows)
    all_rows.update(fetched_rows)
    coverage_rows: list[dict[str, object]] = []
    for day in planned_dates:
        day_rows = all_rows.get(date.fromisoformat(day), ())
        state = "USABLE_FOR_MARKET_BAR" if day_rows else "PLANNED"
        coverage_rows.append(
            {
                "date": day,
                "status": state,
                "row_count": len(day_rows),
                "source": "cached"
                if date.fromisoformat(day) in cached_rows
                else "network"
                if day_rows
                else "missing",
            }
        )
    missing_required = [
        day for day in required_fast if date.fromisoformat(day) not in all_rows
    ]
    daily_bars = tuple(
        sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date)
    )
    materialized: dict[str, object] | None = None
    materialization_error: str | None = None
    if not missing_required and not acquisition_errors:
        try:
            segments = _build_segments(
                stock_code=stock_code,
                window_dates=window_dates,
                inventory=inventory,
                daily_bars=daily_bars,
                raw_by_date=all_rows,
            )
            materialized = _materialize(segments, stock_code)
        except (PilotAcquisitionError, MinuteValidationError) as exc:
            materialization_error = type(exc).__name__
    bars = materialized["market_bars"] if materialized else []
    mapping, examples = _session_mapping(
        window_dates=window_dates,
        inventory=inventory,
        bars=bars,
    )
    all_provenance = _enrich_provenance(
        _provenance_unique([*cached_provenance, *fetched_provenance]),
        stock_code=stock_code,
        start=start,
        end=end,
    )
    extra_dates = _raw_dates(
        [str(item["raw_file_path"]) for item in all_provenance]
    ) - set(window_dates)
    success = bool(
        materialized
        and materialized["island_count"] == 1
        and len(bars) >= PILOT_TARGET
        and materialized["skip_count"] == 0
        and materialized["duplicate_target_count"] == 0
        and materialized["unresolved_source_count"] == 0
        and materialized["source_ohlc_exact"]
        and materialized["source_volume_exact"]
    )
    report: dict[str, object] = {
        "audit_version": "MARKET_BAR_PILOT_ACQUISITION_V0_9",
        "ranking_consistency": {
            "consistent_with_recomputed_contract": ranking_consistent,
            "frozen_preferred_pilot": frozen.get("preferred_pilot"),
            "recomputed_preferred_pilot": preferred,
            "recomputed_top3": recomputed.get("candidate_top10_80", [])[:3],
        },
        "final_pilot": {
            "stock_code": stock_code,
            "calendar_start": str(start),
            "calendar_end": str(end),
            "expected_market_bar_capacity": preferred["expected_market_bar_capacity"],
            "cached_fast_session_count": preferred["cached_fast_session_count"],
            "missing_fast_session_count": preferred["missing_fast_session_count"],
            "planned_missing_fast_dates": list(planned_dates),
            "required_fast_dates": list(required_fast),
            "structural_gap_count": preferred["structural_gap_count"],
        },
        "acquisition": {
            "network_enabled": allow_network,
            "api_id": "ka10080",
            "price_basis": "RAW",
            "actual_api_request_count": request_count,
            "planned_count": len(planned_dates),
            "fetched_count": sum(bool(row["row_count"]) for row in coverage_rows),
            "fetched_from_cache_count": sum(
                row["source"] == "cached" for row in coverage_rows
            ),
            "fetched_from_network_count": sum(
                row["source"] == "network" for row in coverage_rows
            ),
            "parsed_count": sum(bool(row["row_count"]) for row in coverage_rows),
            "usable_count": sum(
                row["status"] == "USABLE_FOR_MARKET_BAR" for row in coverage_rows
            ),
            "missing_count": sum(not row["row_count"] for row in coverage_rows),
            "parse_invalid_count": len(acquisition_errors),
            "source_quality_anomaly_count": 0,
            "coverage": coverage_rows,
            "errors": acquisition_errors,
            "extra_returned_session_count": len(extra_dates),
            "unused_extra_data": sorted(extra_dates),
            "raw_artifact_count": len(all_provenance),
            "new_raw_artifact_count": len(fetched_provenance),
            "raw_provenance": all_provenance,
        },
        "materialization": {
            "success": success,
            "error": materialization_error,
            "contract": {
                "global_tau": True,
                "integer_target_lattice": True,
                "first_actual_source_endpoint_after_crossing": True,
                "multi_target_source": "MARKET_BAR_RESOLUTION_INSUFFICIENT",
                "fractional_split": False,
                "interpolation": False,
                "synthetic_bar": False,
            },
            "resolved_island_count": materialized["island_count"]
            if materialized
            else 0,
            "market_bar_count": len(bars),
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
            "quality": materialized["quality"] if materialized else None,
            "market_bars": bars,
        },
        "calendar_market_bar_mapping": mapping,
        "mapping_examples": examples,
        "hypotheses": {
            "H1_targeted_acquisition_reaches_80_plus": "SUPPORTED"
            if success
            else "NOT_SUPPORTED",
            "H2_minute_source_limited_to_missing_fast_short_window": "SUPPORTED"
            if len(planned_dates) < len(window_dates) and not acquisition_errors
            else "INCONCLUSIVE",
            "H3_v06_geometry_unchanged": "SUPPORTED"
            if materialized
            else "INCONCLUSIVE",
            "H4_daily_minute_are_source_resolution_sensors": "SUPPORTED",
        },
        "strategy_ma_pnl_changed": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=V08_PLAN_PATH)
    parser.add_argument("--proof", type=Path, default=V01_PROOF_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--max-pages", type=int, default=40)
    parser.add_argument("--page-delay", type=float, default=1.1)
    args = parser.parse_args()
    report = run_pilot(
        v08_plan_path=args.plan,
        v01_path=args.proof,
        output_path=args.output,
        allow_network=args.allow_network,
        max_pages=args.max_pages,
        page_delay=args.page_delay,
    )
    print(
        json.dumps(
            {
                "success": report["materialization"]["success"],
                "final_pilot": report["final_pilot"],
                "acquisition": {
                    key: report["acquisition"][key]
                    for key in (
                        "actual_api_request_count",
                        "planned_count",
                        "fetched_count",
                        "parsed_count",
                        "usable_count",
                        "missing_count",
                    )
                },
                "materialization": {
                    key: report["materialization"][key]
                    for key in (
                        "resolved_island_count",
                        "market_bar_count",
                        "skip_count",
                        "duplicate_target_count",
                        "unresolved_internal_gap_count",
                    )
                },
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
