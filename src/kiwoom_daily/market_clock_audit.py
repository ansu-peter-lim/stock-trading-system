"""Descriptive MARKET_CLOCK and moving-average-role audit.

The audit consumes cached Daily RAW/ADJUSTED artifacts and the frozen V0.2
strategy proof.  It is deliberately report-only: no strategy threshold,
adaptive moving average, re-entry order, or PnL calculation is introduced.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import (
    DailyIndicatorPoint,
    PivotKind,
    calculate_daily_indicators,
    detect_daily_pivots,
    moving_average_slope,
    simple_moving_average,
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
from .market_speed_audit import (
    _atr20,
    _daily_returns,
    _past_reference,
    _rolling_mean,
    _rolling_std,
)

RESEARCH_START = date(2023, 9, 1)
RESEARCH_END = date(2026, 8, 28)
PROOF_VERSION = "MARKET_CLOCK_MOVING_AVERAGE_ROLE_AUDIT_V0_1"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_role_audit_v0_1.json"
)
SUMMARY_CSV_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_role_audit_v0_1.csv"
)
CHART_ROOT = Path("data/processed/strategy_charts/market_clock_ma_role_audit_v0_1")
MARKET_SPEED_PATH = Path(
    "data/processed/strategy_review/down_box_v0_3b_market_speed_audit.json"
)
DAILY_PROOF_PATH = Path(
    "data/processed/kiwoom/down_box_reversal_v0_3_daily_execution_proof.json"
)
STOCKS = (
    "005930",
    "000660",
    "035720",
    "005380",
    "035420",
    "068270",
    "105560",
    "012450",
    "034020",
    "066570",
)

ROLE_FIELDS = (
    "stock_code",
    "pivot_kind",
    "pivot_trade_date",
    "confirmed_at",
    "pivot_price",
    "atr20",
    "sma5",
    "sma10",
    "sma20",
    "sma60",
    "sma20_change_5",
    "sma60_change_5",
    "ma5_10_gap_atr",
    "ma10_20_gap_atr",
    "ma20_60_gap_atr",
    "range_speed",
    "range_speed_quartile",
    "abs_net_move_atr_10",
    "direction_speed_quartile",
    "efficiency_10",
    "efficiency_10_quartile",
    "flow_speed",
    "flow_speed_quartile",
    "net_move_atr_10",
    "net_move_atr_20",
    "efficiency_20",
    "low_to_ma5_atr",
    "low_to_ma10_atr",
    "low_to_ma20_atr",
    "low_to_ma60_atr",
    "high_to_ma5_atr",
    "high_to_ma10_atr",
    "high_to_ma20_atr",
    "high_to_ma60_atr",
    "close_to_ma5_atr",
    "close_to_ma10_atr",
    "close_to_ma20_atr",
    "close_to_ma60_atr",
    "nearest_support_ma",
    "nearest_support_distance_atr",
    "support_tie_count",
    "nearest_resistance_ma",
    "nearest_resistance_distance_atr",
    "resistance_tie_count",
)

_MA_NAMES = ("MA5", "MA10", "MA20", "MA60")


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / Decimal(len(values)) if values else None


def _percentile(values: Sequence[Decimal], quantile: Decimal) -> Decimal | None:
    ordered = sorted(values)
    if not ordered:
        return None
    position = Decimal(len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _distribution(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    values = [value for row in rows if (value := row.get(field)) is not None]
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "p25": _percentile(values, Decimal("0.25")),
        "median": _percentile(values, Decimal("0.50")),
        "p75": _percentile(values, Decimal("0.75")),
        "max": max(values) if values else None,
    }


def _quartile(value: Decimal | None, values: Sequence[Decimal]) -> str | None:
    if value is None or not values:
        return None
    q25 = _percentile(values, Decimal("0.25"))
    q50 = _percentile(values, Decimal("0.50"))
    q75 = _percentile(values, Decimal("0.75"))
    if value <= q25:
        return "Q1"
    if value <= q50:
        return "Q2"
    if value <= q75:
        return "Q3"
    return "Q4"


def _true_range_pct(bars: Sequence[DailyBar]) -> list[Decimal | None]:
    result: list[Decimal | None] = [None]
    for previous, current in pairwise(bars):
        previous_close = previous.signal.close
        if previous_close == 0:
            result.append(None)
            continue
        current_signal = current.signal
        true_range = max(
            current_signal.high - current_signal.low,
            abs(current_signal.high - previous_close),
            abs(current_signal.low - previous_close),
        )
        result.append(true_range / previous_close)
    return result


def _efficiency(closes: Sequence[Decimal], index: int, window: int) -> Decimal | None:
    if index < window:
        return None
    denominator = sum(
        (
            abs(closes[item] - closes[item - 1])
            for item in range(index - window + 1, index + 1)
        ),
        Decimal(0),
    )
    if denominator == 0:
        return None
    return abs(closes[index] - closes[index - window]) / denominator


def _clock_series(
    bars: Sequence[DailyBar], points: Sequence[DailyIndicatorPoint]
) -> tuple[dict[str, Any], ...]:
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    canonical_points = tuple(sorted(points, key=lambda point: point.trade_date))
    closes = [bar.signal.close for bar in canonical]
    returns = _daily_returns(closes)
    rv20 = [_rolling_std(returns, i, 20) for i in range(len(canonical))]
    traded_value = [bar.raw.close * Decimal(bar.raw.volume) for bar in canonical]
    flow20 = [_rolling_mean(traded_value, i, 20) for i in range(len(canonical))]
    tr_pct = _true_range_pct(canonical)
    atr_pct20 = [
        _rolling_mean([v for v in tr_pct[i - 19 : i + 1] if v is not None], 19, 20)
        if i >= 19 and all(v is not None for v in tr_pct[i - 19 : i + 1])
        else None
        for i in range(len(canonical))
    ]
    range_refs = [_past_reference(atr_pct20, i) for i in range(len(canonical))]
    rv_refs = [_past_reference(rv20, i) for i in range(len(canonical))]
    flow_refs = [_past_reference(flow20, i) for i in range(len(canonical))]
    sma5 = simple_moving_average(closes, 5)
    sma10 = [point.sma10 for point in canonical_points]
    sma20 = [point.sma20 for point in canonical_points]
    sma60 = [point.sma60 for point in canonical_points]
    sma10_slope = moving_average_slope(sma10, 5)
    sma20_slope = moving_average_slope(sma20, 5)
    sma60_slope = moving_average_slope(sma60, 5)
    rows: list[dict[str, Any]] = []
    for index, bar in enumerate(canonical):
        rv = rv20[index]
        rv_ref = rv_refs[index]
        vol_ratio = rv / rv_ref if rv is not None and rv_ref else None
        variance_speed = vol_ratio**2 if vol_ratio is not None else None
        flow = flow20[index]
        flow_ref = flow_refs[index]
        flow_speed = flow / flow_ref if flow is not None and flow_ref else None
        atr = _atr20(canonical, index)
        range_speed = (
            atr_pct20[index] / range_refs[index]
            if atr_pct20[index] is not None and range_refs[index]
            else None
        )
        net10 = (
            (closes[index] - closes[index - 10]) / atr if index >= 10 and atr else None
        )
        net20 = (
            (closes[index] - closes[index - 20]) / atr if index >= 20 and atr else None
        )
        row = {
            "stock_code": bar.stock_code,
            "trade_date": bar.trade_date,
            "rv20": rv,
            "rv_ref": rv_ref,
            "vol_ratio": vol_ratio,
            "variance_speed": variance_speed,
            "atr_pct20": atr_pct20[index],
            "atr_pct_ref": range_refs[index],
            "range_speed": range_speed,
            "net_move_atr_10": net10,
            "net_move_atr_20": net20,
            "abs_net_move_atr_10": abs(net10) if net10 is not None else None,
            "efficiency_10": _efficiency(closes, index, 10),
            "efficiency_20": _efficiency(closes, index, 20),
            "flow20": flow,
            "flow_ref": flow_ref,
            "flow_speed": flow_speed,
            "atr20": atr,
            "sma5": sma5[index],
            "sma10": sma10[index],
            "sma20": sma20[index],
            "sma60": sma60[index],
            "sma10_change_5": sma10_slope[index],
            "sma20_change_5": sma20_slope[index],
            "sma60_change_5": sma60_slope[index],
        }
        if atr is not None:
            for name, value in (
                ("MA5", sma5[index]),
                ("MA10", sma10[index]),
                ("MA20", sma20[index]),
                ("MA60", sma60[index]),
            ):
                row[f"close_to_{name.casefold()}_atr"] = (
                    abs(closes[index] - value) / atr if value is not None else None
                )
        rows.append(row)
    return tuple(rows)


def _add_quartiles(rows: Sequence[dict[str, Any]]) -> None:
    fields = (
        ("range_speed", "range_speed_quartile"),
        ("abs_net_move_atr_10", "direction_speed_quartile"),
        ("efficiency_10", "efficiency_10_quartile"),
        ("flow_speed", "flow_speed_quartile"),
    )
    distributions = {
        field: [row[field] for row in rows if row.get(field) is not None]
        for field, _ in fields
    }
    for row in rows:
        for field, bucket_field in fields:
            row[bucket_field] = _quartile(row.get(field), distributions[field])


def _ma_distances(
    pivot_price: Decimal,
    values: Mapping[str, Decimal | None],
    atr: Decimal | None,
) -> tuple[dict[str, Decimal | None], str | None, Decimal | None, int]:
    distances = {
        name: abs(pivot_price - values[name]) / atr
        if atr is not None and values[name] is not None
        else None
        for name in _MA_NAMES
    }
    available = [
        (name, value) for name, value in distances.items() if value is not None
    ]
    if not available:
        return distances, None, None, 0
    minimum = min(value for _, value in available)
    tied = [name for name, value in available if value == minimum]
    return distances, tied[0], minimum, len(tied)


def _ma_gap(
    first: Decimal | None, second: Decimal | None, atr: Decimal | None
) -> Decimal | None:
    if first is None or second is None or atr is None:
        return None
    return (first - second) / atr


def _pivot_rows(
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
    clock: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in canonical)
    pivots = detect_daily_pivots(canonical, calendar)
    index_by_date = {bar.trade_date: index for index, bar in enumerate(canonical)}
    points_by_date = {point.trade_date: point for point in points}
    clock_by_date = {row["trade_date"]: row for row in clock}
    rows: list[dict[str, Any]] = []
    for pivot in pivots:
        if not RESEARCH_START <= pivot.pivot_trade_date <= RESEARCH_END:
            continue
        if pivot.confirmed_at.date() > RESEARCH_END:
            continue
        index = index_by_date[pivot.pivot_trade_date]
        bar = canonical[index]
        point = points_by_date[pivot.pivot_trade_date]
        market = dict(clock_by_date[pivot.pivot_trade_date])
        values = {
            "MA5": market.get("sma5"),
            "MA10": point.sma10,
            "MA20": point.sma20,
            "MA60": point.sma60,
        }
        atr = market.get("atr20")
        low_dist = {
            f"low_to_{name.casefold()}_atr": value
            for name, value in zip(
                _MA_NAMES,
                (
                    abs(pivot.price - values[name]) / atr
                    if atr is not None and values[name] is not None
                    else None
                    for name in _MA_NAMES
                ),
                strict=True,
            )
        }
        high_dist = {
            f"high_to_{name.casefold()}_atr": value
            for name, value in zip(
                _MA_NAMES,
                (
                    abs(pivot.price - values[name]) / atr
                    if atr is not None and values[name] is not None
                    else None
                    for name in _MA_NAMES
                ),
                strict=True,
            )
        }
        _, low_name, low_value, low_ties = _ma_distances(pivot.price, values, atr)
        _, high_name, high_value, high_ties = _ma_distances(pivot.price, values, atr)
        close_dist = {
            f"close_to_{name.casefold()}_atr": value
            for name, value in zip(
                _MA_NAMES,
                (
                    abs(bar.signal.close - values[name]) / atr
                    if atr is not None and values[name] is not None
                    else None
                    for name in _MA_NAMES
                ),
                strict=True,
            )
        }
        row = {
            "stock_code": pivot.stock_code,
            "pivot_kind": pivot.kind.value,
            "pivot_trade_date": pivot.pivot_trade_date,
            "confirmed_at": pivot.confirmed_at,
            "pivot_price": pivot.price,
            "atr20": atr,
            "sma5": values["MA5"],
            "sma10": values["MA10"],
            "sma20": values["MA20"],
            "sma60": values["MA60"],
            "sma20_change_5": market.get("sma20_change_5"),
            "sma60_change_5": market.get("sma60_change_5"),
            "ma5_10_gap_atr": _ma_gap(values["MA5"], values["MA10"], atr),
            "ma10_20_gap_atr": _ma_gap(values["MA10"], values["MA20"], atr),
            "ma20_60_gap_atr": _ma_gap(values["MA20"], values["MA60"], atr),
            "nearest_support_ma": low_name if pivot.kind is PivotKind.LOW else None,
            "nearest_support_distance_atr": low_value
            if pivot.kind is PivotKind.LOW
            else None,
            "support_tie_count": low_ties if pivot.kind is PivotKind.LOW else 0,
            "nearest_resistance_ma": high_name
            if pivot.kind is PivotKind.HIGH
            else None,
            "nearest_resistance_distance_atr": high_value
            if pivot.kind is PivotKind.HIGH
            else None,
            "resistance_tie_count": high_ties if pivot.kind is PivotKind.HIGH else 0,
            **low_dist,
            **high_dist,
            **close_dist,
        }
        row.update(
            {
                key: market.get(key)
                for key in (
                    "range_speed",
                    "range_speed_quartile",
                    "abs_net_move_atr_10",
                    "direction_speed_quartile",
                    "efficiency_10",
                    "efficiency_10_quartile",
                    "flow_speed",
                    "flow_speed_quartile",
                    "net_move_atr_10",
                    "net_move_atr_20",
                    "efficiency_20",
                )
            }
        )
        rows.append(row)
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row["stock_code"],
                row["pivot_trade_date"],
                row["pivot_kind"],
            ),
        )
    )


def _role_matrix(
    rows: Sequence[Mapping[str, Any]],
    *,
    bucket_field: str,
    role_field: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for bucket in ("Q1", "Q2", "Q3", "Q4"):
        group = [row for row in rows if row.get(bucket_field) == bucket]
        counts = {
            name: sum(row.get(role_field) == name for row in group)
            for name in _MA_NAMES
        }
        total = sum(counts.values())
        result[bucket] = {
            "count": total,
            "roles": {
                name: {
                    "count": count,
                    "percentage": Decimal(count) / Decimal(total) if total else None,
                }
                for name, count in counts.items()
            },
        }
    return result


def _role_distribution(
    rows: Sequence[Mapping[str, Any]], role_field: str
) -> dict[str, Any]:
    counts = {
        name: sum(row.get(role_field) == name for row in rows) for name in _MA_NAMES
    }
    total = sum(counts.values())
    return {
        "count": total,
        "roles": {
            name: {
                "count": count,
                "percentage": Decimal(count) / Decimal(total) if total else None,
            }
            for name, count in counts.items()
        },
    }


def _directional_matrix(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for bucket in ("Q1", "Q2", "Q3", "Q4"):
        group = [row for row in rows if row.get("direction_speed_quartile") == bucket]
        result[bucket] = {
            "all": _role_distribution(group, "nearest_support_ma"),
            "positive": _role_distribution(
                [
                    row
                    for row in group
                    if row.get("net_move_atr_10") is not None
                    and row["net_move_atr_10"] > 0
                ],
                "nearest_support_ma",
            ),
            "negative": _role_distribution(
                [
                    row
                    for row in group
                    if row.get("net_move_atr_10") is not None
                    and row["net_move_atr_10"] < 0
                ],
                "nearest_support_ma",
            ),
        }
    return result


def _interaction_matrix(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups = {
        "HIGH_RANGE_HIGH_EFFICIENCY": [
            row
            for row in rows
            if row.get("range_speed_quartile") == "Q4"
            and row.get("efficiency_10_quartile") == "Q4"
        ],
        "HIGH_RANGE_LOW_EFFICIENCY": [
            row
            for row in rows
            if row.get("range_speed_quartile") == "Q4"
            and row.get("efficiency_10_quartile") == "Q1"
        ],
    }
    return {
        name: {
            "count": len(group),
            "support_roles": {
                ma: sum(row.get("nearest_support_ma") == ma for row in group)
                for ma in _MA_NAMES
            },
            "resistance_roles": {
                ma: sum(row.get("nearest_resistance_ma") == ma for row in group)
                for ma in _MA_NAMES
            },
        }
        for name, group in groups.items()
    }


def _flow_interaction(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    directional_high = [
        row for row in rows if row.get("direction_speed_quartile") == "Q4"
    ]
    return {
        "HIGH_DIRECTION_HIGH_FLOW": {
            "count": sum(
                row.get("flow_speed_quartile") == "Q4" for row in directional_high
            ),
            "support_roles": {
                ma: sum(
                    row.get("flow_speed_quartile") == "Q4"
                    and row.get("nearest_support_ma") == ma
                    for row in directional_high
                )
                for ma in _MA_NAMES
            },
        },
        "HIGH_DIRECTION_LOW_FLOW": {
            "count": sum(
                row.get("flow_speed_quartile") == "Q1" for row in directional_high
            ),
            "support_roles": {
                ma: sum(
                    row.get("flow_speed_quartile") == "Q1"
                    and row.get("nearest_support_ma") == ma
                    for row in directional_high
                )
                for ma in _MA_NAMES
            },
        },
    }


def _effective_horizon_comparison(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize the two report-only clocks without changing MA periods."""

    return {
        "variance_clock": {
            f"MA{period}": _distribution(rows, f"ma{period}_effective_variance")
            for period in (10, 20, 60)
        },
        "range_clock": {
            f"MA{period}": _distribution(rows, f"ma{period}_effective_range")
            for period in (10, 20, 60)
        },
    }


