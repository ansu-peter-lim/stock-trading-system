"""Report-only MARKET_SPEED audit for the frozen DOWN_BOX V0.3-B cases.

This module adds volatility, traded-value, ATR and normalized moving-average
evidence to the existing 16 upper-exit observations.  It never changes the
strategy rules and never creates a re-entry order, fill, or PnL result.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, localcontext
from itertools import pairwise
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import (
    DailyIndicatorPoint,
    calculate_daily_indicators,
    moving_average_slope,
    simple_moving_average,
)
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.strategy_review.chart import (
    ChartType,
    prepare_review_chart,
    render_review_chart,
)

from .down_box_daily_execution_proof import _load_stock
from .upper_transition_audit import (
    BASELINE_PROOF_PATH,
    _chart_events,
    _existing_distribution,
    _future20_date,
    deterministic_chart_filename,
    select_chart_rows,
)

PROOF_VERSION = "DOWN_BOX_UPPER_TRANSITION_MARKET_SPEED_AUDIT"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/down_box_v0_3b_market_speed_audit.json"
)
SUMMARY_CSV_PATH = Path(
    "data/processed/strategy_review/down_box_v0_3b_market_speed_audit.csv"
)
CHART_ROOT = Path("data/processed/strategy_charts/down_box_v0_3b_market_speed_audit")
UPPER_TRANSITION_PATH = Path(
    "data/processed/strategy_review/down_box_v0_3b_upper_transition_audit.json"
)

_BASELINE_DATE_KEYS = frozenset(
    {
        "entry_signal_date",
        "entry_fill_date",
        "upper_exit_signal_date",
        "upper_exit_fill_date",
        "d1_date",
        "d2_date",
        "d3_date",
    }
)
_BASELINE_DECIMAL_KEYS = frozenset(
    {
        "box_floor",
        "box_upper",
        "entry_raw_price",
        "entry_signal_close",
        "exit_raw_price",
        "sma20_change_5",
        "sma20_prior_change_5",
        "sma20_change_10",
        "sma60_change_5",
        "sma60_flatness_5",
        "d3_runup_from_exit_signal",
        "d3_runup_from_exit_fill",
        "rise_d1",
        "rise_d2",
        "rise_d3",
        "max_daily_rise_d1_d3",
        "d3_distance_from_old_box_upper",
        "future_5_session_return",
        "future_10_session_return",
        "future_20_session_return",
        "maximum_favorable_excursion",
        "maximum_adverse_excursion",
    }
)

SUMMARY_FIELDS = (
    "stock_code",
    "setup_id",
    "entry_type",
    "entry_signal_date",
    "entry_fill_date",
    "upper_exit_signal_date",
    "upper_exit_fill_date",
    "d1_date",
    "d2_date",
    "d3_date",
    "rv20",
    "rv_ref",
    "vol_ratio",
    "variance_speed",
    "flow20",
    "flow_ref",
    "flow_speed",
    "speed_status",
    "ma5_effective",
    "ma10_effective",
    "ma20_effective",
    "ma60_effective",
    "atr20",
    "price_ma5_dist_atr",
    "price_ma10_dist_atr",
    "price_ma20_dist_atr",
    "price_ma60_dist_atr",
    "ma5_10_gap_atr",
    "ma10_20_gap_atr",
    "ma20_60_gap_atr",
    "sma10_change_5_pct",
    "sma20_change_5_pct",
    "sma60_change_5_pct",
    "sma10_change_5_normalized",
    "sma20_change_5_normalized",
    "sma60_change_5_normalized",
    "sma20_regime",
    "sma20_recent_up",
    "sma20_prior_up",
    "sma20_persistent_up",
    "holds_sma5_all_3",
    "sma60_flatness_5",
    "d3_runup_from_exit_fill",
    "future_5_session_return",
    "future_10_session_return",
    "future_20_session_return",
    "maximum_favorable_excursion",
    "maximum_adverse_excursion",
)

_DISTRIBUTION_FIELDS = (
    "vol_ratio",
    "variance_speed",
    "flow_speed",
    "ma5_effective",
    "ma10_effective",
    "ma20_effective",
    "ma60_effective",
    "sma20_change_5_normalized",
    "ma20_60_gap_atr",
    "d3_runup_from_exit_fill",
    "sma60_flatness_5",
)


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


def _rolling_std(
    values: Sequence[Decimal | None], index: int, window: int
) -> Decimal | None:
    if index < window - 1:
        return None
    sample = values[index - window + 1 : index + 1]
    if any(value is None for value in sample):
        return None
    typed = [value for value in sample if value is not None]
    mean = sum(typed, Decimal(0)) / Decimal(window)
    with localcontext() as context:
        context.prec = 40
        variance = sum((value - mean) ** 2 for value in typed) / Decimal(window)
        return variance.sqrt()


def _daily_returns(closes: Sequence[Decimal]) -> list[Decimal | None]:
    values: list[Decimal | None] = [None]
    for previous, current in pairwise(closes):
        values.append(current / previous - Decimal(1) if previous else None)
    return values


def _rolling_mean(values: Sequence[Decimal], index: int, window: int) -> Decimal | None:
    if index < window - 1:
        return None
    sample = values[index - window + 1 : index + 1]
    return sum(sample, Decimal(0)) / Decimal(window)


def _atr20(bars: Sequence[DailyBar], index: int) -> Decimal | None:
    if index < 20:
        return None
    true_ranges: list[Decimal] = []
    for current_index in range(index - 19, index + 1):
        current = bars[current_index].signal
        previous_close = bars[current_index - 1].signal.close
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous_close),
                abs(current.low - previous_close),
            )
        )
    return sum(true_ranges, Decimal(0)) / Decimal(20)


def _past_reference(
    values: Sequence[Decimal | None], index: int, minimum: int = 252
) -> Decimal | None:
    past = [value for value in values[:index] if value is not None]
    if len(past) < minimum:
        return None
    return _median(past[-minimum:])


def _coerce_baseline_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    for key in _BASELINE_DATE_KEYS:
        if isinstance(row.get(key), str):
            row[key] = date.fromisoformat(row[key])
    for key in _BASELINE_DECIMAL_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value:
            row[key] = Decimal(value)
    return row


def load_upper_transition_rows(
    path: Path = UPPER_TRANSITION_PATH,
) -> tuple[dict[str, Any], ...]:
    """Load the immutable V0.3-B rows without altering the source artifact."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(_coerce_baseline_row(row) for row in payload["rows"])


