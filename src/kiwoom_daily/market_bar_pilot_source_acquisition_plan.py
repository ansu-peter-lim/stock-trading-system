"""Offline pilot source-acquisition planning for Market Bars (V0.8).

This module plans targeted minute requests from cached Daily/source metadata.
It never calls an API, downloads a minute bar, builds a Market Bar, or
calculates an indicator.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .global_tau_resolution_adequacy_audit import (
    V01_PROOF_PATH,
    _dec,
    _load_unique_sources,
    _resolve_islands,
)
from .source_aligned_market_bar_design_audit import _partition_runs

OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_pilot_source_acquisition_plan_v0_8.json"
)
MIN_MARKET_BARS = 80
REFERENCE_MARKET_BARS = 120


def _date_from_source(source_id: str) -> date | None:
    try:
        return date.fromisoformat(source_id.split(":")[1])
    except (IndexError, ValueError):
        return None


def _session_inventory(
    data: dict[str, Any],
) -> tuple[dict[str, dict[str, dict[str, Any]]], set[tuple[str, str]]]:
    sources = _load_unique_sources(data)
    sessions: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for source_id, source in sources.items():
        day = _date_from_source(source_id)
        if day is None or "OVERNIGHT" in source_id.upper():
            continue
        stock = source_id.split(":", 1)[0]
        item = sessions[stock].setdefault(
            day.isoformat(),
            {"daily_tau": Decimal(0), "cached_fast": False, "source_count": 0},
        )
        length = _dec(source.get("source_tau_end", 0)) - _dec(
            source.get("source_tau_start", 0)
        )
        item["daily_tau"] += max(length, Decimal(0))
        item["source_count"] += 1
        if "5M" in source_id.upper() or "30M" in source_id.upper():
            item["cached_fast"] = True
    missing: set[tuple[str, str]] = set()
    for row in data.get("unresolved_sessions", []):
        if _dec(row.get("delta_tau", 0)) <= 1:
            continue
        stock, trade_date = str(row.get("stock_code")), str(row.get("trade_date"))
        item = sessions[stock].setdefault(
            trade_date,
            {"daily_tau": Decimal(0), "cached_fast": False, "source_count": 0},
        )
        item["daily_tau"] = max(item["daily_tau"], _dec(row.get("delta_tau", 0)))
        missing.add((stock, trade_date))
    return sessions, missing


def _structural_gap_dates(data: dict[str, Any]) -> set[tuple[str, str]]:
    sources = _load_unique_sources(data)
    gaps: set[tuple[str, str]] = set()
    by_stock: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source_id, source in sources.items():
        by_stock[source_id.split(":", 1)[0]].append(source)
    for stock, values in by_stock.items():
        for run in _partition_runs(values):
            _, unresolved, _ = _resolve_islands(run, stock)
            for row in unresolved:
                start = str(row.get("source_start_datetime", ""))[:10]
                if start:
                    gaps.add((stock, start))
    return gaps


def _windows_for_stock(
    stock: str,
    sessions: dict[str, dict[str, Any]],
    structural: set[tuple[str, str]],
    target: int,
) -> list[dict[str, Any]]:
    dates = sorted(sessions)
    windows: list[dict[str, Any]] = []
    segment: list[str] = []
    for day in dates + [None]:
        if day is None or (stock, day) in structural:
            if segment:
                windows.extend(_segment_windows(stock, segment, sessions, target))
            segment = []
            continue
        segment.append(day)
    return windows


def _segment_windows(
    stock: str, dates: list[str], sessions: dict[str, dict[str, Any]], target: int
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    left = 0
    total = Decimal(0)
    for right, day in enumerate(dates):
        total += sessions[day]["daily_tau"]
        while left < right and total - sessions[dates[left]]["daily_tau"] >= target:
            total -= sessions[dates[left]]["daily_tau"]
            left += 1
        if total < target:
            continue
        window_dates = dates[left : right + 1]
        missing = [
            x
            for x in window_dates
            if not sessions[x]["cached_fast"] and sessions[x]["daily_tau"] >= 1
        ]
        known_tau = sum(
            (
                sessions[x]["daily_tau"]
                for x in window_dates
                if sessions[x]["cached_fast"] or sessions[x]["daily_tau"] < 1
            ),
            Decimal(0),
        )
        result.append(
            {
                "stock_code": stock,
                "calendar_start": window_dates[0],
                "calendar_end": window_dates[-1],
                "calendar_session_count": len(window_dates),
                "total_daily_tau": str(total),
                "expected_market_bar_capacity": int(total),
                "currently_resolved_market_bars": int(known_tau),
                "missing_fast_session_count": len(missing),
                "cached_fast_session_count": len(window_dates) - len(missing),
                "structural_gap_count": 0,
                "missing_fast_calendar_span": (
                    date.fromisoformat(missing[-1]) - date.fromisoformat(missing[0])
                ).days
                + 1
                if missing
                else 0,
                "missing_fast_tau_sum": str(
                    sum((sessions[x]["daily_tau"] for x in missing), Decimal(0))
                ),
                "repairable_session_dates": missing,
                "source_quality_anomaly_count": 0,
            }
        )
    return result


def _candidate_sort_key(window: dict[str, Any]) -> tuple[object, ...]:
    """Return the documented deterministic candidate ranking key."""
    return (
        window["structural_gap_count"],
        window["missing_fast_session_count"],
        window["calendar_session_count"],
        -window["expected_market_bar_capacity"],
        window["source_quality_anomaly_count"],
        window["stock_code"],
    )


def audit_plan(data: dict[str, Any]) -> dict[str, Any]:
    sessions, missing = _session_inventory(data)
    structural = _structural_gap_dates(data)
    candidates80 = [
        w
        for stock, values in sessions.items()
        for w in _windows_for_stock(stock, values, structural, MIN_MARKET_BARS)
    ]
    candidates120 = [
        w
        for stock, values in sessions.items()
        for w in _windows_for_stock(stock, values, structural, REFERENCE_MARKET_BARS)
    ]
    candidates80.sort(key=_candidate_sort_key)
    candidates120.sort(key=_candidate_sort_key)
    top3 = candidates80[:3]
    requested = [
        {
            "stock_code": w["stock_code"],
            "date": d,
            "daily_delta_tau": str(sessions[w["stock_code"]][d]["daily_tau"]),
            "reason": "FAST_MARKET_BAR_RESOLUTION_REQUIRED",
            "cached_minute_exists": sessions[w["stock_code"]][d]["cached_fast"],
            "planned_fetch_required": not sessions[w["stock_code"]][d]["cached_fast"],
        }
        for w in top3
        for d in w["repairable_session_dates"]
    ]
    preferred = top3[0] if top3 else None
    return {
        "audit_version": "MARKET_BAR_PILOT_SOURCE_ACQUISITION_PLAN_V0_8",
        "contract": {
            "global_tau_geometry": "V0.6 frozen",
            "expected_capacity_definition": "floor(total_daily_tau), planning estimate only",
            "network_calls": 0,
            "minute_fetch": False,
            "market_bar_materialization": False,
            "strategy": False,
        },
        "population": {
            "stock_count": len(sessions),
            "structural_gap_date_count": len(structural),
            "candidate_window_count_80": len(candidates80),
            "candidate_window_count_120": len(candidates120),
            "missing_fast_session_count": len(missing),
        },
        "candidate_top10_80": candidates80[:10],
        "candidate_top10_120": candidates120[:10],
        "top3_exact_missing_fast_session_list": requested,
        "preferred_pilot": preferred,
        "preferred_pilot_80_vs_120": {
            "stock_code": preferred["stock_code"],
            "target_80": {
                k: preferred[k]
                for k in (
                    "calendar_start",
                    "calendar_end",
                    "expected_market_bar_capacity",
                    "missing_fast_session_count",
                )
            },
            "target_120": next(
                (
                    w
                    for w in candidates120
                    if w["stock_code"] == preferred["stock_code"]
                ),
                None,
            ),
        }
        if preferred
        else None,
        "calendar_coverage": {
            "oldest_required_date": min(
                (d for values in sessions.values() for d in values), default=None
            ),
            "newest_required_date": max(
                (d for values in sessions.values() for d in values), default=None
            ),
            "api_availability_checked": False,
        },
        "gap_reason_counts": {
            "MISSING_INTRADAY_FOR_FAST_SESSION": len(missing),
            "MULTI_TARGET_OVERNIGHT_GAP": sum(
                1
                for stock, day in structural
                if any(x[0] == stock and x[1].startswith(day) for x in structural)
            ),
            "CORPORATE_ACTION_AMBIGUOUS": 0,
            "OTHER_STRUCTURAL_GAP": 0,
        },
        "success_hypotheses": {
            "H1_targeted_repairs_can_reach_80": "SUPPORTED"
            if preferred
            and preferred["missing_fast_session_count"]
            < preferred["calendar_session_count"]
            else "INCONCLUSIVE",
            "H2_targeted_minutes_enable_MMA60_stream": "INCONCLUSIVE",
            "H3_120_cost_tradeoff_visible": "SUPPORTED"
            if preferred
            else "INCONCLUSIVE",
        },
        "notes": [
            "Candidate windows are planning-only; expected capacity is not a MarketBar OHLC count.",
            "Structural gaps are excluded rather than bridged.",
            "No API availability or credential check was performed.",
        ],
        "source_artifact": str(V01_PROOF_PATH),
    }


def run_audit(
    proof_path: Path = V01_PROOF_PATH, output_path: Path = OUTPUT_PATH
) -> dict[str, Any]:
    report = audit_plan(json.loads(proof_path.read_text(encoding="utf-8")))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, default=V01_PROOF_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    print(
        json.dumps(run_audit(args.proof, args.output)["population"], ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
