"""Offline planning for extending the V1.0 Market-Bar pilot (V1.1).

The planner uses the existing V0.8 source inventory and the V0.9 pilot as
evidence.  It does not call Kiwoom, fetch minute data, materialize Market
Bars, calculate MMA values, or make a strategy decision.  Its only purpose is
to compare deterministic 160/200-bar acquisition windows around the frozen
066570 anchor.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .global_tau_resolution_adequacy_audit import V01_PROOF_PATH
from .market_bar_pilot_source_acquisition_plan import (
    _session_inventory,
    _structural_gap_dates,
    _windows_for_stock,
)

V08_PATH = Path(
    "data/processed/strategy_review/market_bar_pilot_source_acquisition_plan_v0_8.json"
)
V09_PATH = Path("data/processed/strategy_review/market_bar_pilot_acquisition_v0_9.json")
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_continuous_pilot_extension_plan_v1_1.json"
)

ANCHOR_STOCK = "066570"
ANCHOR_START = date(2026, 4, 20)
ANCHOR_END = date(2026, 5, 26)
ANCHOR_MARKET_BARS = 80
TARGETS = (160, 200)
MMA_WARMUP = 60


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _date(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _anchor_included(window: dict[str, Any]) -> bool:
    return (
        _date(window["calendar_start"]) <= ANCHOR_START
        and _date(window["calendar_end"]) >= ANCHOR_END
    )


def _extension_direction(window: dict[str, Any]) -> str:
    start = _date(window["calendar_start"])
    end = _date(window["calendar_end"])
    if start < ANCHOR_START and end == ANCHOR_END:
        return "BACKWARD_ONLY"
    if start == ANCHOR_START and end > ANCHOR_END:
        return "FORWARD_ONLY"
    if start < ANCHOR_START and end > ANCHOR_END:
        return "BOTH_SIDES"
    if start == ANCHOR_START and end == ANCHOR_END:
        return "ANCHOR_ONLY"
    return "OUTSIDE_ANCHOR"


def _candidate_sort_key(window: dict[str, Any]) -> tuple[object, ...]:
    """The V1.1 deterministic planning order.

    Structural safety and acquisition cost are preferred before span and
    capacity.  Anchor inclusion is explicit even though the selected set is
    filtered to anchor-containing windows.
    """

    return (
        int(window["structural_gap_count"]),
        int(window["new_fetch_required_session_count"]),
        not bool(window["anchor_included"]),
        int(window["calendar_session_count"]),
        -int(window["expected_market_bar_capacity"]),
        int(window["source_quality_anomaly_count"]),
        str(window["calendar_start"]),
        str(window["calendar_end"]),
        str(window["stock_code"]),
    )


def _annotate_window(
    window: dict[str, Any],
    sessions: dict[str, dict[str, Any]],
    *,
    target: int,
) -> dict[str, Any]:
    stock = str(window["stock_code"])
    start = _date(window["calendar_start"])
    end = _date(window["calendar_end"])
    dates = [day for day in sorted(sessions[stock]) if start <= _date(day) <= end]
    required_fast = [
        day for day in dates if _decimal(sessions[stock][day]["daily_tau"]) >= 1
    ]
    cached = [day for day in required_fast if bool(sessions[stock][day]["cached_fast"])]
    missing = [day for day in required_fast if day not in cached]
    missing_tau = sum(
        (_decimal(sessions[stock][day]["daily_tau"]) for day in missing), Decimal(0)
    )
    anchor_included = _anchor_included(window)
    result = {
        **window,
        "target_market_bars": target,
        "stock_code": stock,
        "anchor_included": anchor_included,
        "extension_direction": _extension_direction(window),
        "required_fast_session_count": len(required_fast),
        "already_cached_required_sessions": len(cached),
        "new_fetch_required_session_count": len(missing),
        "missing_fast_session_count": len(missing),
        "repairable_session_dates": missing,
        "missing_fast_tau_sum": str(missing_tau),
        "oldest_missing_date": missing[0] if missing else None,
        "newest_missing_date": missing[-1] if missing else None,
        "currently_materializable_market_bars": int(
            window.get("currently_resolved_market_bars", 0)
        ),
        "expected_market_bar_capacity": int(window["expected_market_bar_capacity"]),
        "expected_observations_after_mma60_warmup": max(
            0, int(window["expected_market_bar_capacity"]) - MMA_WARMUP + 1
        ),
        "calendar_span_days": (end - start).days + 1,
        "candidate_target": target,
    }
    return result


def _candidate_windows(
    data: dict[str, Any],
    *,
    target: int,
    stock_code: str = ANCHOR_STOCK,
    anchor_only: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (all generated windows, anchor-containing windows)."""

    sessions, _ = _session_inventory(data)
    structural = _structural_gap_dates(data)
    all_windows: list[dict[str, Any]] = []
    for stock, values in sessions.items():
        if stock != stock_code:
            continue
        all_windows.extend(_windows_for_stock(stock, values, structural, target))
    annotated = [
        _annotate_window(window, sessions, target=target) for window in all_windows
    ]
    annotated.sort(key=_candidate_sort_key)
    selected = [
        window
        for window in annotated
        if window["anchor_included"]
        and (not anchor_only or int(window["structural_gap_count"]) == 0)
    ]
    selected.sort(key=_candidate_sort_key)
    return annotated, selected


