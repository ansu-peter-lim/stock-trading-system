"""Offline V0.3-B audit for upper exits followed by a possible UP transition.

The module consumes the cached Daily RAW/ADJUSTED artifacts used by the frozen
V0.2 proof.  It is intentionally an audit only: no UP re-entry intent, order,
fill, PnL, or strategy parameter is created.  All price observations used for
the transition evidence come from the adjusted signal series; the raw exit
open is retained only for the report-only run-up calculation.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import (
    DailyIndicatorPoint,
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

from .down_box_daily_execution_proof import (
    RESEARCH_END,
    RESEARCH_START,
    DailyProofAction,
    _load_stock,
    audit_upper_transition,
    run_down_box_daily_execution_proof,
)
from .down_box_daily_execution_proof import (
    _distribution as _existing_distribution,
)

PROOF_VERSION = "DOWN_BOX_REVERSAL_V0_3_B_UPPER_EXIT_UP_TRANSITION_AUDIT"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/down_box_v0_3b_upper_transition_audit.json"
)
SUMMARY_CSV_PATH = Path(
    "data/processed/strategy_review/down_box_v0_3b_upper_transition_audit.csv"
)
CHART_ROOT = Path(
    "data/processed/strategy_charts/down_box_v0_3b_upper_transition_audit"
)
BASELINE_PROOF_PATH = Path(
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
    "sma5_d1_pass",
    "sma5_d2_pass",
    "sma5_d3_pass",
    "holds_sma5_all_3",
    "sma20_recent_up",
    "sma20_prior_up",
    "sma20_persistent_up",
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
    "structural_up_transition",
    "future_5_session_return",
    "future_10_session_return",
    "future_20_session_return",
    "maximum_favorable_excursion",
    "maximum_adverse_excursion",
)

_DISTRIBUTION_FIELDS = (
    "sma60_flatness_5",
    "d3_runup_from_exit_fill",
    "max_daily_rise_d1_d3",
    "d3_distance_from_old_box_upper",
)


def _date_index(bars: Sequence[DailyBar]) -> dict[date, int]:
    return {bar.trade_date: index for index, bar in enumerate(bars)}


def _sma20_structure(
    points: Sequence[DailyIndicatorPoint], d3_index: int | None
) -> tuple[bool, bool, bool, Decimal | None, Decimal | None]:
    """Return recent/prior/persistent-up and both percentage changes.

    The prior comparison is deliberately D3-5 versus D3-10.  A missing
    lookback never gets inferred as an up-trend.
    """

    if d3_index is None or d3_index < 10 or d3_index >= len(points):
        return False, False, False, None, None
    now = points[d3_index].sma20
    recent = points[d3_index - 5].sma20
    prior = points[d3_index - 10].sma20
    if now is None or recent is None or prior is None:
        return False, False, False, None, None
    recent_change = now / recent - Decimal(1) if recent else None
    prior_change = recent / prior - Decimal(1) if prior else None
    recent_up = now > recent
    prior_up = recent > prior
    return (
        recent_up,
        prior_up,
        recent_up and prior_up,
        recent_change,
        prior_change,
    )


def build_upper_transition_row(
    trade: Mapping[str, Any],
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
) -> dict[str, Any]:
    """Build one deterministic audit row from one V0.2 upper-exit trade."""

    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    canonical_points = tuple(sorted(points, key=lambda point: point.trade_date))
    date_to_index = _date_index(canonical)
    exit_fill_date = trade["exit_fill_date"]
    d3_indexes = [date_to_index[exit_fill_date] + offset for offset in range(3)]
    d3_index = d3_indexes[-1] if d3_indexes[-1] < len(canonical) else None
    recent_up, prior_up, persistent_up, recent_change, prior_change = _sma20_structure(
        canonical_points, d3_index
    )
    # audit_upper_transition contains the shared, already-tested run-up and
    # excursion definitions.  It never creates an order or modifies a trade.
    base = audit_upper_transition(
        {**trade, "_exit_fill_raw_open": trade["exit_raw_price"]},
        canonical,
        canonical_points,
    )
    d1_d3_dates = base["d1_d3_dates"]
    row = {
        **base,
        "entry_signal_date": trade["entry_daily_signal_date"],
        "entry_fill_date": trade["entry_fill_date"],
        "entry_raw_price": trade["entry_raw_price"],
        "entry_signal_close": trade["entry_daily_signal_close"],
        "exit_raw_price": trade["exit_raw_price"],
        "upper_exit_signal_date": trade["exit_daily_signal_date"],
        "upper_exit_fill_date": exit_fill_date,
        "d1_date": d1_d3_dates[0] if len(d1_d3_dates) > 0 else None,
        "d2_date": d1_d3_dates[1] if len(d1_d3_dates) > 1 else None,
        "d3_date": d1_d3_dates[2] if len(d1_d3_dates) > 2 else None,
        "sma5_d1_pass": base["d1_close_above_sma5"],
        "sma5_d2_pass": base["d2_close_above_sma5"],
        "sma5_d3_pass": base["d3_close_above_sma5"],
        "sma20_recent_up": recent_up,
        "sma20_prior_up": prior_up,
        "sma20_persistent_up": persistent_up,
        "sma20_change_5": recent_change,
        "sma20_prior_change_5": prior_change,
    }
    return {field: row.get(field) for field in SUMMARY_FIELDS} | {
        key: value for key, value in row.items() if key not in SUMMARY_FIELDS
    }


def build_funnel(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Count each structural gate without turning any gate into a filter."""

    return {
        "upper_exit_total": len(rows),
        "sma5_d1": sum(bool(row.get("sma5_d1_pass")) for row in rows),
        "sma5_d1_d2": sum(
            bool(row.get("sma5_d1_pass")) and bool(row.get("sma5_d2_pass"))
            for row in rows
        ),
        "sma5_all3": sum(bool(row.get("holds_sma5_all_3")) for row in rows),
        "sma20_recent_up": sum(bool(row.get("sma20_recent_up")) for row in rows),
        "sma20_prior_up": sum(bool(row.get("sma20_prior_up")) for row in rows),
        "sma20_persistent_up": sum(
            bool(row.get("sma20_persistent_up")) for row in rows
        ),
        "sma5_all3_and_sma20_persistent": sum(
            bool(row.get("holds_sma5_all_3")) and bool(row.get("sma20_persistent_up"))
            for row in rows
        ),
    }


