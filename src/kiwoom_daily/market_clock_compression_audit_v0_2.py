"""Full-Daily MARKET_CLOCK compression and cross-event validation (V0.2).

This is a report-only research audit.  It uses every Daily trading session in
the fixed 10-stock research window and never emits strategy signals, orders,
fills, or PnL.  The existing V0.1 pivot-sampled report is read only for a
descriptive comparison.
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

from src.backtest_engine.indicators import (
    calculate_daily_indicators,
    simple_moving_average,
)
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar

from .down_box_daily_execution_proof import _load_stock
from .market_clock_audit import _atr20, _clock_series
from .market_clock_compression_audit import (
    _band_exit_flags,
    _cross_flags,
    _event_at,
    _metric_summary,
    _ordering_flip_count,
)

PROOF_VERSION = "MARKET_CLOCK_MA_COMPRESSION_BREAKOUT_AUDIT_V0_2"
RESEARCH_START = date(2023, 9, 1)
RESEARCH_END = date(2026, 8, 28)
V01_OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_compression_breakout_audit_v0_1.json"
)
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_compression_breakout_audit_v0_2.json"
)
SUMMARY_CSV_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_compression_breakout_audit_v0_2.csv"
)
EVENT_CSV_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_compression_breakout_events_v0_2.csv"
)
STOCKS = (
    "000660",
    "005930",
    "005380",
    "012450",
    "034020",
    "035420",
    "035720",
    "066570",
    "068270",
    "105560",
)
MA_NAMES = ("MA5", "MA10", "MA20", "MA60")
PERSISTENCE_FIELDS = ("same_side_d1", "same_side_d3", "same_side_d5")

SUMMARY_FIELDS = (
    "stock_code",
    "trade_date",
    "range_speed",
    "range_speed_quartile",
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
    "ma5_10_gap_atr",
    "ma10_20_gap_atr",
    "ma20_60_gap_atr",
    "ma5_60_cluster_width_atr",
    "compression_quartile",
    "compression_duration_sessions",
    "ma5_ma10_order_flip_count_20",
    "ma10_ma20_order_flip_count_20",
    "ma5_ma20_order_flip_count_20",
    "close_ma10_up_cross",
    "close_ma10_down_cross",
    "close_ma20_up_cross",
    "close_ma20_down_cross",
    "up_band_exit",
    "down_band_exit",
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


def _median(values: Iterable[Decimal]) -> Decimal | None:
    values_list = list(values)
    return median(values_list) if values_list else None


def _valid_speed(row: Mapping[str, Any]) -> bool:
    return all(
        row.get(field) is not None
        for field in ("range_speed", "efficiency_10", "flow_speed")
    )


def _valid_compression(row: Mapping[str, Any]) -> bool:
    return row.get("ma_cluster_width_atr") is not None


def _percentile(sorted_values: Sequence[Decimal], quantile: Decimal) -> Decimal | None:
    if not sorted_values:
        return None
    position = Decimal(len(sorted_values) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - Decimal(lower)
    return (
        sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction
    )


def _bucket(
    value: Decimal | None, thresholds: tuple[Decimal, Decimal, Decimal] | None
) -> str | None:
    if value is None or thresholds is None:
        return None
    q25, q50, q75 = thresholds
    if value <= q25:
        return "Q1"
    if value <= q50:
        return "Q2"
    if value <= q75:
        return "Q3"
    return "Q4"


def _assign_quartile(
    rows: Sequence[dict[str, Any]], field: str, output_field: str
) -> None:
    values = sorted(row[field] for row in rows if row.get(field) is not None)
    thresholds = (
        (
            _percentile(values, Decimal("0.25")),
            _percentile(values, Decimal("0.50")),
            _percentile(values, Decimal("0.75")),
        )
        if values
        else None
    )
    for row in rows:
        row[output_field] = _bucket(row.get(field), thresholds)


def _compression_duration(rows: Sequence[dict[str, Any]]) -> None:
    """Attach consecutive C1 duration to each stock's canonical rows."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["stock_code"], []).append(row)
    for entries in grouped.values():
        entries.sort(key=lambda row: row["trade_date"])
        duration = 0
        previous_index: int | None = None
        for row in entries:
            index = row["_index"]
            is_c1 = row.get("compression_quartile") == "C1"
            if is_c1 and previous_index is not None and index == previous_index + 1:
                duration += 1
            elif is_c1:
                duration = 1
            else:
                duration = 0
            row["compression_duration_sessions"] = duration if is_c1 else None
            previous_index = index


