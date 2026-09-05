"""Report-only MARKET_CLOCK / MARKET_PHASE audit (V0.4).

The audit augments the persisted V0.3 full-Daily BAND EXIT population with
event-time phase measurements.  It intentionally creates neither a strategy
state nor a threshold: forward R10 is used only to label descriptive groups.
"""

from __future__ import annotations

import argparse
import itertools
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from src.backtest_engine.models import DailyBar

from .down_box_daily_execution_proof import _load_stock
from .market_clock_compression_audit_v0_2 import (
    OUTPUT_PATH as V02_OUTPUT_PATH,
)
from .market_clock_compression_audit_v0_2 import (
    RESEARCH_END,
    RESEARCH_START,
    STOCKS,
    _json_default,
)
from .market_clock_pre_breakout_acceleration_audit import (
    OUTCOME_FAILED,
    OUTCOME_GOOD,
    OUTCOME_UNAVAILABLE,
    _distribution,
    _percentile,
)
from .market_clock_pre_breakout_acceleration_audit import (
    OUTPUT_PATH as V03_OUTPUT_PATH,
)
from .market_clock_t_event_visual_review_pack import MAPPING_PATH as VISUAL_MAPPING_PATH

PROOF_VERSION = "MARKET_CLOCK_MARKET_PHASE_AUDIT_V0_4"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_clock_market_phase_audit_v0_4.json"
)

PHASE_METRICS = (
    "dir_position_20",
    "dir_position_40",
    "dir_position_60",
    "directional_room_20_atr",
    "directional_room_40_atr",
    "directional_room_60_atr",
    "dir_ma60_dist_atr",
    "abs_ma60_dist_atr",
    "ma20_same_direction_age",
    "ma60_same_direction_age",
    "ma20_sessions_since_turn",
    "ma60_sessions_since_turn",
    "sessions_since_recent_extreme_20",
    "sessions_since_recent_extreme_40",
    "sessions_since_recent_extreme_60",
    "directional_move_20_atr",
    "directional_move_40_atr",
    "directional_move_60_atr",
)

CLOCK_FIELDS = (
    "range_speed_t",
    "range_delta_1",
    "range_delta_3",
    "range_delta_5",
    "efficiency_10_t",
    "eff_delta_1",
    "eff_delta_3",
    "eff_delta_5",
    "flow_speed_t",
    "flow_delta_1",
    "flow_delta_3",
    "flow_delta_5",
    "ma_cluster_width_atr_t",
    "width_delta_1",
    "width_delta_3",
    "width_delta_5",
    "breakout_clearance_atr",
    "event_body_atr",
)


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _date(value: object) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _daily_bars(stocks: Sequence[str]) -> dict[str, tuple[DailyBar, ...]]:
    return {
        stock_code: tuple(
            sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date)
        )
        for stock_code in sorted(stocks)
    }


