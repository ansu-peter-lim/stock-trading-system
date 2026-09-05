"""Report-only pre-breakout market-clock acceleration audit (V0.3).

The audit consumes the full-Daily V0.2 BAND EXIT population.  Every candidate
feature is calculated strictly at event time ``T`` from ``T`` and earlier
sessions.  Forward aligned returns are retained only as outcome labels for
descriptive evaluation; they never enter a feature, a quartile, or a chart
annotation that could be used as a decision rule.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

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
from .market_clock_compression_audit_v0_2 import (
    OUTPUT_PATH as V02_OUTPUT_PATH,
)
from .market_clock_compression_audit_v0_2 import (
    RESEARCH_END,
    RESEARCH_START,
    STOCKS,
    _json_default,
    run_market_clock_compression_audit_v0_2,
)

PROOF_VERSION = "MARKET_CLOCK_PRE_BREAKOUT_ACCELERATION_AUDIT_V0_3"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_clock_pre_breakout_acceleration_audit_v0_3.json"
)
EVENT_CSV_PATH = Path(
    "data/processed/strategy_review/market_clock_pre_breakout_acceleration_events_v0_3.csv"
)
CHART_ROOT = Path(
    "data/processed/strategy_charts/market_clock_pre_breakout_acceleration_audit_v0_3_selected"
)

OUTCOME_GOOD = "GOOD_DIRECTIONAL"
OUTCOME_FAILED = "FAILED"
OUTCOME_UNAVAILABLE = "UNAVAILABLE_HORIZON"

ACCELERATION_METRICS = (
    "range_delta_1",
    "range_delta_3",
    "range_delta_5",
    "range_ratio_pre3",
    "eff_delta_1",
    "eff_delta_3",
    "eff_delta_5",
    "eff_ratio_pre3",
    "width_delta_1",
    "width_delta_3",
    "width_delta_5",
    "width_ratio_pre3",
    "flow_delta_1",
    "flow_delta_3",
    "flow_delta_5",
    "breakout_clearance_atr",
    "event_body_atr",
    "event_return_pct",
    "event_range_atr",
)

EVENT_FIELDS = (
    "stock_code",
    "event_date",
    "direction",
    "direction_label",
    "outcome_label",
    "aligned_return_3_pct",
    "aligned_return_5_pct",
    "aligned_return_10_pct",
    "compression_quartile",
    "range_speed_quartile",
    "efficiency_10_quartile",
    "compression_duration_sessions",
    "ma_ordering",
    "range_speed_t",
    "range_speed_t_minus_1",
    "range_speed_t_minus_3",
    "range_speed_t_minus_5",
    "range_delta_1",
    "range_delta_3",
    "range_delta_5",
    "range_ratio_pre3",
    "efficiency_10_t",
    "efficiency_10_t_minus_1",
    "efficiency_10_t_minus_3",
    "efficiency_10_t_minus_5",
    "eff_delta_1",
    "eff_delta_3",
    "eff_delta_5",
    "eff_ratio_pre3",
    "ma_cluster_width_atr_t",
    "ma_cluster_width_atr_t_minus_1",
    "ma_cluster_width_atr_t_minus_3",
    "ma_cluster_width_atr_t_minus_5",
    "width_delta_1",
    "width_delta_3",
    "width_delta_5",
    "width_ratio_pre3",
    "flow_speed_t",
    "flow_speed_t_minus_1",
    "flow_speed_t_minus_3",
    "flow_speed_t_minus_5",
    "flow_delta_1",
    "flow_delta_3",
    "flow_delta_5",
    "breakout_clearance_atr",
    "event_body_atr",
    "event_return_pct",
    "event_range_atr",
)


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, Decimal)):
        return value.isoformat() if isinstance(value, date) else str(value)
    return str(value)


def _median(values: Iterable[Decimal]) -> Decimal | None:
    items = list(values)
    return median(items) if items else None


def _percentile(values: Sequence[Decimal], quantile: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _distribution(values: Iterable[Decimal]) -> dict[str, Any]:
    items = sorted(values)
    return {
        "count": len(items),
        "mean": sum(items, Decimal(0)) / Decimal(len(items)) if items else None,
        "median": _median(items),
        "p25": _percentile(items, Decimal("0.25")),
        "p75": _percentile(items, Decimal("0.75")),
    }


def _difference(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    return current - previous if current is not None and previous is not None else None


def _ratio_pre3(
    current: Decimal | None, prior_values: Sequence[Decimal | None]
) -> Decimal | None:
    usable = [value for value in prior_values if value is not None]
    baseline = _median(usable)
    if current is None or baseline in (None, Decimal(0)) or len(usable) != 3:
        return None
    return current / baseline


def _metric_features(
    rows_by_index: Mapping[int, Mapping[str, Any]], index: int, field: str, prefix: str
) -> dict[str, Decimal | None]:
    current = rows_by_index.get(index, {}).get(field)
    prior_1 = rows_by_index.get(index - 1, {}).get(field)
    prior_3 = rows_by_index.get(index - 3, {}).get(field)
    prior_5 = rows_by_index.get(index - 5, {}).get(field)
    prior_window = [
        rows_by_index.get(candidate, {}).get(field)
        for candidate in (index - 3, index - 2, index - 1)
    ]
    return {
        f"{prefix}_t": current,
        f"{prefix}_t_minus_1": prior_1,
        f"{prefix}_t_minus_3": prior_3,
        f"{prefix}_t_minus_5": prior_5,
        f"{prefix}_delta_1": _difference(current, prior_1),
        f"{prefix}_delta_3": _difference(current, prior_3),
        f"{prefix}_delta_5": _difference(current, prior_5),
        f"{prefix}_ratio_pre3": _ratio_pre3(current, prior_window),
    }


def _flow_features(
    rows_by_index: Mapping[int, Mapping[str, Any]], index: int
) -> dict[str, Decimal | None]:
    current = rows_by_index.get(index, {}).get("flow_speed")
    prior_1 = rows_by_index.get(index - 1, {}).get("flow_speed")
    prior_3 = rows_by_index.get(index - 3, {}).get("flow_speed")
    prior_5 = rows_by_index.get(index - 5, {}).get("flow_speed")
    return {
        "flow_speed_t": current,
        "flow_speed_t_minus_1": prior_1,
        "flow_speed_t_minus_3": prior_3,
        "flow_speed_t_minus_5": prior_5,
        "flow_delta_1": _difference(current, prior_1),
        "flow_delta_3": _difference(current, prior_3),
        "flow_delta_5": _difference(current, prior_5),
    }


def _ma_ordering(row: Mapping[str, Any]) -> str | None:
    values = [
        ("MA5", row.get("sma5"), 5),
        ("MA10", row.get("sma10"), 10),
        ("MA20", row.get("sma20"), 20),
    ]
    if any(value is None for _, value, _ in values):
        return None
    ordered = sorted(values, key=lambda item: (-item[1], item[2]))
    return ">".join(name for name, _, _ in ordered)


def _outcome_label(event: Mapping[str, Any]) -> str:
    value = event.get("aligned_return_10_pct")
    if value is None:
        return OUTCOME_UNAVAILABLE
    return OUTCOME_GOOD if value > 0 else OUTCOME_FAILED


def _event_bar_metrics(
    *,
    bar: DailyBar,
    previous_bar: DailyBar | None,
    row: Mapping[str, Any],
    direction: int,
) -> dict[str, Decimal | None]:
    atr = row.get("atr20")
    high = row.get("ma_band_high")
    low = row.get("ma_band_low")
    if atr in (None, Decimal(0)):
        clearance = body = event_range = None
    else:
        boundary = high if direction > 0 else low
        clearance = (
            (bar.signal.close - boundary) / atr
            if boundary is not None and direction > 0
            else (boundary - bar.signal.close) / atr
            if boundary is not None
            else None
        )
        body = Decimal(direction) * (bar.signal.close - bar.signal.open) / atr
        event_range = (bar.signal.high - bar.signal.low) / atr
    return {
        "breakout_clearance_atr": clearance,
        "event_body_atr": body,
        "event_return_pct": (
            Decimal(direction)
            * (bar.signal.close / previous_bar.signal.close - Decimal(1))
            * Decimal(100)
            if previous_bar is not None
            else None
        ),
        "event_range_atr": event_range,
    }


def _daily_bars(stocks: Sequence[str]) -> dict[str, tuple[DailyBar, ...]]:
    result: dict[str, tuple[DailyBar, ...]] = {}
    for stock_code in sorted(stocks):
        bars, _ = _load_stock(stock_code)
        result[stock_code] = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    return result


def _decorate_events(
    v02_report: Mapping[str, Any],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in v02_report["rows"]]
    efficiency_values = sorted(
        row["efficiency_10"] for row in rows if row.get("efficiency_10") is not None
    )
    efficiency_thresholds = tuple(
        _percentile(efficiency_values, Decimal(value))
        for value in ("0.25", "0.50", "0.75")
    )
    for row in rows:
        value = row.get("efficiency_10")
        if value is None or any(item is None for item in efficiency_thresholds):
            row["efficiency_10_quartile"] = None
        elif value <= efficiency_thresholds[0]:
            row["efficiency_10_quartile"] = "Q1"
        elif value <= efficiency_thresholds[1]:
            row["efficiency_10_quartile"] = "Q2"
        elif value <= efficiency_thresholds[2]:
            row["efficiency_10_quartile"] = "Q3"
        else:
            row["efficiency_10_quartile"] = "Q4"

    rows_by_stock: dict[str, dict[int, Mapping[str, Any]]] = {}
    rows_by_key: dict[tuple[str, date], Mapping[str, Any]] = {}
    for row in rows:
        rows_by_stock.setdefault(row["stock_code"], {})[row["_index"]] = row
        rows_by_key[(row["stock_code"], row["trade_date"])] = row

    records: list[dict[str, Any]] = []
    for event in v02_report["events"]:
        if event["event_type"] != "BAND_EXIT":
            continue
        stock_code = event["stock_code"]
        event_date = event["event_date"]
        row = rows_by_key[(stock_code, event_date)]
        index = row["_index"]
        bars = daily_by_stock[stock_code]
        bar = bars[index]
        previous_bar = bars[index - 1] if index else None
        direction = event["direction"]
        record: dict[str, Any] = {
            "stock_code": stock_code,
            "event_date": event_date,
            "direction": direction,
            "direction_label": "UP" if direction > 0 else "DOWN",
            "outcome_label": _outcome_label(event),
            "aligned_return_3_pct": event.get("aligned_return_3_pct"),
            "aligned_return_5_pct": event.get("aligned_return_5_pct"),
            "aligned_return_10_pct": event.get("aligned_return_10_pct"),
            "compression_quartile": row.get("compression_quartile"),
            "range_speed_quartile": row.get("range_speed_quartile"),
            "efficiency_10_quartile": row.get("efficiency_10_quartile"),
            "compression_duration_sessions": row.get("compression_duration_sessions"),
            "ma_ordering": _ma_ordering(row),
            "ma_cluster_high": row.get("ma_band_high"),
            "ma_cluster_low": row.get("ma_band_low"),
        }
        history = rows_by_stock[stock_code]
        record.update(_metric_features(history, index, "range_speed", "range_speed"))
        record.update(
            _metric_features(history, index, "efficiency_10", "efficiency_10")
        )
        record.update(
            _metric_features(
                history, index, "ma_cluster_width_atr", "ma_cluster_width_atr"
            )
        )
        record.update(_flow_features(history, index))
        record.update(
            _event_bar_metrics(
                bar=bar,
                previous_bar=previous_bar,
                row=row,
                direction=direction,
            )
        )
        # Concise aliases make the report contract readable without changing
        # the source-specific V0.2 row names.
        record["range_delta_1"] = record.pop("range_speed_delta_1")
        record["range_delta_3"] = record.pop("range_speed_delta_3")
        record["range_delta_5"] = record.pop("range_speed_delta_5")
        record["range_ratio_pre3"] = record.pop("range_speed_ratio_pre3")
        record["eff_delta_1"] = record.pop("efficiency_10_delta_1")
        record["eff_delta_3"] = record.pop("efficiency_10_delta_3")
        record["eff_delta_5"] = record.pop("efficiency_10_delta_5")
        record["eff_ratio_pre3"] = record.pop("efficiency_10_ratio_pre3")
        record["width_delta_1"] = record.pop("ma_cluster_width_atr_delta_1")
        record["width_delta_3"] = record.pop("ma_cluster_width_atr_delta_3")
        record["width_delta_5"] = record.pop("ma_cluster_width_atr_delta_5")
        record["width_ratio_pre3"] = record.pop("ma_cluster_width_atr_ratio_pre3")
        records.append(record)
    records.sort(key=lambda item: (item["stock_code"], item["event_date"]))
    keys = [(item["stock_code"], item["event_date"]) for item in records]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate BAND EXIT event key")
    return records


def _metric_summary(rows: Sequence[Mapping[str, Any]], metric: str) -> dict[str, Any]:
    return _distribution(row[metric] for row in rows if row.get(metric) is not None)


def _outcome_comparison(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label in (OUTCOME_GOOD, OUTCOME_FAILED, OUTCOME_UNAVAILABLE):
        selected = [row for row in rows if row["outcome_label"] == label]
        result[label] = {
            "count": len(selected),
            "metrics": {
                metric: _metric_summary(selected, metric)
                for metric in ACCELERATION_METRICS
            },
        }
    return result


def _quartile(value: Decimal, thresholds: tuple[Decimal, Decimal, Decimal]) -> str:
    if value <= thresholds[0]:
        return "Q1"
    if value <= thresholds[1]:
        return "Q2"
    if value <= thresholds[2]:
        return "Q3"
    return "Q4"


def _increasing(values: Sequence[Decimal | None]) -> bool | None:
    usable = [value for value in values if value is not None]
    if len(usable) != len(values):
        return None
    return all(left <= right for left, right in itertools.pairwise(usable))


def _quartile_evidence(
    rows: Sequence[Mapping[str, Any]], metric: str
) -> dict[str, Any]:
    values = sorted(row[metric] for row in rows if row.get(metric) is not None)
    if not values:
        return {"available": False, "metric": metric, "quartiles": {}}
    thresholds = tuple(
        _percentile(values, Decimal(value)) for value in ("0.25", "0.50", "0.75")
    )
    if any(value is None for value in thresholds):
        return {"available": False, "metric": metric, "quartiles": {}}
    groups = {
        label: [
            row
            for row in rows
            if row.get(metric) is not None
            and _quartile(row[metric], thresholds) == label
        ]
        for label in ("Q1", "Q2", "Q3", "Q4")
    }
    quartiles: dict[str, Any] = {}
    good_rates: list[Decimal | None] = []
    r10_medians: list[Decimal | None] = []
    for label, group in groups.items():
        evaluated = [
            row for row in group if row["outcome_label"] != OUTCOME_UNAVAILABLE
        ]
        good = sum(row["outcome_label"] == OUTCOME_GOOD for row in evaluated)
        good_rate = Decimal(good) / Decimal(len(evaluated)) if evaluated else None
        r10 = [
            row["aligned_return_10_pct"]
            for row in evaluated
            if row.get("aligned_return_10_pct") is not None
        ]
        r10_median = _median(r10)
        quartiles[label] = {
            "count": len(group),
            "evaluated_count": len(evaluated),
            "good_directional_count": good,
            "good_directional_rate": good_rate,
            "median_aligned_return_10_pct": r10_median,
            "feature_distribution": _metric_summary(group, metric),
        }
        good_rates.append(good_rate)
        r10_medians.append(r10_median)
    return {
        "available": True,
        "metric": metric,
        "thresholds": {
            "q25": thresholds[0],
            "q50": thresholds[1],
            "q75": thresholds[2],
        },
        "quartiles": quartiles,
        "good_rate_increasing_q1_to_q4": _increasing(good_rates),
        "median_r10_increasing_q1_to_q4": _increasing(r10_medians),
    }


def _subset(rows: Sequence[Mapping[str, Any]], label: str) -> list[Mapping[str, Any]]:
    return [row for row in rows if row.get("compression_quartile") == label]


def _median_metric(
    comparison: Mapping[str, Any], outcome: str, metric: str
) -> Decimal | None:
    return comparison[outcome]["metrics"][metric]["median"]


def _higher_good_count(
    comparison: Mapping[str, Any], metrics: Sequence[str]
) -> tuple[int, int]:
    available = 0
    higher = 0
    for metric in metrics:
        good = _median_metric(comparison, OUTCOME_GOOD, metric)
        failed = _median_metric(comparison, OUTCOME_FAILED, metric)
        if good is not None and failed is not None:
            available += 1
            higher += good > failed
    return higher, available


def _hypotheses(
    all_comparison: Mapping[str, Any], c1_comparison: Mapping[str, Any]
) -> tuple[dict[str, str], dict[str, Any]]:
    def label(higher: int, available: int) -> str:
        if available == 0:
            return "INCONCLUSIVE"
        if higher == available:
            return "SUPPORTED"
        if higher:
            return "PARTIALLY_SUPPORTED"
        return "NOT_SUPPORTED"

    range_score = _higher_good_count(
        all_comparison, ("range_delta_1", "range_delta_3", "range_delta_5")
    )
    efficiency_score = _higher_good_count(
        all_comparison, ("eff_delta_1", "eff_delta_3", "eff_delta_5")
    )
    width_score = _higher_good_count(
        all_comparison, ("width_delta_1", "width_delta_3", "width_delta_5")
    )
    c1_score = _higher_good_count(
        c1_comparison, ("range_delta_3", "eff_delta_3", "width_delta_3")
    )
    flow_score = _higher_good_count(
        all_comparison, ("flow_delta_1", "flow_delta_3", "flow_delta_5")
    )
    h4 = (
        "SUPPORTED"
        if flow_score[1] and flow_score[0] < flow_score[1]
        else "NOT_SUPPORTED"
        if flow_score[1]
        else "INCONCLUSIVE"
    )
    return (
        {
            "H1_RANGE_ACCELERATION": label(*range_score),
            "H2_EFFICIENCY_ACCELERATION": label(*efficiency_score),
            "H3_CLUSTER_EXPANSION": label(*width_score),
            "H4_FLOW_NOT_REQUIRED": h4,
            "H5_C1_ACCELERATION_SEPARATION": label(*c1_score),
        },
        {
            "range_higher_good_median_count": range_score[0],
            "range_available_metric_count": range_score[1],
            "efficiency_higher_good_median_count": efficiency_score[0],
            "efficiency_available_metric_count": efficiency_score[1],
            "width_higher_good_median_count": width_score[0],
            "width_available_metric_count": width_score[1],
            "flow_higher_good_median_count": flow_score[0],
            "flow_available_metric_count": flow_score[1],
            "c1_higher_good_median_count": c1_score[0],
            "c1_available_metric_count": c1_score[1],
        },
    )


def _chart_groups(
    records: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, list[Mapping[str, Any]]], ...]:
    complete = [row for row in records if _chart_metadata_complete(row)]
    return (
        (
            "C1_GOOD",
            [
                row
                for row in complete
                if row["compression_quartile"] == "C1"
                and row["outcome_label"] == OUTCOME_GOOD
            ],
        ),
        (
            "C1_FAILED",
            [
                row
                for row in complete
                if row["compression_quartile"] == "C1"
                and row["outcome_label"] == OUTCOME_FAILED
            ],
        ),
        (
            "C4_GOOD",
            [
                row
                for row in complete
                if row["compression_quartile"] == "C4"
                and row["outcome_label"] == OUTCOME_GOOD
            ],
        ),
    )


def _chart_metadata_complete(row: Mapping[str, Any]) -> bool:
    return all(
        row.get(field) is not None
        for field in (
            "ma_cluster_width_atr_t",
            "range_speed_t",
            "range_delta_3",
            "efficiency_10_t",
            "eff_delta_3",
            "width_delta_3",
            "flow_delta_3",
            "breakout_clearance_atr",
        )
    )


def _select_chart_records(
    records: Sequence[Mapping[str, Any]],
    *,
    count_per_group: int = 5,
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    selected: list[tuple[str, Mapping[str, Any]]] = []
    for category, group in _chart_groups(records):
        ordered = sorted(group, key=lambda row: (row["event_date"], row["stock_code"]))
        selected.extend((category, row) for row in ordered[:count_per_group])
    return tuple(selected)


def _chart_event(row: Mapping[str, Any], bar: DailyBar) -> ReviewEvent:
    return ReviewEvent(
        ReviewEventType.BOX_BREAKOUT,
        row["event_date"],
        f"BAND EXIT {row['direction_label']}",
        adjusted_plot_price=bar.signal.close,
        details={
            "report_only": True,
            "outcome_label_evaluation_only": row["outcome_label"],
            "breakout_clearance_atr": row.get("breakout_clearance_atr"),
        },
    )


def generate_charts(
    records: Sequence[Mapping[str, Any]],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
    *,
    chart_root: Path = CHART_ROOT,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for category, row in _select_chart_records(records):
        bars = tuple(daily_by_stock[row["stock_code"]])
        date_to_index = {bar.trade_date: index for index, bar in enumerate(bars)}
        bar = bars[date_to_index[row["event_date"]]]
        levels = {
            label: value
            for label, value in (
                ("MA_CLUSTER_HIGH_T", row.get("ma_cluster_high")),
                ("MA_CLUSTER_LOW_T", row.get("ma_cluster_low")),
            )
            if value is not None and value > 0
        }
        prepared = prepare_review_chart(
            bars,
            chart_type=ChartType.EVENT_REVIEW,
            events=(_chart_event(row, bar),),
            calendar=ExplicitTradingCalendar(bar.trade_date for bar in bars),
            focus_date=row["event_date"],
            event_end_date=row["event_date"],
            pre_sessions=60,
            post_sessions=20,
            show_sma5=True,
            horizontal_levels=levels,
        )
        filename = deterministic_chart_filename(
            row["stock_code"],
            ChartType.EVENT_REVIEW,
            row["event_date"],
            slug=f"market-clock-v03-{category.casefold()}",
        )
        summary = {
            "report_only": True,
            "strategy_changes_applied": False,
            "chart_category": category,
            "outcome_label_evaluation_only": row["outcome_label"],
            "cluster_width_atr": row.get("ma_cluster_width_atr_t"),
            "range_speed": row.get("range_speed_t"),
            "range_delta_3": row.get("range_delta_3"),
            "efficiency_10": row.get("efficiency_10_t"),
            "eff_delta_3": row.get("eff_delta_3"),
            "width_delta_3": row.get("width_delta_3"),
            "flow_delta_3": row.get("flow_delta_3"),
            "breakout_clearance_atr": row.get("breakout_clearance_atr"),
        }
        artifact = render_review_chart(
            prepared,
            chart_root / category / filename,
            strategy_policy=PROOF_VERSION,
            summary=summary,
        )
        artifacts.append(
            {
                "stock_code": row["stock_code"],
                "event_date": row["event_date"],
                "category": category,
                "outcome_label_evaluation_only": row["outcome_label"],
                "png_path": artifact.png_path.as_posix(),
                "metadata_path": artifact.metadata_path.as_posix(),
            }
        )
    return artifacts


def _write_event_csv(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=EVENT_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {field: _csv_value(record.get(field)) for field in EVENT_FIELDS}
            )


def run_market_clock_pre_breakout_acceleration_audit(
    *,
    output: Path = OUTPUT_PATH,
    event_csv: Path = EVENT_CSV_PATH,
    chart_root: Path = CHART_ROOT,
    v02_output: Path = V02_OUTPUT_PATH,
    stocks: Sequence[str] = STOCKS,
    generate_review_charts: bool = True,
) -> dict[str, Any]:
    """Build V0.3 from cached Daily data only; no network client is used."""

    v02_report = run_market_clock_compression_audit_v0_2(
        output=v02_output,
        stocks=stocks,
    )
    daily_by_stock = _daily_bars(stocks)
    records = _decorate_events(v02_report, daily_by_stock)
    all_comparison = _outcome_comparison(records)
    c1_records = _subset(records, "C1")
    c4_records = _subset(records, "C4")
    c1_comparison = _outcome_comparison(c1_records)
    c4_comparison = _outcome_comparison(c4_records)
    quartile_evidence = {
        metric: _quartile_evidence(records, metric) for metric in ACCELERATION_METRICS
    }
    hypotheses, hypothesis_evidence = _hypotheses(all_comparison, c1_comparison)
    charts = (
        generate_charts(records, daily_by_stock, chart_root=chart_root)
        if generate_review_charts
        else []
    )
    report: dict[str, Any] = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "research_period": {"start": RESEARCH_START, "end": RESEARCH_END},
        "source_v02": v02_output.as_posix(),
        "population": {
            "stock_count": len(stocks),
            "band_exit_count": len(records),
            "c1_band_exit_count": len(c1_records),
            "c4_band_exit_count": len(c4_records),
            "outcome_counts": {
                label: sum(row["outcome_label"] == label for row in records)
                for label in (OUTCOME_GOOD, OUTCOME_FAILED, OUTCOME_UNAVAILABLE)
            },
        },
        "methodology": {
            "population": "all V0.2 full-Daily BAND EXIT events; UP and DOWN",
            "feature_time_boundary": "event T and prior sessions only",
            "pre3_baseline": "median of T-3, T-2, T-1; all three required",
            "signal_price_basis": "adjusted Daily OHLC / signal price series",
            "outcome_policy": "GOOD_DIRECTIONAL iff aligned_return_10_pct > 0; evaluation only",
            "future_feature_policy": "T+1/T+3/T+5 market-clock values are excluded from all features and quartile evidence",
            "report_only": True,
            "strategy_signals": False,
            "orders": False,
            "fills": False,
            "pnl": False,
            "thresholds": False,
        },
        "records": records,
        "pre_t_feature_distributions": {
            metric: _metric_summary(records, metric) for metric in ACCELERATION_METRICS
        },
        "good_vs_failed": all_comparison,
        "c1_compression": {
            "count": len(c1_records),
            "good_vs_failed": c1_comparison,
        },
        "c4_reference": {
            "count": len(c4_records),
            "good_vs_failed": c4_comparison,
        },
        "quartile_evidence": quartile_evidence,
        "hypotheses": hypotheses,
        "hypothesis_evidence": hypothesis_evidence,
        "market_time_interpretation": {
            "COMPRESSION": "descriptive C1 context; not a strategy state",
            "EXIT_ATTEMPT": "raw full-Daily BAND EXIT event at T",
            "ACCELERATING_EXIT": "interpretive concept measured by T-time deltas only; V0.3 assigns no threshold or state transition",
            "FAILED_EXIT": "evaluation-only aligned_return_10_pct <= 0 label",
        },
        "charts": charts,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_event_csv(event_csv, records)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--event-csv", type=Path, default=EVENT_CSV_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    parser.add_argument("--no-charts", action="store_true")
    args = parser.parse_args()
    report = run_market_clock_pre_breakout_acceleration_audit(
        output=args.output,
        event_csv=args.event_csv,
        chart_root=args.chart_root,
        generate_review_charts=not args.no_charts,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "band_exits": report["population"]["band_exit_count"],
                "outcomes": report["population"]["outcome_counts"],
                "hypotheses": report["hypotheses"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
