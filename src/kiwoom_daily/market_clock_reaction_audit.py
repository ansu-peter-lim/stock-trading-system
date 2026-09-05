"""Report-only MARKET_CLOCK moving-average reaction audit (V0.2).

V0.2 keeps the V0.1 nearest-MA and speed calculations intact, and adds
signed distances, parameter-free pivot reaction labels, clustering evidence,
and P+2 follow-through metadata.  It never changes Strategy V1 signals,
orders, fills, or PnL and reads only the cached daily artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import (
    DailyIndicatorPoint,
    PivotKind,
    calculate_daily_indicators,
)
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.strategy_review.chart import (
    ChartType,
    ReviewEvent,
    ReviewEventType,
    deterministic_chart_filename,
    prepare_review_chart,
    render_review_chart,
)

from .down_box_daily_execution_proof import _load_stock
from .market_clock_audit import (
    DAILY_PROOF_PATH,
    OUTPUT_PATH,
    PROOF_VERSION,
    RESEARCH_END,
    RESEARCH_START,
    STOCKS,
    _add_quartiles,
    _clock_series,
    _distribution,
    _effective_horizon_comparison,
    _large_trade_comparison,
    _median,
    _overlay_events,
    _pivot_rows,
    _role_distribution,
    _role_matrix,
)

V01_OUTPUT_PATH = OUTPUT_PATH
V01_PROOF_VERSION = PROOF_VERSION

PROOF_VERSION = "MARKET_CLOCK_MA_REACTION_ROLE_AUDIT_V0_2"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_reaction_role_audit_v0_2.json"
)
SUMMARY_CSV_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_reaction_role_audit_v0_2.csv"
)
CHART_ROOT = Path(
    "data/processed/strategy_charts/market_clock_ma_reaction_role_audit_v0_2"
)

_MA_NAMES = ("MA5", "MA10", "MA20", "MA60")
_MA_PERIODS = {"MA5": 5, "MA10": 10, "MA20": 20, "MA60": 60}

REACTION_FIELDS = (
    "stock_code",
    "pivot_kind",
    "pivot_trade_date",
    "confirmed_at",
    "pivot_price",
    "follow_through_p2_pct",
    "follow_through_p2_date",
    "atr20",
    "sma5",
    "sma10",
    "sma20",
    "sma60",
    "range_speed",
    "range_speed_quartile",
    "direction_speed_quartile",
    "efficiency_10",
    "efficiency_10_quartile",
    "flow_speed",
    "flow_speed_quartile",
    "net_move_atr_10",
    "abs_net_move_atr_10",
    "nearest_ma",
    "nearest_distance_atr",
    "second_nearest_ma",
    "second_nearest_distance_atr",
    "nearest_margin_atr",
    "ma5_10_gap_abs_atr",
    "ma10_20_gap_abs_atr",
    "ma20_60_gap_abs_atr",
)
for _name in _MA_NAMES:
    REACTION_FIELDS += tuple(
        f"{prefix}_{_name.casefold()}_atr"
        for prefix in ("low_signed_dist", "close_signed_dist", "high_signed_dist")
    )
    REACTION_FIELDS += tuple(
        f"low_{label}_{_name.casefold()}"
        for label in ("wick_reclaim", "full_hold_above", "close_below")
    )
    REACTION_FIELDS += tuple(
        f"high_{label}_{_name.casefold()}"
        for label in ("wick_rejection", "full_hold_below", "close_above")
    )


def _as_bool(value: bool) -> bool:
    return value


def _signed_distance(
    price: Decimal, ma: Decimal | None, atr: Decimal | None
) -> Decimal | None:
    if ma is None or atr is None:
        return None
    return (price - ma) / atr


def _reaction_signatures(
    *,
    kind: PivotKind,
    low: Decimal,
    high: Decimal,
    close: Decimal,
    ma: Decimal | None,
) -> dict[str, bool]:
    if ma is None:
        return {
            "wick_reclaim": False,
            "full_hold_above": False,
            "close_below": False,
            "wick_rejection": False,
            "full_hold_below": False,
            "close_above": False,
        }
    if kind is PivotKind.LOW:
        return {
            "wick_reclaim": low <= ma and close >= ma,
            "full_hold_above": low >= ma,
            "close_below": close < ma,
            "wick_rejection": False,
            "full_hold_below": False,
            "close_above": False,
        }
    return {
        "wick_reclaim": False,
        "full_hold_above": False,
        "close_below": False,
        "wick_rejection": high >= ma and close <= ma,
        "full_hold_below": high <= ma,
        "close_above": close > ma,
    }


def _distance_order(
    row: Mapping[str, Any], kind: PivotKind
) -> list[tuple[str, Decimal]]:
    prefix = "low" if kind is PivotKind.LOW else "high"
    values = [
        (name, row.get(f"{prefix}_to_{name.casefold()}_atr")) for name in _MA_NAMES
    ]
    return sorted(
        ((name, value) for name, value in values if value is not None),
        key=lambda item: (item[1], _MA_PERIODS[item[0]]),
    )


def _decorate_pivot_rows(
    bars: Sequence[DailyBar],
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    index_by_date = {bar.trade_date: index for index, bar in enumerate(canonical)}
    decorated: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        pivot_date = row["pivot_trade_date"]
        index = index_by_date[pivot_date]
        bar = canonical[index]
        kind = PivotKind(row["pivot_kind"])
        market_values = {
            "MA5": row.get("sma5"),
            "MA10": row.get("sma10"),
            "MA20": row.get("sma20"),
            "MA60": row.get("sma60"),
        }
        atr = row.get("atr20")
        ordered = _distance_order(row, kind)
        row["nearest_ma"] = ordered[0][0] if ordered else None
        row["nearest_distance_atr"] = ordered[0][1] if ordered else None
        row["second_nearest_ma"] = ordered[1][0] if len(ordered) > 1 else None
        row["second_nearest_distance_atr"] = ordered[1][1] if len(ordered) > 1 else None
        row["nearest_margin_atr"] = (
            ordered[1][1] - ordered[0][1] if len(ordered) > 1 else None
        )
        row["ma5_10_gap_abs_atr"] = (
            abs(row["ma5_10_gap_atr"])
            if row.get("ma5_10_gap_atr") is not None
            else None
        )
        row["ma10_20_gap_abs_atr"] = (
            abs(row["ma10_20_gap_atr"])
            if row.get("ma10_20_gap_atr") is not None
            else None
        )
        row["ma20_60_gap_abs_atr"] = (
            abs(row["ma20_60_gap_atr"])
            if row.get("ma20_60_gap_atr") is not None
            else None
        )
        for name in _MA_NAMES:
            ma = market_values[name]
            row[f"low_signed_dist_{name.casefold()}_atr"] = _signed_distance(
                bar.signal.low, ma, atr
            )
            row[f"close_signed_dist_{name.casefold()}_atr"] = _signed_distance(
                bar.signal.close, ma, atr
            )
            row[f"high_signed_dist_{name.casefold()}_atr"] = _signed_distance(
                bar.signal.high, ma, atr
            )
            signatures = _reaction_signatures(
                kind=kind,
                low=bar.signal.low,
                high=bar.signal.high,
                close=bar.signal.close,
                ma=ma,
            )
            for label, value in signatures.items():
                prefix = "low" if kind is PivotKind.LOW else "high"
                row[f"{prefix}_{label}_{name.casefold()}"] = _as_bool(value)
        if index + 2 < len(canonical):
            row["follow_through_p2_pct"] = (
                canonical[index + 2].signal.close / bar.signal.close - Decimal(1)
            ) * Decimal(100)
            row["follow_through_p2_date"] = canonical[index + 2].trade_date
        else:
            row["follow_through_p2_pct"] = None
            row["follow_through_p2_date"] = None
        decorated.append(row)
    return tuple(
        sorted(
            decorated,
            key=lambda item: (
                item["stock_code"],
                item["pivot_trade_date"],
                item["pivot_kind"],
            ),
        )
    )


def _reaction_matrix(
    rows: Sequence[Mapping[str, Any]],
    *,
    kind: PivotKind,
) -> dict[str, Any]:
    labels = (
        ("wick_reclaim", "WICK_RECLAIM")
        if kind is PivotKind.LOW
        else ("wick_rejection", "WICK_REJECTION")
    )
    result: dict[str, Any] = {}
    for quartile in ("Q1", "Q2", "Q3", "Q4"):
        group = [
            row
            for row in rows
            if row["pivot_kind"] == kind.value
            and row.get("range_speed_quartile") == quartile
        ]
        per_ma: dict[str, Any] = {}
        for name in _MA_NAMES:
            field = f"{('low' if kind is PivotKind.LOW else 'high')}_{labels[0]}_{name.casefold()}"
            count = sum(row.get(field) is True for row in group)
            per_ma[name] = {
                "count": count,
                "percentage": Decimal(count) / Decimal(len(group)) if group else None,
            }
        result[quartile] = {"count": len(group), "reaction": per_ma}
    return result


def _signature_matrix(
    rows: Sequence[Mapping[str, Any]],
    *,
    kind: PivotKind,
) -> dict[str, Any]:
    labels = (
        ("wick_reclaim", "full_hold_above", "close_below")
        if kind is PivotKind.LOW
        else ("wick_rejection", "full_hold_below", "close_above")
    )
    prefix = "low" if kind is PivotKind.LOW else "high"
    result: dict[str, Any] = {}
    subset = [row for row in rows if row["pivot_kind"] == kind.value]
    for label in labels:
        result[label.upper()] = {
            name: sum(
                row.get(f"{prefix}_{label}_{name.casefold()}") is True for row in subset
            )
            for name in _MA_NAMES
        }
    return result


def _group_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "support_roles": _role_distribution(rows, "nearest_support_ma"),
        "resistance_roles": _role_distribution(rows, "nearest_resistance_ma"),
        "nearest_roles": _role_distribution(rows, "nearest_ma"),
        "signed_distance_medians": {
            "low": {
                name: _distribution(rows, f"low_signed_dist_{name.casefold()}_atr")[
                    "median"
                ]
                for name in _MA_NAMES
            },
            "close": {
                name: _distribution(rows, f"close_signed_dist_{name.casefold()}_atr")[
                    "median"
                ]
                for name in _MA_NAMES
            },
            "high": {
                name: _distribution(rows, f"high_signed_dist_{name.casefold()}_atr")[
                    "median"
                ]
                for name in _MA_NAMES
            },
        },
    }


def _direction_efficiency_group_rows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    return {
        "FAST_POSITIVE_HIGH_EFFICIENCY": [
            row
            for row in rows
            if row.get("range_speed_quartile") == "Q4"
            and row.get("net_move_atr_10") is not None
            and row["net_move_atr_10"] > 0
            and row.get("efficiency_10_quartile") == "Q4"
        ],
        "FAST_NEGATIVE_HIGH_EFFICIENCY": [
            row
            for row in rows
            if row.get("range_speed_quartile") == "Q4"
            and row.get("net_move_atr_10") is not None
            and row["net_move_atr_10"] < 0
            and row.get("efficiency_10_quartile") == "Q4"
        ],
        "FAST_LOW_EFFICIENCY": [
            row
            for row in rows
            if row.get("range_speed_quartile") == "Q4"
            and row.get("efficiency_10_quartile") == "Q1"
        ],
        "SLOW": [row for row in rows if row.get("range_speed_quartile") == "Q1"],
    }


def _direction_efficiency_groups(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        name: _group_summary(group)
        for name, group in _direction_efficiency_group_rows(rows).items()
    }


def _role_period_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for group_name, group in {
        "ALL": rows,
        **_direction_efficiency_group_rows(rows),
    }.items():
        roles = [row["nearest_ma"] for row in group if row.get("nearest_ma")]
        periods = [Decimal(_MA_PERIODS[role]) for role in roles]
        result[group_name] = {
            "count": len(roles),
            "median_nearest_period": _median(periods),
            "mean_nearest_period": (
                sum(periods, Decimal(0)) / Decimal(len(periods)) if periods else None
            ),
        }
    return result


def _flow_reaction_interaction(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    directional = [row for row in rows if row.get("direction_speed_quartile") == "Q4"]
    result: dict[str, Any] = {}
    for label, flow_bucket in (
        ("HIGH_DIRECTION_HIGH_FLOW", "Q4"),
        ("HIGH_DIRECTION_LOW_FLOW", "Q1"),
    ):
        group = [
            row for row in directional if row.get("flow_speed_quartile") == flow_bucket
        ]
        result[label] = {
            "count": len(group),
            "nearest_roles": _role_distribution(group, "nearest_ma"),
            "low_wick_reclaim": _signature_matrix(group, kind=PivotKind.LOW)[
                "WICK_RECLAIM"
            ],
            "high_wick_rejection": _signature_matrix(group, kind=PivotKind.HIGH)[
                "WICK_REJECTION"
            ],
        }
    return result


def _chart_events(row: Mapping[str, Any]) -> tuple[ReviewEvent, ...]:
    label = "PIVOT LOW" if row["pivot_kind"] == PivotKind.LOW.value else "PIVOT HIGH"
    return (
        ReviewEvent(
            ReviewEventType.PULLBACK_TOUCH,
            row["pivot_trade_date"],
            label,
            adjusted_plot_price=row["pivot_price"],
            details={
                "confirmed_at": row["confirmed_at"],
                "reaction_nearest_ma": row.get("nearest_ma"),
                "nearest_margin_atr": row.get("nearest_margin_atr"),
            },
        ),
    )


def _choose_chart_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, dict[str, Any]], ...]:
    valid = [row for row in rows if row.get("range_speed") is not None]
    groups = {
        "FAST_DIRECTIONAL_SHORT_MA": [
            row
            for row in valid
            if row.get("range_speed_quartile") == "Q4"
            and row.get("direction_speed_quartile") == "Q4"
            and row.get("nearest_ma") in {"MA5", "MA10"}
        ],
        "FAST_NOISY": [
            row
            for row in valid
            if row.get("range_speed_quartile") == "Q4"
            and row.get("efficiency_10_quartile") == "Q1"
        ],
        "SLOW_LONG_MA": [
            row
            for row in valid
            if row.get("range_speed_quartile") == "Q1"
            and row.get("nearest_ma") in {"MA20", "MA60"}
        ],
    }

    def ordered(group: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return sorted(
            group,
            key=lambda row: (
                row["pivot_trade_date"],
                row["pivot_kind"],
                row["stock_code"],
            ),
        )

    return tuple(
        (category, dict(row))
        for category, group in groups.items()
        for row in ordered(group)[:5]
    )


def generate_charts(
    rows: Sequence[Mapping[str, Any]],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
    *,
    chart_root: Path = CHART_ROOT,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for category, row in _choose_chart_rows(rows):
        bars = tuple(daily_by_stock[row["stock_code"]])
        date_to_index = {bar.trade_date: index for index, bar in enumerate(bars)}
        pivot_index = date_to_index[row["pivot_trade_date"]]
        confirm_index = min(len(bars) - 1, pivot_index + 2)
        prepared = prepare_review_chart(
            bars,
            chart_type=ChartType.EVENT_REVIEW,
            events=_chart_events(row),
            calendar=ExplicitTradingCalendar(bar.trade_date for bar in bars),
            focus_date=row["pivot_trade_date"],
            event_end_date=bars[confirm_index].trade_date,
            pre_sessions=60,
            post_sessions=20,
            show_sma5=True,
            shade_below_sma10_context=True,
        )
        filename = deterministic_chart_filename(
            row["stock_code"],
            ChartType.EVENT_REVIEW,
            row["pivot_trade_date"],
            slug=f"market-clock-v02-{category.casefold()}",
        )
        artifact = render_review_chart(
            prepared,
            chart_root / category / filename,
            strategy_policy=PROOF_VERSION,
            summary={
                **dict(row),
                "chart_category": category,
                "report_only": True,
                "strategy_changes_applied": False,
            },
        )
        artifacts.append(
            {
                "stock_code": row["stock_code"],
                "pivot_trade_date": row["pivot_trade_date"],
                "pivot_kind": row["pivot_kind"],
                "category": category,
                "png_path": artifact.png_path.as_posix(),
                "metadata_path": artifact.metadata_path.as_posix(),
            }
        )
    return artifacts


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime, Decimal)):
        return value.isoformat() if not isinstance(value, Decimal) else str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime, Decimal)):
        return value.isoformat() if not isinstance(value, Decimal) else str(value)
    return str(value)


def _write_reaction_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REACTION_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {field: _csv_value(row.get(field)) for field in REACTION_FIELDS}
            )


def run_market_clock_reaction_audit(
    *,
    output: Path = OUTPUT_PATH,
    summary_csv: Path = SUMMARY_CSV_PATH,
    chart_root: Path = CHART_ROOT,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    daily_by_stock: dict[str, tuple[DailyBar, ...]] = {}
    clock_by_stock: dict[str, dict[date, Mapping[str, Any]]] = {}
    observation_rows: list[dict[str, Any]] = []
    pivot_inputs: list[
        tuple[
            tuple[DailyBar, ...], tuple[DailyIndicatorPoint, ...], list[dict[str, Any]]
        ]
    ] = []
    provenance: dict[str, Any] = {}
    for stock_code in sorted(stocks):
        bars, source_provenance = _load_stock(stock_code)
        canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
        points = tuple(
            calculate_daily_indicators(
                canonical,
                ExplicitTradingCalendar(bar.trade_date for bar in canonical),
            )
        )
        clock = list(_clock_series(canonical, points))
        daily_by_stock[stock_code] = canonical
        clock_by_stock[stock_code] = {row["trade_date"]: row for row in clock}
        provenance[stock_code] = source_provenance
        observation_rows.extend(
            row for row in clock if RESEARCH_START <= row["trade_date"] <= RESEARCH_END
        )
        pivot_inputs.append((canonical, points, clock))
    observation_rows.sort(key=lambda row: (row["stock_code"], row["trade_date"]))
    _add_quartiles(observation_rows)
    pivot_rows: list[dict[str, Any]] = []
    for bars, points, clock in pivot_inputs:
        pivot_rows.extend(_decorate_pivot_rows(bars, _pivot_rows(bars, points, clock)))
    pivot_rows.sort(
        key=lambda row: (row["stock_code"], row["pivot_trade_date"], row["pivot_kind"])
    )
    charts = generate_charts(pivot_rows, daily_by_stock, chart_root=chart_root)
    overlay = _overlay_events(DAILY_PROOF_PATH, clock_by_stock, pivot_rows)
    v01_payload: Mapping[str, Any] = {}
    if V01_OUTPUT_PATH.exists():
        v01_payload = json.loads(V01_OUTPUT_PATH.read_text(encoding="utf-8"))
    report = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "research_period": {"start": RESEARCH_START, "end": RESEARCH_END},
        "source_v01": V01_OUTPUT_PATH.as_posix(),
        "source_v01_proof_version": V01_PROOF_VERSION,
        "artifact_provenance": provenance,
        "population": {
            "stock_count": len(stocks),
            "daily_observation_count": len(observation_rows),
            "pivot_count": len(pivot_rows),
            "pivot_low_count": sum(
                row["pivot_kind"] == PivotKind.LOW.value for row in pivot_rows
            ),
            "pivot_high_count": sum(
                row["pivot_kind"] == PivotKind.HIGH.value for row in pivot_rows
            ),
        },
        "methodology": {
            "signal_price_basis": "ADJUSTED_DAILY_OHLC",
            "atr_basis": "SIGNAL_ATR20_AT_PIVOT",
            "reaction_labels": "parameter-free, multi-label; nearest MA remains single-label",
            "nearest_tie_break": "MA5, MA10, MA20, MA60",
            "follow_through": "Close[P+2] / Close[P] - 1; research-only",
            "quartile_basis": "V0.1 pooled research observations",
            "strategy_changes": False,
        },
        "v01_matrix": {
            "source": V01_OUTPUT_PATH.as_posix(),
            "range_quartile_support": v01_payload.get(
                "range_quartile_role_matrix", {}
            ).get(
                "support",
                _role_matrix(
                    pivot_rows,
                    bucket_field="range_speed_quartile",
                    role_field="nearest_support_ma",
                ),
            ),
            "range_quartile_resistance": v01_payload.get(
                "range_quartile_role_matrix", {}
            ).get(
                "resistance",
                _role_matrix(
                    pivot_rows,
                    bucket_field="range_speed_quartile",
                    role_field="nearest_resistance_ma",
                ),
            ),
            "directional_speed": v01_payload.get("directional_speed_matrix", {}),
            "efficiency_interaction": v01_payload.get("efficiency_interaction", {}),
            "flow_interaction": v01_payload.get("flow_interaction", {}),
        },
        "signed_distance_distributions": {
            kind: {
                name: {
                    axis: _distribution(
                        [row for row in pivot_rows if row["pivot_kind"] == kind],
                        f"{axis}_signed_dist_{name.casefold()}_atr",
                    )
                    for axis in ("low", "close", "high")
                }
                for name in _MA_NAMES
            }
            for kind in (PivotKind.LOW.value, PivotKind.HIGH.value)
        },
        "pivot_low_signatures": _signature_matrix(pivot_rows, kind=PivotKind.LOW),
        "pivot_high_signatures": _signature_matrix(pivot_rows, kind=PivotKind.HIGH),
        "range_reaction_matrix": {
            "pivot_low_wick_reclaim": _reaction_matrix(pivot_rows, kind=PivotKind.LOW),
            "pivot_high_wick_rejection": _reaction_matrix(
                pivot_rows, kind=PivotKind.HIGH
            ),
        },
        "ma_clustering": {
            "pivot_low": {
                "nearest_distance": _distribution(
                    [
                        row
                        for row in pivot_rows
                        if row["pivot_kind"] == PivotKind.LOW.value
                    ],
                    "nearest_distance_atr",
                ),
                "second_nearest_distance": _distribution(
                    [
                        row
                        for row in pivot_rows
                        if row["pivot_kind"] == PivotKind.LOW.value
                    ],
                    "second_nearest_distance_atr",
                ),
                "nearest_margin": _distribution(
                    [
                        row
                        for row in pivot_rows
                        if row["pivot_kind"] == PivotKind.LOW.value
                    ],
                    "nearest_margin_atr",
                ),
                "pair_gaps": {
                    field: _distribution(
                        [
                            row
                            for row in pivot_rows
                            if row["pivot_kind"] == PivotKind.LOW.value
                        ],
                        field,
                    )
                    for field in (
                        "ma5_10_gap_abs_atr",
                        "ma10_20_gap_abs_atr",
                        "ma20_60_gap_abs_atr",
                    )
                },
            },
            "pivot_high": {
                "nearest_distance": _distribution(
                    [
                        row
                        for row in pivot_rows
                        if row["pivot_kind"] == PivotKind.HIGH.value
                    ],
                    "nearest_distance_atr",
                ),
                "second_nearest_distance": _distribution(
                    [
                        row
                        for row in pivot_rows
                        if row["pivot_kind"] == PivotKind.HIGH.value
                    ],
                    "second_nearest_distance_atr",
                ),
                "nearest_margin": _distribution(
                    [
                        row
                        for row in pivot_rows
                        if row["pivot_kind"] == PivotKind.HIGH.value
                    ],
                    "nearest_margin_atr",
                ),
                "pair_gaps": {
                    field: _distribution(
                        [
                            row
                            for row in pivot_rows
                            if row["pivot_kind"] == PivotKind.HIGH.value
                        ],
                        field,
                    )
                    for field in (
                        "ma5_10_gap_abs_atr",
                        "ma10_20_gap_abs_atr",
                        "ma20_60_gap_abs_atr",
                    )
                },
            },
        },
        "direction_efficiency_interaction": _direction_efficiency_groups(pivot_rows),
        "role_period_summary": _role_period_summary(pivot_rows),
        "flow_reaction_interaction": _flow_reaction_interaction(pivot_rows),
        "follow_through": {
            kind: _distribution(
                [row for row in pivot_rows if row["pivot_kind"] == kind],
                "follow_through_p2_pct",
            )
            for kind in (PivotKind.LOW.value, PivotKind.HIGH.value)
        },
        "strategy_event_overlay": overlay,
        "large_winner_loss_comparison": _large_trade_comparison(
            DAILY_PROOF_PATH, clock_by_stock
        ),
        "representative_charts": charts,
        "effective_horizon_comparison": _effective_horizon_comparison(observation_rows),
        "strategy_changes_applied": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_reaction_csv(summary_csv, pivot_rows)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--summary-csv", type=Path, default=SUMMARY_CSV_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    args = parser.parse_args()
    report = run_market_clock_reaction_audit(
        output=args.output,
        summary_csv=args.summary_csv,
        chart_root=args.chart_root,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "daily_observations": report["population"]["daily_observation_count"],
                "pivot_rows": report["population"]["pivot_count"],
                "charts": len(report["representative_charts"]),
                "network_calls": report["network_calls"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
