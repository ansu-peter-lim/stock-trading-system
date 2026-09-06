"""Offline candidate planning for Market-Bar MMA role replication (V1.4).

The planner uses only the existing ten-stock Daily artifacts, the frozen
MARKET_CLOCK regime cut points, and the local raw-minute coverage index.  It
does not acquire data, materialize Market Bars, recalculate MMA roles, or
make any trading/PnL decision.  Expected Market-Bar capacity is a planning
estimate based on Daily activity-tau intervals; it is never presented as an
observed Market-Bar result.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import calculate_daily_indicators
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar

from .down_box_daily_execution_proof import _load_stock
from .global_tau_resolution_adequacy_audit import V01_PROOF_PATH
from .market_bar_pilot_source_acquisition_plan import (
    _structural_gap_dates,
)
from .market_clock_audit import (
    RESEARCH_END,
    RESEARCH_START,
    _clock_series,
)
from .market_time_invariance_audit import _cached_minute_dates
from .market_time_normalization_audit import market_time_series

OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_mma_role_replication_candidate_plan_v1_4.json"
)
MARKET_CLOCK_REFERENCE_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_role_audit_v0_1.json"
)
MINUTE_ROOT = Path("data/raw/kiwoom/minute")
V13_CHECKPOINT = "c42d5713b45de8da1d4423ff271b4f553e53ead5"
STOCKS = (
    "000660",
    "005380",
    "005930",
    "012450",
    "034020",
    "035420",
    "035720",
    "066570",
    "068270",
    "105560",
)
TARGETS = (160, 200)
MMA60_WARMUP = 60
REQUIRED_OBSERVATIONS_AFTER_WARMUP = 100
REGIMES = (
    "FAST_DIRECTIONAL_HIGH_EFF",
    "FAST_NOISY",
    "SLOW",
    "NORMAL_OTHER",
)


class CandidatePlanError(ValueError):
    """The frozen offline planning inputs cannot satisfy the V1.4 contract."""


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _load_frozen_cuts(path: Path) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
    """Load the original ten-stock global quartile cut points."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    distributions = payload.get("daily_distributions")
    if not isinstance(distributions, Mapping):
        raise CandidatePlanError("frozen MARKET_CLOCK distributions are missing")
    fields = (
        "range_speed",
        "abs_net_move_atr_10",
        "efficiency_10",
        "flow_speed",
    )
    cuts: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
    for field in fields:
        source = distributions.get(field)
        if not isinstance(source, Mapping):
            raise CandidatePlanError(f"frozen quartile source missing: {field}")
        values = tuple(_decimal(source.get(key)) for key in ("p25", "median", "p75"))
        if any(not value.is_finite() for value in values):
            raise CandidatePlanError(f"frozen quartile source invalid: {field}")
        cuts[field] = values
    return cuts


def _quartile(
    value: Decimal | None, cuts: tuple[Decimal, Decimal, Decimal]
) -> str | None:
    if value is None:
        return None
    q25, q50, q75 = cuts
    if value <= q25:
        return "Q1"
    if value <= q50:
        return "Q2"
    if value <= q75:
        return "Q3"
    return "Q4"


def _regime_label(row: Mapping[str, Any]) -> str:
    if (
        row.get("range_speed_quartile") == "Q4"
        and row.get("direction_speed_quartile") == "Q4"
        and row.get("efficiency_10_quartile") == "Q4"
    ):
        return "FAST_DIRECTIONAL_HIGH_EFF"
    if (
        row.get("range_speed_quartile") == "Q4"
        and row.get("efficiency_10_quartile") == "Q1"
    ):
        return "FAST_NOISY"
    if row.get("range_speed_quartile") == "Q1":
        return "SLOW"
    return "NORMAL_OTHER"


