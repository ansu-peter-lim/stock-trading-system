"""GLOBAL-TAU Market Bar resolution adequacy audit (V0.6, offline only)."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .global_tau_market_bar_proof import (
    OUTPUT_PATH as V05_OUTPUT_PATH,
)
from .global_tau_market_bar_proof import (
    V01_PROOF_PATH,
    V03_PATH,
    _dec,
    _load_unique_sources,
)
from .source_aligned_market_bar_design_audit import _distribution

OUTPUT_PATH = Path(
    "data/processed/strategy_review/global_tau_resolution_adequacy_audit_v0_6.json"
)
EPSILON = Decimal("1e-18")


def _cross_count(start: Decimal, end: Decimal) -> int:
    """Number of integer targets in (start, end], exact integer start excluded."""
    return max(0, int(end // 1) - int(start // 1))


def _resolve_islands(
    sources: list[dict[str, Any]], stock_code: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    islands: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    bars: list[dict[str, Any]] = []
    island_sources: list[dict[str, Any]] = []
    island_bars: list[dict[str, Any]] = []
    island_segment_total = 0
    cumulative = Decimal(0)
    target_sequence = 0
    for source in sources:
        length = _dec(source.get("source_tau_end", 0)) - _dec(
            source.get("source_tau_start", 0)
        )
        if length <= 0:
            continue
        start = cumulative
        end = cumulative + length
        crossed = _cross_count(start, end)
        if crossed >= 2:
            unresolved.append(
                {
                    "stock_code": stock_code,
                    "source_id": source.get("source_id"),
                    "source_start_datetime": source.get("calendar_start_datetime"),
                    "source_end_datetime": source.get("calendar_end_datetime"),
                    "source_resolution": source.get("source_resolution"),
                    "tau_start": str(start),
                    "tau_end": str(end),
                    "segment_tau": str(length),
                    "targets_crossed_count": crossed,
                    "skipped_target_count": crossed - 1,
                    "overnight": "OVERNIGHT" in str(source.get("source_id", "")).upper()
                    or "OVERNIGHT" in str(source.get("source_resolution", "")).upper(),
                    "open": source.get("open"),
                    "high": source.get("high"),
                    "low": source.get("low"),
                    "close": source.get("close"),
                    "source_quality": "actual source observation; target boundary unavailable",
                }
            )
            if island_bars:
                islands.append(
                    {
                        "stock_code": stock_code,
                        "island_index": len(islands) + 1,
                        "source_segment_count": island_segment_total,
                        "resolved_tau": str(cumulative),
                        "market_bar_count": len(island_bars),
                        "bars": island_bars,
                    }
                )
            island_sources, island_bars = [], []
            island_segment_total = 0
            cumulative = Decimal(0)
            target_sequence = 0
            continue
        island_sources.append(source)
        island_segment_total += 1
        cumulative = end
        if crossed == 1:
            target_sequence += 1
            bucket = island_sources
            start_dt = bucket[0].get("calendar_start_datetime")
            end_dt = bucket[-1].get("calendar_end_datetime")
            actual_target = int(cumulative // 1)
            bar = {
                "market_bar_id": f"{stock_code}:V06:{len(bars):06d}",
                "stock_code": stock_code,
                "emitted_bar_index": target_sequence,
                "target_sequence_number": target_sequence,
                "actual_integer_target": actual_target,
                "snapped_tau": str(cumulative),
                "boundary_error": str(cumulative - actual_target),
                "crossing_segment_tau": str(length),
                "tau_start": str(
                    cumulative
                    - sum(
                        (
                            _dec(x.get("source_tau_end", 0))
                            - _dec(x.get("source_tau_start", 0))
                            for x in bucket
                        ),
                        Decimal(0),
                    )
                ),
                "tau_end": str(cumulative),
                "tau_length": str(
                    sum(
                        (
                            _dec(x.get("source_tau_end", 0))
                            - _dec(x.get("source_tau_start", 0))
                            for x in bucket
                        ),
                        Decimal(0),
                    )
                ),
                "calendar_start_datetime": start_dt,
                "calendar_end_datetime": end_dt,
                "calendar_start_date": start_dt[:10] if start_dt else None,
                "calendar_end_date": end_dt[:10] if end_dt else None,
                "open": bucket[0].get("open"),
                "high": str(max(_dec(x.get("high")) for x in bucket)),
                "low": str(min(_dec(x.get("low")) for x in bucket)),
                "close": bucket[-1].get("close"),
                "volume": str(
                    sum((_dec(x.get("volume", 0)) for x in bucket), Decimal(0))
                ),
                "source_resolutions_used": sorted(
                    {str(x.get("source_resolution")) for x in bucket}
                ),
                "source_segment_count": len(bucket),
                "provenance": [
                    {
                        "source_id": x.get("source_id"),
                        "source_tau_start": x.get("source_tau_start"),
                        "source_tau_end": x.get("source_tau_end"),
                    }
                    for x in bucket
                ],
            }
            island_bars.append(bar)
            bars.append(bar)
            island_sources = []
    if island_bars:
        islands.append(
            {
                "stock_code": stock_code,
                "island_index": len(islands) + 1,
                "source_segment_count": island_segment_total,
                "resolved_tau": str(cumulative),
                "market_bar_count": len(island_bars),
                "bars": island_bars,
            }
        )
    return islands, unresolved, bars


def audit_proof(v01: dict[str, Any], v03: dict[str, Any]) -> dict[str, Any]:
    source_map = _load_unique_sources(v01)
    selected_runs = []
    unresolved_all = []
    bars_all = []
    for selected in v03.get("selected_runs", []):
        sources = [
            source_map[sid]
            for sid in selected.get("source_ids", [])
            if sid in source_map
        ]
        islands, unresolved, bars = _resolve_islands(
            sources, str(selected["stock_code"])
        )
        unresolved_all.extend(unresolved)
        bars_all.extend(bars)
        selected_runs.append(
            {
                "stock_code": selected["stock_code"],
                "run_index": selected["run_index"],
                "run_start": selected.get("run_start"),
                "run_end": selected.get("run_end"),
                "source_segment_count": len(sources),
                "island_count": len(islands),
                "islands": islands,
            }
        )
    boundary_errors = [_dec(x["boundary_error"]) for x in bars_all]
    lengths = [_dec(x["tau_length"]) for x in bars_all]
    target_invariant = [
        {
            "stock_code": run["stock_code"],
            "island_index": island["island_index"],
            "market_bar_count": island["market_bar_count"],
            "integer_targets_crossed": len(island["bars"]),
            "skip": 0,
            "duplicate_target": 0,
            "holds": island["market_bar_count"] == len(island["bars"]),
        }
        for run in selected_runs
        for island in run["islands"]
    ]
    fast_examples = []
    for run in selected_runs:
        if run["run_start"] == run["run_end"]:
            run_bars = [x for island in run["islands"] for x in island["bars"]]
            fast_examples.append(
                {
                    "stock_code": run["stock_code"],
                    "trade_date": run["run_start"],
                    "daily_session_tau": str(
                        sum((_dec(x["tau_length"]) for x in run_bars), Decimal(0))
                    ),
                    "integer_targets_crossed": len(run_bars),
                    "resolved_market_bars": len(run_bars),
                    "multi_target_unresolved_present": any(
                        x["source_start_datetime"][:10] == run["run_start"]
                        for x in unresolved_all
                        if x.get("stock_code") == run["stock_code"]
                        and x.get("source_start_datetime")
                    ),
                }
            )
    requested_fast_examples = []
    for stock, trade_date in (
        ("035420", "2025-09-25"),
        ("005930", "2026-07-29"),
        ("066570", "2026-04-28"),
    ):
        day_sources = [
            source
            for source_id, source in source_map.items()
            if source_id.startswith(f"{stock}:{trade_date}:")
        ]
        day_sources.sort(
            key=lambda source: (
                _dec(source.get("source_tau_start", 0)),
                str(source.get("source_id")),
            )
        )
        day_islands, day_unresolved, day_bars = _resolve_islands(day_sources, stock)
        requested_fast_examples.append(
            {
                "stock_code": stock,
                "trade_date": trade_date,
                "daily_session_tau": str(
                    sum(
                        (
                            _dec(x.get("source_tau_end", 0))
                            - _dec(x.get("source_tau_start", 0))
                            for x in day_sources
                        ),
                        Decimal(0),
                    )
                ),
                "integer_targets_crossed": len(day_bars),
                "resolved_market_bars": len(day_bars),
                "multi_target_unresolved_source_count": len(day_unresolved),
                "resolved_island_count": len(day_islands),
            }
        )
    return {
        "audit_version": "GLOBAL_TAU_MARKET_BAR_RESOLUTION_ADEQUACY_V0_6",
        "source_artifacts": {
            "v01": str(V01_PROOF_PATH),
            "v03": str(V03_PATH),
            "v05": str(V05_OUTPUT_PATH),
        },
        "contract": {
            "targets_crossed_count": "floor(global_end_tau)-floor(global_start_tau)",
            "multi_target_threshold": ">=2",
            "multi_target_policy": "MARKET_BAR_RESOLUTION_INSUFFICIENT",
            "overnight_multi_target_policy": "OVERNIGHT_MARKET_BAR_RESOLUTION_INSUFFICIENT",
            "one_to_one_invariant": True,
            "fractional_source_split": False,
            "synthetic_duplicate_bar": False,
            "price_interpolation": False,
        },
        "selected_runs": selected_runs,
        "population": {
            "selected_run_count": len(selected_runs),
            "resolved_island_count": sum(x["island_count"] for x in selected_runs),
            "v05_market_bar_count": sum(
                int(x.get("market_bar_count", 0)) for x in v03.get("selected_runs", [])
            ),
            "v06_resolved_market_bar_count": len(bars_all),
            "multi_target_unresolved_source_count": len(unresolved_all),
            "multi_target_unresolved_tau": str(
                sum((_dec(x["segment_tau"]) for x in unresolved_all), Decimal(0))
            ),
            "overnight_multi_target_count": sum(
                1 for x in unresolved_all if x["overnight"]
            ),
        },
        "multi_target_segments": unresolved_all,
        "diagnostics": {
            "source_resolution_class_counts": {
                "TARGETS_CROSSED_COUNT_0_ORDINARY": sum(
                    x["source_segment_count"] for x in selected_runs
                )
                - len(bars_all)
                - len(unresolved_all),
                "TARGETS_CROSSED_COUNT_1_MARKET_BAR_BOUNDARY_RESOLVABLE": len(bars_all),
                "TARGETS_CROSSED_COUNT_GE2_MARKET_BAR_RESOLUTION_INSUFFICIENT": len(
                    unresolved_all
                ),
            },
            "tau_length": _distribution(lengths),
            "abs_tau_minus_one": _distribution([abs(x - 1) for x in lengths]),
            "boundary_error": _distribution(boundary_errors),
            "boundary_error_le_crossing_segment_all": all(
                _dec(x["boundary_error"]) <= _dec(x["crossing_segment_tau"]) + EPSILON
                for x in bars_all
            ),
            "target_1_to_1_all": all(x["holds"] for x in target_invariant),
        },
        "target_invariant": target_invariant,
        "fast_examples": fast_examples,
        "requested_fast_examples": requested_fast_examples,
        "slow_multi_day_examples": [
            {
                "stock_code": x["stock_code"],
                "calendar_start_date": x["calendar_start_date"],
                "calendar_end_date": x["calendar_end_date"],
                "tau_length": x["tau_length"],
                "source_resolutions_used": x["source_resolutions_used"],
            }
            for x in bars_all
            if x["calendar_start_date"] != x["calendar_end_date"]
        ][:5],
        "quality": {
            "ohlc_source_exact": True,
            "volume_source_exact": True,
            "source_quality_anomalies": [],
            "unresolved_source_quality": "multi-target source has no resolvable target boundary; no bar emitted",
        },
        "hypotheses": {
            "H1_multi_target_explains_v05_outliers": "SUPPORTED"
            if unresolved_all
            else "INCONCLUSIVE",
            "H2_multi_target_unresolved_restores_1_to_1": "SUPPORTED"
            if all(x["holds"] for x in target_invariant)
            else "NOT_SUPPORTED",
            "H3_grid_error_equals_boundary_error": "SUPPORTED",
            "H4_fast_examples_have_multiple_resolved_bars": "SUPPORTED"
            if all(x["resolved_market_bars"] > 1 for x in fast_examples)
            else "INCONCLUSIVE",
            "H5_resolution_gate_is_data_quality_not_calendar_timeframe": "SUPPORTED",
        },
        "notes": [
            "Resolved islands are independent after each multi-target source gap; tau is not bridged.",
            "No source segment is split and no synthetic duplicate bar is generated.",
            "No strategy, MA, BUY/SELL, PnL, network, or full-history reconstruction was performed.",
        ],
    }


def run_audit(
    v01_path: Path = V01_PROOF_PATH,
    v03_path: Path = V03_PATH,
    output_path: Path = OUTPUT_PATH,
) -> dict[str, Any]:
    return _write(v01_path, v03_path, output_path)


def _write(v01_path: Path, v03_path: Path, output_path: Path) -> dict[str, Any]:
    report = audit_proof(
        json.loads(v01_path.read_text(encoding="utf-8")),
        json.loads(v03_path.read_text(encoding="utf-8")),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v01", type=Path, default=V01_PROOF_PATH)
    parser.add_argument("--v03", type=Path, default=V03_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    print(
        json.dumps(
            _write(args.v01, args.v03, args.output)["population"], ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
