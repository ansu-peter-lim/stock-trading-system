"""Offline metrics for the three explicitly approved algorithm hypotheses.

This module is an audit layer only.  It never changes Strategy V1 defaults and
never turns any H1/H2/H1-DOWN flag into an execution filter.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from statistics import median
from typing import Any

from src.backtest_engine.core_strategy import (
    DailyCoreSignalGenerator,
    DailyCoreSignalType,
)
from src.backtest_engine.down_strategy import analyze_down_entry
from src.backtest_engine.indicators import (
    DailyIndicatorPoint,
    calculate_daily_indicators,
)
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.kiwoom_minute.pipeline import (
    MinuteCollectionRequest,
    MinutePriceBasis,
    align_source_bars,
)
from src.kiwoom_minute.proof import run_up_path_sequence_proof
from src.kiwoom_minute.small_up_path_proof import (
    MINUTE_REQUIRED_START,
    RESEARCH_END,
    RESEARCH_START,
    _load_cached_minute_series,
    _load_existing_daily_bars,
)

from .chart import ReviewEvent, ReviewEventType

UP_UNIVERSE = (
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
ZERO_COST_FULL_WEIGHT = Decimal("0.10")
ZERO_COST_CAPITAL = Decimal(100000000)


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


class CrossType(str, Enum):
    GOLDEN = "GOLDEN"
    DEATH = "DEATH"


@dataclass(frozen=True, slots=True)
class CrossPoint:
    trade_date: date
    index: int
    cross_type: CrossType
    signal_price: Decimal


def _change_percent(
    values: Sequence[Decimal | None], index: int, lookback: int
) -> Decimal | None:
    if index < lookback:
        return None
    current, previous = values[index], values[index - lookback]
    if current is None or previous is None or previous == 0:
        return None
    return (current / previous - Decimal(1)) * Decimal(100)


def calculate_ma10_direction(
    points: Sequence[DailyIndicatorPoint],
) -> list[dict[str, Decimal | None]]:
    """Return current MA10 and 1/3/5-session percentage changes."""

    values = [point.sma10 for point in points]
    return [
        {
            "ma10_current": values[index],
            "ma10_change_1": _change_percent(values, index, 1),
            "ma10_slope_3": _change_percent(values, index, 3),
            "ma10_slope_5": _change_percent(values, index, 5),
        }
        for index in range(len(points))
    ]


def detect_ma10_ma20_crosses(
    points: Sequence[DailyIndicatorPoint],
) -> list[CrossPoint]:
    """Detect strict V1 GC/DC boundaries using completed Daily sessions."""

    crosses: list[CrossPoint] = []
    for index in range(1, len(points)):
        previous_fast, previous_slow = points[index - 1].sma10, points[index - 1].sma20
        current_fast, current_slow = points[index].sma10, points[index].sma20
        if None in (previous_fast, previous_slow, current_fast, current_slow):
            continue
        kind: CrossType | None = None
        if previous_fast <= previous_slow and current_fast > current_slow:
            kind = CrossType.GOLDEN
        elif previous_fast >= previous_slow and current_fast < current_slow:
            kind = CrossType.DEATH
        if kind is not None:
            crosses.append(
                CrossPoint(
                    points[index].trade_date,
                    index,
                    kind,
                    current_fast,
                )
            )
    return crosses


def recent_cross_metrics(
    points: Sequence[DailyIndicatorPoint],
    crosses: Sequence[CrossPoint],
    index: int,
    *,
    lookback_sessions: int = 10,
) -> dict[str, Any]:
    """Summarize GC OR DC in inclusive T-9..T; T-10 is excluded."""

    if lookback_sessions <= 0:
        raise ValueError("lookback_sessions must be positive")
    recent = [
        cross
        for cross in crosses
        if max(1, index - lookback_sessions + 1) <= cross.index <= index
    ]
    recent.sort(key=lambda cross: cross.index)
    latest = recent[-1] if recent else None
    return {
        "recent_cross_10d": bool(recent),
        "recent_cross_count_10d": len(recent),
        "most_recent_cross_type": latest.cross_type.value if latest else "NONE",
        "most_recent_cross_date": latest.trade_date if latest else None,
        "sessions_since_cross": index - latest.index if latest else None,
    }


def cross_review_events(crosses: Sequence[CrossPoint]) -> tuple[ReviewEvent, ...]:
    """Convert computed cross points to chart markers without price mixing."""

    return tuple(
        ReviewEvent(
            ReviewEventType.GOLDEN_CROSS
            if cross.cross_type is CrossType.GOLDEN
            else ReviewEventType.DEATH_CROSS,
            cross.trade_date,
            "GC" if cross.cross_type is CrossType.GOLDEN else "DC",
            adjusted_plot_price=cross.signal_price,
        )
        for cross in crosses
    )


def down_h1_metrics(
    points: Sequence[DailyIndicatorPoint], index: int
) -> dict[str, Any]:
    """Calculate T-4/T-7 and T-1/T-4 MA10 slopes, excluding T values."""

    if index < 7:
        return {
            "prior_slope_3": None,
            "recent_slope_3": None,
            "deceleration_status": "INSUFFICIENT_DATA",
        }
    values = [point.sma10 for point in points]
    prior: Decimal | None
    recent: Decimal | None
    if values[index - 7] is None or values[index - 4] is None:
        prior = None
    else:
        prior = (values[index - 4] / values[index - 7] - Decimal(1)) * Decimal(100)
    if values[index - 4] is None or values[index - 1] is None:
        recent = None
    else:
        recent = (values[index - 1] / values[index - 4] - Decimal(1)) * Decimal(100)
    status = (
        "INSUFFICIENT_DATA"
        if prior is None or recent is None
        else "PASS"
        if prior < recent < 0
        else "FAIL"
    )
    return {
        "prior_slope_3": prior,
        "recent_slope_3": recent,
        "deceleration_status": status,
    }


def _up_entry_classification(bar: DailyBar, point: DailyIndicatorPoint) -> str | None:
    if point.sma20 is None:
        return None
    lower = point.sma20 * Decimal("0.97")
    upper = point.sma20 * Decimal("1.03")
    low_near = lower <= bar.signal.low <= upper
    close_near = lower <= bar.signal.close <= upper
    if low_near and close_near:
        return "LOW_AND_CLOSE"
    if low_near:
        return "LOW_ONLY"
    if close_near:
        return "CLOSE_ONLY"
    return None


def audit_up_stock(
    bars: Sequence[DailyBar],
    completed_trades: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Audit all stateless UP candidates plus completed proof trades."""

    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in canonical)
    points = tuple(calculate_daily_indicators(canonical, calendar))
    directions = calculate_ma10_direction(points)
    crosses = detect_ma10_ma20_crosses(points)
    point_by_date = {
        point.trade_date: (index, point) for index, point in enumerate(points)
    }
    generator = DailyCoreSignalGenerator(calendar)
    candidates: list[dict[str, Any]] = []
    for index, (bar, point) in enumerate(zip(canonical, points, strict=True)):
        if not RESEARCH_START <= bar.trade_date <= RESEARCH_END:
            continue
        signal = generator.evaluate(
            bar,
            point,
            holding_core=False,
            stock_full_weight=ZERO_COST_FULL_WEIGHT,
        )
        if signal is None or signal.signal_type is not DailyCoreSignalType.ENTER:
            continue
        candidates.append(
            {
                "stock_code": bar.stock_code,
                "signal_date": bar.trade_date,
                "entry_classification": _up_entry_classification(bar, point),
                **directions[index],
                **recent_cross_metrics(points, crosses, index),
                "actual_trade": False,
            }
        )
    trades: list[dict[str, Any]] = []
    for trade in completed_trades:
        signal_date = _as_date(trade["entry_daily_signal_date"])
        if signal_date not in point_by_date:
            raise ValueError(f"proof trade date missing from Daily bars: {signal_date}")
        index, point = point_by_date[signal_date]
        bar = canonical[index]
        trades.append(
            {
                "stock_code": bar.stock_code,
                "signal_date": signal_date,
                "pnl_pct": Decimal(trade["pnl_pct"]),
                "entry_classification": _up_entry_classification(bar, point),
                "close_only": _up_entry_classification(bar, point) == "CLOSE_ONLY",
                "large_winner": Decimal(trade["pnl_pct"]) >= Decimal(20),
                "large_loss": Decimal(trade["pnl_pct"]) <= Decimal(-8),
                **directions[index],
                **recent_cross_metrics(points, crosses, index),
            }
        )
    actual_dates = {row["signal_date"] for row in trades}
    for row in candidates:
        row["actual_trade"] = row["signal_date"] in actual_dates
        matching = [
            trade for trade in trades if trade["signal_date"] == row["signal_date"]
        ]
        if matching:
            row["pnl_pct"] = matching[0]["pnl_pct"]
    return {
        "stock_code": canonical[0].stock_code,
        "candidate_count": len(candidates),
        "trade_count": len(trades),
        "candidates": candidates,
        "trades": trades,
        "crosses": [asdict(cross) for cross in crosses],
        "h1_summary": summarize_h1(trades),
        "h2_summary": summarize_h2(trades),
    }