def _daily_timeline(
    stock_code: str,
    cuts: Mapping[str, tuple[Decimal, Decimal, Decimal]],
) -> tuple[dict[str, Any], ...]:
    """Build one deterministic Daily activity/regime timeline from cache."""

    bars = tuple(sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date))
    if not bars:
        raise CandidatePlanError(f"no Daily bars for {stock_code}")
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
    points = tuple(calculate_daily_indicators(bars, calendar))
    clock = _clock_series(bars, points)
    tau = market_time_series(bars)
    if len(clock) != len(tau) or len(tau) != len(bars):
        raise CandidatePlanError(f"Daily/tau length mismatch for {stock_code}")
    rows: list[dict[str, Any]] = []
    for index, (bar, clock_row, tau_row) in enumerate(
        zip(bars, clock, tau, strict=True)
    ):
        row: dict[str, Any] = {
            "stock_code": stock_code,
            "trade_date": bar.trade_date,
            "_index": index,
            "delta_tau": tau_row.get("delta_tau"),
            "tau": tau_row.get("tau"),
            "clock_status": tau_row.get("clock_status"),
        }
        for field in (
            "range_speed",
            "abs_net_move_atr_10",
            "efficiency_10",
            "flow_speed",
            "atr20",
            "sma5",
            "sma10",
            "sma20",
            "sma60",
        ):
            row[field] = clock_row.get(field)
        for field, quartile_field in (
            ("range_speed", "range_speed_quartile"),
            ("abs_net_move_atr_10", "direction_speed_quartile"),
            ("efficiency_10", "efficiency_10_quartile"),
            ("flow_speed", "flow_speed_quartile"),
        ):
            row[quartile_field] = _quartile(row[field], cuts[field])
        row["source_calendar_regime"] = _regime_label(row)
        rows.append(row)
    return tuple(rows)


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _pure_capacity(start: Decimal, end: Decimal) -> int:
    """Count integer lattice bars wholly inside one source regime interval."""

    # A bar [n-1, n] is wholly inside (start, end] when n-1 >= start and
    # n <= end.  This is only an expected pure-capacity estimate; no Market
    # Bar is emitted here.
    first_target = _ceil(start + Decimal(1))
    last_target = _floor(end)
    return max(0, last_target - first_target + 1)


