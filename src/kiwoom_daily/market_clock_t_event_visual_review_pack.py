"""Outcome-masked visual review pack for C1 full-Daily BAND EXIT events.

This module makes no strategy decision.  It consumes the persisted V0.3
report, pairs three GOOD and three FAILED C1 events for visual comparison, and
renders a blind T-40..T view plus a review T-40..T+20 view for every case.
Blind files contain neither identity nor forward outcome information.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.strategy_review.chart import (
    ChartType,
    PreparedReviewChart,
    ReviewEvent,
    ReviewEventType,
    prepare_review_chart,
    render_review_chart,
    trading_session_date_ticks,
)

from .down_box_daily_execution_proof import _load_stock
from .market_clock_compression_audit_v0_2 import OUTPUT_PATH as V02_OUTPUT_PATH
from .market_clock_pre_breakout_acceleration_audit import OUTPUT_PATH as V03_OUTPUT_PATH

PACK_VERSION = "MARKET_CLOCK_T_EVENT_VISUAL_REVIEW_PACK_V0_1"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_clock_t_event_visual_review_pack_v0_1.json"
)
MAPPING_PATH = Path(
    "data/processed/strategy_review/market_clock_t_event_visual_review_mapping_v0_1.json"
)
CASE_CSV_PATH = Path(
    "data/processed/strategy_review/market_clock_t_event_visual_review_cases_v0_1.csv"
)
CHART_ROOT = Path(
    "data/processed/strategy_charts/market_clock_t_event_visual_review_pack_v0_1"
)

GOOD = "GOOD_DIRECTIONAL"
FAILED = "FAILED"
MATCH_FIELDS = (
    "range_speed_t",
    "ma_cluster_width_atr_t",
    "breakout_clearance_atr",
    "event_body_atr",
)
GAP_FIELDS = (
    "ma5_10_gap_atr",
    "ma10_20_gap_atr",
    "ma20_60_gap_atr",
)
DECIMAL_RECORD_FIELDS = {
    *MATCH_FIELDS,
    "aligned_return_3_pct",
    "aligned_return_5_pct",
    "aligned_return_10_pct",
    "range_speed_t",
    "range_delta_3",
    "efficiency_10_t",
    "eff_delta_3",
    "ma_cluster_width_atr_t",
    "width_delta_3",
    "flow_speed_t",
    "flow_delta_3",
    "ma_cluster_high",
    "ma_cluster_low",
}
DECIMAL_STATE_FIELDS = {
    "sma5",
    "sma10",
    "sma20",
    "sma60",
    "ma5_10_gap_atr",
    "ma10_20_gap_atr",
    "ma20_60_gap_atr",
}
CASE_FIELDS = (
    "case_id",
    "stock_code",
    "event_date",
    "direction_label",
    "matching_distance",
    "sma5_t",
    "sma10_t",
    "sma20_t",
    "sma60_t",
    "ma5_10_gap_atr_t",
    "ma10_20_gap_atr_t",
    "ma20_60_gap_atr_t",
    "ma5_10_gap_atr_delta_5",
    "ma10_20_gap_atr_delta_5",
    "ma20_60_gap_atr_delta_5",
)


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal)):
        return value.isoformat() if isinstance(value, date) else str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, Decimal)):
        return value.isoformat() if isinstance(value, date) else str(value)
    return str(value)


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _load_records(path: Path) -> list[dict[str, Any]]:
    source = json.loads(path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    for raw in source["records"]:
        row = dict(raw)
        row["event_date"] = date.fromisoformat(row["event_date"])
        for field in DECIMAL_RECORD_FIELDS:
            if field in row:
                row[field] = _as_decimal(row[field])
        records.append(row)
    return sorted(records, key=lambda row: (row["stock_code"], row["event_date"]))


def _load_state_rows(
    path: Path,
) -> tuple[
    dict[tuple[str, int], dict[str, Any]], dict[tuple[str, date], dict[str, Any]]
]:
    source = json.loads(path.read_text(encoding="utf-8"))
    by_index: dict[tuple[str, int], dict[str, Any]] = {}
    by_date: dict[tuple[str, date], dict[str, Any]] = {}
    for raw in source["rows"]:
        row = dict(raw)
        row["trade_date"] = date.fromisoformat(row["trade_date"])
        for field in DECIMAL_STATE_FIELDS:
            if field in row:
                row[field] = _as_decimal(row[field])
        by_index[(row["stock_code"], row["_index"])] = row
        by_date[(row["stock_code"], row["trade_date"])] = row
    return by_index, by_date


def _complete_match_record(row: Mapping[str, Any]) -> bool:
    return all(row.get(field) is not None for field in MATCH_FIELDS)


def _record_key(row: Mapping[str, Any]) -> tuple[str, date]:
    return row["stock_code"], row["event_date"]


def _percentile_ranks(
    rows: Sequence[Mapping[str, Any]], field: str
) -> dict[tuple[str, date], Decimal]:
    ordered = sorted(
        rows,
        key=lambda row: (row[field], row["stock_code"], row["event_date"]),
    )
    if len(ordered) == 1:
        return {_record_key(ordered[0]): Decimal(0)}
    return {
        _record_key(row): Decimal(index) / Decimal(len(ordered) - 1)
        for index, row in enumerate(ordered)
    }


def _match_distance(
    good: Mapping[str, Any],
    failed: Mapping[str, Any],
    ranks: Mapping[str, Mapping[tuple[str, date], Decimal]],
) -> Decimal:
    return sum(
        abs(ranks[field][_record_key(good)] - ranks[field][_record_key(failed)])
        for field in MATCH_FIELDS
    )


def select_matched_cases(
    records: Sequence[Mapping[str, Any]], *, pair_count: int = 3
) -> tuple[dict[str, Any], ...]:
    """Greedily select deterministic same-direction GOOD/FAILED C1 pairs."""

    pool = [
        row
        for row in records
        if row.get("compression_quartile") == "C1"
        and row.get("outcome_label") in {GOOD, FAILED}
        and _complete_match_record(row)
    ]
    good = [row for row in pool if row["outcome_label"] == GOOD]
    failed = [row for row in pool if row["outcome_label"] == FAILED]
    ranks = {field: _percentile_ranks(pool, field) for field in MATCH_FIELDS}
    candidates = [
        (
            _match_distance(good_row, failed_row, ranks),
            good_row,
            failed_row,
        )
        for good_row in good
        for failed_row in failed
        if good_row["direction"] == failed_row["direction"]
    ]
    candidates.sort(
        key=lambda item: (
            item[0],
            item[1]["stock_code"],
            item[1]["event_date"],
            item[2]["stock_code"],
            item[2]["event_date"],
        )
    )
    chosen: list[tuple[Decimal, Mapping[str, Any], Mapping[str, Any]]] = []
    used: set[tuple[str, date]] = set()
    for distance, good_row, failed_row in candidates:
        if _record_key(good_row) in used or _record_key(failed_row) in used:
            continue
        chosen.append((distance, good_row, failed_row))
        used.update((_record_key(good_row), _record_key(failed_row)))
        if len(chosen) == pair_count:
            break
    if len(chosen) != pair_count:
        raise ValueError("insufficient same-direction C1 pairs for visual review")
    cases: list[dict[str, Any]] = []
    for pair_index, (distance, good_row, failed_row) in enumerate(chosen):
        for row in (good_row, failed_row):
            case_number = len(cases) + 1
            cases.append(
                {
                    "case_id": f"CASE_{case_number:02d}",
                    "pair_id": f"PAIR_{pair_index + 1:02d}",
                    "matching_distance": distance,
                    "selection_reason": (
                        "same-direction C1 GOOD/FAILED pair; minimum greedy pooled "
                        "percentile-rank L1 distance across RANGE_T, CLUSTER_WIDTH_T, "
                        "CLEARANCE_ATR, and BODY_ATR"
                    ),
                    "record": dict(row),
                }
            )
    return tuple(cases)


def _case_observation(
    case: Mapping[str, Any],
    state_by_index: Mapping[tuple[str, int], Mapping[str, Any]],
    state_by_date: Mapping[tuple[str, date], Mapping[str, Any]],
) -> dict[str, Any]:
    record = case["record"]
    state = state_by_date[(record["stock_code"], record["event_date"])]
    previous = state_by_index.get((record["stock_code"], state["_index"] - 5))
    observation = {
        "case_id": case["case_id"],
        "stock_code": record["stock_code"],
        "event_date": record["event_date"],
        "direction_label": record["direction_label"],
        "outcome_label": record["outcome_label"],
        "matching_distance": case["matching_distance"],
        "selection_reason": case["selection_reason"],
        "sma5_t": state.get("sma5"),
        "sma10_t": state.get("sma10"),
        "sma20_t": state.get("sma20"),
        "sma60_t": state.get("sma60"),
    }
    for field in GAP_FIELDS:
        current = state.get(field)
        previous_value = previous.get(field) if previous is not None else None
        short = field.removesuffix("_atr")
        observation[f"{short}_atr_t"] = current
        observation[f"{short}_atr_delta_5"] = (
            current - previous_value
            if current is not None and previous_value is not None
            else None
        )
    return observation


def _daily_by_stock(
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[DailyBar, ...]]:
    codes = sorted({case["record"]["stock_code"] for case in cases})
    result: dict[str, tuple[DailyBar, ...]] = {}
    for code in codes:
        bars, _ = _load_stock(code)
        result[code] = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    return result


def _event(
    record: Mapping[str, Any], bar: DailyBar, *, include_direction: bool
) -> ReviewEvent:
    label = (
        f"BAND EXIT T {record['direction_label']}"
        if include_direction
        else "BAND EXIT T"
    )
    return ReviewEvent(
        ReviewEventType.BOX_BREAKOUT,
        record["event_date"],
        label,
        adjusted_plot_price=bar.signal.close,
        details={"emphasize_vertical": True, "report_only": True},
    )


def _levels(record: Mapping[str, Any]) -> dict[str, Decimal]:
    return {
        label: value
        for label, value in (
            ("MA_CLUSTER_HIGH_T", record.get("ma_cluster_high")),
            ("MA_CLUSTER_LOW_T", record.get("ma_cluster_low")),
        )
        if value is not None and value > 0
    }


def _prepared_chart(
    case: Mapping[str, Any],
    bars: Sequence[DailyBar],
    *,
    variant: str,
) -> PreparedReviewChart:
    record = case["record"]
    date_to_index = {bar.trade_date: index for index, bar in enumerate(bars)}
    bar = bars[date_to_index[record["event_date"]]]
    prepared = prepare_review_chart(
        bars,
        chart_type=ChartType.EVENT_REVIEW,
        events=(_event(record, bar, include_direction=variant == "REVIEW"),),
        calendar=ExplicitTradingCalendar(bar.trade_date for bar in bars),
        focus_date=record["event_date"],
        event_end_date=record["event_date"],
        pre_sessions=40,
        post_sessions=0 if variant == "BLIND" else 20,
        show_sma5=True,
        horizontal_levels=_levels(record),
    )
    return replace(prepared, stock_code=f"{case['case_id']}_{variant}")


def _blind_metadata(
    case: Mapping[str, Any], prepared: PreparedReviewChart, backend: str
) -> dict[str, Any]:
    record = case["record"]
    date_ticks = trading_session_date_ticks(prepared.window.bars)
    return {
        "case_id": case["case_id"],
        "variant": "BLIND",
        "outcome_masked": True,
        "chart_title": f"{case['case_id']}_BLIND EVENT_REVIEW",
        "visible_session_policy": "T-40 through T inclusive",
        "price_axis_basis": "SIGNAL_ADJUSTED_DAILY_OHLC",
        "moving_average_basis": "SIGNAL_ADJUSTED_DAILY_CLOSE",
        "show_sma5": True,
        "show_sma10": True,
        "show_sma20": True,
        "show_sma60": True,
        "event_marker": "BAND_EXIT_T",
        "event_direction": record["direction_label"],
        "event_time_metrics": {
            "range_t": record.get("range_speed_t"),
            "range_d3": record.get("range_delta_3"),
            "eff10_t": record.get("efficiency_10_t"),
            "eff_d3": record.get("eff_delta_3"),
            "cluster_width_t": record.get("ma_cluster_width_atr_t"),
            "width_d3": record.get("width_delta_3"),
            "clearance_atr": record.get("breakout_clearance_atr"),
            "body_atr": record.get("event_body_atr"),
            "flow_t": record.get("flow_speed_t"),
            "flow_d3": record.get("flow_delta_3"),
        },
        "x_axis_date_policy": "TRADING_SESSION_INTERVAL",
        "x_axis_date_interval_sessions": 10,
        "x_axis_date_format": "DD",
        "x_axis_tick_indexes": [index for index, _ in date_ticks],
        "x_axis_tick_labels": [f"{day.day:02d}" for _, day in date_ticks],
        "render_backend": backend,
    }


def _review_summary(
    case: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, Any]:
    record = case["record"]
    return {
        "case_id": case["case_id"],
        "pair_id": case["pair_id"],
        "selection_reason": case["selection_reason"],
        "matching_distance": case["matching_distance"],
        "outcome_label": record["outcome_label"],
        "aligned_return_3_pct": record.get("aligned_return_3_pct"),
        "aligned_return_5_pct": record.get("aligned_return_5_pct"),
        "aligned_return_10_pct": record.get("aligned_return_10_pct"),
        "event_direction": record["direction_label"],
        "range_t": record.get("range_speed_t"),
        "range_d3": record.get("range_delta_3"),
        "eff10_t": record.get("efficiency_10_t"),
        "eff_d3": record.get("eff_delta_3"),
        "cluster_width_t": record.get("ma_cluster_width_atr_t"),
        "width_d3": record.get("width_delta_3"),
        "clearance_atr": record.get("breakout_clearance_atr"),
        "body_atr": record.get("event_body_atr"),
        "flow_t": record.get("flow_speed_t"),
        "flow_d3": record.get("flow_delta_3"),
        "ma_snapshot": dict(observation),
    }


def _render_cases(
    cases: Sequence[Mapping[str, Any]],
    observations: Mapping[str, Mapping[str, Any]],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
    *,
    chart_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blind_artifacts: list[dict[str, Any]] = []
    review_artifacts: list[dict[str, Any]] = []
    for variant, artifacts in (
        ("BLIND", blind_artifacts),
        ("REVIEW", review_artifacts),
    ):
        for case in cases:
            prepared = _prepared_chart(
                case, daily_by_stock[case["record"]["stock_code"]], variant=variant
            )
            output_path = chart_root / variant / f"{case['case_id']}_{variant}.png"
            artifact = render_review_chart(
                prepared,
                output_path,
                strategy_policy=PACK_VERSION,
                summary=(
                    {"case_id": case["case_id"], "outcome_masked": True}
                    if variant == "BLIND"
                    else _review_summary(case, observations[case["case_id"]])
                ),
            )
            if variant == "BLIND":
                artifact.metadata_path.write_text(
                    json.dumps(
                        _blind_metadata(case, prepared, artifact.backend),
                        ensure_ascii=False,
                        indent=2,
                        default=_json_default,
                    ),
                    encoding="utf-8",
                )
            artifacts.append(
                {
                    "case_id": case["case_id"],
                    "variant": variant,
                    "png_path": artifact.png_path.as_posix(),
                    "metadata_path": artifact.metadata_path.as_posix(),
                }
            )
    return blind_artifacts, review_artifacts


def _write_case_csv(path: Path, observations: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CASE_FIELDS)
        writer.writeheader()
        for row in observations:
            writer.writerow(
                {field: _csv_value(row.get(field)) for field in CASE_FIELDS}
            )


def run_market_clock_t_event_visual_review_pack(
    *,
    output: Path = OUTPUT_PATH,
    mapping_output: Path = MAPPING_PATH,
    case_csv: Path = CASE_CSV_PATH,
    chart_root: Path = CHART_ROOT,
    v03_output: Path = V03_OUTPUT_PATH,
    v02_output: Path = V02_OUTPUT_PATH,
    render_charts: bool = True,
) -> dict[str, Any]:
    """Build the pack from persisted local audit artifacts; never use network."""

    records = _load_records(v03_output)
    state_by_index, state_by_date = _load_state_rows(v02_output)
    cases = select_matched_cases(records)
    observations = [
        _case_observation(case, state_by_index, state_by_date) for case in cases
    ]
    observation_by_id = {row["case_id"]: row for row in observations}
    daily_by_stock = _daily_by_stock(cases) if render_charts else {}
    blind_charts, review_charts = (
        _render_cases(cases, observation_by_id, daily_by_stock, chart_root=chart_root)
        if render_charts
        else ([], [])
    )
    mapping_cases = [
        {
            "case_id": case["case_id"],
            "pair_id": case["pair_id"],
            "stock_code": case["record"]["stock_code"],
            "event_date": case["record"]["event_date"],
            "direction": case["record"]["direction_label"],
            "outcome_label": case["record"]["outcome_label"],
            "aligned_return_3_pct": case["record"].get("aligned_return_3_pct"),
            "aligned_return_5_pct": case["record"].get("aligned_return_5_pct"),
            "aligned_return_10_pct": case["record"].get("aligned_return_10_pct"),
            "matching_distance": case["matching_distance"],
            "selection_reason": case["selection_reason"],
            "matching_input": {
                field: case["record"].get(field) for field in MATCH_FIELDS
            },
        }
        for case in cases
    ]
    report: dict[str, Any] = {
        "pack_version": PACK_VERSION,
        "network_calls": 0,
        "source_v03": v03_output.as_posix(),
        "source_v02": v02_output.as_posix(),
        "methodology": {
            "population": "C1 BAND EXIT events with evaluated GOOD/FAILED outcomes",
            "selection": "three deterministic same-direction GOOD/FAILED matched pairs",
            "matching_fields": MATCH_FIELDS,
            "matching_algorithm": "greedy minimum pooled percentile-rank L1 distance; no strategy threshold",
            "blind_window": "T-40 through T inclusive",
            "review_window": "T-40 through T+20 inclusive",
            "blind_outcome_policy": "outcome, return, stock identity, and full event date are excluded from blind filename, title, and metadata",
            "price_axis_basis": "SIGNAL_ADJUSTED_DAILY_OHLC",
            "moving_average_basis": "SIGNAL_ADJUSTED_DAILY_CLOSE",
            "strategy_changes": False,
            "buy_sell": False,
            "pnl": False,
        },
        "cases": observations,
        "blind_charts": blind_charts,
        "review_charts": review_charts,
    }
    mapping = {
        "pack_version": PACK_VERSION,
        "purpose": "separate CASE_ID-to-outcome mapping for post-blind review",
        "cases": mapping_cases,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    mapping_output.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_case_csv(case_csv, observations)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--mapping-output", type=Path, default=MAPPING_PATH)
    parser.add_argument("--case-csv", type=Path, default=CASE_CSV_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    parser.add_argument("--no-charts", action="store_true")
    args = parser.parse_args()
    report = run_market_clock_t_event_visual_review_pack(
        output=args.output,
        mapping_output=args.mapping_output,
        case_csv=args.case_csv,
        chart_root=args.chart_root,
        render_charts=not args.no_charts,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "case_count": len(report["cases"]),
                "blind_chart_count": len(report["blind_charts"]),
                "review_chart_count": len(report["review_charts"]),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