def summarize_h1(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ma10_1_session": _group_summary(
            trades,
            lambda row: row["ma10_change_1"] is not None and row["ma10_change_1"] < 0,
        )
        | {
            "NON_DOWN": _group_summary(
                trades,
                lambda row: (
                    row["ma10_change_1"] is not None and row["ma10_change_1"] >= 0
                ),
            )
        },
        "ma10_3_session": _signed_groups(trades, "ma10_slope_3"),
        "ma10_5_session": _signed_groups(trades, "ma10_slope_5"),
    }


def _signed_groups(trades: Sequence[dict[str, Any]], field: str) -> dict[str, Any]:
    return {
        "DOWN": _group_summary(
            trades, lambda row: row[field] is not None and row[field] < 0
        ),
        "NON_DOWN": _group_summary(
            trades, lambda row: row[field] is not None and row[field] >= 0
        ),
    }


def summarize_h2(trades: Sequence[dict[str, Any]]) -> dict[str, Any]:
    grouped = {
        "RECENT_CROSS_10D_TRUE": _group_summary(
            trades, lambda row: row["recent_cross_10d"]
        ),
        "RECENT_CROSS_10D_FALSE": _group_summary(
            trades, lambda row: not row["recent_cross_10d"]
        ),
    }
    for cross_type in ("GOLDEN", "DEATH"):
        matching = [
            row for row in trades if row["most_recent_cross_type"] == cross_type
        ]
        grouped[f"MOST_RECENT_{cross_type}"] = _group_summary(matching, lambda _: True)
    return grouped