def _regime_capacity(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals = {name: Decimal(0) for name in REGIMES}
    pure = {name: 0 for name in REGIMES}
    session_counts = {name: 0 for name in REGIMES}
    longest = {name: 0 for name in REGIMES}
    current_regime: str | None = None
    current_start = Decimal(0)
    # Keep the absolute stock-level tau coordinate from market_time_series so
    # candidate estimates use the same frozen integer lattice as materialized
    # Market Bars.  The candidate window is never re-zeroed.
    cumulative = _decimal(rows[0]["tau"]) - _decimal(rows[0]["delta_tau"])
    window_total = Decimal(0)
    current_run = 0
    for row in rows:
        regime = str(row["source_calendar_regime"])
        length = _decimal(row["delta_tau"])
        start = cumulative
        window_total += length
        expected_end = row.get("tau")
        cumulative = (
            _decimal(expected_end) if expected_end is not None else cumulative + length
        )
        totals[regime] += length
        session_counts[regime] += 1
        if regime == current_regime:
            current_run += 1
        else:
            if current_regime is not None:
                pure[current_regime] += _pure_capacity(current_start, start)
            current_regime = regime
            current_start = start
            current_run = 1
        longest[regime] = max(longest[regime], current_run)
    if current_regime is not None:
        pure[current_regime] += _pure_capacity(current_start, cumulative)
    return {
        "tau": totals,
        "pure_capacity": pure,
        "session_count": session_counts,
        "longest_run": longest,
        "total_tau": window_total,
    }


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[object, ...]:
    """Documented deterministic ranking; no optimized weighted score."""

    return (
        int(candidate["structural_gap_count"]),
        -int(candidate["expected_pure_slow_mb_capacity"]),
        not bool(candidate["fast_directional_present"]),
        -int(candidate["expected_market_bar_capacity"]),
        int(candidate["new_fetch_required_session_count"]),
        -int(candidate["min_fast_slow_capacity"]),
        int(candidate["calendar_session_count"]),
        -int(candidate["expected_pure_fast_directional_mb_capacity"]),
        int(candidate["source_quality_anomaly_count"]),
        str(candidate["stock_code"]),
        str(candidate["calendar_start"]),
        str(candidate["calendar_end"]),
        int(candidate["candidate_target"]),
    )


def _balanced_sort_key(candidate: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        -int(candidate["min_fast_slow_capacity"]),
        -int(candidate["expected_pure_slow_mb_capacity"]),
        int(candidate["new_fetch_required_session_count"]),
        int(candidate["calendar_session_count"]),
        str(candidate["stock_code"]),
        str(candidate["calendar_start"]),
        str(candidate["calendar_end"]),
        int(candidate["candidate_target"]),
    )


def _window_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    target: int,
    cached_dates: set[date],
    structural_dates: set[date],
) -> list[dict[str, Any]]:
    """Enumerate shortest windows ending at each valid Daily session."""

    candidates: list[dict[str, Any]] = []
    research = [
        row for row in rows if RESEARCH_START <= row["trade_date"] <= RESEARCH_END
    ]
    segments: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for row in research + [None]:  # type: ignore[list-item]
        valid = (
            row is not None
            and row.get("delta_tau") is not None
            and _decimal(row["delta_tau"]) > 0
            and row["trade_date"] not in structural_dates
        )
        if valid:
            current.append(row)  # type: ignore[arg-type]
        elif current:
            segments.append(current)
            current = []
    for segment in segments:
        left = 0
        total = Decimal(0)
        for right, row in enumerate(segment):
            total += _decimal(row["delta_tau"])
            while (
                left < right and total - _decimal(segment[left]["delta_tau"]) >= target
            ):
                total -= _decimal(segment[left]["delta_tau"])
                left += 1
            if total < target:
                continue
            window = segment[left : right + 1]
            capacity = _regime_capacity(window)
            missing = [
                item["trade_date"]
                for item in window
                if _decimal(item["delta_tau"]) >= 1
                and item["trade_date"] not in cached_dates
            ]
            expected_capacity = _floor(capacity["total_tau"])
            pure = capacity["pure_capacity"]
            structural_count = sum(
                item["trade_date"] in structural_dates for item in window
            )
            start = window[0]["trade_date"]
            end = window[-1]["trade_date"]
            candidate: dict[str, Any] = {
                "candidate_id": f"{window[0]['stock_code']}:{start.isoformat()}:{end.isoformat()}:{target}",
                "stock_code": window[0]["stock_code"],
                "calendar_start": start,
                "calendar_end": end,
                "calendar_session_count": len(window),
                "calendar_span_days": (end - start).days + 1,
                "candidate_target": target,
                "total_daily_tau": capacity["total_tau"],
                "expected_market_bar_capacity": expected_capacity,
                "fast_directional_tau": capacity["tau"]["FAST_DIRECTIONAL_HIGH_EFF"],
                "fast_noisy_tau": capacity["tau"]["FAST_NOISY"],
                "slow_tau": capacity["tau"]["SLOW"],
                "normal_other_tau": capacity["tau"]["NORMAL_OTHER"],
                "expected_pure_fast_directional_mb_capacity": pure[
                    "FAST_DIRECTIONAL_HIGH_EFF"
                ],
                "expected_pure_fast_noisy_mb_capacity": pure["FAST_NOISY"],
                "expected_pure_slow_mb_capacity": pure["SLOW"],
                "slow_calendar_session_count": capacity["session_count"]["SLOW"],
                "fast_directional_session_count": capacity["session_count"][
                    "FAST_DIRECTIONAL_HIGH_EFF"
                ],
                "fast_noisy_session_count": capacity["session_count"]["FAST_NOISY"],
                "longest_consecutive_slow_session_run": capacity["longest_run"]["SLOW"],
                "longest_fast_directional_run": capacity["longest_run"][
                    "FAST_DIRECTIONAL_HIGH_EFF"
                ],
                "fast_directional_present": pure["FAST_DIRECTIONAL_HIGH_EFF"] > 0,
                "slow_present": pure["SLOW"] > 0,
                "fast_noisy_present": pure["FAST_NOISY"] > 0,
                "mma60_research_length_ready": expected_capacity
                >= MMA60_WARMUP + REQUIRED_OBSERVATIONS_AFTER_WARMUP,
                "balanced_regime_candidate": pure["FAST_DIRECTIONAL_HIGH_EFF"] > 0
                and pure["SLOW"] > 0,
                "required_fast_session_count": sum(
                    _decimal(item["delta_tau"]) >= 1 for item in window
                ),
                "already_cached_required_fast_sessions": sum(
                    _decimal(item["delta_tau"]) >= 1
                    and item["trade_date"] in cached_dates
                    for item in window
                ),
                "new_fetch_required_session_count": len(missing),
                "missing_fast_tau_sum": sum(
                    (
                        _decimal(item["delta_tau"])
                        for item in window
                        if _decimal(item["delta_tau"]) >= 1
                        and item["trade_date"] not in cached_dates
                    ),
                    Decimal(0),
                ),
                "repairable_missing_fast_dates": missing,
                "structural_gap_count": structural_count,
                "source_quality_anomaly_count": 0,
                "min_fast_slow_capacity": min(
                    pure["FAST_DIRECTIONAL_HIGH_EFF"], pure["SLOW"]
                ),
            }
            candidates.append(candidate)
    candidates.sort(key=_candidate_sort_key)
    return candidates