def _role_hypothesis(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Provide descriptive fast/slow role counts; no strategy threshold is applied."""

    groups = {
        "FAST_DIRECTIONAL_EFFICIENT": [
            row
            for row in rows
            if row.get("range_speed_quartile") == "Q4"
            and row.get("direction_speed_quartile") == "Q4"
            and row.get("efficiency_10_quartile") == "Q4"
        ],
        "SLOW_OR_NORMAL": [
            row
            for row in rows
            if row.get("range_speed_quartile") in {"Q1", "Q2"}
            or row.get("direction_speed_quartile") in {"Q1", "Q2"}
        ],
    }
    result: dict[str, Any] = {}
    for name, group in groups.items():
        result[name] = {
            "count": len(group),
            "support_roles": _role_distribution(group, "nearest_support_ma"),
            "resistance_roles": _role_distribution(group, "nearest_resistance_ma"),
            "support_distance_atr": _distribution(
                group, "nearest_support_distance_atr"
            ),
            "resistance_distance_atr": _distribution(
                group, "nearest_resistance_distance_atr"
            ),
        }
    result["interpretation"] = {
        "status": "INCONCLUSIVE",
        "reason": "Descriptive quartile audit has no prespecified inferential or strategy threshold.",
    }
    return result


def _overlay_events(
    daily_proof_path: Path,
    clock_by_stock: Mapping[str, Mapping[date, Mapping[str, Any]]],
    pivot_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    payload = json.loads(daily_proof_path.read_text(encoding="utf-8"))
    event_names = {
        "REVERSAL_SETUP_CREATED": "DOWN_BOX_ENTRY",
        "MA5_TURN": "DOWN_BOX_ENTRY",
        "SMA10_REBREAK": "DOWN_BOX_ENTRY",
        "FLOOR_BREAK": "FLOOR_EXIT",
        "UPPER_TAKE_PROFIT": "UPPER_EXIT",
    }
    nearest_by_stock = {
        stock: sorted(
            [row for row in pivot_rows if row["stock_code"] == stock],
            key=lambda row: (row["pivot_trade_date"], row["pivot_kind"]),
        )
        for stock in clock_by_stock
    }
    result: list[dict[str, Any]] = []
    for stock_code, stock_payload in sorted(payload["per_stock"].items()):
        clock = clock_by_stock.get(stock_code, {})
        for event in stock_payload.get("daily_events", []):
            event_type = event_names.get(event.get("event"))
            if event_type is None:
                continue
            event_date = date.fromisoformat(event["event_date"])
            metrics = dict(clock.get(event_date, {}))
            prior_pivots = [
                row
                for row in nearest_by_stock.get(stock_code, [])
                if row["pivot_trade_date"] <= event_date
                and datetime.fromisoformat(str(row["confirmed_at"])).date()
                <= event_date
            ]
            last = prior_pivots[-1] if prior_pivots else None
            result.append(
                {
                    "stock_code": stock_code,
                    "event": event_type,
                    "event_date": event_date,
                    "setup_id": event.get("setup_id"),
                    "source_event": event.get("event"),
                    "nearest_confirmed_pivot_role": (
                        last.get("nearest_support_ma")
                        or last.get("nearest_resistance_ma")
                        if last
                        else None
                    ),
                    "range_speed": metrics.get("range_speed"),
                    "direction_speed": metrics.get("abs_net_move_atr_10"),
                    "efficiency_10": metrics.get("efficiency_10"),
                    "flow_speed": metrics.get("flow_speed"),
                }
            )
    return tuple(
        sorted(
            result, key=lambda row: (row["stock_code"], row["event_date"], row["event"])
        )
    )


def _large_trade_comparison(
    daily_proof_path: Path,
    clock_by_stock: Mapping[str, Mapping[date, Mapping[str, Any]]],
) -> tuple[dict[str, Any], ...]:
    payload = json.loads(daily_proof_path.read_text(encoding="utf-8"))
    result: list[dict[str, Any]] = []
    for stock_code, stock_payload in sorted(payload["per_stock"].items()):
        clock = clock_by_stock.get(stock_code, {})
        for trade in stock_payload.get("completed_trades", []):
            pnl = Decimal(str(trade["pnl_pct"]))
            category = (
                "LARGE_WINNER"
                if pnl >= Decimal(20)
                else "LARGE_LOSS"
                if pnl <= Decimal(-8)
                else None
            )
            if category is None:
                continue
            entry_date = date.fromisoformat(trade["entry_daily_signal_date"])
            exit_date = date.fromisoformat(trade["exit_daily_signal_date"])
            result.append(
                {
                    "stock_code": stock_code,
                    "setup_id": trade["setup_id"],
                    "category": category,
                    "pnl_pct": pnl,
                    "entry_signal_date": entry_date,
                    "exit_signal_date": exit_date,
                    "entry_market_clock": dict(clock.get(entry_date, {})),
                    "exit_market_clock": dict(clock.get(exit_date, {})),
                }
            )
    return tuple(
        sorted(
            result,
            key=lambda row: (
                row["category"],
                row["stock_code"],
                row["entry_signal_date"],
            ),
        )
    )


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
                "nearest_support_ma": row.get("nearest_support_ma"),
                "nearest_resistance_ma": row.get("nearest_resistance_ma"),
            },
        ),
    )


def _select_clock_charts(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, dict[str, Any]], ...]:
    valid = [row for row in rows if row.get("range_speed") is not None]
    groups = {
        "FAST_HIGH_EFFICIENCY": [
            row
            for row in valid
            if row.get("range_speed_quartile") == "Q4"
            and row.get("efficiency_10_quartile") == "Q4"
        ],
        "FAST_LOW_EFFICIENCY": [
            row
            for row in valid
            if row.get("range_speed_quartile") == "Q4"
            and row.get("efficiency_10_quartile") == "Q1"
        ],
        "SLOW": [row for row in valid if row.get("range_speed_quartile") == "Q1"],
    }

    def representative_order(
        group: Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        ordered = sorted(
            group,
            key=lambda row: (
                row["pivot_trade_date"],
                row["pivot_kind"],
                row["stock_code"],
            ),
        )
        # Use one deterministic example per stock before filling remaining
        # slots, keeping representative charts spread across the universe.
        selected: list[Mapping[str, Any]] = []
        seen_stocks: set[str] = set()
        for row in ordered:
            stock_code = str(row["stock_code"])
            if stock_code in seen_stocks:
                continue
            selected.append(row)
            seen_stocks.add(stock_code)
            if len(selected) == 5:
                return selected
        selected.extend(row for row in ordered if row not in selected)
        return selected[:5]

    result: list[tuple[str, dict[str, Any]]] = []
    for category, group in groups.items():
        result.extend((category, dict(row)) for row in representative_order(group))
    return tuple(result)


def generate_charts(
    rows: Sequence[Mapping[str, Any]],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
    *,
    chart_root: Path = CHART_ROOT,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for category, row in _select_clock_charts(rows):
        bars = tuple(daily_by_stock[row["stock_code"]])
        event_date = row["pivot_trade_date"]
        date_to_index = {bar.trade_date: index for index, bar in enumerate(bars)}
        confirm_index = min(len(bars) - 1, date_to_index[event_date] + 2)
        prepared = prepare_review_chart(
            bars,
            chart_type=ChartType.EVENT_REVIEW,
            events=_chart_events(row),
            calendar=ExplicitTradingCalendar(bar.trade_date for bar in bars),
            focus_date=event_date,
            event_end_date=bars[confirm_index].trade_date,
            pre_sessions=60,
            post_sessions=20,
            show_sma5=True,
            shade_below_sma10_context=True,
        )
        filename = deterministic_chart_filename(
            row["stock_code"],
            ChartType.EVENT_REVIEW,
            event_date,
            slug=f"market-clock-{category.casefold()}",
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


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime, Decimal)):
        return value.isoformat() if not isinstance(value, Decimal) else str(value)
    return str(value)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ROLE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {field: _csv_value(row.get(field)) for field in ROLE_FIELDS}
            )


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime, Decimal)):
        return value.isoformat() if not isinstance(value, Decimal) else str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def run_market_clock_audit(
    *,
    output: Path = OUTPUT_PATH,
    summary_csv: Path = SUMMARY_CSV_PATH,
    chart_root: Path = CHART_ROOT,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    daily_by_stock: dict[str, tuple[DailyBar, ...]] = {}
    observation_rows: list[dict[str, Any]] = []
    pivot_rows: list[dict[str, Any]] = []
    clock_by_stock: dict[str, dict[date, Mapping[str, Any]]] = {}
    artifact_provenance: dict[str, Mapping[str, Any]] = {}
    pivot_inputs: list[
        tuple[
            tuple[DailyBar, ...], tuple[DailyIndicatorPoint, ...], list[dict[str, Any]]
        ]
    ] = []
    for stock_code in sorted(stocks):
        bars, provenance = _load_stock(stock_code)
        artifact_provenance[stock_code] = provenance
        canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
        daily_by_stock[stock_code] = canonical
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in canonical)
        points = tuple(calculate_daily_indicators(canonical, calendar))
        clock = list(_clock_series(canonical, points))
        clock_by_stock[stock_code] = {row["trade_date"]: row for row in clock}
        observation_rows.extend(
            row for row in clock if RESEARCH_START <= row["trade_date"] <= RESEARCH_END
        )
        pivot_inputs.append((canonical, points, clock))
    observation_rows = sorted(
        observation_rows, key=lambda row: (row["stock_code"], row["trade_date"])
    )
    # Quartile cut points describe the complete research population, not each
    # stock separately.  The rows are shared with ``clock_by_stock``, so event
    # and pivot metadata use these same global buckets.
    _add_quartiles(observation_rows)
    for canonical, points, clock in pivot_inputs:
        pivot_rows.extend(_pivot_rows(canonical, points, clock))
    pivot_rows = sorted(
        pivot_rows,
        key=lambda row: (row["stock_code"], row["pivot_trade_date"], row["pivot_kind"]),
    )
    charts = generate_charts(pivot_rows, daily_by_stock, chart_root=chart_root)
    overlay = _overlay_events(DAILY_PROOF_PATH, clock_by_stock, pivot_rows)
    large_trades = _large_trade_comparison(DAILY_PROOF_PATH, clock_by_stock)
    daily_distribution_fields = (
        "vol_ratio",
        "variance_speed",
        "range_speed",
        "abs_net_move_atr_10",
        "net_move_atr_10",
        "net_move_atr_20",
        "efficiency_10",
        "efficiency_20",
        "flow_speed",
        "ma10_effective_variance",
        "ma20_effective_variance",
        "ma60_effective_variance",
        "ma10_effective_range",
        "ma20_effective_range",
        "ma60_effective_range",
    )
    for row in observation_rows:
        variance = row.get("variance_speed")
        range_speed = row.get("range_speed")
        row.update(
            {
                f"ma{period}_effective_variance": Decimal(period) * variance
                if variance is not None
                else None
                for period in (10, 20, 60)
            }
        )
        row.update(
            {
                f"ma{period}_effective_range": Decimal(period) * range_speed
                if range_speed is not None
                else None
                for period in (10, 20, 60)
            }
        )
    valid_pivots = [
        row
        for row in pivot_rows
        if row.get("nearest_support_ma") or row.get("nearest_resistance_ma")
    ]
    observations_by_stock = {
        stock_code: sum(row["stock_code"] == stock_code for row in observation_rows)
        for stock_code in sorted(stocks)
    }
    session_counts = set(observations_by_stock.values())
    report = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "research_period": {"start": RESEARCH_START, "end": RESEARCH_END},
        "artifact_provenance": artifact_provenance,
        "population": {
            "stock_count": len(stocks),
            "stock_codes": sorted(stocks),
            "daily_observation_count": len(observation_rows),
            "observations_by_stock": observations_by_stock,
            "sessions_per_stock": next(iter(session_counts))
            if len(session_counts) == 1
            else None,
            "speed_valid_observation_count": sum(
                row.get("range_speed") is not None for row in observation_rows
            ),
            "speed_insufficient_data_count": sum(
                row.get("range_speed") is None for row in observation_rows
            ),
            "pivot_candidate_count": len(pivot_rows),
            "valid_pivot_role_count": len(valid_pivots),
            "pivot_low_count": sum(
                row["pivot_kind"] == PivotKind.LOW.value for row in pivot_rows
            ),
            "pivot_high_count": sum(
                row["pivot_kind"] == PivotKind.HIGH.value for row in pivot_rows
            ),
        },
        "methodology": {
            "reference": "past-only 252 available sessions; no backfill",
            "efficiency": "abs net move / sum absolute close moves; zero denominator => None",
            "pivot": "left2/right2; confirmed at P+2 15:30 Asia/Seoul",
            "nearest_ma_tie_break": "MA5, MA10, MA20, MA60 ascending period",
            "signal_price_basis": "ADJUSTED_DAILY_OHLC",
            "flow_basis": "RAW_CLOSE_X_RAW_VOLUME",
            "quartile_basis": "pooled research observations; linear percentiles; inclusive boundaries",
            "effective_horizon": "report-only period multiplied by speed; fixed MA periods unchanged",
            "strategy_changes": False,
        },
        "daily_distributions": {
            field: _distribution(observation_rows, field)
            for field in daily_distribution_fields
        },
        "effective_horizon_comparison": _effective_horizon_comparison(observation_rows),
        "range_quartile_role_matrix": {
            "support": _role_matrix(
                pivot_rows,
                bucket_field="range_speed_quartile",
                role_field="nearest_support_ma",
            ),
            "resistance": _role_matrix(
                pivot_rows,
                bucket_field="range_speed_quartile",
                role_field="nearest_resistance_ma",
            ),
        },
        "directional_speed_matrix": _directional_matrix(pivot_rows),
        "efficiency_interaction": _interaction_matrix(pivot_rows),
        "flow_interaction": _flow_interaction(pivot_rows),
        "ma_role_hypothesis": _role_hypothesis(pivot_rows),
        "pivot_low_support": {
            "count": sum(
                row["pivot_kind"] == PivotKind.LOW.value for row in pivot_rows
            ),
            "nearest_ma_counts": {
                ma: sum(row.get("nearest_support_ma") == ma for row in pivot_rows)
                for ma in _MA_NAMES
            },
            "distance_distribution": _distribution(
                [row for row in pivot_rows if row["pivot_kind"] == PivotKind.LOW.value],
                "nearest_support_distance_atr",
            ),
            "tie_count_distribution": _distribution(
                [row for row in pivot_rows if row["pivot_kind"] == PivotKind.LOW.value],
                "support_tie_count",
            ),
        },
        "pivot_high_resistance": {
            "count": sum(
                row["pivot_kind"] == PivotKind.HIGH.value for row in pivot_rows
            ),
            "nearest_ma_counts": {
                ma: sum(row.get("nearest_resistance_ma") == ma for row in pivot_rows)
                for ma in _MA_NAMES
            },
            "distance_distribution": _distribution(
                [
                    row
                    for row in pivot_rows
                    if row["pivot_kind"] == PivotKind.HIGH.value
                ],
                "nearest_resistance_distance_atr",
            ),
            "tie_count_distribution": _distribution(
                [
                    row
                    for row in pivot_rows
                    if row["pivot_kind"] == PivotKind.HIGH.value
                ],
                "resistance_tie_count",
            ),
        },
        "pivot_rows": pivot_rows,
        "strategy_event_overlay": overlay,
        "large_winner_loss_comparison": large_trades,
        "representative_charts": charts,
        "market_speed_source": MARKET_SPEED_PATH.as_posix(),
        "strategy_changes_applied": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_csv(summary_csv, pivot_rows)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--summary-csv", type=Path, default=SUMMARY_CSV_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    args = parser.parse_args()
    report = run_market_clock_audit(
        output=args.output,
        summary_csv=args.summary_csv,
        chart_root=args.chart_root,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "daily_observations": report["population"]["daily_observation_count"],
                "pivot_rows": report["population"]["pivot_candidate_count"],
                "charts": len(report["representative_charts"]),
                "network_calls": report["network_calls"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
