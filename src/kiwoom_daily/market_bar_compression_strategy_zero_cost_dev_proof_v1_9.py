"""Zero-cost, normalized development proof for the frozen Market-Bar variants.

This module is intentionally a development-only accounting layer.  It reuses
the frozen V1.8 causal features and signal identities, then starts every
stock/variant at FLAT on the V1.8 common comparison window.  It does not fetch
data, inspect validation stocks, or change strategy parameters.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median
from typing import Any

from .market_bar_compression_strategy_causal_signal_event_proof_v1_8 import (
    BASELINE_INPUT_PATH,
    STREAMS,
    V15_INPUT_PATH,
    VARIANTS,
    _common_window,
    _decorate_causal_features,
    _load_development_streams,
    _simulate_variant,
    _variant_signal,
)
from .market_bar_compression_strategy_causal_signal_event_proof_v1_8 import (
    EVENTS_PATH as V18_EVENTS_PATH,
)
from .market_bar_mma_visual_proof import _render_chart

OUTPUT_PATH = Path(
    "data/processed/strategy_review/"
    "market_bar_compression_strategy_zero_cost_dev_proof_v1_9.json"
)
TRADE_LEDGER_PATH = Path(
    "data/processed/strategy_review/"
    "market_bar_compression_strategy_trade_ledger_v1_9.csv"
)
EQUITY_PATH = Path(
    "data/processed/strategy_review/market_bar_compression_strategy_equity_v1_9.csv"
)
CHART_ROOT = Path(
    "data/processed/strategy_charts/"
    "market_bar_compression_strategy_zero_cost_dev_proof_v1_9"
)
V18_OUTPUT_PATH = Path(
    "data/processed/strategy_review/"
    "market_bar_compression_strategy_causal_signal_event_proof_v1_8.json"
)
V18_CHECKPOINT_HEAD = "9085bf7055994aaf1d868c5b007d3831deabe957"
NORMALIZED_CAPITAL = Decimal("1.0")
COMMON_EXPECTED = {"066570": 116, "035420": 115, "105560": 115}

TRADE_FIELDS = (
    "stock_code",
    "variant",
    "trade_id",
    "entry_signal_event_id",
    "entry_execution_event_id",
    "entry_signal_market_bar_index",
    "entry_signal_market_bar_id",
    "entry_signal_datetime",
    "entry_execution_market_bar_index",
    "entry_execution_market_bar_id",
    "entry_execution_datetime",
    "entry_price",
    "exit_signal_event_id",
    "exit_execution_event_id",
    "exit_signal_market_bar_index",
    "exit_signal_market_bar_id",
    "exit_signal_datetime",
    "exit_execution_market_bar_index",
    "exit_execution_market_bar_id",
    "exit_execution_datetime",
    "exit_price",
    "holding_market_bars",
    "trade_return",
    "entry_compressed_state",
    "entry_width",
    "entry_past_q25",
    "entry_recent_compressed",
    "entry_compression_release",
    "entry_cluster_breakout",
    "entry_mma20_non_declining",
)


class DevelopmentProofError(ValueError):
    """Frozen development proof input or accounting contract is invalid."""


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DevelopmentProofError(f"invalid decimal value: {value}") from exc


def _json_default(value: object) -> str:
    return str(value)


def _token(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _canonical_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = [dict(row) for row in rows]
    result.sort(key=lambda row: int(row["market_bar_index"]))
    indexes = [int(row["market_bar_index"]) for row in result]
    if len(set(indexes)) != len(indexes):
        raise DevelopmentProofError("duplicate market_bar_index")
    if indexes != list(range(1, len(result) + 1)):
        raise DevelopmentProofError("Market-Bar indexes are not continuous")
    return result


def _feature_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "entry_compressed_state": row.get("compressed"),
        "entry_width": row.get("width"),
        "entry_past_q25": row.get("past_q25"),
        "entry_recent_compressed": row.get("recent_compressed"),
        "entry_compression_release": row.get("compression_release"),
        "entry_cluster_breakout": row.get("cluster_breakout"),
        "entry_mma20_non_declining": row.get("mma20_non_declining"),
    }


def _event_id(stock: str, variant: str, index: int, side: str) -> str:
    return f"{stock}:{variant}:SIGNAL:{index:04d}:{side}"


def _execution_id(stock: str, variant: str, index: int, action: str) -> str:
    return f"{stock}:{variant}:EXECUTION:{index:04d}:{action}"


def _trade_return(entry_price: Decimal, exit_price: Decimal) -> Decimal:
    if entry_price <= 0 or exit_price <= 0:
        raise DevelopmentProofError("trade prices must be positive")
    return exit_price / entry_price - Decimal(1)


def _mdd(equity_curve: Sequence[Decimal]) -> Decimal:
    peak: Decimal | None = None
    result = Decimal(0)
    for value in equity_curve:
        if peak is None or value > peak:
            peak = value
        if peak and value / peak - Decimal(1) < result:
            result = value / peak - Decimal(1)
    return result


def _fmt_decimal(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _safe_mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / Decimal(len(values)) if values else None


def _metrics(
    trades: Sequence[Mapping[str, Any]],
    equity_curve: Sequence[Decimal],
    *,
    common_count: int,
    held_bars: int,
    terminal_equity: Decimal,
    turnover_notional: Decimal | None = None,
) -> dict[str, Any]:
    returns = [_decimal(trade["trade_return"]) for trade in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    flats = [value for value in returns if value == 0]
    positive_sum = sum(wins, Decimal(0))
    negative_sum = sum(losses, Decimal(0))
    avg_win = _safe_mean(wins)
    avg_loss = _safe_mean(losses)
    if losses:
        payoff = avg_win / abs(avg_loss) if avg_win is not None else None
        pf = positive_sum / abs(negative_sum) if negative_sum else None
        pf_state = "VALUE"
    elif returns:
        payoff = None
        pf = None
        pf_state = "NO_LOSING_TRADES"
    else:
        payoff = None
        pf = None
        pf_state = "NO_TRADES"
    holding = [_decimal(trade["holding_market_bars"]) for trade in trades]
    realized = (
        terminal_equity if not trades else _decimal(trades[-1]["_realized_equity"])
    )
    return {
        "completed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(flats),
        "win_rate": _safe_mean(
            [Decimal(1) if value > 0 else Decimal(0) for value in returns]
        ),
        "mean_trade_return": _safe_mean(returns),
        "median_trade_return": median(returns) if returns else None,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": payoff,
        "profit_factor": pf,
        "profit_factor_state": pf_state,
        "realized_compounded_return": realized - Decimal(1),
        "terminal_mtm_return": terminal_equity - Decimal(1),
        "maximum_drawdown": _mdd(equity_curve),
        "average_holding_market_bars": _safe_mean(holding),
        "median_holding_market_bars": median(holding) if holding else None,
        "exposure": Decimal(held_bars) / Decimal(common_count)
        if common_count
        else None,
        "turnover_normalized_notional": (
            turnover_notional
            if turnover_notional is not None
            else Decimal(2 * len(trades))
        ),
    }


def _trade_row(
    *,
    stock: str,
    variant: str,
    entry: Mapping[str, Any],
    exit_signal: Mapping[str, Any],
    exit_execution: Mapping[str, Any],
    realized_equity: Decimal,
) -> dict[str, Any]:
    entry_price = _decimal(entry["execution_open"])
    exit_price = _decimal(exit_execution["execution_open"])
    row = {
        "stock_code": stock,
        "variant": variant,
        "trade_id": f"{stock}:{variant}:TRADE:{int(entry['execution_market_bar_index']):04d}",
        "entry_signal_event_id": entry["event_id"],
        "entry_execution_event_id": entry["execution_event_id"],
        "entry_signal_market_bar_index": entry["market_bar_index"],
        "entry_signal_market_bar_id": entry["market_bar_id"],
        "entry_signal_datetime": entry["signal_datetime"],
        "entry_execution_market_bar_index": entry["execution_market_bar_index"],
        "entry_execution_market_bar_id": entry["execution_market_bar_id"],
        "entry_execution_datetime": entry["executed_at"],
        "entry_price": entry_price,
        "exit_signal_event_id": exit_signal["event_id"],
        "exit_execution_event_id": exit_execution["event_id"],
        "exit_signal_market_bar_index": exit_signal["market_bar_index"],
        "exit_signal_market_bar_id": exit_signal["market_bar_id"],
        "exit_signal_datetime": exit_signal["signal_datetime"],
        "exit_execution_market_bar_index": exit_execution["market_bar_index"],
        "exit_execution_market_bar_id": exit_execution["market_bar_id"],
        "exit_execution_datetime": exit_execution["executed_at"],
        "exit_price": exit_price,
        "holding_market_bars": int(exit_execution["market_bar_index"])
        - int(entry["execution_market_bar_index"]),
        "trade_return": _trade_return(entry_price, exit_price),
        **_feature_snapshot(entry),
        "_realized_equity": realized_equity,
    }
    return row


def _simulate_common(
    stock: str,
    rows: Sequence[Mapping[str, Any]],
    variant: str,
    common: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one stock/variant from FLAT on the common window."""

    canonical = _canonical_rows(rows)
    selected = common or _common_window(canonical)
    common_ids = set(selected["market_bar_ids"])
    common_rows = [row for row in canonical if row["market_bar_id"] in common_ids]
    if len(common_rows) != int(selected["common_eligible_bar_count"]):
        raise DevelopmentProofError("common window rows do not match frozen window")
    if not common_rows:
        raise DevelopmentProofError("common window is empty")
    common_indices = {int(row["market_bar_index"]) for row in common_rows}

    position = "FLAT"
    pending: dict[str, Any] | None = None
    entry: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    equity_curve: list[Decimal] = []
    equity_rows: list[dict[str, Any]] = []
    equity = NORMALIZED_CAPITAL
    realized_equity = NORMALIZED_CAPITAL
    quantity: Decimal | None = None
    held_bars = 0

    for index in sorted(common_indices):
        row = canonical[index - 1]
        # Pending signals are from the preceding Market-Bar close.  This is
        # deliberately processed before this bar's close evaluation.
        if pending is not None:
            side = str(pending["side"])
            signal = pending["signal"]
            action = "ENTRY" if side == "BUY" else "EXIT"
            execution = {
                "event_id": _execution_id(stock, variant, index, action),
                "stock_code": stock,
                "variant": variant,
                "event_type": "EXECUTION",
                "signal_type": side,
                "market_bar_index": index,
                "market_bar_id": row["market_bar_id"],
                "signal_event_id": signal["event_id"],
                "execution_open": row["open"],
                "executed_at": row["calendar_start_datetime"],
            }
            if side == "BUY":
                if position != "FLAT":
                    raise DevelopmentProofError("BUY execution while already LONG")
                position = "LONG"
                quantity = equity / _decimal(row["open"])
                entry = {
                    **signal,
                    "execution_open": row["open"],
                    "execution_market_bar_index": index,
                    "execution_market_bar_id": row["market_bar_id"],
                    "executed_at": row["calendar_start_datetime"],
                    "execution_event_id": execution["event_id"],
                }
            else:
                if position != "LONG" or entry is None or quantity is None:
                    raise DevelopmentProofError("SELL execution without LONG position")
                position = "FLAT"
                realized_equity = quantity * _decimal(row["open"])
                execution["execution_open"] = row["open"]
                trades.append(
                    _trade_row(
                        stock=stock,
                        variant=variant,
                        entry=entry,
                        exit_signal=signal,
                        exit_execution=execution,
                        realized_equity=realized_equity,
                    )
                )
                quantity = None
                entry = None
            executions.append(execution)
            pending = None

        if position == "LONG" and quantity is not None:
            equity = quantity * _decimal(row["close"])
            held_bars += 1
        else:
            equity = realized_equity
        equity_curve.append(equity)
        equity_rows.append(
            {
                "stock_code": stock,
                "variant": variant,
                "market_bar_index": index,
                "market_bar_id": row["market_bar_id"],
                "calendar_end_datetime": row["calendar_end_datetime"],
                "position": position,
                "equity": equity,
            }
        )

        previous = canonical[index - 2] if index > 1 else None
        side_reason = _variant_signal(row, previous, variant)
        if side_reason is None:
            continue
        side, raw_reason = side_reason
        next_row = canonical[index] if index < len(canonical) else None
        actionable = (position == "FLAT" and side == "BUY") or (
            position == "LONG" and side == "SELL"
        )
        signal = {
            "event_id": _event_id(stock, variant, index, side),
            "stock_code": stock,
            "variant": variant,
            "event_type": "SIGNAL",
            "signal_type": side,
            "market_bar_index": index,
            "market_bar_id": row["market_bar_id"],
            "signal_datetime": row["calendar_end_datetime"],
            "execution_market_bar_index": next_row["market_bar_index"]
            if next_row
            else None,
            "execution_market_bar_id": next_row["market_bar_id"] if next_row else None,
            "execution_open": next_row["open"] if next_row else None,
            "executed_at": next_row["calendar_start_datetime"] if next_row else None,
            "raw_reason_code": raw_reason,
            "actionable": actionable and next_row is not None,
            "reason_code": "BUY_ACTIONABLE"
            if side == "BUY" and actionable and next_row
            else "SELL_ACTIONABLE"
            if side == "SELL" and actionable and next_row
            else "NO_NEXT_MARKET_BAR"
            if actionable
            else "IGNORED_WHILE_HOLDING"
            if position == "LONG" and side == "BUY"
            else "IGNORED_WHILE_FLAT",
            **_feature_snapshot(row),
        }
        signals.append(signal)
        if signal["actionable"]:
            pending = {"side": side, "signal": signal}

    # An actionable signal on the terminal common bar is intentionally not a
    # synthetic fill.  A pending signal cannot survive because it is rejected
    # when next_row is absent.
    if pending is not None:
        raise DevelopmentProofError("pending order survived common window")

    open_position = None
    if position == "LONG" and entry is not None and quantity is not None:
        final_row = common_rows[-1]
        final_close = _decimal(final_row["close"])
        terminal_equity = quantity * final_close
        open_position = {
            "stock_code": stock,
            "variant": variant,
            "entry_signal_event_id": entry["event_id"],
            "entry_execution_event_id": entry["execution_event_id"],
            "entry_execution_market_bar_index": entry["execution_market_bar_index"],
            "entry_execution_market_bar_id": entry["execution_market_bar_id"],
            "entry_price": entry["execution_open"],
            "last_market_bar_index": final_row["market_bar_index"],
            "last_market_bar_id": final_row["market_bar_id"],
            "last_close": final_close,
            "unrealized_return": _trade_return(
                _decimal(entry["execution_open"]), final_close
            ),
            "mark_to_market_equity": terminal_equity,
            "status": "OPEN_AT_RESEARCH_END",
        }
    else:
        terminal_equity = realized_equity

    for trade in trades:
        trade["_realized_equity"] = realized_equity
    return {
        "stock_code": stock,
        "variant": variant,
        "common_window": dict(selected),
        "signals": signals,
        "executions": executions,
        "trades": trades,
        "open_position": open_position,
        "terminal_position": position,
        "terminal_equity": terminal_equity,
        "realized_equity": realized_equity,
        "equity_curve": equity_curve,
        "equity_rows": equity_rows,
        "held_bars": held_bars,
        "metrics": _metrics(
            trades,
            equity_curve,
            common_count=len(common_rows),
            held_bars=held_bars,
            terminal_equity=terminal_equity,
            turnover_notional=Decimal(len(executions)),
        ),
    }