def _sma_structure(
    points: Sequence[DailyIndicatorPoint], index: int
) -> tuple[bool, bool, bool]:
    if index < 10:
        return False, False, False
    now = points[index].sma20
    recent = points[index - 5].sma20
    prior = points[index - 10].sma20
    if now is None or recent is None or prior is None:
        return False, False, False
    recent_up = now > recent
    prior_up = recent > prior
    return recent_up, prior_up, recent_up and prior_up


def _speed_row(
    baseline: Mapping[str, Any],
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
) -> dict[str, Any]:
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    canonical_points = tuple(sorted(points, key=lambda point: point.trade_date))
    date_to_index = {bar.trade_date: index for index, bar in enumerate(canonical)}
    d3_date = baseline.get("d3_date")
    if d3_date is None:
        d3_date = baseline.get("d1_date")
    if d3_date is None:
        raise ValueError("upper transition row has no D3 date")
    index = date_to_index[d3_date]
    closes = [bar.signal.close for bar in canonical]
    returns = _daily_returns(closes)
    rv20_values = [_rolling_std(returns, i, 20) for i in range(len(canonical))]
    traded_values = [bar.raw.close * Decimal(bar.raw.volume) for bar in canonical]
    flow20_values = [_rolling_mean(traded_values, i, 20) for i in range(len(canonical))]
    rv20 = rv20_values[index]
    rv_ref = _past_reference(rv20_values, index)
    flow20 = flow20_values[index]
    flow_ref = _past_reference(flow20_values, index)
    vol_ratio = rv20 / rv_ref if rv20 is not None and rv_ref else None
    variance_speed = vol_ratio**2 if vol_ratio is not None else None
    flow_speed = flow20 / flow_ref if flow20 is not None and flow_ref else None
    speed_status = (
        "OK"
        if vol_ratio is not None and flow_speed is not None
        else "INSUFFICIENT_DATA"
    )
    effective = (
        {
            f"ma{period}_effective": Decimal(period) * variance_speed
            for period in (5, 10, 20, 60)
        }
        if variance_speed is not None
        else {f"ma{period}_effective": None for period in (5, 10, 20, 60)}
    )
    sma5 = simple_moving_average(closes, 5)
    sma10 = [point.sma10 for point in canonical_points]
    sma20 = [point.sma20 for point in canonical_points]
    sma60 = [point.sma60 for point in canonical_points]
    sma10_slope = moving_average_slope(sma10, 5)[index]
    sma20_slope = moving_average_slope(sma20, 5)[index]
    sma60_slope = moving_average_slope(sma60, 5)[index]
    normalized = (
        {
            "sma10_change_5_normalized": sma10_slope / vol_ratio,
            "sma20_change_5_normalized": sma20_slope / vol_ratio,
            "sma60_change_5_normalized": sma60_slope / vol_ratio,
        }
        if vol_ratio
        and sma10_slope is not None
        and sma20_slope is not None
        and sma60_slope is not None
        else {
            "sma10_change_5_normalized": None,
            "sma20_change_5_normalized": None,
            "sma60_change_5_normalized": None,
        }
    )
    atr = _atr20(canonical, index)
    close = canonical[index].signal.close
    values = {
        "sma5": sma5[index],
        "sma10": sma10[index],
        "sma20": sma20[index],
        "sma60": sma60[index],
    }
    distances = (
        {
            "price_ma5_dist_atr": (close - values["sma5"]) / atr,
            "price_ma10_dist_atr": (close - values["sma10"]) / atr,
            "price_ma20_dist_atr": (close - values["sma20"]) / atr,
            "price_ma60_dist_atr": (close - values["sma60"]) / atr,
            "ma5_10_gap_atr": (values["sma5"] - values["sma10"]) / atr,
            "ma10_20_gap_atr": (values["sma10"] - values["sma20"]) / atr,
            "ma20_60_gap_atr": (values["sma20"] - values["sma60"]) / atr,
        }
        if atr is not None and all(value is not None for value in values.values())
        else {
            "price_ma5_dist_atr": None,
            "price_ma10_dist_atr": None,
            "price_ma20_dist_atr": None,
            "price_ma60_dist_atr": None,
            "ma5_10_gap_atr": None,
            "ma10_20_gap_atr": None,
            "ma20_60_gap_atr": None,
        }
    )
    recent_up, prior_up, persistent_up = _sma_structure(canonical_points, index)
    regime = (
        "PERSISTENT_UP"
        if persistent_up
        else "NEWLY_UP"
        if recent_up and not prior_up
        else "NOT_UP"
    )
    row = {
        **dict(baseline),
        "rv20": rv20,
        "rv_ref": rv_ref,
        "vol_ratio": vol_ratio,
        "variance_speed": variance_speed,
        "flow20": flow20,
        "flow_ref": flow_ref,
        "flow_speed": flow_speed,
        "speed_status": speed_status,
        **effective,
        "atr20": atr,
        **distances,
        "sma10_change_5_pct": sma10_slope,
        "sma20_change_5_pct": sma20_slope,
        "sma60_change_5_pct": sma60_slope,
        **normalized,
        "sma20_regime": regime,
        "sma20_recent_up": recent_up,
        "sma20_prior_up": prior_up,
        "sma20_persistent_up": persistent_up,
    }
    return row