def _fetch_rows_with_tau(
    candidate: dict[str, Any],
    sessions: dict[str, dict[str, Any]],
    *,
    target: int,
) -> list[dict[str, Any]]:
    rows = []
    for day in candidate["repairable_session_dates"]:
        item = sessions[candidate["stock_code"]][day]
        rows.append(
            {
                "stock_code": candidate["stock_code"],
                "date": day,
                "daily_delta_tau": str(item["daily_tau"]),
                "reason": "MARKET_BAR_RESOLUTION_REQUIRED",
                "cached_minute_exists": bool(item["cached_fast"]),
                "planned_fetch_required": True,
                "candidate_target": target,
            }
        )
    return rows


def _best_by_direction(
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any] | None]:
    result: dict[str, dict[str, Any] | None] = {}
    for direction in ("BACKWARD_ONLY", "FORWARD_ONLY", "BOTH_SIDES"):
        matching = [
            candidate
            for candidate in candidates
            if candidate["extension_direction"] == direction
        ]
        result[direction] = min(matching, key=_candidate_sort_key) if matching else None
    return result


def _cost_summary(
    candidate: dict[str, Any] | None,
    *,
    target: int,
    sessions: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "calendar_start": candidate["calendar_start"],
        "calendar_end": candidate["calendar_end"],
        "expected_capacity": candidate["expected_market_bar_capacity"],
        "expected_observations_after_mma60_warmup": candidate[
            "expected_observations_after_mma60_warmup"
        ],
        "calendar_session_count": candidate["calendar_session_count"],
        "calendar_span_days": candidate["calendar_span_days"],
        "required_fast_session_count": candidate["required_fast_session_count"],
        "already_cached_required_sessions": candidate[
            "already_cached_required_sessions"
        ],
        "new_fetch_required_session_count": candidate[
            "new_fetch_required_session_count"
        ],
        "missing_fast_tau_sum": candidate["missing_fast_tau_sum"],
        "oldest_missing_date": candidate["oldest_missing_date"],
        "newest_missing_date": candidate["newest_missing_date"],
        "exact_fetch_dates": _fetch_rows_with_tau(candidate, sessions, target=target),
        "extension_direction": candidate["extension_direction"],
    }


def _overlap_summary(
    candidate160: dict[str, Any] | None,
    candidate200: dict[str, Any] | None,
) -> dict[str, Any]:
    dates160 = set(candidate160["repairable_session_dates"]) if candidate160 else set()
    dates200 = set(candidate200["repairable_session_dates"]) if candidate200 else set()
    additional = sorted(dates200 - dates160)
    return {
        "target_160_fetch_dates": sorted(dates160),
        "target_200_fetch_dates": sorted(dates200),
        "additional_after_160": additional,
        "incremental_fetch_count": len(additional),
        "comparison_available": bool(candidate160 and candidate200),
    }


def _recommendation(
    candidate160: dict[str, Any] | None,
    candidate200: dict[str, Any] | None,
    overlap: dict[str, Any],
) -> str:
    if candidate160 is None:
        return "INCONCLUSIVE"
    if candidate200 is None:
        return "PREFER_160_FIRST"
    incremental = int(overlap["incremental_fetch_count"])
    cost160 = int(candidate160["new_fetch_required_session_count"])
    if incremental == 0:
        return "COST_DIFFERENCE_SMALL_200_PREFERRED"
    if incremental < cost160:
        return "PREFER_200_DIRECT"
    return "200_COST_TOO_HIGH_160_PREFERRED"