def _v18_signal_signature(event: Mapping[str, Any]) -> tuple[str, ...]:
    fields = (
        "event_id",
        "stock_code",
        "variant",
        "signal_type",
        "market_bar_index",
        "market_bar_id",
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
    )
    return tuple(_token(event.get(field)) or "" for field in fields)


def _load_v18_events(path: Path = V18_EVENTS_PATH) -> list[dict[str, str]]:
    if not path.exists():
        raise DevelopmentProofError(f"V1.8 event artifact missing: {path}")
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _v18_regression(
    rows_by_stock: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    v18_events_path: Path = V18_EVENTS_PATH,
) -> dict[str, Any]:
    expected = _load_v18_events(v18_events_path)
    expected_by_stock_variant: dict[tuple[str, str], list[dict[str, str]]] = {}
    for event in expected:
        if event.get("event_type") != "SIGNAL":
            continue
        expected_by_stock_variant.setdefault(
            (event["stock_code"], event["variant"]), []
        ).append(event)
    mismatches: list[str] = []
    signal_count = 0
    execution_count = 0
    for stock in STREAMS:
        rows = _canonical_rows(rows_by_stock[stock])
        common = _common_window(rows)
        common_ids = set(common["market_bar_ids"])
        for variant in VARIANTS:
            regenerated = _simulate_variant(stock, deepcopy(rows), variant)["events"]
            current = [
                event
                for event in regenerated
                if event["event_type"] == "SIGNAL"
                and event["market_bar_id"] in common_ids
            ]
            expected_current = [
                event
                for event in expected_by_stock_variant.get((stock, variant), [])
                if event.get("market_bar_id") in common_ids
            ]
            current.sort(key=lambda event: int(event["market_bar_index"]))
            expected_current.sort(key=lambda event: int(event["market_bar_index"]))
            signal_count += len(current)
            if [_v18_signal_signature(event) for event in current] != [
                _v18_signal_signature(event) for event in expected_current
            ]:
                mismatches.append(f"signal:{stock}:{variant}")
            current_executions = [
                event
                for event in regenerated
                if event["event_type"] == "EXECUTION"
                and event.get("signal_market_bar_id") in common_ids
            ]
            expected_exec = [
                event
                for event in expected
                if event.get("event_type") == "EXECUTION"
                and event.get("stock_code") == stock
                and event.get("variant") == variant
                and event.get("signal_market_bar_id") in common_ids
            ]
            execution_count += len(current_executions)
            current_ids = [
                (event["event_id"], _token(event.get("execution_open")))
                for event in current_executions
            ]
            expected_ids = [
                (event["event_id"], _token(event.get("execution_open")))
                for event in expected_exec
            ]
            if current_ids != expected_ids:
                mismatches.append(f"execution:{stock}:{variant}")
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "mismatches": mismatches,
        "signal_rows_compared": signal_count,
        "execution_rows_compared": execution_count,
        "v1_8_events_path": v18_events_path.as_posix(),
    }


