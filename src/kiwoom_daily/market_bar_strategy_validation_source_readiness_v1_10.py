"""Offline source-readiness plan for unseen Market-Bar validation (V1.10).

This module inventories the local Daily and raw minute artifacts and reuses the
frozen V1.4 *planning* helpers to estimate candidate Market-Bar capacity.  It
does not call an API, materialize Market Bars, calculate strategy signals, or
inspect strategy outcomes.  The expected capacity and missing-minute dates
are planning evidence only; V1.11 must verify them after acquisition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .global_tau_resolution_adequacy_audit import V01_PROOF_PATH
from .market_bar_mma_role_replication_candidate_plan_v1_4 import (
    MARKET_CLOCK_REFERENCE_PATH,
    MINUTE_ROOT,
    RESEARCH_END,
    RESEARCH_START,
    _balanced_sort_key,
    _candidate_sort_key,
    _daily_timeline,
    _load_frozen_cuts,
    _materialize_fetch_rows,
    _structural_gap_dates,
    _window_candidates,
)

OUTPUT_PATH = Path(
    "data/processed/strategy_review/"
    "market_bar_strategy_validation_source_readiness_v1_10.json"
)
DAILY_ROOT = Path("data/raw/kiwoom/daily")
REGISTRY_PATH = Path("docs/research/market_bar_strategy_validation_registry_v1_8.md")
DECISION_PATH = Path("docs/research/market_bar_strategy_dev_decision_v1_9a.md")
INPUT_HEAD = "1bc0380fe4f3e3a4210bad6d0d19aeeffbe9c419"
STOCKS = ("005380", "068270")
TARGETS = (160, 200)
REQUIRED_ROW_FIELDS = (
    "dt",
    "open_pric",
    "high_pric",
    "low_pric",
    "cur_prc",
    "trde_qty",
)
MINUTE_ROW_FIELDS = (
    "cntr_tm",
    "open_pric",
    "high_pric",
    "low_pric",
    "cur_prc",
    "trde_qty",
)
EXPECTED_ROLES = {
    "005380": "BALANCED_STRATEGY_VALIDATION_CANDIDATE",
    "068270": "SLOW_DOMINANT_STRESS_VALIDATION_CANDIDATE",
}


class SourceReadinessError(ValueError):
    """Frozen source-readiness inputs do not satisfy the V1.10 contract."""


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_digest(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(_sha256(path).encode("ascii"))
    return digest.hexdigest()


def _artifact_records(paths: Sequence[Path]) -> list[dict[str, str | None]]:
    records: list[dict[str, str | None]] = []
    for path in sorted(paths):
        requested_date = _parse_daily_date(path.parent.name)
        records.append(
            {
                "raw_file_path": path.as_posix(),
                "raw_file_sha256": _sha256(path),
                "requested_date": requested_date.isoformat()
                if requested_date is not None
                else None,
            }
        )
    return records


def _parse_json(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _parse_daily_date(value: object) -> date | None:
    text = str(value or "")
    if len(text) != 8 or not text.isascii() or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _parse_minute_timestamp(value: object) -> datetime | None:
    text = str(value or "")
    if len(text) != 14 or not text.isascii() or not text.isdigit():
        return None
    try:
        return datetime.fromisoformat(
            f"{text[:4]}-{text[4:6]}-{text[6:8]}T"
            f"{text[8:10]}:{text[10:12]}:{text[12:14]}"
        )
    except ValueError:
        return None


def _iter_rows(payload: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(row for row in value if isinstance(row, Mapping))


def _quality_names(prefix: str, count: int) -> list[str]:
    return [f"{prefix}:{index}" for index in range(count)]


def _daily_inventory(stock_code: str, root: Path) -> dict[str, Any]:
    """Inventory raw and adjusted Daily pages without interpreting prices."""

    by_mode: dict[str, dict[str, Any]] = {}
    for mode in ("raw", "adjusted"):
        paths = tuple(sorted((root / stock_code / mode).glob("**/*.json")))
        dates: list[date] = []
        malformed = 0
        missing_fields = 0
        page_without_rows = 0
        rows = 0
        for path in paths:
            payload = _parse_json(path)
            if payload is None:
                malformed += 1
                continue
            page_rows = _iter_rows(payload, "stk_dt_pole_chart_qry")
            if not page_rows:
                page_without_rows += 1
            rows += len(page_rows)
            for row in page_rows:
                parsed = _parse_daily_date(row.get("dt"))
                if parsed is None:
                    malformed += 1
                else:
                    dates.append(parsed)
                missing_fields += sum(
                    field not in row or row[field] in (None, "")
                    for field in REQUIRED_ROW_FIELDS
                )
        counts = Counter(dates)
        anomalies = _quality_names("MALFORMED_DAILY_ROW", malformed)
        anomalies.extend(_quality_names("MISSING_DAILY_FIELD", missing_fields))
        anomalies.extend(_quality_names("EMPTY_DAILY_PAGE", page_without_rows))
        by_mode[mode] = {
            "page_count": len(paths),
            "artifacts": _artifact_records(paths),
            "raw_row_count": rows,
            "unique_date_count": len(counts),
            "duplicate_date_count": sum(
                count - 1 for count in counts.values() if count > 1
            ),
            "date_start": min(dates) if dates else None,
            "date_end": max(dates) if dates else None,
            "artifact_sha256": _artifact_digest(paths) if paths else None,
            "source_quality_anomalies": anomalies,
        }
    raw = by_mode["raw"]
    adjusted = by_mode["adjusted"]
    raw_dates = set()
    adjusted_dates = set()
    for mode, destination in (("raw", raw_dates), ("adjusted", adjusted_dates)):
        for path in sorted((root / stock_code / mode).glob("**/*.json")):
            payload = _parse_json(path)
            if payload is not None:
                destination.update(
                    parsed
                    for parsed in (
                        _parse_daily_date(row.get("dt"))
                        for row in _iter_rows(payload, "stk_dt_pole_chart_qry")
                    )
                    if parsed is not None
                )
    return {
        "stock_code": stock_code,
        "raw": raw,
        "adjusted": adjusted,
        "raw_adjusted_date_parity": raw_dates == adjusted_dates,
        "research_window_date_count": sum(
            RESEARCH_START <= day <= RESEARCH_END for day in raw_dates
        ),
    }


def _minute_inventory(stock_code: str, root: Path) -> dict[str, Any]:
    """Index local raw minute timestamps; no price normalization is performed."""

    paths = tuple(sorted((root / stock_code / "raw").glob("**/*.json")))
    timestamps: list[datetime] = []
    malformed = 0
    missing_fields = 0
    empty_pages = 0
    raw_rows = 0
    for path in paths:
        payload = _parse_json(path)
        if payload is None:
            malformed += 1
            continue
        page_rows = _iter_rows(payload, "stk_min_pole_chart_qry")
        if not page_rows:
            empty_pages += 1
        raw_rows += len(page_rows)
        for row in page_rows:
            parsed = _parse_minute_timestamp(row.get("cntr_tm"))
            if parsed is None:
                malformed += 1
            else:
                timestamps.append(parsed)
            missing_fields += sum(
                field not in row or row[field] in (None, "")
                for field in MINUTE_ROW_FIELDS
            )
    counts = Counter(timestamps)
    dates = sorted({stamp.date() for stamp in timestamps})
    anomalies = _quality_names("MALFORMED_MINUTE_TIMESTAMP", malformed)
    anomalies.extend(_quality_names("MISSING_MINUTE_FIELD", missing_fields))
    anomalies.extend(_quality_names("EMPTY_MINUTE_PAGE", empty_pages))
    anomalies.extend(
        f"DUPLICATE_MINUTE_TIMESTAMP:{stamp.isoformat()}"
        for stamp, count in sorted(counts.items())
        if count > 1
    )
    return {
        "stock_code": stock_code,
        "mode": "raw",
        "page_count": len(paths),
        "artifacts": _artifact_records(paths),
        "raw_row_count": raw_rows,
        "unique_timestamp_count": len(counts),
        "duplicate_timestamp_count": sum(
            count - 1 for count in counts.values() if count > 1
        ),
        "covered_date_count": len(dates),
        "covered_date_start": min(dates) if dates else None,
        "covered_date_end": max(dates) if dates else None,
        "timestamp_start": min(timestamps) if timestamps else None,
        "timestamp_end": max(timestamps) if timestamps else None,
        "artifact_sha256": _artifact_digest(paths) if paths else None,
        "source_quality_anomalies": anomalies,
    }


def _candidate_summary(candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    fields = (
        "candidate_id",
        "stock_code",
        "calendar_start",
        "calendar_end",
        "calendar_session_count",
        "candidate_target",
        "total_daily_tau",
        "expected_market_bar_capacity",
        "expected_pure_fast_directional_mb_capacity",
        "expected_pure_fast_noisy_mb_capacity",
        "expected_pure_slow_mb_capacity",
        "slow_calendar_session_count",
        "fast_directional_session_count",
        "fast_noisy_session_count",
        "longest_consecutive_slow_session_run",
        "required_fast_session_count",
        "already_cached_required_fast_sessions",
        "new_fetch_required_session_count",
        "missing_fast_tau_sum",
        "repairable_missing_fast_dates",
        "structural_gap_count",
        "source_quality_anomaly_count",
        "balanced_regime_candidate",
    )
    return {field: candidate[field] for field in fields}


def _source_readiness_sort_key(candidate: Mapping[str, Any]) -> tuple[object, ...]:
    """V1.10 general source-only ranking, independent of regime preference."""

    return (
        int(candidate["structural_gap_count"]),
        int(candidate["expected_market_bar_capacity"])
        < int(candidate["candidate_target"]),
        int(candidate["new_fetch_required_session_count"]),
        int(candidate["calendar_span_days"]),
        -int(candidate["expected_market_bar_capacity"]),
        int(candidate["source_quality_anomaly_count"]),
        str(candidate["calendar_start"]),
        str(candidate["stock_code"]),
        str(candidate["calendar_end"]),
        int(candidate["candidate_target"]),
    )


def _role_candidate(
    stock_code: str,
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    target_200 = [
        item
        for item in candidates
        if item["candidate_target"] == 200 and item["structural_gap_count"] == 0
    ]
    if stock_code == "005380":
        eligible = [item for item in target_200 if item["balanced_regime_candidate"]]
        if eligible:
            return dict(
                min(eligible, key=_balanced_sort_key)
            ), "balanced_regime_candidate"
        fallback = [item for item in candidates if item["candidate_target"] == 160]
        return (
            (dict(min(fallback, key=_candidate_sort_key)), "best_160_fallback")
            if fallback
            else (None, "unavailable")
        )
    if target_200:
        return dict(
            min(target_200, key=_candidate_sort_key)
        ), "slow_dominant_stress_source_ranking"
    fallback = [item for item in candidates if item["candidate_target"] == 160]
    return (
        (dict(min(fallback, key=_candidate_sort_key)), "best_160_fallback")
        if fallback
        else (None, "unavailable")
    )


def _registry_verification(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    roles = {
        stock: role
        for stock, role in EXPECTED_ROLES.items()
        if stock in text and role in text
    }
    return {
        "path": str(path),
        "exists": path.exists(),
        "expected_roles_found": roles == EXPECTED_ROLES,
        "roles": roles,
        "validation_order": list(STOCKS),
    }


def _build_report(
    *,
    output_path: Path,
    daily_root: Path = DAILY_ROOT,
    minute_root: Path = MINUTE_ROOT,
    market_clock_reference: Path = MARKET_CLOCK_REFERENCE_PATH,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    if tuple(stocks) != STOCKS:
        raise SourceReadinessError("V1.10 registry is frozen to 005380 then 068270")
    if not REGISTRY_PATH.exists() or not DECISION_PATH.exists():
        raise SourceReadinessError("frozen validation documents are missing")
    registry = _registry_verification(REGISTRY_PATH)
    if not registry["expected_roles_found"]:
        raise SourceReadinessError("validation registry role freeze is not verified")
    cuts = _load_frozen_cuts(market_clock_reference)
    structural_source = json.loads(V01_PROOF_PATH.read_text(encoding="utf-8"))
    structural = _structural_gap_dates(structural_source)
    daily_coverage: dict[str, Any] = {}
    minute_coverage: dict[str, Any] = {}
    timelines: dict[str, tuple[dict[str, Any], ...]] = {}
    cached_by_stock: dict[str, set[date]] = {}
    candidates_by_stock: dict[str, list[dict[str, Any]]] = {}
    candidate_tables: dict[str, Any] = {}
    all_candidate_counts: dict[str, int] = {"160": 0, "200": 0}
    source_anomalies: dict[str, Any] = {}
    for stock in STOCKS:
        daily_coverage[stock] = _daily_inventory(stock, daily_root)
        minute_coverage[stock] = _minute_inventory(stock, minute_root)
        timelines[stock] = _daily_timeline(stock, cuts)
        cached_by_stock[stock] = {
            day
            for day in (
                minute_coverage[stock]["covered_date_start"],
                minute_coverage[stock]["covered_date_end"],
            )
            if day is not None
        }
        # The complete cache index is intentionally derived from the raw
        # timestamps, rather than assuming the coverage is a calendar range.
        cached_by_stock[stock] = set()
        for path in sorted((minute_root / stock / "raw").glob("**/*.json")):
            payload = _parse_json(path)
            if payload is None:
                continue
            for row in _iter_rows(payload, "stk_min_pole_chart_qry"):
                timestamp = _parse_minute_timestamp(row.get("cntr_tm"))
                if timestamp is not None:
                    cached_by_stock[stock].add(timestamp.date())
        structural_dates = {day for code, day in structural if code == stock}
        candidates: list[dict[str, Any]] = []
        by_target: dict[str, list[dict[str, Any]]] = {}
        for target in TARGETS:
            target_candidates = _window_candidates(
                timelines[stock],
                target=target,
                cached_dates=cached_by_stock[stock],
                structural_dates=structural_dates,
            )
            by_target[str(target)] = target_candidates
            candidates.extend(target_candidates)
            all_candidate_counts[str(target)] += len(target_candidates)
        candidates.sort(key=_candidate_sort_key)
        candidates_by_stock[stock] = candidates
        role_candidate, role_basis = _role_candidate(stock, candidates)
        candidate_tables[stock] = {
            "candidate_count_160": len(by_target["160"]),
            "candidate_count_200": len(by_target["200"]),
            "best_160_candidate": _candidate_summary(
                min(by_target["160"], key=_source_readiness_sort_key)
                if by_target["160"]
                else None
            ),
            "best_200_candidate": _candidate_summary(
                min(by_target["200"], key=_source_readiness_sort_key)
                if by_target["200"]
                else None
            ),
            "best_v1_4_regime_ranked_candidate_160": _candidate_summary(
                min(by_target["160"], key=_candidate_sort_key)
                if by_target["160"]
                else None
            ),
            "best_v1_4_regime_ranked_candidate_200": _candidate_summary(
                min(by_target["200"], key=_candidate_sort_key)
                if by_target["200"]
                else None
            ),
            "role": EXPECTED_ROLES[stock],
            "role_selection_basis": role_basis,
            "preferred_candidate": _candidate_summary(role_candidate),
        }
        source_anomalies[stock] = {
            "daily_raw": daily_coverage[stock]["raw"]["source_quality_anomalies"],
            "daily_adjusted": daily_coverage[stock]["adjusted"][
                "source_quality_anomalies"
            ],
            "minute_raw": minute_coverage[stock]["source_quality_anomalies"],
        }
    timeline_by_key = {
        (stock, row["trade_date"]): row
        for stock, timeline in timelines.items()
        for row in timeline
    }
    preferred: dict[str, Any] = {}
    exact_fetch: dict[str, list[dict[str, Any]]] = {}
    for stock in STOCKS:
        candidates = candidates_by_stock[stock]
        role_candidate, role_basis = _role_candidate(stock, candidates)
        if role_candidate is None:
            raise SourceReadinessError(f"no target candidate for {stock}")
        fetch_rows = _materialize_fetch_rows(
            role_candidate, timeline_by_key, cached_by_stock
        )
        preferred[stock] = {
            "role": EXPECTED_ROLES[stock],
            "selection_basis": role_basis,
            "target_market_bars": role_candidate["candidate_target"],
            "expected_market_bar_capacity": role_candidate[
                "expected_market_bar_capacity"
            ],
            "candidate": _candidate_summary(role_candidate),
            "required_fast_total": role_candidate["required_fast_session_count"],
            "cached_usable_count": role_candidate[
                "already_cached_required_fast_sessions"
            ],
            "new_fetch_required_count": role_candidate[
                "new_fetch_required_session_count"
            ],
            "oldest_missing_date": min(role_candidate["repairable_missing_fast_dates"])
            if role_candidate["repairable_missing_fast_dates"]
            else None,
            "newest_missing_date": max(role_candidate["repairable_missing_fast_dates"])
            if role_candidate["repairable_missing_fast_dates"]
            else None,
            "structural_gap_count": role_candidate["structural_gap_count"],
            "source_quality_anomaly_count": len(source_anomalies[stock]["minute_raw"]),
        }
        exact_fetch[stock] = fetch_rows
    structural_checks = {
        stock: {
            "structural_gap_dates_in_research": sorted(
                day
                for code, day in structural
                if code == stock and RESEARCH_START <= day <= RESEARCH_END
            ),
            "preferred_candidate_structural_gap_count": preferred[stock][
                "structural_gap_count"
            ],
            "status": "PASS"
            if preferred[stock]["structural_gap_count"] == 0
            else "FAIL",
        }
        for stock in STOCKS
    }
    report: dict[str, Any] = {
        "plan_version": "MARKET_BAR_STRATEGY_VALIDATION_SOURCE_READINESS_V1_10",
        "input_head": INPUT_HEAD,
        "frozen_documents": {
            "validation_registry": str(REGISTRY_PATH),
            "decision_gate": str(DECISION_PATH),
            "registry_verification": registry,
        },
        "scope": {
            "stocks": list(STOCKS),
            "research_start": RESEARCH_START,
            "research_end": RESEARCH_END,
            "targets": list(TARGETS),
            "validation_order": list(STOCKS),
            "strategy_signals": False,
            "strategy_outputs_inspected": False,
            "future_returns": False,
            "pnl": False,
            "network_calls": 0,
            "market_bar_materialization": False,
            "market_bar_geometry_changed": False,
            "tau_or_compression_changed": False,
        },
        "source_coverage": {
            "daily": daily_coverage,
            "minute_raw": minute_coverage,
            "minute_cache_index": {
                stock: {
                    "cached_usable_date_count": len(cached_by_stock[stock]),
                    "cached_usable_date_start": min(cached_by_stock[stock])
                    if cached_by_stock[stock]
                    else None,
                    "cached_usable_date_end": max(cached_by_stock[stock])
                    if cached_by_stock[stock]
                    else None,
                }
                for stock in STOCKS
            },
        },
        "candidate_tables": candidate_tables,
        "candidate_population": {
            "candidate_count_160": all_candidate_counts["160"],
            "candidate_count_200": all_candidate_counts["200"],
            "per_stock": {
                stock: {
                    "candidate_count_160": candidate_tables[stock][
                        "candidate_count_160"
                    ],
                    "candidate_count_200": candidate_tables[stock][
                        "candidate_count_200"
                    ],
                }
                for stock in STOCKS
            },
        },
        "preferred_selections": preferred,
        "exact_fetch_dates": exact_fetch,
        "structural_gap_checks": structural_checks,
        "source_quality_anomalies": source_anomalies,
        "materialization_plan": {
            "A": "existing cache only; no source writes",
            "B": "acquire only exact missing FAST sessions after explicit network authorization",
            "C": "verify raw timestamp/date coverage and page provenance",
            "D": "materialize Market Bars with frozen V0.6/V0.9 contracts",
            "E": "verify continuous island, exact OHLCV, and no interpolation/synthetic rows",
            "current_phase": "A planning plus exact B fetch list; B-E not executed",
        },
        "required_market_bar_invariants": [
            "continuous island = 1",
            "actual Market Bars >= target",
            "internal unresolved gap = 0",
            "skip = 0",
            "duplicate = 0",
            "OHLC source exact",
            "volume source exact",
            "fractional split = 0",
            "interpolation = 0",
            "synthetic bar = 0",
        ],
        "hypotheses": {
            "R1_005380_targeted_source_feasibility": "PARTIALLY_SUPPORTED",
            "R2_068270_targeted_source_feasibility": "PARTIALLY_SUPPORTED",
            "R3_validation_stream_without_full_history": "SUPPORTED",
            "R4_source_choice_independent_of_strategy_outcome": "SUPPORTED",
        },
        "notes": [
            "Candidate capacity and source-calendar regime fields are planning metadata only.",
            "No strategy indicator, signal, event, return, trade, or PnL output was read or produced.",
            "The exact missing-date lists are acquisition plans, not proof that the source endpoint retains those dates.",
            "API retention and live acquisition remain LIVE_VALIDATION_REQUIRED.",
            "No candidate is replaced for a transient failure; replacement is allowed only for frozen technical reasons.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report


def run_source_readiness(
    *,
    output_path: Path = OUTPUT_PATH,
    daily_root: Path = DAILY_ROOT,
    minute_root: Path = MINUTE_ROOT,
    market_clock_reference: Path = MARKET_CLOCK_REFERENCE_PATH,
) -> dict[str, Any]:
    """Build the deterministic offline V1.10 report."""

    return _build_report(
        output_path=output_path,
        daily_root=daily_root,
        minute_root=minute_root,
        market_clock_reference=market_clock_reference,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--daily-root", type=Path, default=DAILY_ROOT)
    parser.add_argument("--minute-root", type=Path, default=MINUTE_ROOT)
    parser.add_argument(
        "--market-clock-reference", type=Path, default=MARKET_CLOCK_REFERENCE_PATH
    )
    args = parser.parse_args()
    report = run_source_readiness(
        output_path=args.output,
        daily_root=args.daily_root,
        minute_root=args.minute_root,
        market_clock_reference=args.market_clock_reference,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "candidate_population": report["candidate_population"],
                "preferred_selections": {
                    stock: {
                        "target_market_bars": item["target_market_bars"],
                        "expected_market_bar_capacity": item[
                            "expected_market_bar_capacity"
                        ],
                        "new_fetch_required_count": item["new_fetch_required_count"],
                    }
                    for stock, item in report["preferred_selections"].items()
                },
                "network_calls": report["scope"]["network_calls"],
                "strategy_outputs_inspected": report["scope"][
                    "strategy_outputs_inspected"
                ],
            },
            ensure_ascii=False,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