def _rows_by_stock(v02_report: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    numeric_fields = {
        "range_speed",
        "efficiency_10",
        "flow_speed",
        "atr20",
        "sma5",
        "sma10",
        "sma20",
        "sma60",
        "ma_band_high",
        "ma_band_low",
        "ma_cluster_width_atr",
    }
    for source in v02_report["rows"]:
        row = dict(source)
        row["trade_date"] = _date(row["trade_date"])
        for field in numeric_fields:
            row[field] = _decimal(row.get(field))
        result.setdefault(str(row["stock_code"]), []).append(row)
    for rows in result.values():
        rows.sort(key=lambda row: row["_index"])
    return result


def _window_phase_metrics(
    bars: Sequence[DailyBar], index: int, direction: int, atr: Decimal | None
) -> dict[str, Decimal | int | None]:
    """Calculate range/room/move values from T and prior bars only."""
    result: dict[str, Decimal | int | None] = {}
    bar = bars[index]
    for window in (20, 40, 60):
        start = max(0, index - window + 1)
        history = bars[start : index + 1]
        low = min(item.signal.low for item in history)
        high = max(item.signal.high for item in history)
        denominator = high - low
        position = (bar.signal.close - low) / denominator if denominator else None
        result[f"range_position_{window}"] = position
        result[f"dir_position_{window}"] = (
            position if direction > 0 or position is None else Decimal(1) - position
        )
        if atr in (None, Decimal(0)):
            result[f"directional_room_{window}_atr"] = None
            result[f"directional_move_{window}_atr"] = None
        elif direction > 0:
            result[f"directional_room_{window}_atr"] = (high - bar.signal.close) / atr
            result[f"directional_move_{window}_atr"] = (bar.signal.close - low) / atr
        else:
            result[f"directional_room_{window}_atr"] = (bar.signal.close - low) / atr
            result[f"directional_move_{window}_atr"] = (high - bar.signal.close) / atr
        extreme = low if direction > 0 else high
        # A reverse scan intentionally selects the most recent tied extreme.
        extreme_index = max(
            candidate
            for candidate in range(start, index + 1)
            if (
                bars[candidate].signal.low
                if direction > 0
                else bars[candidate].signal.high
            )
            == extreme
        )
        result[f"sessions_since_recent_extreme_{window}"] = index - extreme_index
    return result


def _slope_sign(value: Decimal | None, prior: Decimal | None) -> int | None:
    if value is None or prior is None:
        return None
    return 1 if value > prior else -1 if value < prior else 0


def _trend_metrics(
    rows: Sequence[Mapping[str, Any]],
    row_index: int,
    direction: int,
    atr: Decimal | None,
    close: Decimal,
) -> dict[str, Decimal | int | None]:
    """Use only in-period history; an unobserved pre-period turn remains unavailable."""
    result: dict[str, Decimal | int | None] = {}
    for period in (20, 60):
        field = f"sma{period}"
        row = rows[row_index]
        ma = row.get(field)
        result[f"dir_ma{period}_dist_atr"] = (
            Decimal(direction) * (close - ma) / atr
            if period == 60 and ma is not None and atr not in (None, Decimal(0))
            else None
        )
        if period == 60:
            result["abs_ma60_dist_atr"] = (
                abs(close - ma) / atr
                if ma is not None and atr not in (None, Decimal(0))
                else None
            )
        signs: list[int | None] = []
        for candidate in range(len(rows)):
            prior = rows[candidate - 5].get(field) if candidate >= 5 else None
            signs.append(_slope_sign(rows[candidate].get(field), prior))
        current = signs[row_index]
        same = 0
        if current == direction:
            cursor = row_index
            while cursor >= 0 and signs[cursor] == direction:
                same += 1
                cursor -= 1
        # A usable but oppositely directed MA has zero same-direction age;
        # ``None`` is reserved for an unavailable slope calculation.
        result[f"ma{period}_same_direction_age"] = same if current is not None else None
        opposite_index = next(
            (
                candidate
                for candidate in range(row_index - 1, -1, -1)
                if signs[candidate] == -direction
            ),
            None,
        )
        # This is deliberately unavailable if no confirming opposite slope is
        # observed within the research-period row history.
        result[f"ma{period}_sessions_since_turn"] = (
            row_index - opposite_index
            if current == direction and opposite_index is not None
            else None
        )
    return result


def _phase_record(
    record: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    bars: Sequence[DailyBar],
) -> dict[str, Any]:
    event_date = _date(record["event_date"])
    row_index = next(
        index for index, row in enumerate(rows) if row["trade_date"] == event_date
    )
    bar_index = rows[row_index]["_index"]
    direction = int(record["direction"])
    atr = rows[row_index].get("atr20")
    result = dict(record)
    result["event_date"] = event_date
    result.update(_window_phase_metrics(bars, bar_index, direction, atr))
    result.update(
        _trend_metrics(rows, row_index, direction, atr, bars[bar_index].signal.close)
    )
    return result


def _median(values: Iterable[Decimal]) -> Decimal | None:
    values = list(values)
    return median(values) if values else None


def _quartile(value: Decimal, thresholds: tuple[Decimal, Decimal, Decimal]) -> str:
    return (
        "Q1"
        if value <= thresholds[0]
        else "Q2"
        if value <= thresholds[1]
        else "Q3"
        if value <= thresholds[2]
        else "Q4"
    )


def _quartile_evidence(
    rows: Sequence[Mapping[str, Any]], metric: str
) -> dict[str, Any]:
    values = sorted(
        _decimal(row.get(metric)) for row in rows if row.get(metric) is not None
    )
    if not values:
        return {"metric": metric, "available": False, "quartiles": {}}
    thresholds = tuple(
        _percentile(values, Decimal(value)) for value in ("0.25", "0.50", "0.75")
    )
    assert all(value is not None for value in thresholds)
    result: dict[str, Any] = {}
    for label in ("Q1", "Q2", "Q3", "Q4"):
        group = [
            row
            for row in rows
            if row.get(metric) is not None
            and _quartile(_decimal(row[metric]), thresholds) == label
        ]
        evaluated = [
            row for row in group if row["outcome_label"] != OUTCOME_UNAVAILABLE
        ]
        good = sum(row["outcome_label"] == OUTCOME_GOOD for row in evaluated)
        r10 = [
            _decimal(row["aligned_return_10_pct"])
            for row in evaluated
            if row.get("aligned_return_10_pct") is not None
        ]
        result[label] = {
            "count": len(group),
            "evaluated_count": len(evaluated),
            "good_rate": Decimal(good) / Decimal(len(evaluated)) if evaluated else None,
            "median_aligned_return_10_pct": _median(r10),
        }
    return {
        "metric": metric,
        "available": True,
        "thresholds": {
            "q25": thresholds[0],
            "q50": thresholds[1],
            "q75": thresholds[2],
        },
        "quartiles": result,
    }


def _matrix(rows: Sequence[Mapping[str, Any]], left: str, right: str) -> dict[str, Any]:
    left_values = sorted(
        _decimal(row[left]) for row in rows if row.get(left) is not None
    )
    right_values = sorted(
        _decimal(row[right]) for row in rows if row.get(right) is not None
    )
    if not left_values or not right_values:
        return {"available": False, "left": left, "right": right, "cells": {}}
    left_thresholds = tuple(
        _percentile(left_values, Decimal(value)) for value in ("0.25", "0.50", "0.75")
    )
    right_thresholds = tuple(
        _percentile(right_values, Decimal(value)) for value in ("0.25", "0.50", "0.75")
    )
    cells: dict[str, Any] = {}
    for a, b in itertools.product(("Q1", "Q2", "Q3", "Q4"), repeat=2):
        group = [
            row
            for row in rows
            if row.get(left) is not None
            and row.get(right) is not None
            and _quartile(_decimal(row[left]), left_thresholds) == a
            and _quartile(_decimal(row[right]), right_thresholds) == b
        ]
        evaluated = [
            row for row in group if row["outcome_label"] != OUTCOME_UNAVAILABLE
        ]
        good = sum(row["outcome_label"] == OUTCOME_GOOD for row in evaluated)
        r10 = [
            _decimal(row["aligned_return_10_pct"])
            for row in evaluated
            if row.get("aligned_return_10_pct") is not None
        ]
        cells[f"{a}x{b}"] = {
            "count": len(group),
            "evaluated_count": len(evaluated),
            "good_rate": Decimal(good) / Decimal(len(evaluated)) if evaluated else None,
            "median_aligned_return_10_pct": _median(r10),
        }
    return {"available": True, "left": left, "right": right, "cells": cells}


def _comparison(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        label: {
            "count": sum(row["outcome_label"] == label for row in rows),
            "metrics": {
                metric: _distribution(
                    _decimal(row[metric])
                    for row in rows
                    if row["outcome_label"] == label and row.get(metric) is not None
                )
                for metric in PHASE_METRICS
            },
        }
        for label in (OUTCOME_GOOD, OUTCOME_FAILED, OUTCOME_UNAVAILABLE)
    }


def _descriptive_phase_groups(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Name phase groups only after pooled, report-only move quartiles.

    ``EARLY``/``MIDDLE``/``LATE`` are summary labels, not strategy states and
    are deliberately derived without looking at forward returns.
    """
    metric = "directional_move_40_atr"
    values = sorted(
        _decimal(row[metric]) for row in rows if row.get(metric) is not None
    )
    if not values:
        return {"available": False, "construction": "unavailable", "groups": {}}
    q25 = _percentile(values, Decimal("0.25"))
    q75 = _percentile(values, Decimal("0.75"))
    assert q25 is not None and q75 is not None
    groups = {
        "EARLY": [
            row
            for row in rows
            if row.get(metric) is not None and _decimal(row[metric]) <= q25
        ],
        "MIDDLE": [
            row
            for row in rows
            if row.get(metric) is not None and q25 < _decimal(row[metric]) <= q75
        ],
        "LATE": [
            row
            for row in rows
            if row.get(metric) is not None and _decimal(row[metric]) > q75
        ],
    }
    return {
        "available": True,
        "construction": "pooled directional_move_40_atr: EARLY<=Q1; MIDDLE=(Q1,Q3]; LATE>Q3",
        "thresholds": {"q25": q25, "q75": q75},
        "groups": {label: _comparison(group) for label, group in groups.items()},
    }


def _hypotheses(
    quartiles: Mapping[str, Any],
    comparison: Mapping[str, Any],
    matrices: Mapping[str, Any],
) -> dict[str, str]:
    def median(label: str, metric: str) -> Decimal | None:
        return comparison[label]["metrics"][metric]["median"]

    def relation(metric: str, *, good_lower: bool) -> str:
        good, failed = median(OUTCOME_GOOD, metric), median(OUTCOME_FAILED, metric)
        if good is None or failed is None:
            return "INCONCLUSIVE"
        evidence = good < failed if good_lower else good > failed
        return "SUPPORTED" if evidence else "NOT_SUPPORTED"

    phase = relation("dir_position_40", good_lower=True)
    room = relation("directional_room_40_atr", good_lower=False)
    ma60 = relation("dir_ma60_dist_atr", good_lower=True)
    age = relation("ma60_same_direction_age", good_lower=True)
    interaction = matrices["range_delta_3_x_directional_room_40_atr"]
    cells = interaction.get("cells", {}).values()
    rates = [cell["good_rate"] for cell in cells if cell["good_rate"] is not None]
    room_rates = quartiles["directional_room_40_atr"].get("quartiles", {})
    room_q1 = room_rates.get("Q1", {}).get("good_rate")
    room_q4 = room_rates.get("Q4", {}).get("good_rate")
    # A central tendency advantage without a Q1→Q4 high-room advantage is
    # descriptive but insufficient for a strong directional conclusion.
    h2 = (
        "SUPPORTED"
        if room == "SUPPORTED"
        and room_q1 is not None
        and room_q4 is not None
        and room_q4 > room_q1
        else "PARTIALLY_SUPPORTED"
        if room == "SUPPORTED"
        else room
    )
    h5 = (
        "PARTIALLY_SUPPORTED"
        if len(set(rates)) > 1 and phase != "INCONCLUSIVE" and room != "INCONCLUSIVE"
        else "INCONCLUSIVE"
    )
    return {
        "H1_LATE_EXTREME_FAILED": phase,
        "H2_DIRECTIONAL_ROOM_CONTINUATION": h2,
        "H3_MA60_DISTANCE_MATURITY": ma60,
        "H4_TREND_AGE_LATE_FAILURE": age,
        "H5_CLOCK_PLUS_PHASE_SEPARATION": h5,
    }


def _visual_cases(
    records: Sequence[Mapping[str, Any]], mapping_path: Path
) -> list[dict[str, Any]]:
    if not mapping_path.exists():
        return []
    lookup = {(row["stock_code"], row["event_date"]): row for row in records}
    output: list[dict[str, Any]] = []
    for case in _load_json(mapping_path).get("cases", []):
        key = (case["stock_code"], _date(case["event_date"]))
        record = lookup[key]
        output.append(
            {
                "case_id": case["case_id"],
                "pair_id": case["pair_id"],
                "stock_code": key[0],
                "event_date": key[1],
                "outcome_label": record["outcome_label"],
                "direction_label": record["direction_label"],
                **{metric: record.get(metric) for metric in PHASE_METRICS},
                "range_delta_3": record.get("range_delta_3"),
            }
        )
    return output


def run_market_clock_market_phase_audit(
    *,
    output: Path = OUTPUT_PATH,
    v03_path: Path = V03_OUTPUT_PATH,
    v02_path: Path = V02_OUTPUT_PATH,
    visual_mapping_path: Path = VISUAL_MAPPING_PATH,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    """Build V0.4 from persisted V0.3/V0.2 artifacts and cached Daily bars only."""
    v03 = _load_json(v03_path)
    v02_rows = _rows_by_stock(_load_json(v02_path))
    bars = _daily_bars(stocks)
    records = [
        _phase_record(
            record, v02_rows[record["stock_code"]], bars[record["stock_code"]]
        )
        for record in v03["records"]
    ]
    records.sort(
        key=lambda row: (row["stock_code"], row["event_date"], row["direction"])
    )
    keys = [(row["stock_code"], row["event_date"], row["direction"]) for row in records]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate V0.3 BAND EXIT semantic key")
    comparison = _comparison(records)
    quartiles = {
        metric: _quartile_evidence(records, metric) for metric in PHASE_METRICS
    }
    matrices = {
        f"range_delta_3_x_{metric}": _matrix(records, "range_delta_3", metric)
        for metric in (
            "directional_room_40_atr",
            "dir_position_40",
            "dir_ma60_dist_atr",
        )
    }
    report: dict[str, Any] = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "research_period": {"start": RESEARCH_START, "end": RESEARCH_END},
        "source_v03": v03_path.as_posix(),
        "source_v02": v02_path.as_posix(),
        "population": {
            "band_exit_count": len(records),
            "outcome_counts": {
                label: sum(row["outcome_label"] == label for row in records)
                for label in (OUTCOME_GOOD, OUTCOME_FAILED, OUTCOME_UNAVAILABLE)
            },
        },
        "methodology": {
            "signal_price_basis": "adjusted Daily OHLC / signal price series",
            "feature_time_boundary": "event T and prior trading sessions only",
            "range_windows": "trailing windows include T; tied extremes use most recent occurrence",
            "trend_age": "five-session SMA slope sign; turn unavailable when opposite sign is not observed in research history",
            "outcome_policy": "GOOD_DIRECTIONAL iff aligned_return_10_pct > 0; evaluation only",
            "attached_v03_clock_fields": CLOCK_FIELDS,
            "report_only": True,
            "strategy_signals": False,
            "orders": False,
            "fills": False,
            "pnl": False,
            "thresholds": False,
            "charts_generated": False,
        },
        "records": records,
        "good_vs_failed": comparison,
        "direction_split": {
            "UP": _comparison([row for row in records if row["direction"] > 0]),
            "DOWN": _comparison([row for row in records if row["direction"] < 0]),
        },
        "phase_quartile_evidence": quartiles,
        "descriptive_phase_groups": _descriptive_phase_groups(records),
        "clock_phase_matrices": matrices,
        "visual_anchor_cases": _visual_cases(records, visual_mapping_path),
    }
    report["hypotheses"] = _hypotheses(quartiles, comparison, matrices)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = run_market_clock_market_phase_audit(output=args.output)
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "population": report["population"],
                "hypotheses": report["hypotheses"],
            },
            indent=2,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