def _serialize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in row.items():
        if key.startswith("_"):
            continue
        result[key] = _fmt_decimal(value) if isinstance(value, Decimal) else value
    return result


def _cohort_report(
    stock: str,
    rows: Sequence[Mapping[str, Any]],
    c0: Mapping[str, Any],
    c1: Mapping[str, Any],
) -> dict[str, Any]:
    c1_ids = {
        (int(event["market_bar_index"]), event["signal_type"])
        for event in c1["signals"]
        if event["signal_type"] == "BUY"
    }
    c0_buy = [event for event in c0["signals"] if event["signal_type"] == "BUY"]
    row_by_index = {int(row["market_bar_index"]): row for row in rows}
    groups: dict[str, list[dict[str, Any]]] = {
        "RETAINED_BY_C1": [],
        "REMOVED_AS_COMPRESSED": [],
    }
    for event in c0_buy:
        index = int(event["market_bar_index"])
        group = (
            "RETAINED_BY_C1" if (index, "BUY") in c1_ids else "REMOVED_AS_COMPRESSED"
        )
        groups[group].append(
            {
                "signal_event_id": event["event_id"],
                "market_bar_index": index,
                "market_bar_id": event["market_bar_id"],
                "compressed": row_by_index[index].get("compressed"),
                "outcome": None,
            }
        )
    trade_by_signal = {trade["entry_signal_event_id"]: trade for trade in c0["trades"]}
    for group_rows in groups.values():
        for item in group_rows:
            trade = trade_by_signal.get(item["signal_event_id"])
            item["outcome"] = (
                _serialize_row(trade) if trade else {"status": "OPEN_OR_NOT_COMPLETED"}
            )

    def summary(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        completed = [
            item["outcome"]
            for item in items
            if isinstance(item["outcome"], dict) and "trade_return" in item["outcome"]
        ]
        returns = [_decimal(item["trade_return"]) for item in completed]
        gains = sum((value for value in returns if value > 0), Decimal(0))
        losses = sum((value for value in returns if value < 0), Decimal(0))
        holding = [
            _decimal(item["holding_market_bars"])
            for item in completed
            if item.get("holding_market_bars") is not None
        ]
        return {
            "count": len(items),
            "completed": len(completed),
            "wins": sum(value > 0 for value in returns),
            "losses": sum(value < 0 for value in returns),
            "mean_return": _safe_mean(returns),
            "median_return": median(returns) if returns else None,
            "average_holding_market_bars": _safe_mean(holding),
            "median_holding_market_bars": median(holding) if holding else None,
            "profit_factor": gains / abs(losses) if losses else None,
            "profit_factor_state": "VALUE"
            if losses
            else "NO_LOSING_TRADES"
            if returns
            else "NO_TRADES",
            "market_bar_indexes": [item["market_bar_index"] for item in items],
        }

    return {
        group: {"summary": summary(items), "entries": items}
        for group, items in groups.items()
    }


def _write_csv(
    path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: _fmt_decimal(row.get(field))
                    if isinstance(row.get(field), Decimal)
                    else row.get(field)
                    for field in fields
                }
            )