def _summary_candidate(candidate: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        key: candidate[key]
        for key in (
            "candidate_id",
            "stock_code",
            "calendar_start",
            "calendar_end",
            "candidate_target",
            "expected_market_bar_capacity",
            "expected_pure_fast_directional_mb_capacity",
            "expected_pure_slow_mb_capacity",
            "min_fast_slow_capacity",
            "new_fetch_required_session_count",
            "balanced_regime_candidate",
        )
    }


def _best(
    candidates: Sequence[Mapping[str, Any]],
    predicate: Any = None,
) -> dict[str, Any] | None:
    selected = [
        candidate
        for candidate in candidates
        if predicate is None or predicate(candidate)
    ]
    return min(selected, key=_candidate_sort_key) if selected else None


def _unique_candidates(candidates: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        result[str(candidate["candidate_id"])] = dict(candidate)
    return list(result.values())


def _preferred_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    eligible = [
        dict(candidate)
        for candidate in candidates
        if candidate["balanced_regime_candidate"]
        and candidate["mma60_research_length_ready"]
        and candidate["structural_gap_count"] == 0
    ]
    # Prefer a 200-bar target where its source cost is comparable, but keep
    # ranking descriptive and deterministic rather than introducing a score.
    eligible.sort(
        key=lambda item: (
            int(item["candidate_target"]) != 200,
            *_balanced_sort_key(item),
        )
    )
    distinct: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in eligible:
        stock = str(item["stock_code"])
        if stock == "066570":
            continue
        if stock in seen:
            continue
        distinct.append(item)
        seen.add(stock)
        if len(distinct) == 3:
            break
    if len(distinct) < 2:
        for item in eligible:
            stock = str(item["stock_code"])
            if stock in seen:
                continue
            distinct.append(item)
            seen.add(stock)
            if len(distinct) == 3:
                break
    return distinct


def _materialize_fetch_rows(
    candidate: Mapping[str, Any],
    timeline_by_key: Mapping[tuple[str, date], Mapping[str, Any]],
    cached_by_stock: Mapping[str, set[date]],
) -> list[dict[str, Any]]:
    rows = []
    for day in candidate["repairable_missing_fast_dates"]:
        source = timeline_by_key[(str(candidate["stock_code"]), day)]
        rows.append(
            {
                "stock": candidate["stock_code"],
                "date": day,
                "daily_delta_tau": source["delta_tau"],
                "source_calendar_regime": source["source_calendar_regime"],
                "cached_minute_exists": day
                in cached_by_stock[str(candidate["stock_code"])],
                "planned_fetch_required": True,
                "reason": "MARKET_BAR_RESOLUTION_REQUIRED",
            }
        )
    return rows


def _build_report(
    *,
    output_path: Path,
    market_clock_reference: Path = MARKET_CLOCK_REFERENCE_PATH,
    minute_root: Path = MINUTE_ROOT,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    cuts = _load_frozen_cuts(market_clock_reference)
    structural_source = json.loads(V01_PROOF_PATH.read_text(encoding="utf-8"))
    structural = _structural_gap_dates(structural_source)
    timelines: dict[str, tuple[dict[str, Any], ...]] = {}
    cached_by_stock: dict[str, set[date]] = {}
    all_candidates: dict[int, list[dict[str, Any]]] = {target: [] for target in TARGETS}
    for stock in sorted(stocks):
        timeline = _daily_timeline(stock, cuts)
        timelines[stock] = timeline
        cached_by_stock[stock] = _cached_minute_dates(stock, minute_root)
        for target in TARGETS:
            all_candidates[target].extend(
                _window_candidates(
                    timeline,
                    target=target,
                    cached_dates=cached_by_stock[stock],
                    structural_dates={day for code, day in structural if code == stock},
                )
            )
    for target in TARGETS:
        all_candidates[target].sort(key=_candidate_sort_key)
    combined = _unique_candidates(
        [candidate for target in TARGETS for candidate in all_candidates[target]]
    )
    combined.sort(key=_candidate_sort_key)
    balanced = sorted(
        [candidate for candidate in combined if candidate["balanced_regime_candidate"]],
        key=_balanced_sort_key,
    )
    slow_top = sorted(
        combined,
        key=lambda candidate: (
            -int(candidate["expected_pure_slow_mb_capacity"]),
            *_candidate_sort_key(candidate),
        ),
    )
    timeline_by_key = {
        (stock, row["trade_date"]): row
        for stock, timeline in timelines.items()
        for row in timeline
    }
    preferred = _preferred_candidates(combined)
    preferred_report = []
    for index, candidate in enumerate(preferred, start=1):
        preferred_report.append(
            {
                "replication_label": f"REPLICATION_{chr(64 + index)}",
                "selection_basis": [
                    "balanced_regime_candidate",
                    "expected_pure_slow_capacity",
                    "expected_market_bar_capacity",
                    "new_fetch_required_session_count",
                    "stock_code_deterministic_tie_break",
                ],
                "candidate": candidate,
                "exact_fetch_dates": _materialize_fetch_rows(
                    candidate, timeline_by_key, cached_by_stock
                ),
            }
        )
    by_stock: dict[str, dict[str, Any]] = {}
    for stock in sorted(stocks):
        stock_candidates = [
            candidate for candidate in combined if candidate["stock_code"] == stock
        ]
        stock160 = [
            candidate
            for candidate in stock_candidates
            if candidate["candidate_target"] == 160
        ]
        stock200 = [
            candidate
            for candidate in stock_candidates
            if candidate["candidate_target"] == 200
        ]
        slow_best = max(
            stock_candidates,
            key=lambda candidate: (
                int(candidate["expected_pure_slow_mb_capacity"]),
                int(candidate["min_fast_slow_capacity"]),
                -int(candidate["new_fetch_required_session_count"]),
                str(candidate["calendar_start"]),
            ),
            default=None,
        )
        balanced_best = max(
            (
                candidate
                for candidate in stock_candidates
                if candidate["balanced_regime_candidate"]
            ),
            key=lambda candidate: (
                int(candidate["min_fast_slow_capacity"]),
                int(candidate["expected_market_bar_capacity"]),
                -int(candidate["new_fetch_required_session_count"]),
                str(candidate["calendar_start"]),
            ),
            default=None,
        )
        cheapest = min(
            stock160 or stock200,
            key=lambda candidate: (
                int(candidate["new_fetch_required_session_count"]),
                int(candidate["calendar_session_count"]),
                -int(candidate["expected_market_bar_capacity"]),
                str(candidate["calendar_start"]),
            ),
            default=None,
        )
        by_stock[stock] = {
            "best_160_candidate": _summary_candidate(_best(stock160)),
            "best_200_candidate": _summary_candidate(_best(stock200)),
            "max_expected_pure_slow_capacity": _summary_candidate(slow_best),
            "max_balanced_fast_slow_capacity": _summary_candidate(balanced_best),
            "minimum_fetch_cost_candidate": _summary_candidate(cheapest),
        }
    feasible160 = [
        candidate
        for candidate in all_candidates[160]
        if candidate["balanced_regime_candidate"]
    ]
    feasible200 = [
        candidate
        for candidate in all_candidates[200]
        if candidate["balanced_regime_candidate"]
    ]
    all_dates = [
        row["trade_date"] for timeline in timelines.values() for row in timeline
    ]
    report: dict[str, Any] = {
        "plan_version": "MARKET_BAR_MMA_ROLE_REPLICATION_CANDIDATE_PLAN_V1_4",
        "v13_checkpoint": V13_CHECKPOINT,
        "network_calls": 0,
        "scope": {
            "search_universe": sorted(stocks),
            "research_start": RESEARCH_START,
            "research_end": RESEARCH_END,
            "targets": list(TARGETS),
            "strategy": False,
            "buy_sell": False,
            "pnl": False,
            "market_bar_materialization": False,
            "mma_role_reanalysis": False,
        },
        "frozen_infrastructure": {
            "global_activity_tau": True,
            "integer_target_lattice": True,
            "source_endpoint_snapping": True,
            "multi_target_structural_gap_policy": "exclude; never bridge",
            "ohlcv_exact_aggregation": True,
            "mma_periods": [5, 10, 20, 60],
            "source_calendar_regime_definition": "V1.3 frozen",
            "geometry_changed": False,
            "tau_formula_changed": False,
            "source_acquisition_performed": False,
        },
        "methodology": {
            "expected_market_bar_capacity": "floor(sum of valid Daily delta_tau); planning estimate only",
            "expected_pure_capacity": "integer lattice bars wholly inside a contiguous source-calendar regime interval; planning estimate only",
            "required_fast_session": "Daily delta_tau >= 1",
            "cache_coverage": "raw minute labels indexed from all local page artifacts, including overlap",
            "candidate_window": "shortest valid window ending at each session with target tau reached",
            "structural_gap_source": str(V01_PROOF_PATH),
            "daily_reference": "existing adjusted Daily artifacts and frozen V1.3 regime cut points",
            "retention": "LIVE_VALIDATION_REQUIRED",
        },
        "source_feasibility": {
            "network_calls": 0,
            "api_retention_checked": False,
            "oldest_local_daily_date": min(all_dates) if all_dates else None,
            "newest_local_daily_date": max(all_dates) if all_dates else None,
            "raw_minute_coverage_provenance": "local artifacts only",
        },
        "population": {
            "stock_count": len(stocks),
            "candidate_window_count_160": len(all_candidates[160]),
            "candidate_window_count_200": len(all_candidates[200]),
            "pure_slow_capable_candidate_count": sum(
                candidate["slow_present"] for candidate in combined
            ),
            "balanced_candidate_count_160": len(feasible160),
            "balanced_candidate_count_200": len(feasible200),
            "preferred_replication_count": len(preferred_report),
        },
        "candidate_top10_160": all_candidates[160][:10],
        "candidate_top10_200": all_candidates[200][:10],
        "pure_slow_capacity_top10": slow_top[:10],
        "fast_slow_balanced_top10": balanced[:10],
        "stock_summary": by_stock,
        "preferred_replication_set": preferred_report,
        "regime_sample_targets": {
            str(target): {
                f"expected_pure_{regime.lower()}_capacity_ge_{minimum}": sum(
                    candidate["expected_pure_" + regime.lower() + "_mb_capacity"]
                    >= minimum
                    for candidate in all_candidates[target]
                )
                for regime in (
                    "fast_directional",
                    "fast_noisy",
                    "slow",
                )
                for minimum in (10, 20, 30)
            }
            for target in TARGETS
        },
        "exact_fetch_dates": {
            item["replication_label"]: item["exact_fetch_dates"]
            for item in preferred_report
        },
        "hypotheses": {
            "H1_pure_slow_capable_candidate_exists": (
                "SUPPORTED"
                if any(candidate["slow_present"] for candidate in combined)
                else "NOT_SUPPORTED"
            ),
            "H2_two_stock_fast_slow_160_window": (
                "SUPPORTED"
                if len({candidate["stock_code"] for candidate in feasible160}) >= 2
                else "INCONCLUSIVE"
            ),
            "H3_200_bar_without_full_history_reconstruction": (
                "SUPPORTED"
                if any(
                    candidate["balanced_regime_candidate"]
                    for candidate in all_candidates[200]
                )
                else "INCONCLUSIVE"
            ),
            "H4_pure_slow_shortfall_is_window_composition": "INCONCLUSIVE",
        },
        "notes": [
            "No actual Market Bars were materialized; expected capacities are not observed counts.",
            "No threshold was optimized or promoted to a strategy rule.",
            "A candidate with structural gaps is never bridged or repaired.",
            "A preferred set has at most three distinct stocks and does not pick 066570 by default.",
            "A live acquisition proof is required before any targeted fetch is authorized.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report


def run_plan(
    *,
    output_path: Path = OUTPUT_PATH,
    market_clock_reference: Path = MARKET_CLOCK_REFERENCE_PATH,
    minute_root: Path = MINUTE_ROOT,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    return _build_report(
        output_path=output_path,
        market_clock_reference=market_clock_reference,
        minute_root=minute_root,
        stocks=stocks,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument(
        "--market-clock-reference", type=Path, default=MARKET_CLOCK_REFERENCE_PATH
    )
    parser.add_argument("--minute-root", type=Path, default=MINUTE_ROOT)
    args = parser.parse_args()
    report = run_plan(
        output_path=args.output,
        market_clock_reference=args.market_clock_reference,
        minute_root=args.minute_root,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "candidate_window_count_160": report["population"][
                    "candidate_window_count_160"
                ],
                "candidate_window_count_200": report["population"][
                    "candidate_window_count_200"
                ],
                "pure_slow_capable_candidate_count": report["population"][
                    "pure_slow_capable_candidate_count"
                ],
                "preferred_replication_count": report["population"][
                    "preferred_replication_count"
                ],
                "network_calls": report["network_calls"],
            },
            ensure_ascii=False,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
