"""Proof-only UP-H1 three-session MA10 A/B comparison.

The runner deliberately lives outside the production strategy modules.  It
replays the existing cached ten-stock inputs independently with the already
approved LOW_REQUIRED policy (A) and the proof-only three-session MA10
non-down variant (B).  No strategy default or source artifact is changed.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.kiwoom_minute.pipeline import (
    MinuteCollectionRequest,
    MinutePriceBasis,
    align_source_bars,
)
from src.kiwoom_minute.proof import UpEntryPolicy, run_up_path_sequence_proof
from src.kiwoom_minute.small_up_path_proof import (
    MINUTE_REQUIRED_START,
    RESEARCH_END,
    RESEARCH_START,
    _load_cached_minute_series,
    _load_existing_daily_bars,
)

from .audit import UP_UNIVERSE, ZERO_COST_CAPITAL, ZERO_COST_FULL_WEIGHT, audit_up_stock
from .chart import (
    ChartType,
    ReviewEvent,
    ReviewEventType,
    deterministic_chart_filename,
    prepare_review_chart,
    render_review_chart,
)

OUTPUT_PATH = Path("data/processed/strategy_review/h1_ma10_3d_ab_v0_1.json")
CHART_ROOT = Path("data/processed/strategy_charts/h1_ma10_3d_ab")
POLICY_A = UpEntryPolicy.LOW_REQUIRED
POLICY_B = UpEntryPolicy.LOW_REQUIRED_MA10_3D_NON_DOWN


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _load_inputs(stock_code: str) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Load only immutable Daily and cached RAW/ADJUSTED minute artifacts."""

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
    return daily_bars, source_bars


def _run_variant(
    daily_bars: Sequence[Any], source_bars: Sequence[Any], policy: UpEntryPolicy
) -> dict[str, Any]:
    return run_up_path_sequence_proof(
        daily_bars=daily_bars,
        source_bars=source_bars,
        calendar=ExplicitTradingCalendar(bar.trade_date for bar in daily_bars),
        research_start=RESEARCH_START,
        research_end=RESEARCH_END,
        stock_full_weight=ZERO_COST_FULL_WEIGHT,
        initial_capital=ZERO_COST_CAPITAL,
        entry_policy=policy,
    )


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _direction(delta: Decimal | None) -> str:
    if delta is None:
        return "UNAVAILABLE"
    if delta > 0:
        return "BETTER"
    if delta < 0:
        return "WORSE"
    return "SAME"