def _write_chart(
    *,
    stock: str,
    variant: str,
    label: str,
    signal_index: int,
    rows: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any],
) -> str:
    start = max(0, signal_index - 20)
    end = min(len(rows), signal_index + 21)
    selected = list(rows[start:end])
    chart_rows = [dict(row) for row in selected]
    indexes = [int(row["market_bar_index"]) for row in chart_rows]
    path = CHART_ROOT / f"{stock}_{variant}_{label}.png"
    tick_indexes = list(range(len(chart_rows)))
    tick_labels = [str(index) for index in indexes]
    _render_chart(
        output_path=path,
        title=f"{stock} {variant} {label}",
        rows=chart_rows,
        ma_fields=("MMA5", "MMA10", "MMA20", "MMA60"),
        tick_labels=tick_labels,
        tick_indexes=tick_indexes,
    )
    path.with_suffix(".json").write_text(
        json.dumps(
            {**metadata, "chart_path": path.as_posix()},
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )
    return path.as_posix()


def _make_charts(
    rows_by_stock: Mapping[str, Sequence[Mapping[str, Any]]],
    results: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> list[str]:
    paths: list[str] = []
    CHART_ROOT.mkdir(parents=True, exist_ok=True)
    for stock in STREAMS:
        rows = rows_by_stock[stock]
        c0 = results[stock]["C0_RAW_MMA20"]
        c1 = results[stock]["C1_FILTER_COMPRESSED"]
        c1_buy = {
            int(event["market_bar_index"])
            for event in c1["signals"]
            if event["signal_type"] == "BUY"
        }
        c0_buys = [event for event in c0["signals"] if event["signal_type"] == "BUY"]
        for label, is_retained in (("removed", False), ("retained", True)):
            candidate = next(
                (
                    event
                    for event in c0_buys
                    if (int(event["market_bar_index"]) in c1_buy) is is_retained
                ),
                None,
            )
            if candidate is None:
                continue
            paths.append(
                _write_chart(
                    stock=stock,
                    variant="C0_C1",
                    label=label,
                    signal_index=int(candidate["market_bar_index"]) - 1,
                    rows=rows,
                    metadata={
                        "stock_code": stock,
                        "variant": "C0_C1",
                        "case": label,
                        "signal_event_id": candidate["event_id"],
                        "market_bar_index": candidate["market_bar_index"],
                    },
                )
            )
    h1 = [
        result
        for result in results.values()
        for result in [result["H1_COMPRESSION_RELEASE"]]
        if result["trades"]
    ]
    if h1:
        trade = h1[0]["trades"][0]
        stock = h1[0]["stock_code"]
        rows = rows_by_stock[stock]
        paths.append(
            _write_chart(
                stock=stock,
                variant="H1_COMPRESSION_RELEASE",
                label="completed_trade",
                signal_index=int(trade["entry_signal_market_bar_index"]) - 1,
                rows=rows,
                metadata={
                    "stock_code": stock,
                    "variant": "H1_COMPRESSION_RELEASE",
                    "trade_id": trade["trade_id"],
                    "entry_signal_market_bar_index": trade[
                        "entry_signal_market_bar_index"
                    ],
                    "exit_signal_market_bar_index": trade[
                        "exit_signal_market_bar_index"
                    ],
                },
            )
        )
    return paths


def _judgment(results: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> str:
    better = 0
    worse = 0
    for stock in STREAMS:
        c0 = results[stock]["C0_RAW_MMA20"]["metrics"]
        c1 = results[stock]["C1_FILTER_COMPRESSED"]["metrics"]
        if c1["terminal_mtm_return"] > c0["terminal_mtm_return"]:
            better += 1
        elif c1["terminal_mtm_return"] < c0["terminal_mtm_return"]:
            worse += 1
    if better and not worse:
        return "SUPPORTED_DEV"
    if worse and not better:
        return "NOT_SUPPORTED_DEV"
    if better or worse:
        return "PARTIALLY_SUPPORTED_DEV"
    return "INCONCLUSIVE_DEV"


def run_proof(
    *,
    output_path: Path = OUTPUT_PATH,
    trade_ledger_path: Path = TRADE_LEDGER_PATH,
    equity_path: Path = EQUITY_PATH,
    baseline_input: Path = BASELINE_INPUT_PATH,
    v15_input: Path = V15_INPUT_PATH,
    v18_events_path: Path = V18_EVENTS_PATH,
    create_charts: bool = True,
) -> dict[str, Any]:
    rows_by_stock, _verification = _load_development_streams(baseline_input, v15_input)
    rows_by_stock = {
        stock: _canonical_rows(rows) for stock, rows in rows_by_stock.items()
    }
    for rows in rows_by_stock.values():
        _decorate_causal_features(rows)
    windows = {stock: _common_window(rows) for stock, rows in rows_by_stock.items()}
    if {
        stock: windows[stock]["common_eligible_bar_count"] for stock in STREAMS
    } != COMMON_EXPECTED:
        raise DevelopmentProofError("V1.8 common comparison window changed")
    regression = _v18_regression(rows_by_stock, v18_events_path=v18_events_path)
    if regression["status"] != "PASS":
        raise DevelopmentProofError(
            f"V1.8 regression failed: {regression['mismatches']}"
        )

    results: dict[str, dict[str, dict[str, Any]]] = {}
    all_trades: list[dict[str, Any]] = []
    all_equity: list[dict[str, Any]] = []
    for stock in STREAMS:
        results[stock] = {}
        for variant in VARIANTS:
            result = _simulate_common(
                stock, rows_by_stock[stock], variant, windows[stock]
            )
            results[stock][variant] = result
            all_trades.extend(result["trades"])
            all_equity.extend(result["equity_rows"])

    charts = _make_charts(rows_by_stock, results) if create_charts else []
    _write_csv(trade_ledger_path, all_trades, TRADE_FIELDS)
    _write_csv(
        equity_path,
        all_equity,
        (
            "stock_code",
            "variant",
            "market_bar_index",
            "market_bar_id",
            "calendar_end_datetime",
            "position",
            "equity",
        ),
    )

    cohorts = {
        stock: _cohort_report(
            stock,
            rows_by_stock[stock],
            results[stock]["C0_RAW_MMA20"],
            results[stock]["C1_FILTER_COMPRESSED"],
        )
        for stock in STREAMS
    }
    h1_trades = sum(
        len(results[stock]["H1_COMPRESSION_RELEASE"]["trades"]) for stock in STREAMS
    )
    h1_status = (
        "SAMPLE_SPARSE"
        if h1_trades == 1
        else "SUFFICIENT_FOR_DESCRIPTIVE_REPORT"
        if h1_trades > 1
        else "NO_COMPLETED_CYCLE"
    )
    per_stock_summary: dict[str, dict[str, Any]] = {}
    for stock in STREAMS:
        per_stock_summary[stock] = {}
        for variant in VARIANTS:
            item = results[stock][variant]
            per_stock_summary[stock][variant] = {
                "common_window": item["common_window"],
                "entry_executions": sum(
                    event["signal_type"] == "BUY" for event in item["executions"]
                ),
                "exit_executions": sum(
                    event["signal_type"] == "SELL" for event in item["executions"]
                ),
                "open_at_end": item["open_position"],
                "metrics": item["metrics"],
                "terminal_position": item["terminal_position"],
            }

    filter_effect: dict[str, Any] = {}
    for stock in STREAMS:
        c0 = results[stock]["C0_RAW_MMA20"]
        c1 = results[stock]["C1_FILTER_COMPRESSED"]
        c0_entries = sum(event["signal_type"] == "BUY" for event in c0["executions"])
        c1_entries = sum(event["signal_type"] == "BUY" for event in c1["executions"])
        filter_effect[stock] = {
            "c0_entries": c0_entries,
            "c1_entries": c1_entries,
            "removed_entries": c0_entries - c1_entries,
            "retained_entry_ratio": Decimal(c1_entries) / Decimal(c0_entries)
            if c0_entries
            else None,
            "c0_metrics": c0["metrics"],
            "c1_metrics": c1["metrics"],
        }

    report: dict[str, Any] = {
        "proof_version": "MARKET_BAR_COMPRESSION_STRATEGY_ZERO_COST_DEVELOPMENT_PROOF_V1_9",
        "v1_8_checkpoint": {
            "design_head": V18_CHECKPOINT_HEAD,
            "source_commit": "UNCOMMITTED_V1_8_WORKTREE_AT_RUN",
            "output_artifact": V18_OUTPUT_PATH.as_posix(),
            "event_artifact": v18_events_path.as_posix(),
            "regression": regression,
        },
        "network_calls": 0,
        "scope": {
            "development_stocks": list(STREAMS),
            "validation_stocks": [],
            "validation_accessed": False,
            "network": False,
            "strategy_rule_changes": 0,
            "parameter_changes": 0,
            "transaction_costs": {
                "commission": Decimal(0),
                "tax": Decimal(0),
                "slippage": Decimal(0),
            },
        },
        "accounting_contract": {
            "capital": NORMALIZED_CAPITAL,
            "position_sizing": "FULL_NOTIONAL_LONG_FRACTIONAL_QUANTITY",
            "entry_exit_price": "actual Market-Bar OPEN",
            "trade_return": "exit_open/entry_open - 1",
            "equity_compounding": "equity_before * exit_open/entry_open",
            "terminal_open_policy": "OPEN_AT_RESEARCH_END_NO_FORCED_EXIT",
        },
        "common_window_policy": {
            "source": "V1.8_COMMON_COMPARISON_WINDOW",
            "warmup_market_bars": "MB001-MB085",
            "starts_flat": True,
            "window_counts": {
                stock: windows[stock]["common_eligible_bar_count"] for stock in STREAMS
            },
            "native_backtest": "DIAGNOSTIC_ONLY",
        },
        "stocks": per_stock_summary,
        "c0_vs_c1": filter_effect,
        "cohorts_c0_entries": cohorts,
        "existing_backtest_core": {
            "inspected": "src/backtest_engine/zero_cost_accounting.py",
            "reused": False,
            "reason": "V1.9 requires normalized fractional full-notional quantity; the existing integer-share strategy core is not changed.",
        },
        "h1_sparse": {
            "completed_cycles": h1_trades,
            "status": h1_status,
            "ranking_or_tuning_allowed": False,
        },
        "mechanical_exit_audit": {
            stock: {
                variant: {
                    "exit_signal_count": sum(
                        event["signal_type"] == "SELL"
                        for event in results[stock][variant]["signals"]
                    ),
                    "executable_exit_signal_count": sum(
                        event["signal_type"] == "SELL"
                        and event["execution_market_bar_index"] is not None
                        for event in results[stock][variant]["signals"]
                    ),
                    "completed_cycle_rate": (
                        Decimal(len(results[stock][variant]["trades"]))
                        / Decimal(
                            sum(
                                event["signal_type"] == "BUY"
                                for event in results[stock][variant]["executions"]
                            )
                        )
                        if results[stock][variant]["executions"]
                        and sum(
                            event["signal_type"] == "BUY"
                            for event in results[stock][variant]["executions"]
                        )
                        else None
                    ),
                    "terminal_open": results[stock][variant]["open_position"]
                    is not None,
                }
                for variant in VARIANTS
            }
            for stock in STREAMS
        },
        "hypothesis_judgments": {
            "STRATEGY_H1": _judgment(results),
            "STRATEGY_H2": "INCONCLUSIVE_SPARSE_DEV"
            if h1_status == "SAMPLE_SPARSE"
            else "DESCRIPTIVE_ONLY",
            "STRATEGY_H3": "MECHANICALLY_SUPPORTED"
            if all(
                all(
                    event["execution_market_bar_index"] is not None
                    for event in results[stock][variant]["signals"]
                    if event["signal_type"] == "SELL"
                )
                for stock in STREAMS
                for variant in VARIANTS
            )
            else "MECHANICALLY_PROBLEMATIC",
            "STRATEGY_H4": "IMPLEMENTATION_SUPPORTED"
            if regression["status"] == "PASS"
            else "FAILED",
        },
        "overall_dev_result": _judgment(results),
        "trade_ledger_path": trade_ledger_path.as_posix(),
        "equity_path": equity_path.as_posix(),
        "charts": charts,
        "registry_unchanged": True,
        "notes": [
            "Only V1.8 development streams were read.",
            "No validation signal, return, or PnL calculation was performed for 005380 or 068270.",
            "H1 is descriptive and SAMPLE_SPARSE; no rule tuning or superiority claim is made.",
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
    parser.add_argument("--trade-ledger", type=Path, default=TRADE_LEDGER_PATH)
    parser.add_argument("--equity", type=Path, default=EQUITY_PATH)
    parser.add_argument("--baseline-input", type=Path, default=BASELINE_INPUT_PATH)
    parser.add_argument("--v15-input", type=Path, default=V15_INPUT_PATH)
    parser.add_argument("--v18-events", type=Path, default=V18_EVENTS_PATH)
    parser.add_argument("--no-charts", action="store_true")
    args = parser.parse_args()
    report = run_proof(
        output_path=args.output,
        trade_ledger_path=args.trade_ledger,
        equity_path=args.equity,
        baseline_input=args.baseline_input,
        v15_input=args.v15_input,
        v18_events_path=args.v18_events,
        create_charts=not args.no_charts,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "network_calls": report["network_calls"],
                "overall_dev_result": report["overall_dev_result"],
                "charts": len(report["charts"]),
            },
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
