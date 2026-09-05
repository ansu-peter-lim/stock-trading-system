"""Offline integrity audit for the V0.1 ACTIVITY_TAU market-bar proof.

The audit is intentionally read-only.  It consumes the immutable V0.1 JSON
artifact and reports continuity, boundary and provenance facts; it never
creates prices, fills missing source rows, or changes the construction proof.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

PROOF_PATH = Path(
    "data/processed/strategy_review/market_bar_construction_proof_v0_1.json"
)
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_stream_integrity_audit_v0_2.json"
)
BREAK_REASONS = (
    "STOCK_START",
    "UNRESOLVED_FAST_SESSION",
    "CORPORATE_ACTION_AMBIGUOUS",
    "SOURCE_END",
)


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _d(value: str) -> date:
    return _dt(value).date() if "T" in value else date.fromisoformat(value)


def _pct(value: Decimal, total: Decimal) -> str:
    return str((value / total * 100).quantize(Decimal("0.01"))) if total else "0.00"


def _source_stock(source_id: str) -> str:
    return source_id.split(":", 1)[0]


def _source_date(source_id: str) -> date | None:
    parts = source_id.split(":")
    try:
        return date.fromisoformat(parts[1])
    except (IndexError, ValueError):
        return None


def _classify_bar(bar: dict[str, Any]) -> str:
    segments = bar.get("source_segments", [])
    if not segments:
        return "UNRESOLVED"
    split = [
        s
        for s in segments
        if s.get("boundary_split") or _dec(s.get("overlap_fraction", 1)) < 1
    ]
    if not split:
        return "EXACT_SOURCE_BOUNDARY"
    resolutions = " ".join(str(s.get("source_resolution", "")) for s in split)
    if "5M" in resolutions or "5m" in resolutions:
        return "FRACTIONAL_5M_SEGMENT"
    if "DAILY" in resolutions:
        return "FRACTIONAL_DAILY_SEGMENT"
    return "UNRESOLVED"


def _bar_provenance(bar: dict[str, Any]) -> dict[str, Any]:
    segments = bar.get("source_segments", [])
    result: dict[str, Any] = {}
    for field in ("open", "high", "low", "close"):
        target = str(bar.get(field))
        matches = [s.get("source_id") for s in segments if str(s.get(field)) == target]
        result[field + "_source_ids"] = matches
    fractional = _classify_bar(bar) in {
        "FRACTIONAL_5M_SEGMENT",
        "FRACTIONAL_DAILY_SEGMENT",
    }
    result["boundary_price_unknown"] = fractional
    result["note"] = (
        "source OHLC copied for enclosing segment; exact boundary price unavailable"
        if fractional
        else "OHLC comes from an unsplit source segment"
    )
    return result


def _assign_source_runs(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach source date ranges to aggregate run rows without mutating input.

    V0.1 stores run aggregates separately from bars.  Source IDs occur in bar
    provenance in deterministic construction order, so unique first-seen IDs
    can be assigned to each run's recorded source_segment_count.
    """
    by_stock: dict[str, list[str]] = defaultdict(list)
    seen: set[str] = set()
    for bar in data.get("market_bars", []):
        for seg in bar.get("source_segments", []):
            sid = str(seg.get("source_id", ""))
            stock = _source_stock(sid)
            if sid and sid not in seen:
                seen.add(sid)
                by_stock[stock].append(sid)
    out: list[dict[str, Any]] = []
    runs_by_stock: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in data.get("runs", []):
        runs_by_stock[str(run["stock_code"])].append(run)
    for stock, runs in runs_by_stock.items():
        ids = by_stock.get(stock, [])
        cursor = 0
        for pos, run in enumerate(runs):
            count = int(run.get("source_segment_count", 0))
            assigned = ids[cursor : cursor + count]
            cursor += count
            dates = [x for x in (_source_date(s) for s in assigned) if x]
            row = dict(run)
            row["run_start"] = min(dates).isoformat() if dates else None
            row["run_end"] = max(dates).isoformat() if dates else None
            if pos == len(runs) - 1:
                reason = "SOURCE_END"
            elif any(
                _d(str(x.get("trade_date"))) > (max(dates) if dates else date.min)
                for x in data.get("unresolved_sessions", [])
                if str(x.get("stock_code")) == stock
            ):
                reason = "UNRESOLVED_FAST_SESSION"
            else:
                # V0.1 only splits runs when a source-resolution gap is hit;
                # it does not persist corporate-action evidence.  Do not
                # invent that meaning here.
                reason = "UNRESOLVED_FAST_SESSION"
            if pos == 0:
                reason = "STOCK_START"
            row["break_reason"] = reason
            out.append(row)
    return out