def build_market_speed_rows(
    baseline_rows: Sequence[Mapping[str, Any]],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for baseline in baseline_rows:
        stock_code = baseline["stock_code"]
        bars = tuple(daily_by_stock[stock_code])
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        points = tuple(calculate_daily_indicators(bars, calendar))
        rows.append(_speed_row(baseline, bars, points))
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row["stock_code"],
                row["upper_exit_fill_date"],
                row["setup_id"],
            ),
        )
    )


def build_regime_outcomes(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for regime in ("PERSISTENT_UP", "NEWLY_UP", "NOT_UP"):
        group = [row for row in rows if row.get("sma20_regime") == regime]
        result: dict[str, Any] = {"count": len(group)}
        for horizon in (5, 10, 20):
            values = [
                row[f"future_{horizon}_session_return"]
                for row in group
                if row.get(f"future_{horizon}_session_return") is not None
            ]
            result[f"future_{horizon}"] = {
                "count": len(values),
                "mean": _mean(values),
                "median": _median(values),
            }
        for field in (
            "maximum_favorable_excursion",
            "maximum_adverse_excursion",
            "variance_speed",
            "flow_speed",
            "sma60_flatness_5",
        ):
            result[field] = _existing_distribution(group, field)
        output[regime] = result
    return output


def _rank_rows(
    rows: Sequence[Mapping[str, Any]], *, descending: bool
) -> list[dict[str, Any]]:
    available = [row for row in rows if row.get("future_20_session_return") is not None]
    ordered = sorted(
        available,
        key=lambda row: (
            (
                -row["future_20_session_return"]
                if descending
                else row["future_20_session_return"]
            ),
            row["stock_code"],
            row["upper_exit_fill_date"],
            row["setup_id"],
        ),
    )
    return [dict(row) for row in ordered[:5]]


def build_distributions(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    structural = [row for row in rows if row.get("structural_up_transition")]
    top = _rank_rows(rows, descending=True)
    bottom = _rank_rows(rows, descending=False)
    groups = {
        "all_upper_exits": rows,
        "structural_candidates": structural,
        "future20_top5": top,
        "future20_bottom5": bottom,
    }
    return {
        group_name: {
            field: _existing_distribution(group_rows, field)
            for field in _DISTRIBUTION_FIELDS
        }
        for group_name, group_rows in groups.items()
    }


def _chart_speed_rows(
    rows: Sequence[Mapping[str, Any]],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
    *,
    chart_root: Path,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for category, row in select_chart_rows(rows):
        bars = tuple(daily_by_stock[row["stock_code"]])
        focus = row["entry_signal_date"]
        event_end = _future20_date(row, bars)
        prepared = prepare_review_chart(
            bars,
            chart_type=ChartType.EVENT_REVIEW,
            events=_chart_events(row, bars),
            calendar=ExplicitTradingCalendar(bar.trade_date for bar in bars),
            focus_date=focus,
            event_end_date=event_end,
            pre_sessions=60,
            post_sessions=0,
            show_sma5=True,
            shade_below_sma10_context=True,
            horizontal_levels={
                "BOX_FLOOR": row["box_floor"],
                "BOX_UPPER": row["box_upper"],
                "UPPER_SELL_LEVEL": row["box_upper"] * Decimal("0.97"),
            },
        )
        filename = deterministic_chart_filename(
            row["stock_code"],
            ChartType.EVENT_REVIEW,
            focus,
            slug=f"market-speed-{category.casefold()}",
        )
        artifact = render_review_chart(
            prepared,
            chart_root / category / filename,
            strategy_policy=PROOF_VERSION,
            summary={
                **dict(row),
                "chart_category": category,
                "report_only": True,
                "up_reentry_order_created": False,
                "pnl_calculated": False,
            },
        )
        artifacts.append(
            {
                "stock_code": row["stock_code"],
                "setup_id": row["setup_id"],
                "category": category,
                "png_path": artifact.png_path.as_posix(),
                "metadata_path": artifact.metadata_path.as_posix(),
            }
        )
    return artifacts


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, Decimal)):
        return value.isoformat() if isinstance(value, date) else str(value)
    return str(value)


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {field: _csv_value(row.get(field)) for field in SUMMARY_FIELDS}
            )


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal)):
        return value.isoformat() if isinstance(value, date) else str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def run_market_speed_audit(
    *,
    output: Path = OUTPUT_PATH,
    summary_csv: Path = SUMMARY_CSV_PATH,
    chart_root: Path = CHART_ROOT,
    upper_transition_path: Path = UPPER_TRANSITION_PATH,
    stocks: Sequence[str] = (),
) -> dict[str, Any]:
    baseline_rows = load_upper_transition_rows(upper_transition_path)
    selected_stocks = tuple(stocks) or tuple(
        sorted({row["stock_code"] for row in baseline_rows})
    )
    daily_by_stock: dict[str, tuple[DailyBar, ...]] = {}
    for stock_code in selected_stocks:
        bars, _ = _load_stock(stock_code)
        daily_by_stock[stock_code] = bars
    rows = build_market_speed_rows(
        [row for row in baseline_rows if row["stock_code"] in daily_by_stock],
        daily_by_stock,
    )
    if len(rows) != 16:
        raise ValueError(f"expected 16 upper exits, got {len(rows)}")
    top5 = _rank_rows(rows, descending=True)
    bottom5 = _rank_rows(rows, descending=False)
    charts = _chart_speed_rows(rows, daily_by_stock, chart_root=chart_root)
    report = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "baseline": "FROZEN_V0_2_RULES_LONG_HISTORY_DAILY_PROOF",
        "baseline_proof_path": BASELINE_PROOF_PATH.as_posix(),
        "upper_transition_source": upper_transition_path.as_posix(),
        "methodology": {
            "signal_price_basis": "ADJUSTED_DAILY_OHLC_CLOSE",
            "flow_price_basis": "RAW_DAILY_CLOSE",
            "rv20": "population_std_of_trailing_20_close_to_close_returns",
            "reference": "median_of_previous_252_available_values_only",
            "insufficient_reference": "INSUFFICIENT_DATA; no backfill or inference",
            "atr20": "20-session_true_range_mean_on_adjusted_ohlc",
            "variance_speed": "(RV20 / RV_REF) ** 2",
            "flow_speed": "FLOW20 / FLOW_REF",
            "future_metrics": "report_only; no strategy filter or PnL",
        },
        "accounting_contract": {
            "completed_trade_pnl": "REALIZED_ONLY",
            "cumulative_return": "RESEARCH_END_MARK_TO_MARKET_RAW_CLOSE",
            "mdd": "DAILY_MARK_TO_MARKET_RAW_CLOSE",
            "market_speed_reentry": "NOT_IMPLEMENTED",
        },
        "upper_exit_count": len(rows),
        "rows": list(rows),
        "funnel": {
            "persistent_up": sum(
                row["sma20_regime"] == "PERSISTENT_UP" for row in rows
            ),
            "newly_up": sum(row["sma20_regime"] == "NEWLY_UP" for row in rows),
            "not_up": sum(row["sma20_regime"] == "NOT_UP" for row in rows),
            "speed_ok": sum(row["speed_status"] == "OK" for row in rows),
            "speed_insufficient_data": sum(
                row["speed_status"] == "INSUFFICIENT_DATA" for row in rows
            ),
        },
        "regime_outcomes": build_regime_outcomes(rows),
        "distributions": build_distributions(rows),
        "future20_top5": top5,
        "future20_bottom5": bottom5,
        "large_missed_trends": [
            row
            for row in rows
            if (row["stock_code"], row["upper_exit_signal_date"])
            in {
                ("035420", date(2025, 5, 29)),
                ("012450", date(2026, 1, 2)),
                ("000660", date(2025, 5, 7)),
            }
        ],
        "chart_policy": {
            "x_axis_date_policy": "TRADING_SESSION_INTERVAL",
            "x_axis_date_interval_sessions": 10,
            "x_axis_date_format": "DD",
            "price_axis": "SIGNAL_ADJUSTED_DAILY_OHLC",
            "raw_fill_price": "METADATA_ONLY",
        },
        "representative_charts": charts,
        "strategy_changes_applied": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_csv(summary_csv, rows)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--summary-csv", type=Path, default=SUMMARY_CSV_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    args = parser.parse_args()
    report = run_market_speed_audit(
        output=args.output,
        summary_csv=args.summary_csv,
        chart_root=args.chart_root,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "upper_exit_count": report["upper_exit_count"],
                "speed_ok": report["funnel"]["speed_ok"],
                "structural_regime": report["funnel"]["persistent_up"],
                "charts": len(report["representative_charts"]),
                "network_calls": report["network_calls"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
