"""Offline inventory for continuous Market-Bar research streams (V0.7).

This module audits cached source coverage only.  It freezes the V0.6 geometry,
does not calculate moving averages, and never bridges an unresolved gap.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
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
    "data/processed/strategy_review/market_bar_continuous_research_stream_audit_v0_7.json"
)
V03_PATH = Path(
    "data/processed/strategy_review/source_aligned_market_bar_design_audit_v0_3.json"
)


def _island_inventory(
    v01: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_map = _load_unique_sources(v01)
    all_islands: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for stock in sorted(
        {_stock_id for _stock_id in (sid.split(":", 1)[0] for sid in source_map)}
    ):
        sources = [s for sid, s in source_map.items() if sid.startswith(f"{stock}:")]
        for run_index, run in enumerate(_partition_runs(sources), 1):
            islands, unresolved, _ = _resolve_islands(run, stock)
            gaps.extend(unresolved)
            for index, island in enumerate(islands, 1):
                dates = [
                    x.get("calendar_start_date")
                    for x in island.get("bars", [])
                    if x.get("calendar_start_date")
                ] + [
                    x.get("calendar_end_date")
                    for x in island.get("bars", [])
                    if x.get("calendar_end_date")
                ]
                all_islands.append(
                    {
                        "island_id": f"{stock}:RUN{run_index}:ISLAND{index}",
                        "stock_code": stock,
                        "run_index": run_index,
                        "island_index": index,
                        "calendar_start": min(dates) if dates else None,
                        "calendar_end": max(dates) if dates else None,
                        "source_tau": island.get("resolved_tau"),
                        "market_bar_count": island.get("market_bar_count", 0),
                        "source_segment_count": island.get("source_segment_count", 0),
                    }
                )
    return all_islands, gaps


def _nearby_missing(island: dict[str, Any], missing: list[dict[str, Any]]) -> int:
    start, end = island.get("calendar_start"), island.get("calendar_end")
    if not start or not end:
        return 0
    return sum(
        1
        for row in missing
        if row.get("stock_code") == island["stock_code"]
        and start <= str(row.get("trade_date")) <= end
    )


def audit_proof(v01: dict[str, Any]) -> dict[str, Any]:
    islands, multi_target_gaps = _island_inventory(v01)
    missing_fast = [
        x for x in v01.get("unresolved_sessions", []) if _dec(x.get("delta_tau", 0)) > 1
    ]
    for island in islands:
        island["missing_fast_session_count_nearby"] = _nearby_missing(
            island, missing_fast
        )
        n = int(island["market_bar_count"])
        island["MMA5_READY"] = n >= 5
        island["MMA10_READY"] = n >= 10
        island["MMA20_READY"] = n >= 20
        island["MMA60_READY"] = n >= 60
        island["bars_after_MMA60_warmup"] = max(n - 59, 0)
    by_stock: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for island in islands:
        by_stock[island["stock_code"]].append(island)
    stock_summary = []
    all_stocks = set(by_stock)
    all_stocks.update(str(x.get("stock_code")) for x in v01.get("runs", []))
    for stock in sorted(all_stocks):
        by_stock.setdefault(stock, [])
        ranked = sorted(
            by_stock[stock], key=lambda x: (-int(x["market_bar_count"]), x["island_id"])
        )
        stock_summary.append(
            {
                "stock_code": stock,
                "longest_island_bars": ranked[0]["market_bar_count"] if ranked else 0,
                "second_longest_island_bars": ranked[1]["market_bar_count"]
                if len(ranked) > 1
                else 0,
                "MMA20_ready_island_count": sum(x["MMA20_READY"] for x in ranked),
                "MMA60_ready_island_count": sum(x["MMA60_READY"] for x in ranked),
                "repairable_missing_fast_sessions": sum(
                    x["missing_fast_session_count_nearby"] for x in ranked
                ),
                "source_quality_anomaly_count": 0,
                "corporate_action_ambiguity_count": 0,
            }
        )
    gap_counts = Counter()
    gap_counts["MISSING_INTRADAY_FOR_FAST_SESSION"] = len(missing_fast)
    gap_counts["MULTI_TARGET_OVERNIGHT_GAP"] = sum(
        1 for x in multi_target_gaps if x.get("overnight")
    )
    gap_counts["CORPORATE_ACTION_AMBIGUOUS"] = 0
    gap_counts["SOURCE_START"] = len(by_stock)
    gap_counts["SOURCE_END"] = len(by_stock)
    gap_counts["OTHER"] = 0
    bridges = []
    for row in stock_summary:
        longest = int(row["longest_island_bars"])
        bridges.append(
            {
                "stock_code": row["stock_code"],
                "bars_needed_for_80": max(0, 80 - longest),
                "repairable_fast_sessions": row["repairable_missing_fast_sessions"],
                "bridge_80_possible_without_other_source": row[
                    "repairable_missing_fast_sessions"
                ]
                >= max(0, 80 - longest),
                "bars_needed_for_120": max(0, 120 - longest),
                "bridge_120_possible_without_other_source": row[
                    "repairable_missing_fast_sessions"
                ]
                >= max(0, 120 - longest),
            }
        )
    bridge_map = {x["stock_code"]: x for x in bridges}
    pilot_ranked = sorted(
        stock_summary,
        key=lambda x: (
            bridge_map[x["stock_code"]]["bars_needed_for_80"],
            -int(x["longest_island_bars"]),
            x["source_quality_anomaly_count"],
            x["corporate_action_ambiguity_count"],
            x["stock_code"],
        ),
    )
    return {
        "audit_version": "MARKET_BAR_CONTINUOUS_RESEARCH_STREAM_AUDIT_V0_7",
        "source_artifacts": {"v01": str(V01_PROOF_PATH), "v03": str(V03_PATH)},
        "contract": {
            "geometry": "V0.6 GLOBAL_TAU resolution gate frozen",
            "market_ma_calculation": False,
            "strategy": False,
            "gap_bridging": False,
            "network_calls": 0,
        },
        "population": {
            "resolved_island_count": len(islands),
            "stock_count": len(by_stock),
            "multi_target_gap_count": len(multi_target_gaps),
            "missing_fast_session_count": len(missing_fast),
        },
        "islands": islands,
        "stock_summary": stock_summary,
        "gap_reason_counts": dict(gap_counts),
        "structurally_unresolved_overnight_gaps": [
            x for x in multi_target_gaps if x.get("overnight")
        ],
        "repairable_fast_gap_inventory": [
            {
                "stock_code": x["stock_code"],
                "trade_date": x.get("source_start_datetime", "")[:10],
                "daily_delta_tau": x.get("segment_tau"),
                "nearby_island_bar_count": 0,
            }
            for x in missing_fast
        ],
        "bridge_analysis": bridges,
        "preferred_pilot_top3": [x["stock_code"] for x in pilot_ranked[:3]],
        "notes": [
            "MMA readiness is a bar-count readiness gate only; no MA values are calculated.",
            "Missing FAST sessions are potentially repairable, but no source is fetched or estimated.",
            "MULTI_TARGET_OVERNIGHT_GAP is structurally unresolved because its price path is unavailable.",
            "No full-history reconstruction or gap bridging is performed.",
        ],
    }


def run_audit(
    proof_path: Path = V01_PROOF_PATH, output_path: Path = OUTPUT_PATH
) -> dict[str, Any]:
    report = audit_proof(json.loads(proof_path.read_text(encoding="utf-8")))
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