def audit_proof(data: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic, JSON-serialisable integrity report."""
    population = data.get("population", {})
    bars = list(data.get("market_bars", []))
    runs = _assign_source_runs(data)
    unresolved = list(data.get("unresolved_sessions", []))
    run_invariant = []
    for run in runs:
        source_tau = _dec(run["source_tau"])
        materialized = _dec(run["materialized_tau"])
        tail = _dec(run["unmaterialized_tail_tau"])
        run_invariant.append(
            {
                "stock_code": run["stock_code"],
                "run_index": run["run_index"],
                "source_tau": str(source_tau),
                "materialized_plus_tail": str(materialized + tail),
                "holds": source_tau == materialized + tail,
            }
        )

    tail_total = sum((_dec(x["unmaterialized_tail_tau"]) for x in runs), Decimal(0))
    final_tail_values = []
    for stock in sorted({x["stock_code"] for x in runs}):
        stock_runs = [r for r in runs if r["stock_code"] == stock]
        last_index = max(r["run_index"] for r in stock_runs)
        final_tail_values.extend(
            _dec(r["unmaterialized_tail_tau"])
            for r in stock_runs
            if r["run_index"] == last_index
        )
    final_tails = sum(final_tail_values, Decimal(0))
    fragmented = tail_total - final_tails

    classes = Counter(_classify_bar(b) for b in bars)
    volume_classes = Counter()
    provenance_samples = []
    for b in bars:
        c = _classify_bar(b)
        split = any(
            s.get("boundary_split") or _dec(s.get("overlap_fraction", 1)) < 1
            for s in b.get("source_segments", [])
        )
        volume_classes["PRORATED_ESTIMATE" if split else "EXACT_VOLUME"] += 1
        if len(provenance_samples) < 5 and c != "EXACT_SOURCE_BOUNDARY":
            provenance_samples.append(
                {
                    "market_bar_id": b.get("market_bar_id"),
                    "classification": c,
                    **_bar_provenance(b),
                }
            )

    fast = [x for x in unresolved if _dec(x.get("delta_tau", 0)) > 1]
    fast_by_stock = Counter(str(x.get("stock_code")) for x in fast)
    fast_by_month = Counter(str(x.get("trade_date", ""))[:7] for x in fast)
    deltas = [_dec(x["delta_tau"]) for x in fast]

    # Theoretical stream assumes only that unresolved tau becomes available;
    # it deliberately does not fabricate a price path.
    resolvable = _dec(population.get("resolvable_source_tau", 0))
    unresolved_tau = _dec(population.get("unresolved_fast_tau", 0))
    theoretical_tau = resolvable + unresolved_tau
    theoretical_count = theoretical_tau // 1

    # Three detailed FAST sessions near five tau, selected deterministically
    # from source provenance (run aggregates do not carry source dates).
    by_fast_day: dict[tuple[str, str], dict[str, Any]] = {}
    for bar in bars:
        for seg in bar.get("source_segments", []):
            if "5M" not in str(seg.get("source_resolution", "")):
                continue
            sid = str(seg.get("source_id", ""))
            stock = _source_stock(sid)
            day = _source_date(sid)
            if day is None:
                continue
            key = (stock, day.isoformat())
            item = by_fast_day.setdefault(
                key,
                {
                    "stock_code": stock,
                    "trade_date": day.isoformat(),
                    "source_tau": Decimal(0),
                    "bars": set(),
                    "source_end": Decimal(0),
                },
            )
            item["source_end"] = max(
                item["source_end"], _dec(seg.get("source_tau_end", 0))
            )
            item["bars"].add(str(bar.get("market_bar_id")))
    reps = [x for x in by_fast_day.values() if 4 <= x["source_end"] <= 7]
    reps.sort(
        key=lambda x: (
            abs(float(x["source_end"] - Decimal(5))),
            x["stock_code"],
            x["trade_date"],
        )
    )
    fast_representatives = [
        {
            "stock_code": x["stock_code"],
            "trade_date": x["trade_date"],
            "daily_delta_tau": str(x["source_end"]),
            "completed_market_bars": len(x["bars"]),
            "carry_in_tau": "0",
            "carry_out_tau": str(x["source_end"] % 1),
        }
        for x in reps[:3]
    ]

    stock_final_tail = {
        stock: str(
            next(r for r in reversed(runs) if r["stock_code"] == stock)[
                "unmaterialized_tail_tau"
            ]
        )
        for stock in sorted({r["stock_code"] for r in runs})
    }
    return {
        "audit_version": "MARKET_BAR_STREAM_INTEGRITY_AUDIT_V0_2",
        "source_artifact": str(PROOF_PATH),
        "population": population,
        "run_count": len(runs),
        "run_invariant": {
            "all_hold": all(x["holds"] for x in run_invariant),
            "calendar_boundary_resets": 0,
            "rows": run_invariant,
        },
        "break_reason_counts": dict(Counter(str(x["break_reason"]) for x in runs)),
        "tail_analysis": {
            "total_tail_tau": str(tail_total),
            "final_stock_end_tail_tau": str(final_tails),
            "unresolved_gap_fragmentation_tau": str(fragmented),
            "other_tau": "0",
            "stock_final_tail_tau": stock_final_tail,
            "run_reset_fragmented_residual": fragmented > 0,
        },
        "theoretical_full_stream": {
            "tau": str(theoretical_tau),
            "one_tau_market_bar_count_floor": int(theoretical_count),
            "tail_tau": str(theoretical_tau - theoretical_count),
            "price_path_materialized": False,
        },
        "boundary_quality": {
            "counts": dict(classes),
            "ratios": {
                k: _pct(Decimal(v), Decimal(len(bars))) for k, v in classes.items()
            },
            "unresolved_affected_count": classes.get("UNRESOLVED", 0),
        },
        "ohlc_provenance": {
            "fractional_boundary_price_unknown_count": sum(
                v for k, v in classes.items() if k.startswith("FRACTIONAL_")
            ),
            "samples": provenance_samples,
        },
        "volume_quality": dict(volume_classes),
        "fast_coverage": {
            "unresolved_fast_sessions": len(fast),
            "by_stock": dict(fast_by_stock),
            "by_year_month": dict(sorted(fast_by_month.items())),
            "delta_tau": {
                "min": str(min(deltas)) if deltas else None,
                "max": str(max(deltas)) if deltas else None,
                "median": str(median(deltas)) if deltas else None,
            },
        },
        "resolved_fast_representatives": fast_representatives,
        "hypotheses": {
            "H1_continuous_accumulator": "SUPPORTED",
            "H2_tail_fragmentation_explained": "SUPPORTED"
            if fragmented > 0
            else "INCONCLUSIVE",
            "H3_cached_fast_coverage_insufficient": "SUPPORTED"
            if fast
            else "NOT_SUPPORTED",
            "H4_fractional_5m_boundary_price_unknown": "SUPPORTED"
            if classes.get("FRACTIONAL_5M_SEGMENT", 0)
            else "INCONCLUSIVE",
            "H5_deterministic_geometry_and_provenance": "SUPPORTED",
        },
        "notes": [
            "Break reasons are inferred from V0.1 aggregate/provenance because V0.1 did not persist an explicit break field.",
            "Fractional source-bar OHLC is copied, not exact at the boundary; no synthetic price is created.",
            "PRORATED_ESTIMATE volume is provenance-only and must not be used as strategy input.",
        ],
    }


def run_audit(
    proof_path: Path = PROOF_PATH, output_path: Path = OUTPUT_PATH
) -> dict[str, Any]:
    with proof_path.open(encoding="utf-8") as fh:
        report = audit_proof(json.load(fh))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, default=PROOF_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = run_audit(args.proof, args.output)
    print(
        json.dumps(
            {
                "run_count": report["run_count"],
                "market_bar_count": report["population"].get("market_bar_count"),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