def _band_series(
    sma5: Sequence[Decimal | None],
    sma10: Sequence[Decimal | None],
    sma20: Sequence[Decimal | None],
) -> tuple[list[Decimal | None], list[Decimal | None]]:
    highs: list[Decimal | None] = []
    lows: list[Decimal | None] = []
    for values in zip(sma5, sma10, sma20, strict=True):
        if any(value is None for value in values):
            highs.append(None)
            lows.append(None)
        else:
            highs.append(max(values))
            lows.append(min(values))
    return highs, lows


def _event_metric_snapshot(
    rows_by_index: Mapping[int, Mapping[str, Any]], index: int, offset: int
) -> dict[str, Any] | None:
    row = rows_by_index.get(index + offset)
    if row is None:
        return None
    return {
        "trade_date": row["trade_date"],
        "range_speed": row.get("range_speed"),
        "efficiency_10": row.get("efficiency_10"),
        "flow_speed": row.get("flow_speed"),
        "ma_cluster_width_atr": row.get("ma_cluster_width_atr"),
    }


def _decorate_band_acceleration(
    event: dict[str, Any], rows_by_index: Mapping[int, Mapping[str, Any]], index: int
) -> None:
    event["market_clock_snapshot"] = {
        str(offset): _event_metric_snapshot(rows_by_index, index, offset)
        for offset in (-5, -3, 0, 1, 3, 5)
    }


