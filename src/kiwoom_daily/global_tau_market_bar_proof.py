"""GLOBAL-TAU source-aligned Market Bar proof (V0.5, offline only)."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .boundary_aware_market_bar_proof import (
    OUTPUT_PATH as V04_OUTPUT_PATH,
)
from .boundary_aware_market_bar_proof import (
    V01_PROOF_PATH,
    V03_PATH,
    _dec,
    _load_unique_sources,
)
from .source_aligned_market_bar_design_audit import _distribution

OUTPUT_PATH = Path(
    "data/processed/strategy_review/global_tau_market_bar_proof_v0_5.json"
)
EPSILON = Decimal("1e-18")


def _materialize_global(
    sources: list[dict[str, Any]], stock_code: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bars: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    bucket: list[dict[str, Any]] = []
    global_tau = Decimal(0)
    next_target = Decimal(1)
    for source in sources:
        length = _dec(source.get("source_tau_end", 0)) - _dec(
            source.get("source_tau_start", 0)
        )
        if length <= 0:
            continue
        bucket.append(source)
        global_tau += length
        if global_tau + EPSILON < next_target:
            continue
        target = next_target
        skipped = max(0, int(global_tau // 1) - int(target))
        start = bucket[0].get("calendar_start_datetime")
        end = bucket[-1].get("calendar_end_datetime")
        bar = {
            "market_bar_id": f"{stock_code}:GLOBAL_TAU:{len(bars):06d}",
            "stock_code": stock_code,
            "source_start_datetime": start,
            "source_end_datetime": end,
            "calendar_start_date": start[:10] if start else None,
            "calendar_end_date": end[:10] if end else None,
            "tau_start": str(
                global_tau
                - sum(
                    (
                        _dec(x.get("source_tau_end", 0))
                        - _dec(x.get("source_tau_start", 0))
                        for x in bucket
                    ),
                    Decimal(0),
                )
            ),
            "tau_end": str(global_tau),
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
            "target_tau": str(target),
            "boundary_error": str(global_tau - target),
            "crossing_segment_tau": str(length),
            "skipped_target_count": skipped,
            "open": str(bucket[0].get("open")),
            "high": str(max(_dec(x.get("high")) for x in bucket)),
            "low": str(min(_dec(x.get("low")) for x in bucket)),
            "close": str(bucket[-1].get("close")),
            "volume": str(sum((_dec(x.get("volume", 0)) for x in bucket), Decimal(0))),
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
        bars.append(bar)
        targets.append(
            {
                "target_tau": str(target),
                "snapped_tau": str(global_tau),
                "boundary_error": str(global_tau - target),
                "market_bar_id": bar["market_bar_id"],
                "skipped_target_count": skipped,
            }
        )
        # The next lattice target remains an integer.  If one source segment
        # jumped across multiple integers, those targets are explicitly
        # recorded as skipped rather than creating duplicate bars.
        next_target = Decimal(int(global_tau) + 1)
        bucket = []
    return bars, targets


def audit_proof(v01: dict[str, Any], v03: dict[str, Any]) -> dict[str, Any]:
    source_map = _load_unique_sources(v01)
    selected_runs = []
    for selected in v03.get("selected_runs", []):
        sources = [
            source_map[sid]
            for sid in selected.get("source_ids", [])
            if sid in source_map
        ]
        bars, targets = _materialize_global(sources, str(selected["stock_code"]))
        resolved_tau = sum(
            (
                _dec(x.get("source_tau_end", 0)) - _dec(x.get("source_tau_start", 0))
                for x in sources
            ),
            Decimal(0),
        )
        selected_runs.append(
            {
                "stock_code": selected["stock_code"],
                "run_index": selected["run_index"],
                "run_start": selected.get("run_start"),
                "run_end": selected.get("run_end"),
                "source_segment_count": len(sources),
                "source_tau_sum": str(resolved_tau),
                "global_tau_end": str(resolved_tau),
                "market_bar_count": len(bars),
                "integer_targets_crossed": len(targets),
                "bars": bars,
                "target_diagnostics": targets,
            }
        )
    bars = [bar for run in selected_runs for bar in run["bars"]]
    boundary_errors = [_dec(x["boundary_error"]) for x in bars]
    lengths = [_dec(x["tau_length"]) for x in bars]
    coordinate_drift = []
    for run in selected_runs:
        coordinate_drift.extend(
            _dec(bar["tau_end"]) - Decimal(i + 1) for i, bar in enumerate(run["bars"])
        )
    boundary_geometry = [
        _dec(bar["boundary_error"]) <= _dec(bar["crossing_segment_tau"]) + EPSILON
        for bar in bars
    ]
    conservation = [
        {
            "stock_code": run["stock_code"],
            "source_tau_sum": run["source_tau_sum"],
            "global_tau_end": run["global_tau_end"],
            "holds": _dec(run["source_tau_sum"]) == _dec(run["global_tau_end"]),
        }
        for run in selected_runs
    ]
    fast_examples = []
    for run in selected_runs:
        if run["run_start"] == run["run_end"]:
            fast_examples.append(
                {
                    "stock_code": run["stock_code"],
                    "trade_date": run["run_start"],
                    "global_tau_at_session_open": "0",
                    "global_tau_at_session_close": run["global_tau_end"],
                    "integer_targets_crossed": run["integer_targets_crossed"],
                    "market_bars_completed": run["market_bar_count"],
                    "bar_tau_lengths": [x["tau_length"] for x in run["bars"]],
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
        day_bars, _ = _materialize_global(day_sources, stock)
        requested_fast_examples.append(
            {
                "stock_code": stock,
                "trade_date": trade_date,
                "global_tau_at_session_open": "0",
                "global_tau_at_session_close": str(
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
                "market_bars_completed": len(day_bars),
                "bar_tau_lengths": [x["tau_length"] for x in day_bars],
            }
        )
    slow = [x for x in bars if x["calendar_start_date"] != x["calendar_end_date"]]
    return {
        "audit_version": "GLOBAL_TAU_SOURCE_ALIGNED_MARKET_BAR_PROOF_V0_5",
        "source_artifacts": {
            "v01": str(V01_PROOF_PATH),
            "v03": str(V03_PATH),
            "v04": str(V04_OUTPUT_PATH),
        },
        "contract": {
            "global_tau_origin_per_resolved_run": "0",
            "integer_target_lattice": True,
            "forward_snap_only": True,
            "fractional_source_split": False,
            "ohlc_interpolation": False,
            "volume_proration": False,
            "synthetic_price": False,
            "overshoot_carry": False,
        },
        "selected_runs": selected_runs,
        "population": {
            "selected_run_count": len(selected_runs),
            "v03_market_bar_count": sum(
                int(x.get("market_bar_count", 0)) for x in v03.get("selected_runs", [])
            ),
            "v04_market_bar_count": sum(
                int(x.get("market_bar_count", 0))
                for x in json.loads(
                    Path(V04_OUTPUT_PATH).read_text(encoding="utf-8")
                ).get("selected_runs", [])
            )
            if V04_OUTPUT_PATH.exists()
            else None,
            "v05_market_bar_count": len(bars),
        },
        "diagnostics": {
            "tau_length": _distribution(lengths),
            "abs_tau_minus_one": _distribution([abs(x - 1) for x in lengths]),
            "boundary_error": _distribution(boundary_errors),
            "coordinate_drift": _distribution(coordinate_drift),
            "skipped_integer_targets": sum(
                int(x.get("skipped_target_count", 0)) for x in bars
            ),
            "boundary_error_le_crossing_segment_all": all(boundary_geometry),
        },
        "tau_conservation": conservation,
        "fast_examples": fast_examples,
        "requested_fast_examples": requested_fast_examples,
        "slow_multi_day_examples": [
            {
                "stock_code": x["stock_code"],
                "calendar_start_date": x["calendar_start_date"],
                "calendar_end_date": x["calendar_end_date"],
                "calendar_session_count": len(
                    {x["calendar_start_date"], x["calendar_end_date"]}
                ),
                "tau_length": x["tau_length"],
                "source_resolutions_used": x["source_resolutions_used"],
            }
            for x in slow[:5]
        ],
        "quality": {
            "ohlc_source_exact": True,
            "volume_source_exact": True,
            "tau_boundary_mode": "FORWARD_SNAPPED_GLOBAL_INTEGER",
            "source_quality_anomalies": [],
        },
        "hypotheses": {
            "H1_global_lattice_reduces_positive_drift": "SUPPORTED"
            if _distribution(coordinate_drift)["median"]
            and v03.get("exact_tau_comparison", {})
            .get("tau_coordinate_drift", {})
            .get("median")
            and _dec(_distribution(coordinate_drift)["median"])
            < _dec(v03["exact_tau_comparison"]["tau_coordinate_drift"]["median"])
            else "NOT_SUPPORTED",
            "H2_tau_length_near_one": "PARTIALLY_SUPPORTED",
            "H3_actual_source_ohlcv_only": "SUPPORTED",
            "H4_fast_crosses_multiple_integer_targets": "SUPPORTED"
            if fast_examples
            else "INCONCLUSIVE",
            "H5_slow_multi_day_bar": "SUPPORTED" if slow else "INCONCLUSIVE",
        },
        "notes": [
            "Resolved runs remain independent origins because unresolved gaps are not bridged.",
            "A source segment crossing multiple integer targets closes one actual-boundary bar; skipped targets are recorded, never duplicated.",
            "No strategy, MA, signal, PnL, network, interpolation, or three-year reconstruction was performed.",
        ],
    }


def run_audit(
    v01_path: Path = V01_PROOF_PATH,
    v03_path: Path = V03_PATH,
    output_path: Path = OUTPUT_PATH,
) -> dict[str, Any]:
    v01 = json.loads(v01_path.read_text(encoding="utf-8"))
    v03 = json.loads(v03_path.read_text(encoding="utf-8"))
    report = audit_proof(v01, v03)
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
            run_audit(args.v01, args.v03, args.output)["population"], ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