def _rank_by_future20(
    rows: Sequence[Mapping[str, Any]], *, reverse: bool
) -> list[dict[str, Any]]:
    available = [row for row in rows if row.get("future_20_session_return") is not None]
    ordered = sorted(
        available,
        key=lambda row: (
            (
                -row["future_20_session_return"]
                if reverse
                else row["future_20_session_return"]
            ),
            row["stock_code"],
            row["upper_exit_fill_date"],
            row["setup_id"],
        ),
    )
    return [dict(row) for row in ordered[:5]]


def select_chart_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Select TOP/BOTTOM five, adding the required 012450 anchor once."""

    selected: dict[tuple[str, str, str], tuple[str, dict[str, Any]]] = {}
    for category, category_rows in (
        ("TOP5_FUTURE20", _rank_by_future20(rows, reverse=True)),
        ("BOTTOM5_FUTURE20", _rank_by_future20(rows, reverse=False)),
    ):
        for row in category_rows:
            selected[(category, row["stock_code"], row["setup_id"])] = (category, row)
    anchor = next((row for row in rows if row.get("stock_code") == "012450"), None)
    if anchor is not None:
        entity = (anchor["stock_code"], anchor["setup_id"])
        if not any(key[1:] == entity for key in selected):
            key = ("ANCHOR_012450", entity[0], entity[1])
            selected[key] = ("ANCHOR_012450", dict(anchor))
    return tuple(
        sorted(
            selected.values(),
            key=lambda item: (
                item[0],
                item[1]["stock_code"],
                item[1]["upper_exit_fill_date"],
                item[1]["setup_id"],
            ),
        )
    )


def _bar_for_date(bars: Sequence[DailyBar], value: date) -> DailyBar:
    for bar in bars:
        if bar.trade_date == value:
            return bar
    raise ValueError(f"date is not in Daily bars: {value}")


def _chart_events(
    row: Mapping[str, Any], bars: Sequence[DailyBar]
) -> tuple[ReviewEvent, ...]:
    events: list[ReviewEvent] = []
    for event_type, field, label, fill_field in (
        (
            ReviewEventType.BOX_BUY_CANDIDATE,
            "entry_signal_date",
            "ENTRY",
            None,
        ),
        (
            ReviewEventType.ENTRY_FILL,
            "entry_fill_date",
            "ENTRY FILL",
            "entry_raw_price",
        ),
        (
            ReviewEventType.UPPER_TAKE_PROFIT,
            "upper_exit_signal_date",
            "UPPER EXIT",
            None,
        ),
        (
            ReviewEventType.EXIT_FILL,
            "upper_exit_fill_date",
            "EXIT FILL",
            "exit_raw_price",
        ),
    ):
        event_date = row.get(field)
        if event_date is None:
            continue
        bar = _bar_for_date(bars, event_date)
        if fill_field is None:
            events.append(
                ReviewEvent(
                    event_type,
                    event_date,
                    label,
                    adjusted_plot_price=bar.signal.close,
                )
            )
        else:
            events.append(
                ReviewEvent(
                    event_type,
                    event_date,
                    label,
                    raw_fill_price=row[fill_field],
                    source_label="KA10081 RAW DAILY OPEN",
                )
            )
    for day_number, field in enumerate(("d1_date", "d2_date", "d3_date"), 1):
        event_date = row.get(field)
        if event_date is not None:
            bar = _bar_for_date(bars, event_date)
            events.append(
                ReviewEvent(
                    ReviewEventType.REVERSAL_WAIT,
                    event_date,
                    f"D{day_number}",
                    adjusted_plot_price=bar.signal.close,
                )
            )
    return tuple(events)


def _future20_date(row: Mapping[str, Any], bars: Sequence[DailyBar]) -> date:
    dates = _date_index(bars)
    d3 = row.get("d3_date")
    if d3 is None:
        return row["upper_exit_fill_date"]
    index = dates[d3]
    return bars[min(len(bars) - 1, index + 20)].trade_date


def generate_review_charts(
    rows: Sequence[Mapping[str, Any]],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
    *,
    chart_root: Path = CHART_ROOT,
) -> list[dict[str, Any]]:
    """Render only report-only TOP/BOTTOM charts; no re-entry is executed."""

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
            slug=f"down-box-v0-3b-{category.casefold()}",
        )
        artifact = render_review_chart(
            prepared,
            chart_root / category / filename,
            strategy_policy=PROOF_VERSION,
            summary={
                **dict(row),
                "chart_category": category,
                "review_only": True,
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


def _write_summary_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
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


def _collect_rows(
    stocks: Sequence[str],
) -> tuple[tuple[dict[str, Any], ...], dict[str, tuple[DailyBar, ...]]]:
    all_rows: list[dict[str, Any]] = []
    daily_by_stock: dict[str, tuple[DailyBar, ...]] = {}
    for stock_code in sorted(stocks):
        bars, _ = _load_stock(stock_code)
        daily_by_stock[stock_code] = bars
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        proof = run_down_box_daily_execution_proof(
            daily_bars=bars,
            calendar=calendar,
            research_start=RESEARCH_START,
            research_end=RESEARCH_END,
        )
        points = tuple(calculate_daily_indicators(bars, calendar))
        for trade in proof["completed_trades"]:
            if trade["exit_action"] != DailyProofAction.FULL_TAKE_PROFIT_UPPER.value:
                continue
            all_rows.append(build_upper_transition_row(trade, bars, points))
    ordered = tuple(
        sorted(
            all_rows,
            key=lambda row: (
                row["stock_code"],
                row["upper_exit_fill_date"],
                row["setup_id"],
            ),
        )
    )
    return ordered, daily_by_stock


def build_report(
    rows: Sequence[Mapping[str, Any]],
    *,
    charts: Sequence[Mapping[str, Any]] = (),
    baseline_proof_path: Path = BASELINE_PROOF_PATH,
) -> dict[str, Any]:
    """Build JSON-safe-in-meaning report data while retaining Decimal values."""

    ordered = tuple(
        sorted(
            (dict(row) for row in rows),
            key=lambda row: (
                row["stock_code"],
                row["upper_exit_fill_date"],
                row["setup_id"],
            ),
        )
    )
    structural = tuple(row for row in ordered if row.get("structural_up_transition"))
    top5 = _rank_by_future20(ordered, reverse=True)
    bottom5 = _rank_by_future20(ordered, reverse=False)
    distributions = {
        "all_upper_exits": {
            field: _existing_distribution(ordered, field)
            for field in _DISTRIBUTION_FIELDS
        },
        "structural_candidates": {
            field: _existing_distribution(structural, field)
            for field in _DISTRIBUTION_FIELDS
        },
        "future20_top5": {
            field: _existing_distribution(top5, field) for field in _DISTRIBUTION_FIELDS
        },
        "future20_bottom5": {
            field: _existing_distribution(bottom5, field)
            for field in _DISTRIBUTION_FIELDS
        },
    }
    return {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "baseline": "FROZEN_V0_2_RULES_LONG_HISTORY_DAILY_PROOF",
        "baseline_proof_path": baseline_proof_path.as_posix(),
        "accounting_contract": {
            "completed_trade_pnl": "REALIZED_ONLY",
            "cumulative_return": "RESEARCH_END_MARK_TO_MARKET_RAW_CLOSE",
            "mdd": "DAILY_MARK_TO_MARKET_RAW_CLOSE",
            "upper_transition_future_returns": "RESEARCH_ONLY_NO_PNL",
        },
        "upper_exit_count": len(ordered),
        "funnel": build_funnel(ordered),
        "rows": list(ordered),
        "structural_candidate_count": len(structural),
        "future20_top5": top5,
        "future20_bottom5": bottom5,
        "distributions": distributions,
        "chart_policy": {
            "future_window": "D3_CLOSE_PLUS_20_TRADING_SESSIONS",
            "x_axis_date_policy": "TRADING_SESSION_INTERVAL",
            "x_axis_date_interval_sessions": 10,
            "x_axis_date_format": "DD",
            "price_axis": "SIGNAL_ADJUSTED_DAILY_OHLC",
            "raw_fill_price": "METADATA_ONLY",
        },
        "representative_charts": [dict(item) for item in charts],
    }


def run_upper_transition_audit(
    *,
    output: Path = OUTPUT_PATH,
    summary_csv: Path = SUMMARY_CSV_PATH,
    chart_root: Path = CHART_ROOT,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    """Re-run the frozen Daily proof and write only new audit artifacts."""

    rows, daily_by_stock = _collect_rows(stocks)
    charts = generate_review_charts(rows, daily_by_stock, chart_root=chart_root)
    report = build_report(rows, charts=charts)
    if report["upper_exit_count"] != 16:
        raise ValueError(
            f"expected 16 V0.2 upper exits, got {report['upper_exit_count']}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_summary_csv(summary_csv, rows)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--summary-csv", type=Path, default=SUMMARY_CSV_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    args = parser.parse_args()
    report = run_upper_transition_audit(
        output=args.output,
        summary_csv=args.summary_csv,
        chart_root=args.chart_root,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "upper_exit_count": report["upper_exit_count"],
                "structural_candidate_count": report["structural_candidate_count"],
                "charts": len(report["representative_charts"]),
                "network_calls": report["network_calls"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
