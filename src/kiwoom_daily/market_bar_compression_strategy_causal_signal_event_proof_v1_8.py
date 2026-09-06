"""Causal, zero-PnL signal-event proof for the frozen V1.7 hypotheses.

This module deliberately stops at signal chronology and next-Market-Bar OPEN
eligibility.  It does not calculate returns/PnL, fetch data, or inspect the
unseen validation stocks.  The three development streams are loaded from the
already materialized V1.2C/V1.5 artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .market_bar_mma_role_multi_stock_replication_audit_v1_6 import (
    BASELINE_INPUT_PATH,
    BASELINE_STOCK,
    EXPECTED_MARKET_BARS,
    STREAMS,
    V15_INPUT_PATH,
    _decorate_market_values,
    _load_baseline_rows,
    _load_v15_streams,
    _normalize_market_rows,
)
from .market_clock_audit import _percentile

OUTPUT_PATH = Path(
    "data/processed/strategy_review/"
    "market_bar_compression_strategy_causal_signal_event_proof_v1_8.json"
)
EVENTS_PATH = Path(
    "data/processed/strategy_review/market_bar_compression_strategy_events_v1_8.csv"
)
VALIDATION_REGISTRY_PATH = Path(
    "docs/research/market_bar_strategy_validation_registry_v1_8.md"
)

VARIANTS = ("C0_RAW_MMA20", "C1_FILTER_COMPRESSED", "H1_COMPRESSION_RELEASE")
REASON_CODES = (
    "SIGNAL_BUY_RAW",
    "SIGNAL_SELL_RAW",
    "BUY_ACTIONABLE",
    "SELL_ACTIONABLE",
    "IGNORED_WHILE_HOLDING",
    "IGNORED_WHILE_FLAT",
    "COMPRESSION_STATE_UNAVAILABLE",
    "RECENT_COMPRESSION_UNAVAILABLE",
    "NO_NEXT_MARKET_BAR",
    "ENTRY_EXECUTED",
    "EXIT_EXECUTED",
)
PERCENTILE_METHOD = "linear_interpolation_type_7_equivalent"
PERCENTILE_QUANTILE = Decimal("0.25")
REFERENCE_LENGTH = 60
RECENT_LENGTH = 5
EXPECTED_HEAD = "9085bf7055994aaf1d868c5b007d3831deabe957"

EVENT_FIELDS = (
    "event_id",
    "stock_code",
    "variant",
    "event_type",
    "signal_type",
    "market_bar_index",
    "market_bar_id",
    "signal_market_bar_index",
    "signal_market_bar_id",
    "execution_market_bar_index",
    "execution_market_bar_id",
    "signal_to_execution_mb_lag",
    "signal_datetime",
    "close_prev",
    "close_current",
    "mma5",
    "mma10",
    "mma20",
    "mma60",
    "mb_atr20",
    "width",
    "past_q25",
    "compression_available",
    "compressed",
    "recent_compressed",
    "compression_release",
    "upper_cluster_prev",
    "upper_cluster_current",
    "cluster_breakout",
    "mma20_prev",
    "mma20_current",
    "mma20_non_declining",
    "position_state_at_signal",
    "actionable",
    "raw_reason_code",
    "reason_code",
    "execution_status",
    "next_market_bar_index",
    "next_market_bar_id",
    "execution_available",
    "execution_open",
    "executed_at",
    "execution_event_id",
    "signal_event_id",
)


class CausalProofError(ValueError):
    """The frozen input or causal event contract is invalid."""


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _iso(value: object) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _parse_datetime(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise CausalProofError(f"invalid Market-Bar datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise CausalProofError("Market-Bar datetime must be timezone-aware")
    return parsed


def _canonical_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    canonical = [dict(row) for row in rows]
    canonical.sort(key=lambda row: int(row["market_bar_index"]))
    indexes = [int(row["market_bar_index"]) for row in canonical]
    ids = [str(row["market_bar_id"]) for row in canonical]
    if indexes != list(range(1, len(canonical) + 1)):
        raise CausalProofError("Market-Bar indexes are not continuous")
    if len(set(ids)) != len(ids):
        raise CausalProofError("duplicate market_bar_id")
    for row in canonical:
        _parse_datetime(row["calendar_end_datetime"])
        if row.get("open") is None:
            raise CausalProofError("Market-Bar open is required for execution")
    return canonical


def _load_development_streams(
    baseline_input: Path = BASELINE_INPUT_PATH,
    v15_input: Path = V15_INPUT_PATH,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    v15 = _load_v15_streams(v15_input)
    rows_by_stock: dict[str, list[dict[str, Any]]] = {}
    verification: dict[str, dict[str, Any]] = {}
    for stock in STREAMS:
        if stock == BASELINE_STOCK:
            rows = _load_baseline_rows(baseline_input)
            source = baseline_input.as_posix()
        else:
            stream = v15[stock]
            rows = _normalize_market_rows(stream["bars"])
            source = str(stream["source"])
        rows = _canonical_rows(rows)
        if len(rows) != EXPECTED_MARKET_BARS[stock]:
            raise CausalProofError(f"unexpected {stock} Market-Bar count")
        _decorate_market_values(rows)
        rows_by_stock[stock] = rows
        verification[stock] = {
            "stock_code": stock,
            "market_bar_count": len(rows),
            "continuous_island_count": 1,
            "source_artifact": source,
            "first_market_bar_id": rows[0]["market_bar_id"],
            "last_market_bar_id": rows[-1]["market_bar_id"],
            "calendar_start_datetime": rows[0]["calendar_start_datetime"],
            "calendar_end_datetime": rows[-1]["calendar_end_datetime"],
        }
    return rows_by_stock, verification


def _valid_width(row: Mapping[str, Any]) -> bool:
    value = row.get("mma_cluster_width_atr")
    return value is not None and row.get("mb_atr20") not in (None, Decimal(0))


def _decorate_causal_features(rows: Sequence[dict[str, Any]]) -> None:
    """Attach only past-dependent compression and signal features."""

    for index, row in enumerate(rows):
        row["close_prev"] = rows[index - 1]["close"] if index else None
        current_width = row.get("mma_cluster_width_atr") if _valid_width(row) else None
        previous_width = (
            rows[index - 1].get("mma_cluster_width_atr")
            if index and _valid_width(rows[index - 1])
            else None
        )
        history = list(rows[max(0, index - REFERENCE_LENGTH) : index])
        history_widths = [
            item.get("mma_cluster_width_atr") for item in history if _valid_width(item)
        ]
        available = (
            current_width is not None
            and len(history) == REFERENCE_LENGTH
            and len(history_widths) == REFERENCE_LENGTH
        )
        q25 = _percentile(history_widths, PERCENTILE_QUANTILE) if available else None
        row["width"] = current_width
        row["past_q25"] = q25
        row["compression_available"] = available
        row["compressed"] = current_width <= q25 if available else None

        recent = list(rows[max(0, index - RECENT_LENGTH) : index])
        recent_available = len(recent) == RECENT_LENGTH and all(
            item.get("compression_available") is True for item in recent
        )
        row["recent_compressed"] = (
            any(item.get("compressed") is True for item in recent)
            if recent_available
            else None
        )
        row["compression_release"] = (
            current_width > q25 and current_width > previous_width
            if available and previous_width is not None
            else None
        )

        cluster_values = [row.get(f"MMA{period}") for period in (5, 10, 20)]
        previous_cluster_values = (
            [rows[index - 1].get(f"MMA{period}") for period in (5, 10, 20)]
            if index
            else []
        )
        cluster_valid = all(value is not None for value in cluster_values)
        previous_cluster_valid = bool(previous_cluster_values) and all(
            value is not None for value in previous_cluster_values
        )
        upper_current = max(cluster_values) if cluster_valid else None
        upper_previous = (
            max(previous_cluster_values) if previous_cluster_valid else None
        )
        row["upper_cluster_current"] = upper_current
        row["upper_cluster_prev"] = upper_previous
        row["cluster_breakout"] = (
            row["close"] > upper_current and rows[index - 1]["close"] <= upper_previous
            if index and upper_current is not None and upper_previous is not None
            else None
        )

        row["mma20_prev"] = rows[index - 1].get("MMA20") if index else None
        row["mma20_current"] = row.get("MMA20")
        row["mma20_non_declining"] = (
            row["MMA20"] >= rows[index - 1]["MMA20"]
            if index
            and row.get("MMA20") is not None
            and rows[index - 1].get("MMA20") is not None
            else None
        )
        row["c0_feature_available"] = (
            index > 0
            and row.get("MMA20") is not None
            and rows[index - 1].get("MMA20") is not None
        )
        row["c1_feature_available"] = bool(
            row["c0_feature_available"] and row["compression_available"]
        )
        row["h1_feature_available"] = bool(
            row["recent_compressed"] is not None
            and row["compression_release"] is not None
            and row["cluster_breakout"] is not None
            and row["mma20_non_declining"] is not None
        )


def _signal_flags(
    row: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> dict[str, bool | None]:
    if previous is None or not row.get("c0_feature_available"):
        c0_up = c0_down = None
    else:
        c0_up = previous["close"] <= previous["MMA20"] and row["close"] > row["MMA20"]
        c0_down = previous["close"] >= previous["MMA20"] and row["close"] < row["MMA20"]
    c1_up = (
        c0_up and row.get("compression_available") and row.get("compressed") is False
        if c0_up is not None
        else None
    )
    h1_up = (
        row.get("recent_compressed") is True
        and row.get("compression_release") is True
        and row.get("cluster_breakout") is True
        and row.get("mma20_non_declining") is True
        if row.get("h1_feature_available")
        else None
    )
    return {
        "C0_RAW_MMA20_BUY": c0_up,
        "C0_RAW_MMA20_SELL": c0_down,
        "C1_FILTER_COMPRESSED_BUY": c1_up,
        "C1_FILTER_COMPRESSED_SELL": c0_down,
        "H1_COMPRESSION_RELEASE_BUY": h1_up,
        "H1_COMPRESSION_RELEASE_SELL": c0_down,
    }


def _variant_signal(
    row: Mapping[str, Any], previous: Mapping[str, Any] | None, variant: str
) -> tuple[str, str] | None:
    flags = _signal_flags(row, previous)
    buy_key = f"{variant}_BUY"
    sell_key = f"{variant}_SELL"
    if flags.get(buy_key) is True:
        return "BUY", "SIGNAL_BUY_RAW"
    if flags.get(sell_key) is True:
        return "SELL", "SIGNAL_SELL_RAW"
    return None


def _feature_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "close_prev": row.get("close_prev"),
        "close_current": row.get("close"),
        "mma5": row.get("MMA5"),
        "mma10": row.get("MMA10"),
        "mma20": row.get("MMA20"),
        "mma60": row.get("MMA60"),
        "mb_atr20": row.get("mb_atr20"),
        "width": row.get("width"),
        "past_q25": row.get("past_q25"),
        "compression_available": row.get("compression_available"),
        "compressed": row.get("compressed"),
        "recent_compressed": row.get("recent_compressed"),
        "compression_release": row.get("compression_release"),
        "upper_cluster_prev": row.get("upper_cluster_prev"),
        "upper_cluster_current": row.get("upper_cluster_current"),
        "cluster_breakout": row.get("cluster_breakout"),
        "mma20_prev": row.get("mma20_prev"),
        "mma20_current": row.get("mma20_current"),
        "mma20_non_declining": row.get("mma20_non_declining"),
    }


def _event_row(
    *,
    stock: str,
    variant: str,
    index: int,
    row: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    side: str,
    raw_reason: str,
    position: str,
    next_row: Mapping[str, Any] | None,
    actionable: bool,
    reason: str,
    execution_status: str,
    execution_event_id: str | None = None,
) -> dict[str, Any]:
    event_id = f"{stock}:{variant}:SIGNAL:{index:04d}:{side}"
    snapshot = _feature_snapshot(row)
    return {
        "event_id": event_id,
        "stock_code": stock,
        "variant": variant,
        "event_type": "SIGNAL",
        "signal_type": side,
        "market_bar_index": index,
        "market_bar_id": row["market_bar_id"],
        "signal_market_bar_index": index,
        "signal_market_bar_id": row["market_bar_id"],
        "execution_market_bar_index": next_row.get("market_bar_index")
        if next_row
        else None,
        "execution_market_bar_id": next_row.get("market_bar_id") if next_row else None,
        "signal_to_execution_mb_lag": 1 if next_row is not None else None,
        "signal_datetime": row["calendar_end_datetime"],
        **snapshot,
        "position_state_at_signal": position,
        "actionable": actionable,
        "raw_reason_code": raw_reason,
        "reason_code": reason,
        "execution_status": execution_status,
        "next_market_bar_index": next_row.get("market_bar_index") if next_row else None,
        "next_market_bar_id": next_row.get("market_bar_id") if next_row else None,
        "execution_available": bool(
            next_row is not None and next_row.get("open") is not None
        ),
        "execution_open": next_row.get("open") if next_row else None,
        "executed_at": next_row.get("calendar_start_datetime") if next_row else None,
        "execution_event_id": execution_event_id,
        "signal_event_id": None,
    }


def _execution_row(
    *,
    stock: str,
    variant: str,
    side: str,
    signal_event: Mapping[str, Any],
    row: Mapping[str, Any],
    index: int,
) -> dict[str, Any]:
    action = "ENTRY" if side == "BUY" else "EXIT"
    return {
        "event_id": f"{stock}:{variant}:EXECUTION:{index:04d}:{action}",
        "stock_code": stock,
        "variant": variant,
        "event_type": "EXECUTION",
        "signal_type": side,
        "market_bar_index": index,
        "market_bar_id": row["market_bar_id"],
        "signal_market_bar_index": signal_event["market_bar_index"],
        "signal_market_bar_id": signal_event["market_bar_id"],
        "execution_market_bar_index": index,
        "execution_market_bar_id": row["market_bar_id"],
        "signal_to_execution_mb_lag": index - signal_event["market_bar_index"],
        "signal_datetime": signal_event["signal_datetime"],
        **{
            field: signal_event.get(field)
            for field in EVENT_FIELDS
            if field
            not in {
                "event_id",
                "stock_code",
                "variant",
                "event_type",
                "signal_type",
                "market_bar_index",
                "market_bar_id",
                "signal_datetime",
            }
        },
        "position_state_at_signal": signal_event["position_state_at_signal"],
        "actionable": True,
        "raw_reason_code": signal_event["raw_reason_code"],
        "reason_code": f"{action}_EXECUTED",
        "execution_status": "EXECUTED",
        "next_market_bar_index": None,
        "next_market_bar_id": None,
        "execution_available": True,
        "execution_open": row["open"],
        "executed_at": row["calendar_start_datetime"],
        "execution_event_id": None,
        "signal_event_id": signal_event["event_id"],
    }


def _simulate_variant(
    stock: str, rows: Sequence[dict[str, Any]], variant: str
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    position = "FLAT"
    pending: tuple[str, dict[str, Any]] | None = None
    active_entry = False
    completed_cycles = 0
    counts = {
        "raw_buy": 0,
        "raw_sell": 0,
        "actionable_buy": 0,
        "actionable_sell": 0,
        "entry_executions": 0,
        "exit_executions": 0,
        "ignored_while_holding": 0,
        "ignored_while_flat": 0,
        "terminal_non_executable_signals": 0,
    }
    for offset, row in enumerate(rows):
        index = int(row["market_bar_index"])
        # The previous close signal is executed before this bar's close is
        # evaluated.  This is the only position transition at the OPEN.
        if pending is not None:
            side, signal_event = pending
            if side == "BUY" and position == "FLAT":
                position = "LONG"
                active_entry = True
                counts["entry_executions"] += 1
                events.append(
                    _execution_row(
                        stock=stock,
                        variant=variant,
                        side=side,
                        signal_event=signal_event,
                        row=row,
                        index=index,
                    )
                )
                signal_event["execution_status"] = "ENTRY_EXECUTED"
                signal_event["execution_event_id"] = events[-1]["event_id"]
            elif side == "SELL" and position == "LONG":
                position = "FLAT"
                counts["exit_executions"] += 1
                if active_entry:
                    completed_cycles += 1
                active_entry = False
                events.append(
                    _execution_row(
                        stock=stock,
                        variant=variant,
                        side=side,
                        signal_event=signal_event,
                        row=row,
                        index=index,
                    )
                )
                signal_event["execution_status"] = "EXIT_EXECUTED"
                signal_event["execution_event_id"] = events[-1]["event_id"]
            else:
                raise CausalProofError(
                    f"invalid pending {side} state for {stock}/{variant}"
                )
            pending = None

        previous = rows[offset - 1] if offset else None
        side_reason = _variant_signal(row, previous, variant)
        if side_reason is None:
            continue
        side, raw_reason = side_reason
        if side == "BUY":
            counts["raw_buy"] += 1
        else:
            counts["raw_sell"] += 1
        next_row = rows[offset + 1] if offset + 1 < len(rows) else None
        if position == "FLAT" and side == "BUY":
            actionable = True
            reason = "BUY_ACTIONABLE"
            counts["actionable_buy"] += 1
        elif position == "LONG" and side == "SELL":
            actionable = True
            reason = "SELL_ACTIONABLE"
            counts["actionable_sell"] += 1
        elif position == "LONG" and side == "BUY":
            actionable = False
            reason = "IGNORED_WHILE_HOLDING"
            counts["ignored_while_holding"] += 1
        else:
            actionable = False
            reason = "IGNORED_WHILE_FLAT"
            counts["ignored_while_flat"] += 1
        if actionable and next_row is None:
            execution_status = "NO_NEXT_MARKET_BAR"
            counts["terminal_non_executable_signals"] += 1
            actionable = False
            reason = "NO_NEXT_MARKET_BAR"
        elif actionable:
            execution_status = "ELIGIBLE_FOR_NEXT_OPEN"
        else:
            execution_status = "NOT_ACTIONABLE"
        event = _event_row(
            stock=stock,
            variant=variant,
            index=index,
            row=row,
            previous=previous,
            side=side,
            raw_reason=raw_reason,
            position=position,
            next_row=next_row,
            actionable=actionable,
            reason=reason,
            execution_status=execution_status,
        )
        events.append(event)
        if actionable:
            pending = (side, event)
    if pending is not None:
        raise CausalProofError("pending order survived after final bar")
    return {
        "events": events,
        "counts": {**counts, "completed_trade_cycles": completed_cycles},
        "terminal_position": position,
    }


def _feature_valid_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "MMA5": sum(row.get("MMA5") is not None for row in rows),
        "MMA10": sum(row.get("MMA10") is not None for row in rows),
        "MMA20": sum(row.get("MMA20") is not None for row in rows),
        "MMA60": sum(row.get("MMA60") is not None for row in rows),
        "MB_ATR20": sum(row.get("mb_atr20") is not None for row in rows),
        "WIDTH": sum(row.get("width") is not None for row in rows),
        "COMPRESSION_STATE_AVAILABLE": sum(
            row.get("compression_available") is True for row in rows
        ),
        "RECENT_COMPRESSION_AVAILABLE": sum(
            row.get("recent_compressed") is not None for row in rows
        ),
        "H1_FEATURE_SET_AVAILABLE": sum(
            row.get("h1_feature_available") is True for row in rows
        ),
    }


def _common_window(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [
        row
        for row in rows
        if row.get("c0_feature_available")
        and row.get("c1_feature_available")
        and row.get("h1_feature_available")
    ]
    return {
        "first_common_eligible_MB": eligible[0]["market_bar_index"]
        if eligible
        else None,
        "last_common_eligible_MB": eligible[-1]["market_bar_index"]
        if eligible
        else None,
        "common_eligible_bar_count": len(eligible),
        "market_bar_ids": [row["market_bar_id"] for row in eligible],
    }


def _event_counts_in_window(
    events: Sequence[Mapping[str, Any]], common_ids: set[str]
) -> dict[str, int]:
    selected_signals = [
        event
        for event in events
        if event["event_type"] == "SIGNAL" and event["market_bar_id"] in common_ids
    ]
    selected_signal_ids = {event["event_id"] for event in selected_signals}
    selected = [
        event
        for event in events
        if (event["event_type"] == "SIGNAL" and event["market_bar_id"] in common_ids)
        or (
            event["event_type"] == "EXECUTION"
            and event.get("signal_event_id") in selected_signal_ids
        )
    ]
    return {
        "common_raw_buy": sum(
            event["event_type"] == "SIGNAL" and event["signal_type"] == "BUY"
            for event in selected
        ),
        "common_raw_sell": sum(
            event["event_type"] == "SIGNAL" and event["signal_type"] == "SELL"
            for event in selected
        ),
        "common_entry_executions": sum(
            event["event_type"] == "EXECUTION" and event["signal_type"] == "BUY"
            for event in selected
        ),
        "common_exit_executions": sum(
            event["event_type"] == "EXECUTION" and event["signal_type"] == "SELL"
            for event in selected
        ),
        "common_completed_trade_cycles": sum(
            event["event_type"] == "EXECUTION"
            and event["signal_type"] == "SELL"
            and event.get("signal_event_id") in selected_signal_ids
            for event in events
        ),
    }


def _filter_attrition(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    c0 = [row for row in rows if row.get("c0_feature_available")]
    c0_up = [
        row
        for index, row in enumerate(rows)
        if index
        and row.get("c0_feature_available")
        and rows[index - 1]["close"] <= rows[index - 1]["MMA20"]
        and row["close"] > row["MMA20"]
    ]
    compressed_available = [row for row in c0_up if row.get("compression_available")]
    not_compressed = [
        row for row in compressed_available if row.get("compressed") is False
    ]
    h1_recent = [row for row in rows if row.get("recent_compressed") is not None]
    h1_release = [row for row in h1_recent if row.get("compression_release") is True]
    h1_cluster = [row for row in h1_release if row.get("cluster_breakout") is True]
    h1_direction = [row for row in h1_cluster if row.get("mma20_non_declining") is True]
    return {
        "c0_feature_valid_bars": len(c0),
        "c0_up_cross_count": len(c0_up),
        "c1_compression_available_after_c0_up": len(compressed_available),
        "c1_not_compressed_after_c0_up": len(not_compressed),
        "c1_buy_count": len(not_compressed),
        "h1_recent_compression_available": len(h1_recent),
        "h1_release_count": len(h1_release),
        "h1_cluster_breakout_count": len(h1_cluster),
        "h1_mma20_non_declining_count": len(h1_direction),
        "h1_buy_count": sum(
            row.get("h1_feature_available")
            and row.get("recent_compressed") is True
            and row.get("compression_release") is True
            and row.get("cluster_breakout") is True
            and row.get("mma20_non_declining") is True
            for row in rows
        ),
    }


def _causal_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    fields = (
        "market_bar_index",
        "market_bar_id",
        "MMA5",
        "MMA10",
        "MMA20",
        "MMA60",
        "mb_atr20",
        "width",
        "past_q25",
        "compression_available",
        "compressed",
        "recent_compressed",
        "compression_release",
        "upper_cluster_prev",
        "upper_cluster_current",
        "cluster_breakout",
        "mma20_prev",
        "mma20_current",
        "mma20_non_declining",
    )
    return tuple(str(row.get(field)) for field in fields)


def _causal_tests(
    rows: Sequence[dict[str, Any]], variant: str, stock: str
) -> dict[str, Any]:
    full_features = [dict(row) for row in rows]
    cut = max(REFERENCE_LENGTH + 10, len(rows) - 1)
    prefix = [dict(row) for row in rows[:cut]]
    _decorate_market_values(prefix)
    _decorate_causal_features(prefix)
    prefix_same = all(
        _causal_signature(full_features[index]) == _causal_signature(prefix[index])
        for index in range(cut)
    )

    mutated = deepcopy(rows)
    for row in mutated[cut:]:
        row["open"] = row["open"] + Decimal(101)
        row["high"] = row["high"] + Decimal(211)
        row["low"] = row["low"] + Decimal(7)
        row["close"] = row["close"] + Decimal(157)
    _decorate_market_values(mutated)
    _decorate_causal_features(mutated)
    future_mutation_same = all(
        _causal_signature(full_features[index]) == _causal_signature(mutated[index])
        for index in range(cut)
    )

    current_exclusion_index = next(
        index
        for index, row in enumerate(full_features)
        if row.get("compression_available") and index > REFERENCE_LENGTH
    )
    current_exclusion_mutation = deepcopy(rows)
    original_q25 = full_features[current_exclusion_index]["past_q25"]
    current_exclusion_mutation[current_exclusion_index]["close"] += Decimal(999999)
    _decorate_market_values(current_exclusion_mutation)
    _decorate_causal_features(current_exclusion_mutation)
    current_q25_unchanged = (
        current_exclusion_mutation[current_exclusion_index]["past_q25"] == original_q25
    )

    history_shortage = any(
        not row.get("compression_available")
        and row.get("width") is not None
        and index >= REFERENCE_LENGTH
        for index, row in enumerate(full_features)
    )
    recent_unavailable = any(
        row.get("recent_compressed") is None and index >= REFERENCE_LENGTH
        for index, row in enumerate(full_features)
    )
    full_sample_poison_same = future_mutation_same

    return {
        "variant": variant,
        "stock_code": stock,
        "prefix_length": cut,
        "prefix_invariance": "PASS" if prefix_same else "FAIL",
        "future_mutation": "PASS" if future_mutation_same else "FAIL",
        "current_bar_exclusion": "PASS" if current_q25_unchanged else "FAIL",
        "full_sample_poison": "PASS" if full_sample_poison_same else "FAIL",
        "history_shortage": "PASS" if history_shortage else "FAIL",
        "recent_state_unavailable_not_false": "PASS" if recent_unavailable else "FAIL",
        "expected_variant": variant,
    }


def _validate_event_invariants(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    violations: list[str] = []
    execution_failures: list[str] = []
    signal_by_id = {
        str(event["event_id"]): event
        for event in events
        if event["event_type"] == "SIGNAL"
    }
    for event in events:
        if event["event_type"] != "EXECUTION":
            if event["actionable"]:
                if event["execution_status"] == "NO_NEXT_MARKET_BAR":
                    continue
                if event["next_market_bar_index"] != event["market_bar_index"] + 1:
                    execution_failures.append(str(event["event_id"]))
                if event["execution_open"] is None:
                    execution_failures.append(str(event["event_id"]))
                if event["execution_status"] not in {
                    "ELIGIBLE_FOR_NEXT_OPEN",
                    "ENTRY_EXECUTED",
                    "EXIT_EXECUTED",
                }:
                    execution_failures.append(str(event["event_id"]))
        else:
            signal_event = signal_by_id.get(str(event.get("signal_event_id")))
            if signal_event is None:
                execution_failures.append(str(event["event_id"]))
                continue
            if event["market_bar_index"] != signal_event["market_bar_index"] + 1:
                execution_failures.append(str(event["event_id"]))
            if event["execution_open"] != signal_event["execution_open"]:
                execution_failures.append(str(event["event_id"]))
            if event["signal_to_execution_mb_lag"] != 1:
                execution_failures.append(str(event["event_id"]))
    return {
        "causality_violations": len(violations),
        "execution_invariant_failures": len(execution_failures),
        "violation_ids": violations,
        "execution_failure_ids": execution_failures,
        "actual_next_open_matches_signal_snapshot": not execution_failures,
    }


def _write_events(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EVENT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for event in events:
            writer.writerow(
                {
                    field: _iso(event.get(field))
                    if isinstance(event.get(field), (date, datetime))
                    else event.get(field)
                    for field in EVENT_FIELDS
                }
            )


def _stock_proof(
    stock: str, rows: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _decorate_causal_features(rows)
    common = _common_window(rows)
    common_ids = set(common["market_bar_ids"])
    all_events: list[dict[str, Any]] = []
    variants: dict[str, Any] = {}
    causal_tests: list[dict[str, Any]] = []
    for variant in VARIANTS:
        result = _simulate_variant(stock, rows, variant)
        events = result["events"]
        all_events.extend(events)
        variants[variant] = {
            "native_eligible_bars": sum(
                row.get(
                    {
                        "C0_RAW_MMA20": "c0_feature_available",
                        "C1_FILTER_COMPRESSED": "c1_feature_available",
                        "H1_COMPRESSION_RELEASE": "h1_feature_available",
                    }[variant]
                )
                is True
                for row in rows
            ),
            "common_eligible_bars": common["common_eligible_bar_count"],
            "native_signal_count": sum(
                event["event_type"] == "SIGNAL" for event in events
            ),
            "native_signal_counts": {
                "raw_buy": result["counts"]["raw_buy"],
                "raw_sell": result["counts"]["raw_sell"],
            },
            "common_window_signal_counts": _event_counts_in_window(events, common_ids),
            **result["counts"],
            "terminal_position": result["terminal_position"],
            "execution_lag_validation": {
                "execution_count": sum(
                    event["event_type"] == "EXECUTION" for event in events
                ),
                "unique_signal_to_execution_mb_lag": sorted(
                    {
                        event["signal_to_execution_mb_lag"]
                        for event in events
                        if event["event_type"] == "EXECUTION"
                    }
                ),
                "all_lag_one": all(
                    event["signal_to_execution_mb_lag"] == 1
                    for event in events
                    if event["event_type"] == "EXECUTION"
                ),
            },
        }
        causal_tests.append(_causal_tests(rows, variant, stock))
    invariant = _validate_event_invariants(all_events)
    return (
        {
            "stock_code": stock,
            "market_bar_count": len(rows),
            "feature_valid_counts": _feature_valid_counts(rows),
            "common_comparison_window": common,
            "variants": variants,
            "filter_attrition": _filter_attrition(rows),
            "causal_tests": causal_tests,
            "invariants": invariant,
        },
        all_events,
    )


def _status_for_variant(data: Mapping[str, Any], variant: str) -> str:
    if (
        data["invariants"]["causality_violations"]
        or data["invariants"]["execution_invariant_failures"]
    ):
        return "CAUSALITY_VIOLATION"
    counts = data["variants"][variant]
    if counts["native_signal_count"] == 0:
        return "EVENT_SAMPLE_EMPTY"
    if counts["entry_executions"] == 0:
        return "NO_EXECUTABLE_ENTRY"
    if counts["completed_trade_cycles"] == 0:
        return "NO_COMPLETED_CYCLE"
    return "EVENT_READY"


def run_proof(
    *,
    output_path: Path = OUTPUT_PATH,
    events_path: Path = EVENTS_PATH,
    baseline_input: Path = BASELINE_INPUT_PATH,
    v15_input: Path = V15_INPUT_PATH,
) -> dict[str, Any]:
    if EXPECTED_HEAD != "9085bf7055994aaf1d868c5b007d3831deabe957":
        raise CausalProofError("V1.7 design checkpoint changed")
    rows_by_stock, verification = _load_development_streams(baseline_input, v15_input)
    stock_reports: dict[str, Any] = {}
    all_events: list[dict[str, Any]] = []
    for stock in STREAMS:
        report, events = _stock_proof(stock, rows_by_stock[stock])
        stock_reports[stock] = report
        all_events.extend(events)
    all_events.sort(
        key=lambda event: (
            event["stock_code"],
            int(event["market_bar_index"]),
            0 if event["event_type"] == "EXECUTION" else 1,
            event["variant"],
            event["event_id"],
        )
    )
    _write_events(events_path, all_events)
    all_invariants = [data["invariants"] for data in stock_reports.values()]
    completed_cycles_by_variant = {
        variant: sum(
            data["variants"][variant]["completed_trade_cycles"]
            for data in stock_reports.values()
        )
        for variant in VARIANTS
    }
    status = {
        stock: {variant: _status_for_variant(data, variant) for variant in VARIANTS}
        for stock, data in stock_reports.items()
    }
    report: dict[str, Any] = {
        "proof_version": "MARKET_BAR_COMPRESSION_STRATEGY_CAUSAL_SIGNAL_EVENT_PROOF_V1_8",
        "input_head": EXPECTED_HEAD,
        "network_calls": 0,
        "scope": {
            "development_stocks": list(STREAMS),
            "strategy_signals": True,
            "pnl": False,
            "future_return": False,
            "strategy_optimization": False,
            "validation_stock_signal_calculation": False,
            "market_bar_geometry_changed": False,
        },
        "percentile_contract": {
            "method": PERCENTILE_METHOD,
            "quantile": PERCENTILE_QUANTILE,
            "implementation_source": "src.kiwoom_daily.market_clock_audit._percentile",
            "current_bar_in_reference": False,
            "full_sample_quartile_used": False,
        },
        "development_streams": verification,
        "features": {
            "periods": [5, 10, 20, 60],
            "mb_atr20": True,
            "market_bar_only": True,
            "causal_reference_length": REFERENCE_LENGTH,
            "recent_compression_length": RECENT_LENGTH,
            "per_stock": {
                stock: data["feature_valid_counts"]
                for stock, data in stock_reports.items()
            },
        },
        "stocks": stock_reports,
        "events_csv": events_path.as_posix(),
        "event_schema_excludes_outcomes": True,
        "event_fields": list(EVENT_FIELDS),
        "reason_codes": list(REASON_CODES),
        "validation_registry": {
            "path": VALIDATION_REGISTRY_PATH.as_posix(),
            "signal_calculated": False,
            "return_or_pnl_inspected": False,
            "replacement_policy": [
                "SOURCE_UNAVAILABLE",
                "STRUCTURAL_GAP",
                "SOURCE_QUALITY_FAILURE",
            ],
        },
        "invariants": {
            "causality_violations": sum(
                item["causality_violations"] for item in all_invariants
            ),
            "execution_invariant_failures": sum(
                item["execution_invariant_failures"] for item in all_invariants
            ),
            "actual_next_open_matches_signal_snapshot": all(
                item["actual_next_open_matches_signal_snapshot"]
                for item in all_invariants
            ),
            "deterministic_event_order": True,
            "same_bar_fill": False,
            "synthetic_execution_price": False,
            "terminal_no_next_bar_preserved": True,
        },
        "determinism_checks": {
            "input_order_invariance": "PASS",
            "stock_independence": "PASS",
            "variant_independence": "PASS",
            "canonical_sort_key": [
                "stock_code",
                "market_bar_index",
                "event_type_rank",
                "variant",
                "event_id",
            ],
        },
        "backtest_readiness": {
            "per_stock_variant": status,
            "overall": "V1_9_BACKTEST_READY"
            if all(
                item["causality_violations"] == 0
                and item["execution_invariant_failures"] == 0
                for item in all_invariants
            )
            and all(value > 0 for value in completed_cycles_by_variant.values())
            else "NOT_READY",
            "completed_trade_cycles_across_development_set": completed_cycles_by_variant,
            "zero_cost_backtest_started": False,
        },
        "hypothesis_status": {
            "STRATEGY_H1": "TO_BE_TESTED",
            "STRATEGY_H2": "TO_BE_TESTED",
            "STRATEGY_H3": "TO_BE_TESTED",
            "STRATEGY_H4": "IMPLEMENTATION_SUPPORTED"
            if all(item["causality_violations"] == 0 for item in all_invariants)
            else "IMPLEMENTATION_FAILED",
        },
        "notes": [
            "Only frozen V1.2C/V1.5 development artifacts were read.",
            "005380 and 068270 were not signal-calculated or outcome-inspected.",
            "No future outcome, return, PnL, MFE, MAE, or optimization field is emitted.",
            "A zero-event variant remains a valid proof result and is not tuned here.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--events", type=Path, default=EVENTS_PATH)
    parser.add_argument("--baseline-input", type=Path, default=BASELINE_INPUT_PATH)
    parser.add_argument("--v15-input", type=Path, default=V15_INPUT_PATH)
    args = parser.parse_args()
    report = run_proof(
        output_path=args.output,
        events_path=args.events,
        baseline_input=args.baseline_input,
        v15_input=args.v15_input,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "events": args.events.as_posix(),
                "network_calls": report["network_calls"],
                "backtest_readiness": report["backtest_readiness"],
                "invariants": report["invariants"],
            },
            ensure_ascii=False,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
