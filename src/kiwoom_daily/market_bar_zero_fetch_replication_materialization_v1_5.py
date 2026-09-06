"""Offline zero-fetch Market-Bar replication materialization (V1.5).

This module is intentionally narrower than the candidate planner.  It freezes
the two V1.4 zero-fetch candidates (035420 and 105560), indexes the timestamp
coverage of their existing immutable RAW minute artifacts, and materializes
each stream with the already-frozen V0.6 geometry.  It does not fetch data,
change tau geometry, calculate MMA roles, or make strategy/PnL decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from src.kiwoom_minute.pipeline import MinuteCollectionRequest, MinutePriceBasis

from .down_box_daily_execution_proof import _load_stock
from .market_bar_200_pilot_resume_materialization import (
    _quality_for_rows,
)
from .market_bar_mma_role_replication_candidate_plan_v1_4 import (
    MARKET_CLOCK_REFERENCE_PATH,
    _daily_timeline,
    _load_frozen_cuts,
)
from .market_bar_pilot_acquisition import MINUTE_ROOT, _build_segments, _materialize

OUTPUT_PATH = Path(
    "data/processed/strategy_review/"
    "market_bar_zero_fetch_replication_materialization_v1_5.json"
)
V14_PLAN_PATH = Path(
    "data/processed/strategy_review/"
    "market_bar_mma_role_replication_candidate_plan_v1_4.json"
)
V14_CHECKPOINT = "f017256f11874a7569af916dfde09d1b73ce5a94"
TARGET_MARKET_BARS = 200
MMA60_RESEARCH_START = 60
REGIMES = (
    "FAST_DIRECTIONAL_HIGH_EFF",
    "FAST_NOISY",
    "SLOW",
    "NORMAL_OTHER",
)
FROZEN_CANDIDATES = {
    "REPLICATION_B": {
        "stock_code": "035420",
        "calendar_start": "2025-12-15",
        "calendar_end": "2026-06-19",
        "expected_market_bar_capacity": 200,
        "expected_pure_fast_directional_mb_capacity": 26,
        "expected_pure_slow_mb_capacity": 28,
        "new_fetch_required_session_count": 0,
    },
    "REPLICATION_C": {
        "stock_code": "105560",
        "calendar_start": "2026-01-05",
        "calendar_end": "2026-07-13",
        "expected_market_bar_capacity": 200,
        "expected_pure_fast_directional_mb_capacity": 20,
        "expected_pure_slow_mb_capacity": 33,
        "new_fetch_required_session_count": 0,
    },
}


class ReplicationMaterializationError(ValueError):
    """The frozen V1.5 offline proof inputs are not reproducible."""


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


def _load_frozen_candidates(path: Path) -> dict[str, dict[str, Any]]:
    """Require the exact B/C candidates from the V1.4 plan."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    found: dict[str, dict[str, Any]] = {}
    for item in payload.get("preferred_replication_set", []):
        label = str(item.get("replication_label", ""))
        if label in FROZEN_CANDIDATES and isinstance(item.get("candidate"), dict):
            found[label] = dict(item["candidate"])
    if set(found) != set(FROZEN_CANDIDATES):
        raise ReplicationMaterializationError("V1.4 B/C candidates are missing")
    for label, expected in FROZEN_CANDIDATES.items():
        actual = found[label]
        for field, value in expected.items():
            if actual.get(field) != value:
                raise ReplicationMaterializationError(
                    f"frozen candidate changed: {label}.{field}"
                )
    return found