def _metric_comparison(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("cumulative_return_pct", "mdd_pct", "win_rate_pct", "profit_factor"):
        av, bv = _decimal(a.get(key)), _decimal(b.get(key))
        delta = None if av is None or bv is None else bv - av
        result[key] = {"a": av, "b": bv, "delta": delta, "direction": _direction(delta)}
    usable = [
        item["direction"]
        for item in result.values()
        if item["direction"] in {"BETTER", "WORSE"}
    ]
    better = usable.count("BETTER")
    worse = usable.count("WORSE")
    result["overall"] = (
        "BETTER"
        if better >= 2 and better > worse
        else "WORSE"
        if worse >= 2 and worse > better
        else "SAME"
    )
    return result


def _entry_dates(result: Mapping[str, Any]) -> set[date]:
    return {
        _as_date(trade["entry_daily_signal_date"])
        for trade in result["completed_trades"]
    }


def _candidate_funnel(audit: Mapping[str, Any]) -> dict[str, Any]:
    candidates = list(audit["candidates"])
    low = [
        row
        for row in candidates
        if row["entry_classification"] in {"LOW_ONLY", "LOW_AND_CLOSE"}
    ]
    distribution = Counter(
        "NON_DOWN"
        if row["ma10_slope_3"] is not None and row["ma10_slope_3"] >= 0
        else "DOWN"
        if row["ma10_slope_3"] is not None
        else "INSUFFICIENT"
        for row in low
    )
    return {
        "stateless_daily_up_candidates": len(candidates),
        "low_required_candidates": len(low),
        "ma10_slope_3_distribution": dict(sorted(distribution.items())),
        "ma10_slope_3_non_down_survivors": distribution["NON_DOWN"],
        "ma10_slope_3_blocked_down": distribution["DOWN"],
        "ma10_slope_3_insufficient": distribution["INSUFFICIENT"],
    }


def _aggregate_funnel(stock_rows: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Sum candidate funnel counts without treating stocks as one portfolio."""

    distributions = Counter()
    totals = Counter()
    for row in stock_rows.values():
        funnel = row["funnel"]
        totals.update(
            {
                "stateless_daily_up_candidates": funnel[
                    "stateless_daily_up_candidates"
                ],
                "low_required_candidates": funnel["low_required_candidates"],
                "ma10_slope_3_non_down_survivors": funnel[
                    "ma10_slope_3_non_down_survivors"
                ],
                "ma10_slope_3_blocked_down": funnel["ma10_slope_3_blocked_down"],
                "ma10_slope_3_insufficient": funnel["ma10_slope_3_insufficient"],
            }
        )
        distributions.update(funnel["ma10_slope_3_distribution"])
    return {
        **dict(sorted(totals.items())),
        "ma10_slope_3_distribution": dict(sorted(distributions.items())),
    }


def _trade_lookup(result: Mapping[str, Any]) -> dict[date, Mapping[str, Any]]:
    return {
        _as_date(trade["entry_daily_signal_date"]): trade
        for trade in result["completed_trades"]
    }


def _entry_changes(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    a_dates, b_dates = _entry_dates(a), _entry_dates(b)
    removed, replacements = sorted(a_dates - b_dates), sorted(b_dates - a_dates)
    a_lookup, b_lookup = _trade_lookup(a), _trade_lookup(b)
    return {
        "removed_entries": [
            {
                "signal_date": day,
                "a_pnl_pct": a_lookup[day].get("pnl_pct"),
                "a_ma10_slope_3": a_lookup[day].get("ma10_slope_3"),
            }
            for day in removed
        ],
        "replacement_entries": [
            {
                "signal_date": day,
                "b_pnl_pct": b_lookup[day].get("pnl_pct"),
                "b_ma10_slope_3": b_lookup[day].get("ma10_slope_3"),
            }
            for day in replacements
        ],
    }


def _independent_aggregate(results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    trades = [
        trade for result in results.values() for trade in result["completed_trades"]
    ]
    wins = [trade for trade in trades if trade["pnl_amount"] > 0]
    losses = [trade for trade in trades if trade["pnl_amount"] < 0]
    gross_profit = sum((trade["pnl_amount"] for trade in wins), Decimal(0))
    gross_loss = -sum((trade["pnl_amount"] for trade in losses), Decimal(0))
    stock_returns = {
        code: result["metrics"]["cumulative_return_pct"]
        for code, result in sorted(results.items())
    }
    mdds = [result["metrics"]["mdd_pct"] for result in results.values()]
    returns = list(stock_returns.values())
    return {
        "independent_single_stock": True,
        "trade_count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": Decimal(len(wins)) / Decimal(len(trades)) * Decimal(100)
        if trades
        else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "mean_stock_return_pct": sum(returns, Decimal(0)) / len(returns)
        if returns
        else None,
        "median_stock_return_pct": median(returns) if returns else None,
        "mean_mdd_pct": sum(mdds, Decimal(0)) / len(mdds) if mdds else None,
        "median_mdd_pct": median(mdds) if mdds else None,
        "stock_returns": stock_returns,
        "profitable_stock_count": sum(value > 0 for value in returns),
        "losing_stock_count": sum(value < 0 for value in returns),
    }


def _large_trade_audit(
    a_results: Mapping[str, Mapping[str, Any]],
    b_results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    winners: list[dict[str, Any]] = []
    losses: list[dict[str, Any]] = []
    for code in sorted(a_results):
        a_lookup, b_lookup = (
            _trade_lookup(a_results[code]),
            _trade_lookup(b_results[code]),
        )
        for trade in a_results[code]["completed_trades"]:
            pnl = trade["pnl_pct"]
            day = _as_date(trade["entry_daily_signal_date"])
            if pnl >= Decimal(20):
                winners.append(
                    {
                        "stock_code": code,
                        "signal_date": day,
                        "a_pnl_pct": pnl,
                        "a_ma10_slope_3": a_lookup[day].get("ma10_slope_3"),
                        "status": "PRESERVED" if day in b_lookup else "REMOVED",
                        "b_pnl_pct": b_lookup[day].get("pnl_pct")
                        if day in b_lookup
                        else None,
                    }
                )
            if pnl <= Decimal(-8):
                losses.append(
                    {
                        "stock_code": code,
                        "signal_date": day,
                        "a_pnl_pct": pnl,
                        "status": "PRESERVED" if day in b_lookup else "REMOVED",
                        "b_pnl_pct": b_lookup[day].get("pnl_pct")
                        if day in b_lookup
                        else None,
                    }
                )
    return {
        "large_winners": winners,
        "large_losses": losses,
        "large_winner_count_a": len(winners),
        "large_winner_preserved_count": sum(
            row["status"] == "PRESERVED" for row in winners
        ),
        "large_loss_count_a": len(losses),
        "large_loss_removed_count": sum(row["status"] == "REMOVED" for row in losses),
    }


def _judgment(
    aggregate_a: Mapping[str, Any],
    aggregate_b: Mapping[str, Any],
    large: Mapping[str, Any],
) -> str:
    directions = [
        _direction(
            _decimal(aggregate_b.get("mean_stock_return_pct"))
            - _decimal(aggregate_a.get("mean_stock_return_pct"))
        )
        if _decimal(aggregate_a.get("mean_stock_return_pct")) is not None
        and _decimal(aggregate_b.get("mean_stock_return_pct")) is not None
        else "UNAVAILABLE",
        _direction(
            _decimal(aggregate_b.get("median_stock_return_pct"))
            - _decimal(aggregate_a.get("median_stock_return_pct"))
        )
        if _decimal(aggregate_a.get("median_stock_return_pct")) is not None
        and _decimal(aggregate_b.get("median_stock_return_pct")) is not None
        else "UNAVAILABLE",
        _direction(
            _decimal(aggregate_b.get("profit_factor"))
            - _decimal(aggregate_a.get("profit_factor"))
        )
        if _decimal(aggregate_a.get("profit_factor")) is not None
        and _decimal(aggregate_b.get("profit_factor")) is not None
        else "UNAVAILABLE",
    ]
    better, worse = directions.count("BETTER"), directions.count("WORSE")
    winners_preserved = (
        large["large_winner_preserved_count"] == large["large_winner_count_a"]
    )
    if (
        better == len([item for item in directions if item != "UNAVAILABLE"])
        and better >= 2
        and winners_preserved
    ):
        return "PROMISING_MA10_3D_STRONG"
    if better > worse and winners_preserved:
        return "PROMISING_BUT_MIXED"
    if worse >= 2 or not winners_preserved:
        return "KEEP_LOW_REQUIRED"
    return "PROMISING_BUT_MIXED"


def _trade_events(trade: Mapping[str, Any], label: str) -> tuple[ReviewEvent, ...]:
    signal_date = _as_date(trade["entry_daily_signal_date"])
    exit_signal_date = _as_date(trade["exit_daily_signal_date"])
    entry_fill_date = _as_date(trade["entry_fill_date"])
    exit_fill_date = _as_date(trade["exit_fill_date"])
    return (
        ReviewEvent(
            ReviewEventType.LOW_REQUIRED_ENTRY_CANDIDATE,
            signal_date,
            label,
            adjusted_plot_price=trade["entry_daily"]["signal_close"],
            details={"ma10_slope_3": trade.get("ma10_slope_3")},
        ),
        ReviewEvent(
            ReviewEventType.ENTRY_FILL,
            entry_fill_date,
            "ENTRY RAW",
            raw_fill_price=trade["entry_raw_price"],
            source_label=trade["entry_fill_source_label"],
        ),
        ReviewEvent(
            ReviewEventType.DAILY_FULL_EXIT,
            exit_signal_date,
            "FULL EXIT",
            adjusted_plot_price=trade["exit_daily"]["signal_close"],
        ),
        ReviewEvent(
            ReviewEventType.EXIT_FILL,
            exit_fill_date,
            "EXIT RAW",
            raw_fill_price=trade["exit_raw_price"],
            source_label=trade["exit_fill_source_label"],
        ),
    )


def generate_representative_charts(
    stock_bars: Mapping[str, Sequence[Any]],
    a_results: Mapping[str, Mapping[str, Any]],
    b_results: Mapping[str, Mapping[str, Any]],
    large: Mapping[str, Any],
    output_root: Path = CHART_ROOT,
) -> list[dict[str, Any]]:
    """Render bounded representative charts with proof metadata annotations."""

    selected: list[tuple[str, Mapping[str, Any], date]] = []
    winner_rows = list(large["large_winners"])
    loss_rows = list(large["large_losses"])
    selected.extend(
        ("REMOVED_LARGE_LOSS", a_results[row["stock_code"]], row["signal_date"])
        for row in loss_rows
        if row["status"] == "REMOVED"
    )
    selected.extend(
        ("REMOVED_LARGE_WINNER", a_results[row["stock_code"]], row["signal_date"])
        for row in winner_rows
        if row["status"] == "REMOVED"
    )
    for code in sorted(a_results):
        for trade in a_results[code]["completed_trades"]:
            day = _as_date(trade["entry_daily_signal_date"])
            if day in _entry_dates(b_results[code]) and trade["pnl_pct"] >= Decimal(20):
                selected.append(("SURVIVING_LARGE_WINNER", a_results[code], day))
    for code in sorted(a_results):
        for trade in b_results[code]["completed_trades"]:
            day = _as_date(trade["entry_daily_signal_date"])
            if day not in _entry_dates(a_results[code]):
                selected.append(("REPLACEMENT_ENTRY", b_results[code], day))
    # Keep chart output reviewable even if the proof has many changed entries.
    bounded: list[tuple[str, Mapping[str, Any], date]] = []
    per_kind = Counter()
    for kind, result, day in sorted(
        selected, key=lambda item: (item[0], item[1]["stock_code"], item[2])
    ):
        if per_kind[kind] >= 5:
            continue
        per_kind[kind] += 1
        bounded.append((kind, result, day))
    artifacts: list[dict[str, Any]] = []
    for kind, result, focus in bounded:
        trade = next(
            trade
            for trade in result["completed_trades"]
            if _as_date(trade["entry_daily_signal_date"]) == focus
        )
        prepared = prepare_review_chart(
            stock_bars[result["stock_code"]],
            chart_type=ChartType.EVENT_REVIEW,
            events=_trade_events(trade, kind),
            calendar=ExplicitTradingCalendar(
                bar.trade_date for bar in stock_bars[result["stock_code"]]
            ),
            focus_date=focus,
            event_end_date=_as_date(trade["exit_fill_date"]),
            show_ma20_band=True,
        )
        slug = f"h1-{kind.casefold().replace('_', '-')}"
        output = (
            output_root
            / result["stock_code"]
            / deterministic_chart_filename(
                result["stock_code"], ChartType.EVENT_REVIEW, focus, slug=slug
            )
        )
        artifact = render_review_chart(
            prepared,
            output,
            strategy_policy="UP_H1_MA10_3D_AB_PROOF_ONLY",
            summary={
                "h1_category": kind,
                "ma10_slope_3": trade.get("ma10_slope_3"),
                "pnl_pct": trade["pnl_pct"],
                "entry_signal_date": focus,
            },
        )
        artifacts.append(
            {
                "stock_code": result["stock_code"],
                "focus_date": focus,
                "category": kind,
                "png": artifact.png_path.as_posix(),
                "metadata": artifact.metadata_path.as_posix(),
                "backend": artifact.backend,
            }
        )
    return artifacts


def run_h1_ab_proof(
    *, output: Path = OUTPUT_PATH, charts: bool = True
) -> dict[str, Any]:
    """Run independent A/B replays from cached artifacts and write proof JSON."""

    stock_bars: dict[str, tuple[Any, ...]] = {}
    a_results: dict[str, dict[str, Any]] = {}
    b_results: dict[str, dict[str, Any]] = {}
    stock_rows: dict[str, Any] = {}
    for code in UP_UNIVERSE:
        daily_bars, source_bars = _load_inputs(code)
        stock_bars[code] = daily_bars
        a = _run_variant(daily_bars, source_bars, POLICY_A)
        b = _run_variant(daily_bars, source_bars, POLICY_B)
        a_results[code], b_results[code] = a, b
        audit = audit_up_stock(daily_bars, a["completed_trades"])
        # The proof runner intentionally stores only execution fields.  Add the
        # audited signal-time slope to in-memory copies so entry-change and
        # winner/loss reports can explain the H1 decision without altering any
        # persisted baseline artifact.
        slope_by_date = {
            row["signal_date"]: row["ma10_slope_3"]
            for row in (*audit["candidates"], *audit["trades"])
        }
        for variant in (a, b):
            for trade in variant["completed_trades"]:
                trade["ma10_slope_3"] = slope_by_date.get(
                    _as_date(trade["entry_daily_signal_date"])
                )
        stock_rows[code] = {
            "policy_a": {
                "entry_policy": POLICY_A.value,
                "counts": a["counts"],
                "metrics": a["metrics"],
            },
            "policy_b": {
                "entry_policy": POLICY_B.value,
                "counts": b["counts"],
                "metrics": b["metrics"],
            },
            "funnel": _candidate_funnel(audit),
            "metric_comparison": _metric_comparison(a["metrics"], b["metrics"]),
            "entry_changes": _entry_changes(a, b),
            "candidate_count_audit": audit["candidate_count"],
        }
    aggregate_a = _independent_aggregate(a_results)
    aggregate_b = _independent_aggregate(b_results)
    large = _large_trade_audit(a_results, b_results)
    aggregate_funnel = _aggregate_funnel(stock_rows)
    chart_artifacts = (
        generate_representative_charts(stock_bars, a_results, b_results, large)
        if charts
        else []
    )
    result = {
        "proof_version": "UP_H1_MA10_3D_AB_PROOF_V0.1",
        "network_calls": 0,
        "strategy_defaults_changed": False,
        "universe": list(UP_UNIVERSE),
        "period": {
            "minute_required_start": MINUTE_REQUIRED_START,
            "research_start": RESEARCH_START,
            "research_end": RESEARCH_END,
        },
        "contract": {
            "policy_a": "LOW_REQUIRED",
            "policy_b": "LOW_REQUIRED_MA10_3D_NON_DOWN",
            "ma10_slope_3": "MA10[T] / MA10[T-3] - 1; T included; no future values",
            "boundary": "slope < 0 blocks; slope == 0 and slope > 0 allow",
            "low_band": "signal_low in inclusive signal_SMA20 +/- 3%",
            "price_basis": "signal prices for signal; RAW prices for fills",
            "cost_profile": "ZERO_COST",
            "stock_full_weight": ZERO_COST_FULL_WEIGHT,
        },
        "stocks": stock_rows,
        "aggregate_funnel": aggregate_funnel,
        "aggregate": {"policy_a": aggregate_a, "policy_b": aggregate_b},
        "large_trade_audit": large,
        "removed_entry_count": sum(
            len(row["entry_changes"]["removed_entries"]) for row in stock_rows.values()
        ),
        "replacement_entry_count": sum(
            len(row["entry_changes"]["replacement_entries"])
            for row in stock_rows.values()
        ),
        "charts": chart_artifacts,
        "judgment": _judgment(aggregate_a, aggregate_b, large),
        "next_candidate": "UP-H1 MA10 3-session stateful proof review; no production default change",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--no-charts", action="store_true")
    args = parser.parse_args()
    result = run_h1_ab_proof(output=args.output, charts=not args.no_charts)
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "policy_a_trades": result["aggregate"]["policy_a"]["trade_count"],
                "policy_b_trades": result["aggregate"]["policy_b"]["trade_count"],
                "removed_entries": result["removed_entry_count"],
                "replacement_entries": result["replacement_entry_count"],
                "judgment": result["judgment"],
                "network_calls": result["network_calls"],
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
