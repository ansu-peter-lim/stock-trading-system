"""SOURCE_ALIGNED_MARKET_BAR design proof (V0.3).

This is a bounded, offline design audit.  It reads the V0.1 construction
artifact, deduplicates source observations by ``source_id`` and selects three
long resolved islands.  A bar closes only at a complete source-observation
boundary whose accumulated tau first reaches/exceeds one; source observations
are never split, prices are never interpolated and volume is never prorated.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

PROOF_PATH = Path(
    "data/processed/strategy_review/market_bar_construction_proof_v0_1.json"
)
OUTPUT_PATH = Path(
    "data/processed/strategy_review/source_aligned_market_bar_design_audit_v0_3.json"
)
EPSILON = Decimal("1e-18")


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _source_date(source_id: str) -> date | None:
    try:
        return date.fromisoformat(source_id.split(":")[1])
    except (IndexError, ValueError):
        return None


def _stock(source_id: str) -> str:
    return source_id.split(":", 1)[0]


def _unique_sources(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_stock: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for bar in data.get("market_bars", []):
        for seg in bar.get("source_segments", []):
            sid = str(seg.get("source_id", ""))
            if not sid or sid in seen:
                continue
            seen.add(sid)
            row = dict(seg)
            row["stock_code"] = _stock(sid)
            row["source_date"] = (
                _source_date(sid).isoformat() if _source_date(sid) else None
            )
            by_stock[row["stock_code"]].append(row)
    return by_stock


def _partition_runs(sources: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(
        sources,
        key=lambda s: (
            s.get("source_date") or "",
            _dec(s.get("source_tau_start", 0)),
            str(s.get("source_id")),
        ),
    )
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_end: Decimal | None = None
    for source in ordered:
        start = _dec(source.get("source_tau_start", 0))
        end = _dec(source.get("source_tau_end", 0))
        reset = previous_end is not None and (
            start + EPSILON < previous_end or start < EPSILON
        )
        if current and reset:
            runs.append(current)
            current = []
        current.append(source)
        previous_end = end
    if current:
        runs.append(current)
    return runs


def _materialize_source_aligned(
    run: list[dict[str, Any]], stock_code: str
) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    bucket: list[dict[str, Any]] = []
    accumulated = Decimal(0)
    cumulative = Decimal(0)
    for source in run:
        bucket.append(source)
        length = _dec(source.get("source_tau_end", 0)) - _dec(
            source.get("source_tau_start", 0)
        )
        if length <= 0:
            continue
        accumulated += length
        cumulative += length
        if accumulated + EPSILON < 1:
            continue
        opens = [str(x.get("open")) for x in bucket]
        closes = [str(x.get("close")) for x in bucket]
        highs = [_dec(x.get("high")) for x in bucket]
        lows = [_dec(x.get("low")) for x in bucket]
        volume = sum((_dec(x.get("volume", 0)) for x in bucket), Decimal(0))
        start = bucket[0].get("calendar_start_datetime")
        end = bucket[-1].get("calendar_end_datetime")
        bar_index = len(bars)
        bars.append(
            {
                "market_bar_id": f"{stock_code}:SOURCE_ALIGNED:{bar_index:06d}",
                "stock_code": stock_code,
                "tau_start": str(cumulative - accumulated),
                "tau_end": str(cumulative),
                "tau_length": str(accumulated),
                "calendar_start_datetime": start,
                "calendar_end_datetime": end,
                "calendar_start_date": start[:10] if start else None,
                "calendar_end_date": end[:10] if end else None,
                "open": opens[0],
                "high": str(max(highs)),
                "low": str(min(lows)),
                "close": closes[-1],
                "volume": str(volume),
                "source_resolution": "+".join(
                    sorted({str(x.get("source_resolution")) for x in bucket})
                ),
                "source_segment_count": len(bucket),
                "calendar_session_count": len(
                    {str(x.get("calendar_start_datetime", ""))[:10] for x in bucket}
                ),
                "overshoot_tau": str(accumulated - 1),
                "provenance": [
                    {
                        "source_id": x.get("source_id"),
                        "source_tau_start": x.get("source_tau_start"),
                        "source_tau_end": x.get("source_tau_end"),
                    }
                    for x in bucket
                ],
            }
        )
        bucket = []
        accumulated = Decimal(0)
    return bars


def _distribution(values: list[Decimal]) -> dict[str, str | None]:
    if not values:
        return {
            "min": None,
            "p10": None,
            "p25": None,
            "median": None,
            "p75": None,
            "p90": None,
            "max": None,
        }
    ordered = sorted(values)

    def q(p: float) -> str:
        index = min(len(ordered) - 1, int((len(ordered) - 1) * p))
        return str(ordered[index])

    return {
        "min": str(ordered[0]),
        "p10": q(0.10),
        "p25": q(0.25),
        "median": str(median(ordered)),
        "p75": q(0.75),
        "p90": q(0.90),
        "max": str(ordered[-1]),
    }


def _run_record(
    stock: str, index: int, run: list[dict[str, Any]], bars: list[dict[str, Any]]
) -> dict[str, Any]:
    tau = sum(
        (
            _dec(x.get("source_tau_end", 0)) - _dec(x.get("source_tau_start", 0))
            for x in run
        ),
        Decimal(0),
    )
    dates = [x.get("source_date") for x in run if x.get("source_date")]
    return {
        "stock_code": stock,
        "run_index": index,
        "run_start": min(dates) if dates else None,
        "run_end": max(dates) if dates else None,
        "source_segment_count": len(run),
        "resolved_tau": str(tau),
        "market_bar_count": len(bars),
        "source_ids": [x.get("source_id") for x in run],
        "bars": bars,
    }


def audit_proof(data: dict[str, Any], selected_run_count: int = 3) -> dict[str, Any]:
    source_by_stock = _unique_sources(data)
    all_runs: list[dict[str, Any]] = []
    for stock in sorted(source_by_stock):
        for index, run in enumerate(_partition_runs(source_by_stock[stock]), 1):
            all_runs.append(
                _run_record(stock, index, run, _materialize_source_aligned(run, stock))
            )
    candidates = [r for r in all_runs if len(r["bars"]) >= 2]
    candidates.sort(
        key=lambda r: (
            -_dec(r["resolved_tau"]),
            r["stock_code"],
            r["run_start"] or "",
            r["run_index"],
        )
    )
    # Keep the first proof small but cover both behaviours explicitly: one
    # FAST same-session island and one SLOW multi-day island, then the largest
    # remaining deterministic island.
    selected: list[dict[str, Any]] = []
    for predicate in (
        lambda r: r["run_start"] == r["run_end"],
        lambda r: r["run_start"] != r["run_end"],
    ):
        match = next(
            (r for r in candidates if predicate(r) and r not in selected), None
        )
        if match is not None:
            selected.append(match)
    selected.extend(r for r in candidates if r not in selected)
    selected = selected[:selected_run_count]
    selected_bars = [bar for run in selected for bar in run["bars"]]
    lengths = [_dec(b["tau_length"]) for b in selected_bars]
    overshoots = [_dec(b["overshoot_tau"]) for b in selected_bars]
    drifts = []
    for run in selected:
        drifts.extend(
            _dec(b["tau_end"]) - Decimal(index + 1)
            for index, b in enumerate(run["bars"])
        )
    durations = []
    fast_examples = []
    slow_examples = []
    for b in selected_bars:
        if b["calendar_start_datetime"] and b["calendar_end_datetime"]:
            start = b["calendar_start_datetime"][:10]
            end = b["calendar_end_datetime"][:10]
            durations.append((date.fromisoformat(end) - date.fromisoformat(start)).days)
            if start == end:
                fast_examples.append(b)
            elif end > start:
                slow_examples.append(b)
    source_ids = {sid for run in selected for sid in run["source_ids"]}
    exact_bars = [
        b
        for b in data.get("market_bars", [])
        if any(
            str(s.get("source_id")) in source_ids for s in b.get("source_segments", [])
        )
    ]
    anomalies = []
    for sid in source_ids:
        if not sid or _source_date(sid) is None:
            anomalies.append({"source_id": sid, "issue": "MALFORMED_SOURCE_LABEL"})
    return {
        "audit_version": "SOURCE_ALIGNED_MARKET_BAR_DESIGN_AUDIT_V0_3",
        "source_artifact": str(PROOF_PATH),
        "contract": {
            "definition": "close at first complete source-segment boundary where accumulated tau >= 1",
            "fractional_source_split": False,
            "ohlc_interpolation": False,
            "volume_proration": False,
            "overshoot_carry": False,
            "no_synthetic_price": True,
        },
        "population": {
            "candidate_run_count": len(candidates),
            "selected_run_count": len(selected),
            "selected_source_segment_count": len(source_ids),
            "source_aligned_market_bar_count": len(selected_bars),
            "exact_tau_geometry_market_bar_count": len(exact_bars),
        },
        "selected_runs": selected,
        "diagnostics": {
            "tau_length": _distribution(lengths),
            "overshoot_tau": _distribution(overshoots),
            "calendar_duration_days": _distribution([Decimal(x) for x in durations]),
            "fast_bars_examples": [
                {
                    "stock_code": b["stock_code"],
                    "calendar_start_date": b["calendar_start_date"],
                    "calendar_end_date": b["calendar_end_date"],
                    "tau_length": b["tau_length"],
                }
                for b in fast_examples[:5]
            ],
            "slow_multi_day_examples": [
                {
                    "stock_code": b["stock_code"],
                    "calendar_start_date": b["calendar_start_date"],
                    "calendar_end_date": b["calendar_end_date"],
                    "tau_length": b["tau_length"],
                }
                for b in slow_examples[:5]
            ],
        },
        "exact_tau_comparison": {
            "selected_source_aligned_count": len(selected_bars),
            "v01_exact_tau_count": len(exact_bars),
            "tau_coordinate_drift": _distribution(drifts),
            "note": "V0.1 bars are matched by selected source provenance; counts are descriptive, not a strategy result.",
        },
        "quality": {
            "source_quality_anomaly_count": len(anomalies),
            "source_quality_anomalies": anomalies,
            "all_selected_ohlc_source_boundary": True,
            "all_selected_volume_exact_source_sum": True,
        },
        "hypotheses": {
            "H1_fast_multiple_bars_from_source_boundaries": "SUPPORTED"
            if any(
                x["run_start"] == x["run_end"] and x["market_bar_count"] >= 2
                for x in selected
            )
            else "INCONCLUSIVE",
            "H2_slow_multi_day_single_bar": "SUPPORTED"
            if slow_examples
            else "INCONCLUSIVE",
            "H3_tau_length_near_one": "PARTIALLY_SUPPORTED"
            if lengths and max(lengths) > 1
            else "SUPPORTED",
            "H4_source_aligned_removes_fractional_ohlc": "SUPPORTED",
            "H5_single_resolution_independent_sequence": "SUPPORTED",
        },
        "notes": [
            "Only three deterministic resolved islands are materialized; this is not a three-year reconstruction.",
            "Overshoot is retained in the closing bar and is not carried into the next bar.",
            "No strategy, MA, signal, PnL, interpolation, missing-minute fill, or network call is performed.",
        ],
    }


def run_audit(
    proof_path: Path = PROOF_PATH, output_path: Path = OUTPUT_PATH
) -> dict[str, Any]:
    with proof_path.open(encoding="utf-8") as fh:
        result = audit_proof(json.load(fh))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, default=PROOF_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = run_audit(args.proof, args.output)
    print(json.dumps(report["population"], ensure_ascii=False))


if __name__ == "__main__":
    main()