def _raw_timestamp_index(
    stock_code: str, root: Path
) -> tuple[dict[date, tuple[str, ...]], tuple[dict[str, Any], ...]]:
    """Index actual ``cntr_tm`` dates in every local page, not directory dates."""

    by_date: dict[date, set[str]] = {}
    pages: list[dict[str, Any]] = []
    for path in sorted((root / stock_code / "raw").glob("**/page-*.json")):
        try:
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        digest = hashlib.sha256(raw).hexdigest()
        covered: set[date] = set()
        values = payload.values() if isinstance(payload, dict) else ()
        for value in values:
            if not isinstance(value, list):
                continue
            for row in value:
                label = row.get("cntr_tm") if isinstance(row, dict) else None
                text = str(label) if label is not None else ""
                if len(text) < 8 or not text[:8].isdigit():
                    continue
                try:
                    day = date.fromisoformat(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
                except ValueError:
                    continue
                covered.add(day)
                by_date.setdefault(day, set()).add(path.as_posix())
        pages.append(
            {
                "raw_file_path": path.as_posix(),
                "raw_file_sha256": digest,
                "covered_dates": sorted(day.isoformat() for day in covered),
            }
        )
    return (
        {day: tuple(sorted(paths)) for day, paths in by_date.items()},
        tuple(pages),
    )


def _coverage_precheck(
    *,
    stock_code: str,
    window_rows: Sequence[Mapping[str, Any]],
    raw_root: Path,
) -> tuple[dict[str, Any], dict[date, tuple[Any, ...]]]:
    window_dates = [str(row["trade_date"]) for row in window_rows]
    # V1.4 defines required FAST sessions as delta_tau >= 1.  Both frozen
    # candidates have strictly positive minute-required deltas at this point;
    # keeping the predicate identical makes the estimate auditable.
    required = {
        row["trade_date"]
        for row in window_rows
        if row.get("delta_tau") is not None and _decimal(row["delta_tau"]) >= Decimal(1)
    }
    raw_index, raw_pages = _raw_timestamp_index(stock_code, raw_root)
    request = MinuteCollectionRequest(
        stock_code,
        min(row["trade_date"] for row in window_rows),
        max(row["trade_date"] for row in window_rows),
        MinutePriceBasis.RAW,
    )
    del request  # the request object validates the canonical source identity.
    from .market_bar_200_pilot_resume_materialization import _load_coverage

    rows_by_date, page_records, _, conflicts, _ = _load_coverage(
        raw_root=raw_root,
        stock_code=stock_code,
        window_dates=window_dates,
        required_fast_dates=required,
    )
    conflict_dates = {date.fromisoformat(item["trading_date"]) for item in conflicts}
    status_rows: list[dict[str, Any]] = []
    counts = {
        "required_fast_total": len(required),
        "raw_covered": 0,
        "parsed": 0,
        "source_quality_valid": 0,
        "usable": 0,
        "missing": 0,
        "invalid": 0,
    }
    for day in sorted(required):
        available_raw = day in raw_index
        parsed_rows = rows_by_date.get(day, ())
        parsed = bool(parsed_rows)
        quality_valid, quality = _quality_for_rows(parsed_rows)
        source_quality_valid = bool(parsed and quality_valid and quality is not None)
        conflict = day in conflict_dates
        usable = source_quality_valid and not conflict
        counts["raw_covered"] += int(available_raw)
        counts["parsed"] += int(parsed)
        counts["source_quality_valid"] += int(source_quality_valid)
        counts["usable"] += int(usable)
        counts["missing"] += int(not available_raw)
        counts["invalid"] += int(available_raw and not usable)
        status_rows.append(
            {
                "date": day,
                "available_raw": available_raw,
                "parsed": parsed,
                "source_quality_valid": source_quality_valid,
                "usable_for_market_bar": usable,
                "status": (
                    "USABLE_FOR_MARKET_BAR"
                    if usable
                    else "SOURCE_QUALITY_INVALID"
                    if available_raw
                    else "MISSING_RAW_TIMESTAMP_COVERAGE"
                ),
                "row_count": len(parsed_rows),
                "source_quality": quality,
                "conflict": conflict,
                "raw_page_count": len(raw_index.get(day, ())),
                "raw_page_paths": raw_index.get(day, ()),
            }
        )
    coverage_complete = counts["usable"] == counts["required_fast_total"]
    return (
        {
            **counts,
            "complete": coverage_complete,
            "required_sessions": status_rows,
            "raw_page_count": len(raw_pages),
            "raw_pages": raw_pages,
            "parsed_page_count": sum(bool(item.get("parsed")) for item in page_records),
            "parse_invalid_page_count": sum(
                not bool(item.get("parsed")) for item in page_records
            ),
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
        },
        rows_by_date,
    )


def _regime_shares(
    bar: Mapping[str, Any], daily_regimes: Mapping[date, str]
) -> tuple[dict[str, Decimal], str]:
    contributions = {name: Decimal(0) for name in REGIMES}
    for item in bar.get("provenance", []):
        if not isinstance(item, Mapping):
            continue
        source_id = str(item.get("source_id", ""))
        parts = source_id.split(":")
        try:
            source_day = date.fromisoformat(parts[1])
        except (IndexError, ValueError):
            source_day = None
        label = daily_regimes.get(source_day, "NORMAL_OTHER")
        if label not in contributions:
            label = "NORMAL_OTHER"
        start = _decimal(item.get("source_tau_start", 0))
        end = _decimal(item.get("source_tau_end", 0))
        if end < start:
            raise ReplicationMaterializationError("negative source tau contribution")
        contributions[label] += end - start
    total = sum(contributions.values(), Decimal(0))
    if total <= 0:
        raise ReplicationMaterializationError(
            "Market Bar has no source tau contribution"
        )
    shares = {name: contributions[name] / total for name in REGIMES}
    shares["NORMAL_OTHER"] = Decimal(1) - sum(
        (shares[name] for name in REGIMES if name != "NORMAL_OTHER"), Decimal(0)
    )
    pure = [name for name, share in shares.items() if share == Decimal(1)]
    return shares, f"PURE_{pure[0]}" if len(pure) == 1 else "MIXED_SOURCE_REGIME"


def _consecutive_summary(
    rows: Sequence[Mapping[str, Any]], label: str
) -> dict[str, Any]:
    indexes = [
        int(row["market_bar_index"])
        for row in rows
        if row.get("primary_source_regime") == label
    ]
    if not indexes:
        return {
            "count": 0,
            "longest_consecutive_run": 0,
            "first_market_bar_index": None,
            "last_market_bar_index": None,
        }
    longest = current = 1
    for previous, current_index in pairwise(indexes):
        current = current + 1 if current_index == previous + 1 else 1
        longest = max(longest, current)
    return {
        "count": len(indexes),
        "longest_consecutive_run": longest,
        "first_market_bar_index": indexes[0],
        "last_market_bar_index": indexes[-1],
    }


def _decorate_market_bars(
    bars: Sequence[Mapping[str, Any]],
    daily_regimes: Mapping[date, str],
    stock_code: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, original in enumerate(bars, start=1):
        row = dict(original)
        shares, primary = _regime_shares(original, daily_regimes)
        row["source_market_bar_id"] = row.get("market_bar_id")
        row["market_bar_index"] = index
        row["market_bar_id"] = f"{stock_code}:MB{index:03d}"
        row["integer_target"] = row.get("actual_integer_target")
        row["source_calendar_regime_tau_share"] = shares
        row["source_calendar_regime_tau_share_sum"] = sum(shares.values(), Decimal(0))
        row["source_calendar_regime_tau_share_sum_exact"] = row[
            "source_calendar_regime_tau_share_sum"
        ] == Decimal(1)
        row["primary_source_regime"] = primary
        row["source_segment_provenance"] = list(row.get("provenance", []))
        result.append(row)
    return result


def _mb60_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    region = [
        row for row in rows if int(row["market_bar_index"]) >= MMA60_RESEARCH_START
    ]
    return {
        **{
            f"{label.lower()}_count": sum(
                row.get("primary_source_regime") == f"PURE_{label}" for row in region
            )
            for label in REGIMES
        },
        "mixed_count": sum(
            row.get("primary_source_regime") == "MIXED_SOURCE_REGIME" for row in region
        ),
        "market_bar_count": len(region),
    }


def _regime_boundary_diagnostics(
    rows: Sequence[Mapping[str, Any]], daily_regimes: Mapping[date, str]
) -> dict[str, Any]:
    """Expose observed mixed bars without changing their classification."""

    mixed = [
        row for row in rows if row.get("primary_source_regime") == "MIXED_SOURCE_REGIME"
    ]
    samples: list[dict[str, Any]] = []
    for row in mixed[:10]:
        source_days: list[str] = []
        source_regimes: list[str] = []
        for item in row.get("provenance", []):
            parts = str(item.get("source_id", "")).split(":")
            try:
                day = date.fromisoformat(parts[1])
            except (IndexError, ValueError):
                continue
            source_days.append(day.isoformat())
            source_regimes.append(daily_regimes.get(day, "NORMAL_OTHER"))
        samples.append(
            {
                "market_bar_index": row["market_bar_index"],
                "source_days": sorted(set(source_days)),
                "source_regimes": sorted(set(source_regimes)),
                "tau_share": row["source_calendar_regime_tau_share"],
            }
        )
    return {
        "mixed_bar_count": len(mixed),
        "mixed_bar_samples": samples,
        "interpretation": "A bar crossing source-calendar regime contributions is MIXED; no threshold relabeling is applied.",
    }


def _materialization_quality(
    materialized: Mapping[str, Any], bars: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    lengths = [_decimal(row["tau_length"]) for row in bars]
    errors = [abs(_decimal(row["boundary_error"])) for row in bars]
    holds = [
        error <= _decimal(row.get("crossing_segment_tau", 0))
        for row, error in zip(bars, errors, strict=True)
    ]
    return {
        "tau_length": _distribution(lengths),
        "abs_tau_length_minus_one": _distribution([abs(x - 1) for x in lengths]),
        "boundary_error": _distribution(errors),
        "boundary_error_le_crossing_segment_tau": all(holds),
        "regime_share_sum_exact": all(
            bool(row.get("source_calendar_regime_tau_share_sum_exact")) for row in bars
        ),
        "market_bar_count": len(bars),
        "source_total_tau": materialized["quality"]["total_source_tau"],
    }


def _materialize_candidate(
    label: str,
    candidate: Mapping[str, Any],
    *,
    cuts: Mapping[str, tuple[Decimal, Decimal, Decimal]],
    raw_root: Path,
) -> dict[str, Any]:
    stock_code = str(candidate["stock_code"])
    start = date.fromisoformat(str(candidate["calendar_start"]))
    end = date.fromisoformat(str(candidate["calendar_end"]))
    timeline = tuple(
        row
        for row in _daily_timeline(stock_code, cuts)
        if start <= row["trade_date"] <= end
    )
    if (
        not timeline
        or timeline[0]["trade_date"] != start
        or timeline[-1]["trade_date"] != end
    ):
        raise ReplicationMaterializationError(f"Daily window mismatch: {label}")
    daily_regimes = {
        row["trade_date"]: str(row["source_calendar_regime"]) for row in timeline
    }
    planning_tau_origin = _decimal(timeline[0]["tau"]) - _decimal(
        timeline[0]["delta_tau"]
    )
    coverage, rows_by_date = _coverage_precheck(
        stock_code=stock_code, window_rows=timeline, raw_root=raw_root
    )
    materialized: dict[str, Any] | None = None
    error: str | None = None
    if coverage["complete"]:
        inventory = {
            row["trade_date"].isoformat(): {"daily_tau": row["delta_tau"]}
            for row in timeline
        }
        try:
            daily_bars = tuple(
                sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date)
            )
            segments = _build_segments(
                stock_code=stock_code,
                window_dates=list(inventory),
                inventory=inventory,
                daily_bars=daily_bars,
                raw_by_date=rows_by_date,
            )
            materialized = _materialize(segments, stock_code)
        except (ValueError, ReplicationMaterializationError) as exc:
            error = type(exc).__name__
    raw_bars = materialized["market_bars"] if materialized else []
    bars = _decorate_market_bars(raw_bars, daily_regimes, stock_code)
    counts = {
        "pure_fast_directional_count": sum(
            row["primary_source_regime"] == "PURE_FAST_DIRECTIONAL_HIGH_EFF"
            for row in bars
        ),
        "pure_fast_noisy_count": sum(
            row["primary_source_regime"] == "PURE_FAST_NOISY" for row in bars
        ),
        "pure_slow_count": sum(
            row["primary_source_regime"] == "PURE_SLOW" for row in bars
        ),
        "pure_normal_other_count": sum(
            row["primary_source_regime"] == "PURE_NORMAL_OTHER" for row in bars
        ),
    }
    counts["mixed_count"] = sum(
        row["primary_source_regime"] == "MIXED_SOURCE_REGIME" for row in bars
    )
    success = bool(
        materialized
        and materialized["island_count"] == 1
        and len(bars) >= TARGET_MARKET_BARS
        and materialized["unresolved_source_count"] == 0
        and materialized["skip_count"] == 0
        and materialized["duplicate_target_count"] == 0
        and materialized["source_ohlc_exact"]
        and materialized["source_volume_exact"]
        and _materialization_quality(materialized, bars)[
            "boundary_error_le_crossing_segment_tau"
        ]
        and _materialization_quality(materialized, bars)["regime_share_sum_exact"]
    )
    if not success:
        if not coverage["complete"]:
            readiness = "SOURCE_COVERAGE_INSUFFICIENT"
        elif materialized and materialized["unresolved_source_count"]:
            readiness = "MARKET_BAR_CONTINUITY_FAILED"
        elif counts["pure_fast_directional_count"] < 10:
            readiness = "PURE_FAST_INSUFFICIENT"
        elif counts["pure_slow_count"] < 10:
            readiness = "PURE_SLOW_INSUFFICIENT"
        else:
            readiness = "MARKET_BAR_CONTINUITY_FAILED"
    elif counts["pure_fast_directional_count"] < 10:
        readiness = "PURE_FAST_INSUFFICIENT"
    elif counts["pure_slow_count"] < 10:
        readiness = "PURE_SLOW_INSUFFICIENT"
    else:
        readiness = "REPLICATION_READY"
    quality = _materialization_quality(materialized, bars) if materialized else None
    runs = {
        f"{name.lower()}_run": _consecutive_summary(bars, f"PURE_{name}")
        for name in REGIMES
    }
    runs["mixed_run"] = _consecutive_summary(bars, "MIXED_SOURCE_REGIME")
    return {
        "replication_label": label,
        "stock_code": stock_code,
        "calendar_start": start,
        "calendar_end": end,
        "expected": {
            "market_bars": candidate["expected_market_bar_capacity"],
            "pure_fast_directional": candidate[
                "expected_pure_fast_directional_mb_capacity"
            ],
            "pure_slow": candidate["expected_pure_slow_mb_capacity"],
        },
        "source_coverage": coverage,
        "materialization": {
            "success": success,
            "actual_market_bars": len(bars),
            "expected_actual_difference": len(bars)
            - int(candidate["expected_market_bar_capacity"]),
            "resolved_island_count": materialized["island_count"]
            if materialized
            else 0,
            "internal_unresolved_gap_count": materialized["unresolved_source_count"]
            if materialized
            else None,
            "structural_multi_target_gap_count": sum(
                int(item.get("targets_crossed_count", 0)) >= 2
                for item in materialized.get("unresolved_sources", [])
            )
            if materialized
            else None,
            "skip_count": materialized["skip_count"] if materialized else None,
            "duplicate_target_count": materialized["duplicate_target_count"]
            if materialized
            else None,
            "ohlc_source_exact": materialized["source_ohlc_exact"]
            if materialized
            else None,
            "volume_source_exact": materialized["source_volume_exact"]
            if materialized
            else None,
            "fractional_split_count": 0,
            "interpolation_count": 0,
            "synthetic_bar_count": 0,
            "error": error,
        },
        "tau_quality": quality,
        "actual_regime_counts": {**counts, "total": len(bars)},
        "expected_vs_actual": {
            "market_bars": {
                "expected": candidate["expected_market_bar_capacity"],
                "actual": len(bars),
                "difference": len(bars)
                - int(candidate["expected_market_bar_capacity"]),
            },
            "pure_fast_directional": {
                "expected": candidate["expected_pure_fast_directional_mb_capacity"],
                "actual": counts["pure_fast_directional_count"],
                "difference": counts["pure_fast_directional_count"]
                - int(candidate["expected_pure_fast_directional_mb_capacity"]),
            },
            "pure_slow": {
                "expected": candidate["expected_pure_slow_mb_capacity"],
                "actual": counts["pure_slow_count"],
                "difference": counts["pure_slow_count"]
                - int(candidate["expected_pure_slow_mb_capacity"]),
            },
            "planning_alignment": {
                "planning_absolute_tau_origin": planning_tau_origin,
                "materialization_tau_origin": Decimal(0),
                "planning_absolute_tau_fraction": planning_tau_origin % Decimal(1),
                "interpretation": "V1.4 planning capacity uses the stock-level tau lattice; this stream materializes from local tau zero. Differences are reported, never corrected by changing geometry.",
            },
        },
        "regime_runs": runs,
        "regime_boundary_diagnostics": _regime_boundary_diagnostics(
            bars, daily_regimes
        ),
        "mb060_plus_regime_counts": _mb60_counts(bars),
        "research_readiness_tiers": {
            "pure_fast_directional_ge_10": counts["pure_fast_directional_count"] >= 10,
            "pure_fast_directional_ge_20": counts["pure_fast_directional_count"] >= 20,
            "pure_fast_directional_ge_30": counts["pure_fast_directional_count"] >= 30,
            "pure_slow_ge_10": counts["pure_slow_count"] >= 10,
            "pure_slow_ge_20": counts["pure_slow_count"] >= 20,
            "pure_slow_ge_30": counts["pure_slow_count"] >= 30,
        },
        "replication_readiness": readiness,
        "market_bars": bars,
        "notes": [
            "Only local raw minute pages and existing Daily artifacts were read.",
            "No missing source was fetched or inferred; no source segment was split.",
            "Pure labels require tau share exactly 1; all other bars are MIXED_SOURCE_REGIME.",
        ],
    }


def _hypotheses(streams: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    values = list(streams.values())
    successes = [item for item in values if item["materialization"]["success"]]
    h1 = "SUPPORTED" if len(successes) == 2 else "INCONCLUSIVE"
    directional = True
    for item in successes:
        expected = item["expected_vs_actual"]
        fast_expected = int(expected["pure_fast_directional"]["expected"])
        slow_expected = int(expected["pure_slow"]["expected"])
        fast_actual = int(expected["pure_fast_directional"]["actual"])
        slow_actual = int(expected["pure_slow"]["actual"])
        directional &= fast_actual > 0 and slow_actual > 0
        directional &= (fast_expected <= slow_expected) == (fast_actual <= slow_actual)
    h2 = (
        "SUPPORTED"
        if successes and directional
        else "PARTIALLY_SUPPORTED"
        if successes
        else "INCONCLUSIVE"
    )
    h3 = (
        "SUPPORTED"
        if any(
            item["mb060_plus_regime_counts"]["fast_directional_high_eff_count"] > 0
            and item["mb060_plus_regime_counts"]["slow_count"] > 0
            for item in successes
        )
        else "NOT_SUPPORTED"
        if successes
        else "INCONCLUSIVE"
    )
    h4 = (
        "SUPPORTED"
        if any(
            int(item["actual_regime_counts"]["pure_slow_count"]) > 0
            for item in successes
        )
        else "NOT_SUPPORTED"
        if successes
        else "INCONCLUSIVE"
    )
    return {
        "H1_zero_fetch_candidates_materialize_200_continuous": h1,
        "H2_planning_pure_capacity_directionally_corresponds": h2,
        "H3_fast_and_slow_exist_after_mb060": h3,
        "H4_v13_pure_slow_shortfall_is_composition": h4,
    }


def run_audit(
    *,
    output_path: Path = OUTPUT_PATH,
    plan_path: Path = V14_PLAN_PATH,
    market_clock_reference: Path = MARKET_CLOCK_REFERENCE_PATH,
    raw_root: Path = MINUTE_ROOT,
) -> dict[str, Any]:
    candidates = _load_frozen_candidates(plan_path)
    cuts = _load_frozen_cuts(market_clock_reference)
    streams = {
        label: _materialize_candidate(
            label, candidates[label], cuts=cuts, raw_root=raw_root
        )
        for label in ("REPLICATION_B", "REPLICATION_C")
    }
    report = {
        "audit_version": "MARKET_BAR_ZERO_FETCH_REPLICATION_MATERIALIZATION_V1_5",
        "v14_checkpoint": V14_CHECKPOINT,
        "network_calls": 0,
        "scope": {
            "frozen_candidates": ["REPLICATION_B", "REPLICATION_C"],
            "excluded_candidates": ["REPLICATION_A", "068270"],
            "strategy": False,
            "mma_role_reanalysis": False,
            "buy_sell": False,
            "pnl": False,
            "pivot_calculation": False,
        },
        "frozen_infrastructure": {
            "global_activity_tau": True,
            "integer_target_lattice": True,
            "first_actual_source_endpoint_after_target_crossing": True,
            "multi_target_source_segment_is_unresolved_gap": True,
            "fractional_split": False,
            "interpolation": False,
            "synthetic_bar": False,
            "tau_formula_changed": False,
            "regime_semantics": "V1.3 frozen source-calendar labels",
        },
        "streams": streams,
        "hypotheses": _hypotheses(streams),
        "primary_decision": (
            "CASE_A_BOTH_READY"
            if all(
                item["replication_readiness"] == "REPLICATION_READY"
                for item in streams.values()
            )
            else "CASE_B_ONE_READY"
            if any(
                item["replication_readiness"] == "REPLICATION_READY"
                for item in streams.values()
            )
            else "CASE_D_SOURCE_COVERAGE_OR_CONTINUITY_INSUFFICIENT"
            if any(
                item["replication_readiness"] == "SOURCE_COVERAGE_INSUFFICIENT"
                for item in streams.values()
            )
            else "CASE_C_PURE_REGIME_INSUFFICIENT"
        ),
        "notes": [
            "Planning estimates and observed Market-Bar counts are reported separately.",
            "Differences are not used to alter geometry, tau, or regime definitions.",
            "No network/API call, backfill, candidate switch, or raw-artifact write occurred.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--plan", type=Path, default=V14_PLAN_PATH)
    parser.add_argument(
        "--market-clock-reference", type=Path, default=MARKET_CLOCK_REFERENCE_PATH
    )
    parser.add_argument("--raw-root", type=Path, default=MINUTE_ROOT)
    args = parser.parse_args()
    report = run_audit(
        output_path=args.output,
        plan_path=args.plan,
        market_clock_reference=args.market_clock_reference,
        raw_root=args.raw_root,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "network_calls": report["network_calls"],
                "primary_decision": report["primary_decision"],
                "streams": {
                    label: {
                        "readiness": value["replication_readiness"],
                        "actual_market_bars": value["materialization"][
                            "actual_market_bars"
                        ],
                        "pure_fast": value["actual_regime_counts"][
                            "pure_fast_directional_count"
                        ],
                        "pure_slow": value["actual_regime_counts"]["pure_slow_count"],
                    }
                    for label, value in report["streams"].items()
                },
            },
            ensure_ascii=False,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