def _anchor_baseline(v09: dict[str, Any]) -> dict[str, Any]:
    final = v09.get("final_pilot", {})
    materialization = v09.get("materialization", {})
    bars = materialization.get("market_bars", [])
    return {
        "stock_code": final.get("stock_code", ANCHOR_STOCK),
        "calendar_start": final.get("calendar_start", ANCHOR_START.isoformat()),
        "calendar_end": final.get("calendar_end", ANCHOR_END.isoformat()),
        "market_bar_count": len(bars),
        "continuous_island_count": materialization.get("resolved_island_count"),
        "structural_gap_count": len(materialization.get("unresolved", [])),
        "source_artifact": str(V09_PATH),
    }


def build_plan(
    data: dict[str, Any],
    *,
    v09_data: dict[str, Any] | None = None,
    stock_code: str = ANCHOR_STOCK,
) -> dict[str, Any]:
    sessions, _ = _session_inventory(data)
    if stock_code not in sessions:
        raise ValueError(
            f"planning stock not present in source inventory: {stock_code}"
        )
    generated: dict[int, list[dict[str, Any]]] = {}
    selected: dict[int, list[dict[str, Any]]] = {}
    for target in TARGETS:
        generated[target], selected[target] = _candidate_windows(
            data, target=target, stock_code=stock_code
        )

    preferred = {
        target: (selected[target][0] if selected[target] else None)
        for target in TARGETS
    }
    direction_best = {
        str(target): _best_by_direction(selected[target]) for target in TARGETS
    }
    costs = {
        str(target): _cost_summary(preferred[target], target=target, sessions=sessions)
        for target in TARGETS
    }
    overlap = _overlap_summary(preferred[160], preferred[200])
    recommendation = _recommendation(preferred[160], preferred[200], overlap)
    baseline = _anchor_baseline(v09_data or {})

    return {
        "plan_version": "MARKET_BAR_CONTINUOUS_PILOT_EXTENSION_PLAN_V1_1",
        "contract": {
            "global_activity_tau": True,
            "integer_target_lattice": True,
            "one_target_one_market_bar": True,
            "first_actual_source_endpoint_after_target_crossing": True,
            "multi_target_source_segment_is_structural_gap": True,
            "fractional_source_split": False,
            "price_interpolation": False,
            "synthetic_market_bar": False,
            "source_ohlc_aggregation": "actual source only",
            "source_volume_aggregation": "actual source only",
            "mma_definition": "V1.0 unchanged; simple SMA of Market-Bar closes",
            "mma_periods": [5, 10, 20, 60],
            "calendar_sma_or_mtma": False,
            "strategy": False,
            "buy_sell": False,
            "pnl": False,
            "network_calls": 0,
            "backfill": False,
        },
        "anchor_pilot": {
            **baseline,
            "frozen": True,
            "anchor_market_bars": ANCHOR_MARKET_BARS,
            "anchor_continuous_islands": 1,
            "anchor_gap": 0,
        },
        "planning_population": {
            "stock_code": stock_code,
            "candidate_scope": "anchor-containing windows for one stock",
            "source_inventory": str(V01_PROOF_PATH),
            "v08_plan_reference": str(V08_PATH),
            "v09_anchor_reference": str(V09_PATH),
            "anchor_start": ANCHOR_START.isoformat(),
            "anchor_end": ANCHOR_END.isoformat(),
            "targets": list(TARGETS),
            "target_160_candidate_count": len(selected[160]),
            "target_200_candidate_count": len(selected[200]),
            "target_160_generated_window_count": len(generated[160]),
            "target_200_generated_window_count": len(generated[200]),
            "structural_gap_policy": "exclude candidate; never bridge",
        },
        "candidate_top10_160": selected[160][:10],
        "candidate_top10_200": selected[200][:10],
        "preferred_candidate_160": preferred[160],
        "preferred_candidate_200": preferred[200],
        "direction_best": direction_best,
        "acquisition_cost": costs,
        "overlap_160_to_200": overlap,
        "recommendation": {
            "label": recommendation,
            "basis": [
                "new_fetch_required_session_count",
                "incremental_fetch_count_after_160",
                "calendar_session_count",
                "expected_observations_after_mma60_warmup",
            ],
            "automatic_strategy_decision": False,
        },
        "exact_fetch_dates": {
            "target_160": (costs["160"]["exact_fetch_dates"] if costs["160"] else []),
            "target_200": (costs["200"]["exact_fetch_dates"] if costs["200"] else []),
        },
        "cache_reuse": {
            "target_160_already_cached_required_sessions": (
                costs["160"]["already_cached_required_sessions"] if costs["160"] else 0
            ),
            "target_200_already_cached_required_sessions": (
                costs["200"]["already_cached_required_sessions"] if costs["200"] else 0
            ),
            "target_160_new_fetch_required": (
                costs["160"]["new_fetch_required_session_count"] if costs["160"] else 0
            ),
            "target_200_new_fetch_required": (
                costs["200"]["new_fetch_required_session_count"] if costs["200"] else 0
            ),
        },
        "calendar_coverage": {
            "oldest_required_date": min(sessions[stock_code]),
            "newest_required_date": max(sessions[stock_code]),
            "api_availability_checked": False,
            "needs_live_acquisition_validation": True,
        },
        "hypotheses": {
            "H1_targeted_repairs_can_reach_160": (
                "SUPPORTED"
                if preferred[160]
                and preferred[160]["structural_gap_count"] == 0
                and preferred[160]["expected_market_bar_capacity"] >= 160
                else "INCONCLUSIVE"
            ),
            "H2_targeted_repairs_can_reach_200": (
                "SUPPORTED"
                if preferred[200]
                and preferred[200]["structural_gap_count"] == 0
                and preferred[200]["expected_market_bar_capacity"] >= 200
                else "INCONCLUSIVE"
            ),
            "H3_incremental_160_to_200_cost_is_research_efficient": (
                "SUPPORTED"
                if overlap["comparison_available"]
                and overlap["incremental_fetch_count"]
                < preferred[160]["new_fetch_required_session_count"]
                else "INCONCLUSIVE"
            ),
            "H4_geometry_contract_supports_continuous_extension": (
                "SUPPORTED"
                if all(
                    candidate["structural_gap_count"] == 0
                    for target in TARGETS
                    for candidate in selected[target][:1]
                )
                else "INCONCLUSIVE"
            ),
        },
        "source_feasibility": {
            "known_api_availability": "not checked; needs live acquisition validation",
            "oldest_required_date": costs["200"]["oldest_missing_date"]
            if costs["200"] and costs["200"]["oldest_missing_date"]
            else (costs["160"]["oldest_missing_date"] if costs["160"] else None),
            "newest_required_date": costs["200"]["newest_missing_date"]
            if costs["200"] and costs["200"]["newest_missing_date"]
            else (costs["160"]["newest_missing_date"] if costs["160"] else None),
            "credential_metadata_saved": False,
        },
        "planning_notes": [
            "Only 066570 and windows containing the frozen 80-bar anchor are ranked.",
            "Expected capacity is floor(total_daily_tau), not a materialized OHLC count.",
            "Daily tau is used for capacity planning only; missing minute rows are not estimated.",
            "Structural hard breaks are excluded and never bridged.",
            "No API availability, credential, or retention claim was made offline.",
            "120-bar V0.8 planning is retained as reference; V1.1 targets are 160 and 200.",
            "No Market-Bar materialization or MMA/strategy interpretation is performed.",
        ],
    }


def run_plan(
    *,
    proof_path: Path = V01_PROOF_PATH,
    v09_path: Path = V09_PATH,
    output_path: Path = OUTPUT_PATH,
) -> dict[str, Any]:
    data = json.loads(proof_path.read_text(encoding="utf-8"))
    v09_data = (
        json.loads(v09_path.read_text(encoding="utf-8")) if v09_path.exists() else {}
    )
    report = build_plan(data, v09_data=v09_data)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, default=V01_PROOF_PATH)
    parser.add_argument("--v09", type=Path, default=V09_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = run_plan(proof_path=args.proof, v09_path=args.v09, output_path=args.output)
    print(
        json.dumps(
            {
                "target_160_candidates": report["planning_population"][
                    "target_160_candidate_count"
                ],
                "target_200_candidates": report["planning_population"][
                    "target_200_candidate_count"
                ],
                "recommendation": report["recommendation"]["label"],
                "network_calls": report["contract"]["network_calls"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