def _group_summary(trades: Sequence[dict[str, Any]], predicate: Any) -> dict[str, Any]:
    values = [row["pnl_pct"] for row in trades if predicate(row)]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins, Decimal(0))
    gross_loss = sum((-value for value in losses), Decimal(0))
    return {
        "count": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (Decimal(len(wins)) / Decimal(len(values)) * Decimal(100))
        if values
        else None,
        "mean_pnl_pct": sum(values, Decimal(0)) / Decimal(len(values))
        if values
        else None,
        "median_pnl_pct": median(values) if values else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "large_winner_count": sum(value >= Decimal(20) for value in values),
        "large_loss_count": sum(value <= Decimal(-8) for value in values),
    }


def audit_down_stock(
    bars: Sequence[DailyBar],
    completed_trades: Sequence[dict[str, Any]],
    proof_audits: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Audit every existing DOWN origin context + SMA10 breakout."""

    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in canonical)
    points = tuple(calculate_daily_indicators(canonical, calendar))
    actual_by_date = {
        _as_date(trade["entry_daily_signal_date"]): trade for trade in completed_trades
    }
    candidate_dates = {
        _as_date(audit["trade_date"])
        for audit in proof_audits
        if audit["kind"] == "DAILY_ENTER_SIGNAL"
    }
    rows: list[dict[str, Any]] = []
    for index, (bar, point) in enumerate(zip(canonical, points, strict=True)):
        facts = analyze_down_entry(canonical, points, index)
        if not facts.origin_context_satisfied:
            continue
        if not RESEARCH_START <= bar.trade_date <= RESEARCH_END:
            continue
        h1 = down_h1_metrics(points, index)
        trade = actual_by_date.get(bar.trade_date)
        rows.append(
            {
                "stock_code": bar.stock_code,
                "breakout_date": bar.trade_date,
                "prior_slope_3": h1["prior_slope_3"],
                "recent_slope_3": h1["recent_slope_3"],
                "deceleration_status": h1["deceleration_status"],
                "rise_branch": facts.rise_branch.value if facts.rise_branch else None,
                "block_reasons": [reason.value for reason in facts.block_reasons],
                "candidate": bar.trade_date in candidate_dates,
                "actual_trade": trade is not None,
                "pnl_pct": Decimal(trade["pnl_pct"]) if trade else None,
                "red_three_soldiers": facts.red_three_soldiers,
            }
        )
    return {
        "stock_code": canonical[0].stock_code,
        "breakout_count": len(rows),
        "breakouts": rows,
        "summary": summarize_down(rows),
    }


def summarize_down(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for status in ("PASS", "FAIL", "INSUFFICIENT_DATA"):
        subset = [row for row in rows if row["deceleration_status"] == status]
        result[f"DECELERATION_{status}"] = {
            "breakout_count": len(subset),
            "candidate_count": sum(row["candidate"] for row in subset),
            "actual_trade_count": sum(row["actual_trade"] for row in subset),
            "blocked_count": sum(bool(row["block_reasons"]) for row in subset),
            "rise_branch_distribution": dict(
                sorted(Counter(row["rise_branch"] for row in subset).items())
            ),
            "trade_pnl_pct": [
                row["pnl_pct"] for row in subset if row["pnl_pct"] is not None
            ],
        }
    return result


def _load_up_proof_result(
    stock_code: str,
) -> tuple[tuple[DailyBar, ...], dict[str, Any]]:
    daily_bars = _load_existing_daily_bars(stock_code)
    raw = _load_cached_minute_series(
        MinuteCollectionRequest(
            stock_code, MINUTE_REQUIRED_START, RESEARCH_END, MinutePriceBasis.RAW
        )
    )
    adjusted = _load_cached_minute_series(
        MinuteCollectionRequest(
            stock_code, MINUTE_REQUIRED_START, RESEARCH_END, MinutePriceBasis.ADJUSTED
        )
    )
    if raw is None or adjusted is None:
        raise FileNotFoundError(f"missing cached minute artifacts for {stock_code}")
    source_bars, _ = align_source_bars(raw, adjusted, latest_label_time=time(15, 30))
    result = run_up_path_sequence_proof(
        daily_bars=daily_bars,
        source_bars=source_bars,
        calendar=ExplicitTradingCalendar(bar.trade_date for bar in daily_bars),
        research_start=RESEARCH_START,
        research_end=RESEARCH_END,
        stock_full_weight=ZERO_COST_FULL_WEIGHT,
        initial_capital=ZERO_COST_CAPITAL,
    )
    return daily_bars, result


def run_artifact_audit(
    *,
    up_proof_path: Path = Path(
        "data/processed/kiwoom/ten_stock_up_entry_policy_comparison.json"
    ),
    down_proof_path: Path = Path(
        "data/processed/kiwoom/ten_stock_down_path_sequence_proof.json"
    ),
) -> dict[str, Any]:
    """Run only offline calculations and return a deterministic audit document."""

    up_proof = json.loads(up_proof_path.read_text(encoding="utf-8"))
    down_proof = json.loads(down_proof_path.read_text(encoding="utf-8"))
    if set(UP_UNIVERSE) - set(up_proof.get("stocks", {})):
        raise ValueError("UP proof artifact does not cover the requested universe")
    up_stocks: dict[str, Any] = {}
    down_stocks: dict[str, Any] = {}
    for stock_code in UP_UNIVERSE:
        daily_bars, up_result = _load_up_proof_result(stock_code)
        up_stocks[stock_code] = audit_up_stock(
            daily_bars,
            up_result["completed_trades"],
        )
        stock_down = down_proof["per_stock"][stock_code]
        down_stocks[stock_code] = audit_down_stock(
            daily_bars,
            stock_down["completed_trades"],
            stock_down["audits"],
        )
    all_trades = [trade for stock in up_stocks.values() for trade in stock["trades"]]
    all_down = [row for stock in down_stocks.values() for row in stock["breakouts"]]
    return {
        "audit_version": "STRATEGY_ALGORITHM_IMPROVEMENT_AUDIT_V0.1",
        "network_calls": 0,
        "strategy_defaults_changed": False,
        "hypotheses": {
            "UP-H1_MA10_DIRECTION": {
                "slope_lookbacks": [1, 3, 5],
                "production_filter_applied": False,
            },
            "UP-H2_RECENT_MA10_MA20_CROSS": {
                "lookback_sessions": 10,
                "range": "T-9..T inclusive",
                "cross_operator": "GOLDEN OR DEATH",
                "production_filter_applied": False,
            },
            "DOWN-H1_MA10_DECELERATION": {
                "prior": "MA10[T-4]/MA10[T-7]-1",
                "recent": "MA10[T-1]/MA10[T-4]-1",
                "pass": "prior < recent < 0",
                "production_filter_applied": False,
            },
        },
        "up": {
            "stocks": up_stocks,
            "aggregate_h1": summarize_h1(all_trades),
            "aggregate_h2": summarize_h2(all_trades),
            "completed_trade_count": len(all_trades),
        },
        "down": {
            "stocks": down_stocks,
            "aggregate": summarize_down(all_down),
            "breakout_count": len(all_down),
        },
        "ab": {
            "executed": False,
            "reason": "audit-first; no production filter applied",
        },
    }


def write_artifact_audit(
    output: Path = Path(
        "data/processed/strategy_review/algorithm_improvement_audit_v0_1.json"
    ),
) -> dict[str, Any]:
    result = run_artifact_audit()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return result


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal, Enum)):
        return (
            value.isoformat()
            if isinstance(value, date)
            else str(value.value if isinstance(value, Enum) else value)
        )
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "data/processed/strategy_review/algorithm_improvement_audit_v0_1.json"
        ),
    )
    args = parser.parse_args()
    result = write_artifact_audit(args.output)
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "up_completed_trades": result["up"]["completed_trade_count"],
                "down_breakouts": result["down"]["breakout_count"],
                "network_calls": result["network_calls"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
