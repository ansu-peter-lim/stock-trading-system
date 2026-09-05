"""Report-only MARKET_CLOCK MA compression and breakout audit.

The audit consumes the existing V0.2 pivot CSV and cached adjusted DailyBars.
It creates descriptive observations only: no strategy signal, order, fill, or
PnL is produced.  Speed buckets are inherited from V0.2; the compression
quartile is descriptive and is not a strategy threshold.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from itertools import pairwise
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
from .market_clock_audit import _atr20, _quartile

PROOF_VERSION = "MARKET_CLOCK_MA_COMPRESSION_BREAKOUT_AUDIT_V0_1"
SOURCE_V02_CSV = Path(
    "data/processed/strategy_review/market_clock_ma_reaction_role_audit_v0_2.csv"
)
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_compression_breakout_audit_v0_1.json"
)
SUMMARY_CSV_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_compression_breakout_audit_v0_1.csv"
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
PAIR_NAMES = ("MA5_MA10", "MA10_MA20", "MA5_MA20")
WINDOW_SESSIONS = 20
FOLLOW_THROUGH_HORIZONS = (3, 5, 10)
PERSISTENCE_HORIZONS = (1, 3, 5)

SUMMARY_FIELDS = (
    "stock_code",
    "pivot_kind",
    "pivot_trade_date",
    "range_speed_quartile",
    "compression_quartile",
    "ma_band_high",
    "ma_band_low",
    "ma_cluster_width_atr",
    "ma5_10_gap_atr",
    "ma10_20_gap_atr",
    "ma20_60_gap_atr",
    "ma5_60_cluster_width_atr",
    "ma5_ma10_order_flip_count_20",
    "ma10_ma20_order_flip_count_20",
    "ma5_ma20_order_flip_count_20",
    "ma5_10_gap_abs_atr",
    "ma10_20_gap_abs_atr",
    "ma20_60_gap_abs_atr",
    "close_ma10_cross_count_20",
    "close_ma10_up_cross_count_20",
    "close_ma10_down_cross_count_20",
    "close_ma20_cross_count_20",
    "close_ma20_up_cross_count_20",
    "close_ma20_down_cross_count_20",
    "up_band_exit",
    "down_band_exit",
)


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


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


def _parse_v02_rows(path: Path) -> tuple[dict[str, Any], ...]:
    with path.open(encoding="utf-8", newline="") as stream:
        source_rows = list(csv.DictReader(stream))
    rows: list[dict[str, Any]] = []
    decimal_fields = {
        "range_speed",
        "net_move_atr_10",
        "efficiency_10",
        "flow_speed",
    }
    for source in source_rows:
        row = dict(source)
        row["pivot_trade_date"] = date.fromisoformat(row["pivot_trade_date"])
        for field in decimal_fields:
            row[field] = _decimal(row.get(field))
        rows.append(row)
    return tuple(rows)


def _speed_valid(row: Mapping[str, Any]) -> bool:
    return all(
        row.get(field) is not None
        for field in (
            "range_speed",
            "net_move_atr_10",
            "efficiency_10",
            "flow_speed",
        )
    )


def _relation(first: Decimal | None, second: Decimal | None) -> int | None:
    if first is None or second is None:
        return None
    return (first > second) - (first < second)


def _ordering_flip_count(
    first: Sequence[Decimal | None],
    second: Sequence[Decimal | None],
    index: int,
    window: int = WINDOW_SESSIONS,
) -> int:
    """Count strict positive↔negative ordering changes in trailing sessions.

    Ties and missing values do not create a flip.  The window contains the
    current session and at most the previous ``window - 1`` sessions.
    """

    start = max(0, index - window + 1)
    relations = [
        _relation(first[item], second[item]) for item in range(start, index + 1)
    ]
    nonzero = [value for value in relations if value in (-1, 1)]
    return sum(previous != current for previous, current in pairwise(nonzero))


def _cross_flags(
    previous_close: Decimal | None,
    previous_ma: Decimal | None,
    close: Decimal | None,
    ma: Decimal | None,
) -> tuple[bool, bool]:
    """Return exact (up, down) close/MA cross flags."""

    if None in (previous_close, previous_ma, close, ma):
        return False, False
    return (
        previous_close <= previous_ma and close > ma,
        previous_close >= previous_ma and close < ma,
    )


def _band_exit_flags(
    previous_close: Decimal | None,
    previous_high: Decimal | None,
    previous_low: Decimal | None,
    close: Decimal | None,
    band_high: Decimal | None,
    band_low: Decimal | None,
) -> tuple[bool, bool]:
    if None in (
        previous_close,
        previous_high,
        previous_low,
        close,
        band_high,
        band_low,
    ):
        return False, False
    return (
        previous_close <= previous_high and close > band_high,
        previous_close >= previous_low and close < band_low,
    )


def _follow_return(
    closes: Sequence[Decimal], index: int, direction: int, horizon: int
) -> Decimal | None:
    target = index + horizon
    if target >= len(closes):
        return None
    return Decimal(direction) * (closes[target] / closes[index] - Decimal(1)) * 100


def _same_side(
    closes: Sequence[Decimal],
    references: Sequence[Decimal | None],
    index: int,
    direction: int,
    horizon: int,
) -> bool | None:
    target = index + horizon
    if target >= len(closes) or references[target] is None:
        return None
    if direction > 0:
        return closes[target] > references[target]
    return closes[target] < references[target]


def _event_at(
    *,
    event_type: str,
    stock_code: str,
    event_date: date,
    index: int,
    direction: int,
    closes: Sequence[Decimal],
    reference: Sequence[Decimal | None],
    opposite_indexes: Mapping[str, Sequence[tuple[int, int]]],
    source_pivot_kinds: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_type": event_type,
        "stock_code": stock_code,
        "event_date": event_date,
        "direction": direction,
        "source_pivot_kinds": source_pivot_kinds,
        "aligned_return_3_pct": _follow_return(closes, index, direction, 3),
        "aligned_return_5_pct": _follow_return(closes, index, direction, 5),
        "aligned_return_10_pct": _follow_return(closes, index, direction, 10),
    }
    for horizon in PERSISTENCE_HORIZONS:
        row[f"same_side_d{horizon}"] = _same_side(
            closes, reference, index, direction, horizon
        )
        row[f"opposite_recross_within_{horizon}"] = any(
            index < candidate <= index + horizon and candidate_direction == -direction
            for candidate, candidate_direction in opposite_indexes.get(event_type, ())
        )
    return row


def _median(values: Iterable[Decimal]) -> Decimal | None:
    values_list = list(values)
    return median(values_list) if values_list else None


def _metric_summary(
    events: Sequence[Mapping[str, Any]],
    *,
    event_type: str,
) -> dict[str, Any]:
    selected = [row for row in events if row["event_type"] == event_type]
    result: dict[str, Any] = {
        "count": len(selected),
        "up_count": sum(row["direction"] > 0 for row in selected),
        "down_count": sum(row["direction"] < 0 for row in selected),
    }
    for field in (
        "same_side_d1",
        "same_side_d3",
        "same_side_d5",
        "opposite_recross_within_1",
        "opposite_recross_within_3",
        "opposite_recross_within_5",
    ):
        values = [row[field] for row in selected if row[field] is not None]
        result[field] = {
            "count": len(values),
            "true_count": sum(values),
            "rate": (Decimal(sum(values)) / Decimal(len(values)) if values else None),
        }
    for horizon in FOLLOW_THROUGH_HORIZONS:
        field = f"aligned_return_{horizon}_pct"
        result[field] = {
            "count": sum(row[field] is not None for row in selected),
            "median": _median(row[field] for row in selected if row[field] is not None),
        }
    return result


def _row_churn_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordering_fields = (
        "ma5_ma10_order_flip_count_20",
        "ma10_ma20_order_flip_count_20",
        "ma5_ma20_order_flip_count_20",
    )
    result: dict[str, Any] = {
        "ordering_flip": {
            field: {
                "count": sum(row.get(field) is not None for row in rows),
                "median": _median(
                    Decimal(row[field]) for row in rows if row.get(field) is not None
                ),
                "total": sum(row[field] for row in rows if row.get(field) is not None),
            }
            for field in ordering_fields
        },
        "price_cross_churn": {},
    }
    for name in ("ma10", "ma20"):
        prefix = f"close_{name}"
        result["price_cross_churn"][name.upper()] = {
            "count": sum(row.get(f"{prefix}_cross_count_20", 0) for row in rows),
            "median_count_per_pivot": _median(
                Decimal(row[f"{prefix}_cross_count_20"]) for row in rows
            ),
            "up_count": sum(row.get(f"{prefix}_up_cross_count_20", 0) for row in rows),
            "down_count": sum(
                row.get(f"{prefix}_down_cross_count_20", 0) for row in rows
            ),
        }
    return result


def _group_summary(
    rows: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    pivot_keys = {(row["stock_code"], row["pivot_trade_date"]) for row in rows}
    group_events = [
        row for row in events if (row["stock_code"], row["event_date"]) in pivot_keys
    ]
    return {
        "pivot_count": len(rows),
        "unique_stock_date_count": len(pivot_keys),
        **_row_churn_summary(rows),
        "ma10_cross": _metric_summary(group_events, event_type="CLOSE_MA10_CROSS"),
        "ma20_cross": _metric_summary(group_events, event_type="CLOSE_MA20_CROSS"),
        "band_exit": _metric_summary(group_events, event_type="BAND_EXIT"),
    }


def _build_stock_rows(
    stock_code: str,
    bars: Sequence[DailyBar],
    v02_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in canonical)
    points = tuple(calculate_daily_indicators(canonical, calendar))
    sma5 = simple_moving_average([bar.signal.close for bar in canonical], 5)
    sma10 = [point.sma10 for point in points]
    sma20 = [point.sma20 for point in points]
    sma60 = [point.sma60 for point in points]
    closes = [bar.signal.close for bar in canonical]
    atrs = [_atr20(canonical, index) for index in range(len(canonical))]
    date_to_index = {bar.trade_date: index for index, bar in enumerate(canonical)}
    source_rows = [row for row in v02_rows if row["stock_code"] == stock_code]
    source_rows = [row for row in source_rows if _speed_valid(row)]

    # One observation per V0.2 pivot row.  Duplicate stock/date LOW+HIGH rows
    # remain visible, while event records are de-duplicated by stock/date/type.
    prepared: list[dict[str, Any]] = []
    event_inputs: dict[tuple[str, date], set[str]] = {}
    for source in source_rows:
        day = source["pivot_trade_date"]
        if day not in date_to_index:
            continue
        index = date_to_index[day]
        values = [sma5[index], sma10[index], sma20[index]]
        if any(value is None for value in values) or atrs[index] in (None, Decimal(0)):
            continue
        band_high = max(value for value in values if value is not None)
        band_low = min(value for value in values if value is not None)
        atr = atrs[index]
        assert atr is not None
        compression = (band_high - band_low) / atr
        cluster_60 = (
            (max((*values, sma60[index])) - min((*values, sma60[index]))) / atr
            if sma60[index] is not None
            else None
        )
        previous_values = (
            sma5[index - 1] if index else None,
            sma10[index - 1] if index else None,
            sma20[index - 1] if index else None,
            sma60[index - 1] if index else None,
        )
        previous_band_high = (
            max(value for value in previous_values[:3] if value is not None)
            if all(value is not None for value in previous_values[:3])
            else None
        )
        previous_band_low = (
            min(value for value in previous_values[:3] if value is not None)
            if all(value is not None for value in previous_values[:3])
            else None
        )
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
            previous_band_high,
            previous_band_low,
            closes[index],
            band_high,
            band_low,
        )
        prepared.append(
            {
                "stock_code": stock_code,
                "pivot_kind": source["pivot_kind"],
                "pivot_trade_date": day,
                "range_speed_quartile": source["range_speed_quartile"],
                "ma_band_high": band_high,
                "ma_band_low": band_low,
                "ma_cluster_width_atr": compression,
                "ma5_10_gap_atr": (sma5[index] - sma10[index]) / atr,
                "ma10_20_gap_atr": (sma10[index] - sma20[index]) / atr,
                "ma20_60_gap_atr": (
                    (sma20[index] - sma60[index]) / atr
                    if sma60[index] is not None
                    else None
                ),
                "ma5_60_cluster_width_atr": cluster_60,
                "ma5_10_gap_abs_atr": abs(sma5[index] - sma10[index]) / atr,
                "ma10_20_gap_abs_atr": abs(sma10[index] - sma20[index]) / atr,
                "ma20_60_gap_abs_atr": (
                    abs(sma20[index] - sma60[index]) / atr
                    if sma60[index] is not None
                    else None
                ),
                "ma5_ma10_order_flip_count_20": _ordering_flip_count(
                    sma5, sma10, index
                ),
                "ma10_ma20_order_flip_count_20": _ordering_flip_count(
                    sma10, sma20, index
                ),
                "ma5_ma20_order_flip_count_20": _ordering_flip_count(
                    sma5, sma20, index
                ),
                "close_ma10_cross_count_20": 0,
                "close_ma10_up_cross_count_20": 0,
                "close_ma10_down_cross_count_20": 0,
                "close_ma20_cross_count_20": 0,
                "close_ma20_up_cross_count_20": 0,
                "close_ma20_down_cross_count_20": 0,
                "up_band_exit": up_band,
                "down_band_exit": down_band,
            }
        )
        event_names = set()
        if up10 or down10:
            event_names.add("CLOSE_MA10_CROSS")
        if up20 or down20:
            event_names.add("CLOSE_MA20_CROSS")
        if up_band or down_band:
            event_names.add("BAND_EXIT")
        event_inputs.setdefault((stock_code, day), set()).update(event_names)

    # Compression quartiles are descriptive and are calculated within SLOW
    # (the existing V0.2 RANGE_SPEED Q1 bucket) only.
    slow_widths = [
        row["ma_cluster_width_atr"]
        for row in prepared
        if row["range_speed_quartile"] == "Q1"
    ]
    for row in prepared:
        row["compression_quartile"] = (
            _quartile(row["ma_cluster_width_atr"], slow_widths)
            if row["range_speed_quartile"] == "Q1"
            else None
        )

    # Build all daily cross indexes so persistence and opposite recrosses use
    # the complete historical series, not only pivot dates.
    indexes: dict[str, list[tuple[int, int]]] = {
        "CLOSE_MA10_CROSS": [],
        "CLOSE_MA20_CROSS": [],
        "BAND_EXIT": [],
    }
    daily_flags: dict[int, tuple[bool, bool, bool, bool, bool, bool]] = {}
    band_high_series = [
        max(value for value in (sma5[i], sma10[i], sma20[i]) if value is not None)
        if all(value is not None for value in (sma5[i], sma10[i], sma20[i]))
        else None
        for i in range(len(canonical))
    ]
    band_low_series = [
        min(value for value in (sma5[i], sma10[i], sma20[i]) if value is not None)
        if all(value is not None for value in (sma5[i], sma10[i], sma20[i]))
        else None
        for i in range(len(canonical))
    ]
    for index, day in enumerate(bar.trade_date for bar in canonical):
        if index == 0:
            continue
        values = [sma5[index], sma10[index], sma20[index]]
        if any(value is None for value in values):
            continue
        band_high = max(value for value in values if value is not None)
        band_low = min(value for value in values if value is not None)
        previous = [sma5[index - 1], sma10[index - 1], sma20[index - 1]]
        if any(value is None for value in previous):
            continue
        previous_high = max(value for value in previous if value is not None)
        previous_low = min(value for value in previous if value is not None)
        up10, down10 = _cross_flags(
            closes[index - 1], sma10[index - 1], closes[index], sma10[index]
        )
        up20, down20 = _cross_flags(
            closes[index - 1], sma20[index - 1], closes[index], sma20[index]
        )
        up_band, down_band = _band_exit_flags(
            closes[index - 1],
            previous_high,
            previous_low,
            closes[index],
            band_high,
            band_low,
        )
        if up10 or down10:
            indexes["CLOSE_MA10_CROSS"].append((index, 1 if up10 else -1))
        if up20 or down20:
            indexes["CLOSE_MA20_CROSS"].append((index, 1 if up20 else -1))
        if up_band or down_band:
            indexes["BAND_EXIT"].append((index, 1 if up_band else -1))
        daily_flags[index] = (up10, down10, up20, down20, up_band, down_band)

    events: list[dict[str, Any]] = []
    for (event_stock, day), pivot_event_names in sorted(event_inputs.items()):
        if event_stock != stock_code or not pivot_event_names:
            continue
        index = date_to_index.get(day)
        if index is None or index not in daily_flags:
            continue
        up10, down10, up20, down20, up_band, down_band = daily_flags[index]
        if not pivot_event_names:
            continue
        source_kinds = ",".join(
            sorted(
                row["pivot_kind"]
                for row in source_rows
                if row["pivot_trade_date"] == day
            )
        )
        if "CLOSE_MA10_CROSS" in pivot_event_names:
            direction = 1 if up10 else -1
            events.append(
                _event_at(
                    event_type="CLOSE_MA10_CROSS",
                    stock_code=stock_code,
                    event_date=day,
                    index=index,
                    direction=direction,
                    closes=closes,
                    reference=sma10,
                    opposite_indexes=indexes,
                    source_pivot_kinds=source_kinds,
                )
            )
        if "CLOSE_MA20_CROSS" in pivot_event_names:
            direction = 1 if up20 else -1
            events.append(
                _event_at(
                    event_type="CLOSE_MA20_CROSS",
                    stock_code=stock_code,
                    event_date=day,
                    index=index,
                    direction=direction,
                    closes=closes,
                    reference=sma20,
                    opposite_indexes=indexes,
                    source_pivot_kinds=source_kinds,
                )
            )
        if "BAND_EXIT" in pivot_event_names:
            direction = 1 if up_band else -1
            reference = band_high_series if direction > 0 else band_low_series
            events.append(
                _event_at(
                    event_type="BAND_EXIT",
                    stock_code=stock_code,
                    event_date=day,
                    index=index,
                    direction=direction,
                    closes=closes,
                    reference=reference,
                    opposite_indexes=indexes,
                    source_pivot_kinds=source_kinds,
                )
            )

    # Fill trailing cross counts from the complete daily event indexes.
    for row in prepared:
        index = date_to_index[row["pivot_trade_date"]]
        start = max(0, index - WINDOW_SESSIONS + 1)
        row["close_ma10_up_cross_count_20"] = sum(
            _cross_flags(closes[item - 1], sma10[item - 1], closes[item], sma10[item])[
                0
            ]
            for item, _direction in indexes["CLOSE_MA10_CROSS"]
            if start <= item <= index
        )
        row["close_ma10_down_cross_count_20"] = sum(
            _cross_flags(closes[item - 1], sma10[item - 1], closes[item], sma10[item])[
                1
            ]
            for item, _direction in indexes["CLOSE_MA10_CROSS"]
            if start <= item <= index
        )
        row["close_ma10_cross_count_20"] = (
            row["close_ma10_up_cross_count_20"] + row["close_ma10_down_cross_count_20"]
        )
        row["close_ma20_up_cross_count_20"] = sum(
            _cross_flags(closes[item - 1], sma20[item - 1], closes[item], sma20[item])[
                0
            ]
            for item, _direction in indexes["CLOSE_MA20_CROSS"]
            if start <= item <= index
        )
        row["close_ma20_down_cross_count_20"] = sum(
            _cross_flags(closes[item - 1], sma20[item - 1], closes[item], sma20[item])[
                1
            ]
            for item, _direction in indexes["CLOSE_MA20_CROSS"]
            if start <= item <= index
        )
        row["close_ma20_cross_count_20"] = (
            row["close_ma20_up_cross_count_20"] + row["close_ma20_down_cross_count_20"]
        )
    return prepared, events


def _write_summary_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {field: _csv_value(row.get(field)) for field in SUMMARY_FIELDS}
            )


def _group_events(
    rows: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    predicate: Any,
) -> list[dict[str, Any]]:
    selected = [row for row in rows if predicate(row)]
    keys = {(row["stock_code"], row["pivot_trade_date"]) for row in selected}
    return [
        dict(event)
        for event in events
        if (event["stock_code"], event["event_date"]) in keys
    ]


def _compression_summary(
    rows: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    slow_rows = [row for row in rows if row["range_speed_quartile"] == "Q1"]
    result: dict[str, Any] = {}
    for quartile in ("Q1", "Q2", "Q3", "Q4"):
        group = [row for row in slow_rows if row["compression_quartile"] == quartile]
        result[quartile] = {
            "pivot_count": len(group),
            "unique_stock_date_count": len(
                {(row["stock_code"], row["pivot_trade_date"]) for row in group}
            ),
            **_row_churn_summary(group),
            "ma_cluster_width_atr_median": _median(
                row["ma_cluster_width_atr"] for row in group
            ),
            "ma10_cross_count": len(
                _group_events(
                    group,
                    [
                        event
                        for event in events
                        if event["event_type"] == "CLOSE_MA10_CROSS"
                    ],
                    lambda _: True,
                )
            ),
            "ma20_cross_count": len(
                _group_events(
                    group,
                    [
                        event
                        for event in events
                        if event["event_type"] == "CLOSE_MA20_CROSS"
                    ],
                    lambda _: True,
                )
            ),
            "band_exit_count": len(
                _group_events(
                    group,
                    [event for event in events if event["event_type"] == "BAND_EXIT"],
                    lambda _: True,
                )
            ),
        }
        group_events = _group_events(group, events, lambda _: True)
        for event_type, label in (
            ("CLOSE_MA10_CROSS", "ma10_cross"),
            ("CLOSE_MA20_CROSS", "ma20_cross"),
            ("BAND_EXIT", "band_exit"),
        ):
            metric = _metric_summary(group_events, event_type=event_type)
            result[quartile][f"{label}_whipsaw_rate"] = metric[
                "opposite_recross_within_5"
            ]["rate"]
            result[quartile][f"{label}_same_side_d3_rate"] = metric["same_side_d3"][
                "rate"
            ]
            for horizon in FOLLOW_THROUGH_HORIZONS:
                result[quartile][f"{label}_aligned_return_{horizon}_median_pct"] = (
                    metric[f"aligned_return_{horizon}_pct"]["median"]
                )
    return result


def _core_comparison(
    rows: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    selected = [
        row
        for row in rows
        if row["range_speed_quartile"] == "Q1" and row["compression_quartile"] == "Q1"
    ]
    event_rows = _group_events(selected, events, lambda _: True)
    return {
        "pivot_count": len(selected),
        "unique_stock_date_count": len(
            {(row["stock_code"], row["pivot_trade_date"]) for row in selected}
        ),
        **_row_churn_summary(selected),
        "ma10_cross": _metric_summary(event_rows, event_type="CLOSE_MA10_CROSS"),
        "ma20_cross": _metric_summary(event_rows, event_type="CLOSE_MA20_CROSS"),
        "band_exit": _metric_summary(event_rows, event_type="BAND_EXIT"),
    }


def _hypothesis_judgment(report: Mapping[str, Any]) -> dict[str, str]:
    core = report["core_comparison"]
    ma10 = core["ma10_cross"]
    ma20 = core["ma20_cross"]
    band = core["band_exit"]
    # Descriptive, fixed rules: no statistical or strategy threshold.
    h1 = (
        "SUPPORTED"
        if ma10["count"] > 0
        and ma10["opposite_recross_within_5"]["rate"] is not None
        and ma10["opposite_recross_within_5"]["rate"] >= Decimal("0.5")
        else "PARTIALLY_SUPPORTED"
        if ma10["count"] > 0
        else "INCONCLUSIVE"
    )
    h2 = (
        "SUPPORTED"
        if ma20["count"] > ma10["count"]
        and ma20["same_side_d3"]["rate"] is not None
        and ma10["same_side_d3"]["rate"] is not None
        and ma20["same_side_d3"]["rate"] > ma10["same_side_d3"]["rate"]
        else "PARTIALLY_SUPPORTED"
        if ma20["count"] > 0
        else "INCONCLUSIVE"
    )
    h3 = (
        "SUPPORTED"
        if band["count"] > 0
        and band["same_side_d3"]["rate"] is not None
        and ma10["same_side_d3"]["rate"] is not None
        and band["same_side_d3"]["rate"] > ma10["same_side_d3"]["rate"]
        else "PARTIALLY_SUPPORTED"
        if band["count"] > 0
        else "INCONCLUSIVE"
    )
    return {"H1": h1, "H2": h2, "H3": h3}


def run_market_clock_compression_audit(
    *,
    output: Path = OUTPUT_PATH,
    summary_csv: Path = SUMMARY_CSV_PATH,
    source_v02_csv: Path = SOURCE_V02_CSV,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    source_rows = _parse_v02_rows(source_v02_csv)
    valid_source = [row for row in source_rows if _speed_valid(row)]
    all_rows: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    for stock_code in sorted(stocks):
        bars, _ = _load_stock(stock_code)
        rows, events = _build_stock_rows(stock_code, bars, valid_source)
        all_rows.extend(rows)
        all_events.extend(events)
    all_rows.sort(
        key=lambda row: (row["stock_code"], row["pivot_trade_date"], row["pivot_kind"])
    )
    all_events.sort(
        key=lambda row: (row["stock_code"], row["event_date"], row["event_type"])
    )
    # A pivot date can have both LOW and HIGH rows.  Event records are already
    # date/type unique; this is an explicit audit invariant.
    event_keys = [
        (row["stock_code"], row["event_date"], row["event_type"]) for row in all_events
    ]
    if len(event_keys) != len(set(event_keys)):
        raise ValueError("duplicate event key in compression audit")
    report: dict[str, Any] = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "source_v02_csv": source_v02_csv.as_posix(),
        "population": {
            "source_pivot_rows": len(source_rows),
            "valid_speed_pivot_rows": len(valid_source),
            "invalid_speed_pivot_rows": len(source_rows) - len(valid_source),
            "output_pivot_rows": len(all_rows),
            "output_unique_stock_date_rows": len(
                {(row["stock_code"], row["pivot_trade_date"]) for row in all_rows}
            ),
            "event_rows": len(all_events),
        },
        "methodology": {
            "signal_price_basis": "V0.2 adjusted Daily OHLC / signal close",
            "speed_bucket": "inherited V0.2 range_speed_quartile; SLOW=Q1",
            "compression_quartile": "within SLOW only; Q1=narrowest/highest compression",
            "recent_window": "current session plus previous 19 sessions",
            "ordering_flip": "strict positive-to-negative or negative-to-positive; ties ignored",
            "ma_gaps": "*_gap_atr is signed first-minus-second; *_gap_abs_atr is magnitude",
            "cross_semantics": "prev close <= prev MA and close > MA (UP); inverse DOWN",
            "band_exit_semantics": "prev close <= prev band high and close > current band high; inverse DOWN",
            "follow_through": "direction * (Close[T+k]/Close[T]-1), percentage; report-only",
            "event_deduplication": "one stock/date/event_type despite LOW+HIGH pivot overlap",
            "strategy_changes": False,
            "buy_sell_signals": False,
            "pnl": False,
        },
        "rows": all_rows,
        "events": all_events,
        "slow_compression": _compression_summary(all_rows, all_events),
        "core_comparison": _core_comparison(all_rows, all_events),
        "ma60_cluster": {
            "rows_with_ma5_60_cluster": sum(
                row["ma5_60_cluster_width_atr"] is not None for row in all_rows
            ),
            "median": _median(
                row["ma5_60_cluster_width_atr"]
                for row in all_rows
                if row["ma5_60_cluster_width_atr"] is not None
            ),
            "slow_median": _median(
                row["ma5_60_cluster_width_atr"]
                for row in all_rows
                if row["range_speed_quartile"] == "Q1"
                and row["ma5_60_cluster_width_atr"] is not None
            ),
        },
    }
    report["hypotheses"] = _hypothesis_judgment(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_summary_csv(summary_csv, all_rows)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--summary-csv", type=Path, default=SUMMARY_CSV_PATH)
    parser.add_argument("--source-v02-csv", type=Path, default=SOURCE_V02_CSV)
    args = parser.parse_args()
    report = run_market_clock_compression_audit(
        output=args.output,
        summary_csv=args.summary_csv,
        source_v02_csv=args.source_v02_csv,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "valid_speed_pivots": report["population"]["valid_speed_pivot_rows"],
                "events": report["population"]["event_rows"],
                "hypotheses": report["hypotheses"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