def _event_outcome_groups(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for label, selected in (
        (
            "GOOD_DIRECTIONAL",
            [
                event
                for event in events
                if event["event_type"] == "BAND_EXIT"
                and event.get("aligned_return_10_pct") is not None
                and event["aligned_return_10_pct"] > 0
            ],
        ),
        (
            "FAILED",
            [
                event
                for event in events
                if event["event_type"] == "BAND_EXIT"
                and event.get("aligned_return_10_pct") is not None
                and event["aligned_return_10_pct"] <= 0
            ],
        ),
    ):
        result[label] = {"count": len(selected)}
        for phase, offset in (("pre", -5), ("post", 5)):
            for field in (
                "range_speed",
                "efficiency_10",
                "flow_speed",
                "ma_cluster_width_atr",
            ):
                values = [
                    event["market_clock_snapshot"][str(offset)][field]
                    for event in selected
                    if event.get("market_clock_snapshot", {}).get(str(offset))
                    and event["market_clock_snapshot"][str(offset)].get(field)
                    is not None
                ]
                result[label][f"{phase}_{field}_median"] = _median(values)
        for field in (
            "range_speed",
            "efficiency_10",
            "flow_speed",
            "ma_cluster_width_atr",
        ):
            pre = result[label][f"pre_{field}_median"]
            post = result[label][f"post_{field}_median"]
            result[label][f"post_minus_pre_{field}"] = (
                post - pre if pre is not None and post is not None else None
            )
    unavailable = [
        event
        for event in events
        if event["event_type"] == "BAND_EXIT"
        and event.get("aligned_return_10_pct") is None
    ]
    result["UNAVAILABLE_HORIZON"] = {"count": len(unavailable)}
    return result


def _frequency(count: int, denominator: int) -> Decimal | None:
    if denominator == 0:
        return None
    return Decimal(count) * Decimal(100) / Decimal(denominator)


def _regime_event_summary(
    rows: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    predicate: Any,
) -> dict[str, Any]:
    selected = [row for row in rows if predicate(row)]
    keys = {(row["stock_code"], row["trade_date"]) for row in selected}
    selected_events = [
        event for event in events if (event["stock_code"], event["event_date"]) in keys
    ]
    result: dict[str, Any] = {
        "daily_session_count": len(keys),
        "row_count": len(selected),
    }
    for event_type, label in (
        ("CLOSE_MA10_CROSS", "ma10_cross"),
        ("CLOSE_MA20_CROSS", "ma20_cross"),
        ("BAND_EXIT", "band_exit"),
    ):
        metric = _metric_summary(selected_events, event_type=event_type)
        result[label] = {
            **metric,
            "events_per_100_sessions": _frequency(metric["count"], len(keys)),
        }
    return result


def _duration_summary(
    rows: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    c1_rows = [row for row in rows if row.get("compression_quartile") == "C1"]
    result: dict[str, Any] = {}
    for bucket, predicate in (
        ("D1", lambda value: value == 1),
        ("D2", lambda value: value == 2),
        ("D3", lambda value: value == 3),
        ("D4_PLUS", lambda value: value is not None and value >= 4),
    ):
        group = [
            row
            for row in c1_rows
            if predicate(row.get("compression_duration_sessions"))
        ]
        keys = {(row["stock_code"], row["trade_date"]) for row in group}
        group_events = [
            event
            for event in events
            if (event["stock_code"], event["event_date"]) in keys
        ]
        ma10 = _metric_summary(group_events, event_type="CLOSE_MA10_CROSS")
        ma20 = _metric_summary(group_events, event_type="CLOSE_MA20_CROSS")
        band = _metric_summary(group_events, event_type="BAND_EXIT")
        result[bucket] = {
            "daily_session_count": len(keys),
            "ma10_events_per_100_sessions": _frequency(ma10["count"], len(keys)),
            "ma10_whipsaw_within_5_rate": ma10["opposite_recross_within_5"]["rate"],
            "ma20_same_side_d3_rate": ma20["same_side_d3"]["rate"],
            "band_same_side_d3_rate": band["same_side_d3"]["rate"],
            "ma10_event_count": ma10["count"],
            "ma20_event_count": ma20["count"],
            "band_event_count": band["count"],
        }
    return result


def _build_stock_rows(
    stock_code: str, bars: Sequence[DailyBar]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in canonical)
    points = tuple(calculate_daily_indicators(canonical, calendar))
    clock = list(_clock_series(canonical, points))
    clock_by_date = {row["trade_date"]: row for row in clock}
    closes = [bar.signal.close for bar in canonical]
    sma5 = simple_moving_average(closes, 5)
    sma10 = [point.sma10 for point in points]
    sma20 = [point.sma20 for point in points]
    sma60 = [point.sma60 for point in points]
    atrs = [_atr20(canonical, index) for index in range(len(canonical))]
    highs, lows = _band_series(sma5, sma10, sma20)
    {bar.trade_date: index for index, bar in enumerate(canonical)}
    rows: list[dict[str, Any]] = []
    for index, bar in enumerate(canonical):
        day = bar.trade_date
        if not RESEARCH_START <= day <= RESEARCH_END:
            continue
        speed = clock_by_date[day]
        values = (sma5[index], sma10[index], sma20[index])
        atr = atrs[index]
        cluster = (
            (highs[index] - lows[index]) / atr
            if highs[index] is not None
            and lows[index] is not None
            and atr not in (None, Decimal(0))
            else None
        )
        cluster_60 = (
            (max((*values, sma60[index])) - min((*values, sma60[index]))) / atr
            if all(value is not None for value in (*values, sma60[index]))
            and atr not in (None, Decimal(0))
            else None
        )
        previous_high = highs[index - 1] if index else None
        previous_low = lows[index - 1] if index else None
        up10, down10 = _cross_flags(
            closes[index - 1] if index else None,
            sma10[index - 1] if index else None,
            closes[index],
            sma10[index],
        )
        up20, down20 = _cross_flags(
            closes[index - 1] if index else None,
            sma20[index - 1] if index else None,
            closes[index],
            sma20[index],
        )
        up_band, down_band = _band_exit_flags(
            closes[index - 1] if index else None,
            previous_high,
            previous_low,
            closes[index],
            highs[index],
            lows[index],
        )
        rows.append(
            {
                "stock_code": stock_code,
                "trade_date": day,
                "range_speed": speed.get("range_speed"),
                "range_speed_quartile": speed.get("range_speed_quartile"),
                "efficiency_10": speed.get("efficiency_10"),
                "flow_speed": speed.get("flow_speed"),
                # Required by the existing pooled RANGE_SPEED quartile helper.
                "abs_net_move_atr_10": speed.get("abs_net_move_atr_10"),
                "atr20": atr,
                "sma5": sma5[index],
                "sma10": sma10[index],
                "sma20": sma20[index],
                "sma60": sma60[index],
                "ma_band_high": highs[index],
                "ma_band_low": lows[index],
                "ma_cluster_width_atr": cluster,
                "ma5_10_gap_atr": (
                    abs(sma5[index] - sma10[index]) / atr
                    if sma5[index] is not None
                    and sma10[index] is not None
                    and atr not in (None, Decimal(0))
                    else None
                ),
                "ma10_20_gap_atr": (
                    abs(sma10[index] - sma20[index]) / atr
                    if sma10[index] is not None
                    and sma20[index] is not None
                    and atr not in (None, Decimal(0))
                    else None
                ),
                "ma20_60_gap_atr": (
                    abs(sma20[index] - sma60[index]) / atr
                    if sma20[index] is not None
                    and sma60[index] is not None
                    and atr not in (None, Decimal(0))
                    else None
                ),
                "ma5_60_cluster_width_atr": cluster_60,
                "compression_quartile": None,
                "compression_duration_sessions": None,
                "ma5_ma10_order_flip_count_20": _ordering_flip_count(
                    sma5, sma10, index
                ),
                "ma10_ma20_order_flip_count_20": _ordering_flip_count(
                    sma10, sma20, index
                ),
                "ma5_ma20_order_flip_count_20": _ordering_flip_count(
                    sma5, sma20, index
                ),
                "close_ma10_up_cross": up10,
                "close_ma10_down_cross": down10,
                "close_ma20_up_cross": up20,
                "close_ma20_down_cross": down20,
                "up_band_exit": up_band,
                "down_band_exit": down_band,
                "_index": index,
            }
        )
    return rows, []


def _build_events(
    rows_by_stock: Mapping[str, Sequence[dict[str, Any]]],
    all_bars: Mapping[str, Sequence[DailyBar]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for stock_code, rows in rows_by_stock.items():
        bars = tuple(sorted(all_bars[stock_code], key=lambda bar: bar.trade_date))
        points = tuple(
            calculate_daily_indicators(
                bars, ExplicitTradingCalendar(bar.trade_date for bar in bars)
            )
        )
        closes = [bar.signal.close for bar in bars]
        sma5 = simple_moving_average(closes, 5)
        sma10 = [point.sma10 for point in points]
        sma20 = [point.sma20 for point in points]
        highs, lows = _band_series(sma5, sma10, sma20)
        by_index = {row["_index"]: row for row in rows}
        indexes: dict[str, list[tuple[int, int]]] = {
            "CLOSE_MA10_CROSS": [],
            "CLOSE_MA20_CROSS": [],
            "BAND_EXIT": [],
        }
        flags: dict[int, tuple[bool, bool, bool, bool, bool, bool]] = {}
        for index in range(1, len(bars)):
            up10, down10 = _cross_flags(
                closes[index - 1], sma10[index - 1], closes[index], sma10[index]
            )
            up20, down20 = _cross_flags(
                closes[index - 1], sma20[index - 1], closes[index], sma20[index]
            )
            up_band, down_band = _band_exit_flags(
                closes[index - 1],
                highs[index - 1],
                lows[index - 1],
                closes[index],
                highs[index],
                lows[index],
            )
            flags[index] = (up10, down10, up20, down20, up_band, down_band)
            if up10 or down10:
                indexes["CLOSE_MA10_CROSS"].append((index, 1 if up10 else -1))
            if up20 or down20:
                indexes["CLOSE_MA20_CROSS"].append((index, 1 if up20 else -1))
            if up_band or down_band:
                indexes["BAND_EXIT"].append((index, 1 if up_band else -1))
        for index, row in by_index.items():
            up10, down10, up20, down20, up_band, down_band = flags.get(
                index, (False, False, False, False, False, False)
            )
            if not (up10 or down10 or up20 or down20 or up_band or down_band):
                continue
            day = row["trade_date"]
            if up10 or down10:
                event = _event_at(
                    event_type="CLOSE_MA10_CROSS",
                    stock_code=stock_code,
                    event_date=day,
                    index=index,
                    direction=1 if up10 else -1,
                    closes=closes,
                    reference=sma10,
                    opposite_indexes=indexes,
                    source_pivot_kinds="FULL_DAILY",
                )
                events.append(event)
            if up20 or down20:
                events.append(
                    _event_at(
                        event_type="CLOSE_MA20_CROSS",
                        stock_code=stock_code,
                        event_date=day,
                        index=index,
                        direction=1 if up20 else -1,
                        closes=closes,
                        reference=sma20,
                        opposite_indexes=indexes,
                        source_pivot_kinds="FULL_DAILY",
                    )
                )
            if up_band or down_band:
                event = _event_at(
                    event_type="BAND_EXIT",
                    stock_code=stock_code,
                    event_date=day,
                    index=index,
                    direction=1 if up_band else -1,
                    closes=closes,
                    reference=highs if up_band else lows,
                    opposite_indexes=indexes,
                    source_pivot_kinds="FULL_DAILY",
                )
                _decorate_band_acceleration(event, by_index, index)
                events.append(event)
    return events


def _write_csv(
    path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _write_event_csv(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    fields = (
        "event_type",
        "stock_code",
        "event_date",
        "direction",
        "aligned_return_3_pct",
        "aligned_return_5_pct",
        "aligned_return_10_pct",
        "same_side_d1",
        "same_side_d3",
        "same_side_d5",
        "opposite_recross_within_1",
        "opposite_recross_within_3",
        "opposite_recross_within_5",
    )
    _write_csv(path, fields, events)


def _compare_v01(report: Mapping[str, Any], v01_path: Path) -> dict[str, Any]:
    if not v01_path.exists():
        return {"available": False, "source": v01_path.as_posix()}
    old = json.loads(v01_path.read_text(encoding="utf-8"))
    old_core = old.get("core_comparison", {})
    new_core = report["regimes"]["SLOW_C1"]
    return {
        "available": True,
        "source": v01_path.as_posix(),
        "pivot_sampled_slow_high_compression": {
            "pivot_count": old_core.get("pivot_count"),
            "ma10_event_count": old_core.get("ma10_cross", {}).get("count"),
            "ma20_event_count": old_core.get("ma20_cross", {}).get("count"),
            "band_event_count": old_core.get("band_exit", {}).get("count"),
            "ma10_whipsaw_within_5": old_core.get("ma10_cross", {})
            .get("opposite_recross_within_5", {})
            .get("rate"),
        },
        "full_daily_slow_c1": {
            "daily_session_count": new_core.get("daily_session_count"),
            "ma10_event_count": new_core.get("ma10_cross", {}).get("count"),
            "ma20_event_count": new_core.get("ma20_cross", {}).get("count"),
            "band_event_count": new_core.get("band_exit", {}).get("count"),
            "ma10_whipsaw_within_5": new_core.get("ma10_cross", {})
            .get("opposite_recross_within_5", {})
            .get("rate"),
        },
        "interpretation": "different population units; compare descriptively, not as a strategy result",
    }


def _hypotheses(report: Mapping[str, Any]) -> dict[str, str]:
    compression = report["compression_regimes"]
    c_values = [
        compression[f"C{index}"]["ma10_cross"]["events_per_100_sessions"]
        for index in range(1, 5)
    ]
    c_whips = [
        compression[f"C{index}"]["ma10_cross"]["opposite_recross_within_5"]["rate"]
        for index in range(1, 5)
    ]
    c_freq_pairs = [
        (a, b)
        for a, b in itertools.pairwise(c_values)
        if a is not None and b is not None
    ]
    c_freq_valid = [value for value in c_values if value is not None]
    c_whip_valid = [value for value in c_whips if value is not None]
    h1 = (
        "SUPPORTED"
        if len(c_freq_valid) == 4 and all(a >= b for a, b in c_freq_pairs)
        else "PARTIALLY_SUPPORTED"
        if (len(c_freq_valid) >= 2 and c_freq_valid[0] > c_freq_valid[-1])
        or (len(c_whip_valid) >= 2 and c_whip_valid[0] > c_whip_valid[-1])
        else "NOT_SUPPORTED"
    )
    duration = report["duration"]
    rates = [
        duration[key]["ma10_whipsaw_within_5_rate"]
        for key in ("D1", "D2", "D3", "D4_PLUS")
        if duration[key]["ma10_whipsaw_within_5_rate"] is not None
    ]
    h2 = (
        "SUPPORTED"
        if len(rates) >= 2 and all(a <= b for a, b in itertools.pairwise(rates))
        else "PARTIALLY_SUPPORTED"
        if len(rates) >= 2 and rates[-1] > rates[0]
        else "INCONCLUSIVE"
    )
    core = report["regimes"]["SLOW_C1"]
    ma10_d3 = core["ma10_cross"]["same_side_d3"]["rate"]
    ma20_d3 = core["ma20_cross"]["same_side_d3"]["rate"]
    band_d3 = core["band_exit"]["same_side_d3"]["rate"]
    ma10_whip = core["ma10_cross"]["opposite_recross_within_5"]["rate"]
    ma20_whip = core["ma20_cross"]["opposite_recross_within_5"]["rate"]
    band_whip = core["band_exit"]["opposite_recross_within_5"]["rate"]
    h3 = (
        "SUPPORTED"
        if ma20_d3 is not None
        and ma10_d3 is not None
        and ma20_d3 > ma10_d3
        and ma20_whip is not None
        and ma10_whip is not None
        and ma20_whip < ma10_whip
        else "PARTIALLY_SUPPORTED"
        if ma20_d3 is not None
        and ma10_d3 is not None
        and ma20_d3 > ma10_d3
        or ma20_whip is not None
        and ma10_whip is not None
        and ma20_whip < ma10_whip
        else "NOT_SUPPORTED"
    )
    h4 = (
        "SUPPORTED"
        if band_d3 is not None
        and ma20_d3 is not None
        and band_d3 > ma20_d3
        and band_whip is not None
        and ma20_whip is not None
        and band_whip < ma20_whip
        else "PARTIALLY_SUPPORTED"
        if band_d3 is not None
        and ma20_d3 is not None
        and band_d3 > ma20_d3
        or band_whip is not None
        and ma20_whip is not None
        and band_whip < ma20_whip
        else "NOT_SUPPORTED"
    )
    outcome = report["band_exit_outcomes"]
    good = outcome.get("GOOD_DIRECTIONAL", {})
    failed = outcome.get("FAILED", {})
    acceleration_fields = (
        "range_speed",
        "efficiency_10",
        "flow_speed",
        "ma_cluster_width_atr",
    )
    better = 0
    available = 0
    for field in acceleration_fields:
        good_delta = good.get(f"post_minus_pre_{field}")
        failed_delta = failed.get(f"post_minus_pre_{field}")
        if good_delta is not None and failed_delta is not None:
            available += 1
            better += good_delta > failed_delta
    h5 = (
        "SUPPORTED"
        if available and better == available
        else "PARTIALLY_SUPPORTED"
        if better
        else "NOT_SUPPORTED"
    )
    return {"H1": h1, "H2": h2, "H3": h3, "H4": h4, "H5": h5}


def run_market_clock_compression_audit_v0_2(
    *,
    output: Path = OUTPUT_PATH,
    summary_csv: Path = SUMMARY_CSV_PATH,
    event_csv: Path = EVENT_CSV_PATH,
    v01_path: Path = V01_OUTPUT_PATH,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    all_bars: dict[str, tuple[DailyBar, ...]] = {}
    rows_by_stock: dict[str, list[dict[str, Any]]] = {}
    for stock_code in sorted(stocks):
        bars, _ = _load_stock(stock_code)
        all_bars[stock_code] = tuple(sorted(bars, key=lambda bar: bar.trade_date))
        rows, _ = _build_stock_rows(stock_code, all_bars[stock_code])
        rows_by_stock[stock_code] = rows
    all_rows = [row for rows in rows_by_stock.values() for row in rows]
    all_rows.sort(key=lambda row: (row["stock_code"], row["trade_date"]))
    # Existing V0.1 uses pooled observation quartiles.  Recreate the
    # RANGE_SPEED quartile over all full-daily rows before compression.
    _assign_quartile(all_rows, "range_speed", "range_speed_quartile")
    valid_compression = [row for row in all_rows if _valid_compression(row)]
    _assign_quartile(all_rows, "ma_cluster_width_atr", "compression_quartile")
    for row in all_rows:
        quartile = row.get("compression_quartile")
        row["compression_quartile"] = (
            f"C{quartile[1:]}" if isinstance(quartile, str) else None
        )
    _compression_duration(all_rows)
    events = _build_events(rows_by_stock, all_bars)
    events.sort(
        key=lambda event: (
            event["stock_code"],
            event["event_date"],
            event["event_type"],
        )
    )
    event_keys = [
        (event["stock_code"], event["event_date"], event["event_type"])
        for event in events
    ]
    if len(event_keys) != len(set(event_keys)):
        raise ValueError("duplicate full-daily event key")
    valid_rows = [
        row for row in all_rows if _valid_speed(row) and _valid_compression(row)
    ]
    regimes = {
        "ALL_VALID": _regime_event_summary(valid_rows, events, lambda row: True),
        "SLOW": _regime_event_summary(
            valid_rows, events, lambda row: row.get("range_speed_quartile") == "Q1"
        ),
    }
    compression_regimes: dict[str, Any] = {}
    for index in range(1, 5):
        compression_regimes[f"C{index}"] = _regime_event_summary(
            valid_rows,
            events,
            lambda row, q=f"C{index}": row.get("compression_quartile") == q,
        )
    regimes.update(
        {
            "SLOW_C1": _regime_event_summary(
                valid_rows,
                events,
                lambda row: (
                    row.get("range_speed_quartile") == "Q1"
                    and row.get("compression_quartile") == "C1"
                ),
            ),
            "SLOW_C4": _regime_event_summary(
                valid_rows,
                events,
                lambda row: (
                    row.get("range_speed_quartile") == "Q1"
                    and row.get("compression_quartile") == "C4"
                ),
            ),
        }
    )
    report: dict[str, Any] = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "research_period": {"start": RESEARCH_START, "end": RESEARCH_END},
        "source_v01": v01_path.as_posix(),
        "population": {
            "stock_count": len(stocks),
            "daily_session_count": len(all_rows),
            "expected_daily_session_count": 7260,
            "valid_speed_and_compression_count": len(valid_rows),
            "compression_valid_count": len(valid_compression),
            "event_count": len(events),
            "event_type_counts": {
                event_type: sum(event["event_type"] == event_type for event in events)
                for event_type in ("CLOSE_MA10_CROSS", "CLOSE_MA20_CROSS", "BAND_EXIT")
            },
        },
        "methodology": {
            "population_unit": "all research-period Daily sessions; no pivot filter",
            "warmup": "prior cached history may be used only to compute trailing indicators",
            "signal_price_basis": "adjusted Daily OHLC / signal close",
            "slow_bucket": "existing RANGE_SPEED Q1",
            "compression_quartiles": "full valid Daily population; C1=narrowest, C4=widest",
            "recent_window": "current plus previous 19 sessions",
            "ordering_flip": "strict sign reversal; ties ignored",
            "cross_semantics": "prev Close <= prev SMA and current Close > SMA; inverse DOWN",
            "band_exit_semantics": "prev Close <= prev band high and current Close > current band high; inverse DOWN",
            "frequency_denominator": "unique valid stock/date sessions in each regime",
            "duration": "consecutive C1 sessions per stock; D4_PLUS means four or more",
            "market_clock_offsets": [-5, -3, 0, 1, 3, 5],
            "band_outcome": "GOOD_DIRECTIONAL iff aligned_return_10_pct > 0; otherwise FAILED",
            "report_only": True,
            "strategy_signals": False,
            "pnl": False,
        },
        "rows": all_rows,
        "events": events,
        "regimes": regimes,
        "compression_regimes": compression_regimes,
        "duration": _duration_summary(all_rows, events),
        "band_exit_outcomes": _event_outcome_groups(events),
        "ma60_cluster": {
            "all_valid_median": _median(
                row["ma5_60_cluster_width_atr"]
                for row in valid_rows
                if row.get("ma5_60_cluster_width_atr") is not None
            ),
            "slow_median": _median(
                row["ma5_60_cluster_width_atr"]
                for row in valid_rows
                if row.get("range_speed_quartile") == "Q1"
                and row.get("ma5_60_cluster_width_atr") is not None
            ),
        },
    }
    report["pivot_sample_comparison"] = _compare_v01(report, v01_path)
    report["hypotheses"] = _hypotheses(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_csv(summary_csv, SUMMARY_FIELDS, all_rows)
    _write_event_csv(event_csv, events)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--summary-csv", type=Path, default=SUMMARY_CSV_PATH)
    parser.add_argument("--event-csv", type=Path, default=EVENT_CSV_PATH)
    parser.add_argument("--v01", type=Path, default=V01_OUTPUT_PATH)
    args = parser.parse_args()
    report = run_market_clock_compression_audit_v0_2(
        output=args.output,
        summary_csv=args.summary_csv,
        event_csv=args.event_csv,
        v01_path=args.v01,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "daily_sessions": report["population"]["daily_session_count"],
                "valid_rows": report["population"]["valid_speed_and_compression_count"],
                "events": report["population"]["event_count"],
                "hypotheses": report["hypotheses"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
