"""Generate a complete visual audit for the existing DOWN breakout proof."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.down_strategy import (
    DownBlockReason,
    analyze_down_entry,
)
from src.backtest_engine.indicators import calculate_daily_indicators
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.kiwoom_minute.small_up_path_proof import _load_existing_daily_bars

from .audit import down_h1_metrics
from .chart import (
    ChartType,
    ReviewEvent,
    ReviewEventType,
    deterministic_chart_filename,
    prepare_review_chart,
    render_review_chart,
)

DOWN_PROOF = Path("data/processed/kiwoom/ten_stock_down_path_sequence_proof.json")
OUTPUT_PATH = Path("data/processed/strategy_review/down_breakout_15_visual_audit.json")
CHART_ROOT = Path("data/processed/strategy_charts/down_breakout_15")

# Explicitly retained as a contract check against the 15 rows found in the
# existing DOWN proof.  No new breakout is inferred by this audit tool.
BREAKOUTS = (
    ("035720", date(2026, 3, 17)),
    ("035720", date(2026, 4, 8)),
    ("035720", date(2026, 5, 14)),
    ("035720", date(2026, 7, 2)),
    ("005380", date(2026, 7, 23)),
    ("035420", date(2025, 12, 29)),
    ("035420", date(2026, 3, 17)),
    ("035420", date(2026, 4, 10)),
    ("035420", date(2026, 7, 21)),
    ("068270", date(2026, 5, 21)),
    ("012450", date(2025, 12, 3)),
    ("012450", date(2026, 6, 12)),
    ("012450", date(2026, 7, 23)),
    ("034020", date(2026, 6, 15)),
    ("034020", date(2026, 7, 23)),
)


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _find_trade(
    trades: list[Mapping[str, Any]], signal_date: date
) -> Mapping[str, Any] | None:
    matches = [
        trade
        for trade in trades
        if date.fromisoformat(str(trade["entry_daily_signal_date"])) == signal_date
    ]
    if len(matches) > 1:
        raise ValueError(f"duplicate completed trade for {signal_date}")
    return matches[0] if matches else None


def _candidate_exists(audits: list[Mapping[str, Any]], signal_date: date) -> bool:
    return any(
        audit.get("kind") == "DAILY_ENTER_SIGNAL"
        and date.fromisoformat(str(audit["trade_date"])) == signal_date
        for audit in audits
    )


def _block_event(reason: str, event_date: date, close: Decimal) -> ReviewEvent:
    mapping = {
        DownBlockReason.STEEP_MA20.value: (
            ReviewEventType.REJECTED_STEEP_MA20,
            "STEEP",
        ),
        DownBlockReason.MA20_RESISTANCE.value: (
            ReviewEventType.REJECTED_MA20_RESISTANCE,
            "MA20 RESIST",
        ),
        DownBlockReason.MA60_RESISTANCE.value: (
            ReviewEventType.REJECTED_MA60_RESISTANCE,
            "MA60 RESIST",
        ),
    }
    event_type, label = mapping[reason]
    return ReviewEvent(
        event_type,
        event_date,
        label,
        adjusted_plot_price=close,
        details={"reason": reason},
    )


def _events(
    *,
    event_date: date,
    bar: Any,
    facts: Any,
    h1: Mapping[str, Any],
    candidate: bool,
    trade: Mapping[str, Any] | None,
) -> tuple[ReviewEvent, ...]:
    close = bar.signal.close
    events: list[ReviewEvent] = [
        ReviewEvent(
            ReviewEventType.DOWN_CONTEXT_SATISFIED,
            event_date,
            "10D BELOW MA10",
            adjusted_plot_price=close,
            details={"prior_ten_below_sma10": facts.prior_ten_below_sma10},
        ),
        ReviewEvent(
            ReviewEventType.SMA10_BREAKOUT,
            event_date,
            "BREAKOUT",
            adjusted_plot_price=close,
            details={"rise_pct": facts.rise_pct},
        ),
        ReviewEvent(
            ReviewEventType.DECELERATION_PASS
            if h1["deceleration_status"] == "PASS"
            else ReviewEventType.DECELERATION_FAIL,
            event_date,
            "DECEL PASS" if h1["deceleration_status"] == "PASS" else "DECEL FAIL",
            adjusted_plot_price=close,
            details={
                "prior_slope_3": h1["prior_slope_3"],
                "recent_slope_3": h1["recent_slope_3"],
            },
        ),
    ]
    events.extend(
        _block_event(reason.value, event_date, close) for reason in facts.block_reasons
    )
    if facts.red_three_soldiers:
        events.append(
            ReviewEvent(
                ReviewEventType.RED_THREE_SOLDIERS,
                event_date,
                "3 SOLDIERS",
                adjusted_plot_price=close,
            )
        )
    if candidate:
        events.append(
            ReviewEvent(
                ReviewEventType.DAILY_BUY_CANDIDATE,
                event_date,
                "BUY",
                adjusted_plot_price=close,
            )
        )
    if trade is not None:
        events.extend(
            (
                ReviewEvent(
                    ReviewEventType.ENTRY_FILL,
                    date.fromisoformat(str(trade["entry_fill_date"])),
                    "ENTRY",
                    raw_fill_price=Decimal(str(trade["entry_raw_price"])),
                    source_label=str(trade["entry_fill_source_label"]),
                ),
                ReviewEvent(
                    ReviewEventType.DAILY_FULL_EXIT,
                    date.fromisoformat(str(trade["exit_daily_signal_date"])),
                    "EXIT SIGNAL",
                    adjusted_plot_price=Decimal(
                        str(trade["exit_daily"]["signal_close"])
                    ),
                ),
                ReviewEvent(
                    ReviewEventType.EXIT_FILL,
                    date.fromisoformat(str(trade["exit_fill_date"])),
                    "EXIT",
                    raw_fill_price=Decimal(str(trade["exit_raw_price"])),
                    source_label=str(trade["exit_fill_source_label"]),
                ),
            )
        )
    return tuple(events)


def _row_and_chart(
    stock_code: str,
    signal_date: date,
    bars: tuple[Any, ...],
    stock_proof: Mapping[str, Any],
    output_root: Path,
) -> dict[str, Any]:
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in canonical)
    points = tuple(calculate_daily_indicators(canonical, calendar))
    index_by_date = {bar.trade_date: index for index, bar in enumerate(canonical)}
    if signal_date not in index_by_date:
        raise ValueError(f"breakout date missing from Daily bars: {signal_date}")
    index = index_by_date[signal_date]
    bar, point = canonical[index], points[index]
    facts = analyze_down_entry(canonical, points, index)
    if not facts.origin_context_satisfied:
        raise ValueError(
            f"expected DOWN breakout context for {stock_code} {signal_date}"
        )
    h1 = down_h1_metrics(points, index)
    audits = list(stock_proof["audits"])
    candidate = _candidate_exists(audits, signal_date)
    trade = _find_trade(list(stock_proof["completed_trades"]), signal_date)
    status = h1["deceleration_status"]
    if status not in {"PASS", "FAIL"}:
        raise ValueError(
            f"unexpected deceleration status for {stock_code} {signal_date}"
        )
    events = _events(
        event_date=signal_date,
        bar=bar,
        facts=facts,
        h1=h1,
        candidate=candidate,
        trade=trade,
    )
    prepared = prepare_review_chart(
        canonical,
        chart_type=ChartType.EVENT_REVIEW,
        events=events,
        calendar=calendar,
        focus_date=signal_date,
        pre_sessions=60,
        post_sessions=20,
        shade_below_sma10_context=True,
    )
    group = status.casefold()
    chart_path = (
        output_root
        / group
        / stock_code
        / deterministic_chart_filename(
            stock_code,
            ChartType.EVENT_REVIEW,
            signal_date,
            slug=f"down-breakout-decel-{group}",
        )
    )
    summary = {
        "stock_code": stock_code,
        "signal_date": signal_date,
        "rise_pct": facts.rise_pct,
        "ma10_prior_slope_3": h1["prior_slope_3"],
        "ma10_recent_slope_3": h1["recent_slope_3"],
        "deceleration_status": status,
        "ma20_slope": point.ma20_slope_5,
        "steep_block": DownBlockReason.STEEP_MA20 in facts.block_reasons,
        "ma20_resistance": DownBlockReason.MA20_RESISTANCE in facts.block_reasons,
        "ma60_resistance": DownBlockReason.MA60_RESISTANCE in facts.block_reasons,
        "soldiers": facts.red_three_soldiers,
        "daily_candidate": candidate,
        "actual_trade": trade is not None,
        "trade_pnl": Decimal(str(trade["pnl_pct"])) if trade else None,
        "blocks": [reason.value for reason in facts.block_reasons],
        "window": {"pre_sessions": 60, "post_sessions": 20},
    }
    artifact = render_review_chart(
        prepared,
        chart_path,
        strategy_policy="DOWN_REVERSAL_V1_ZERO_COST_VISUAL_AUDIT",
        summary=summary,
    )
    return {
        **summary,
        "chart_path": artifact.png_path.as_posix(),
        "metadata_path": artifact.metadata_path.as_posix(),
        "render_backend": artifact.backend,
    }


def run_down_visual_audit(
    *, output: Path = OUTPUT_PATH, chart_root: Path = CHART_ROOT
) -> dict[str, Any]:
    """Render all 15 existing breakouts without changing strategy parameters."""

    proof = json.loads(DOWN_PROOF.read_text(encoding="utf-8"))
    daily_cache: dict[str, tuple[Any, ...]] = {}
    rows: list[dict[str, Any]] = []
    for stock_code, signal_date in BREAKOUTS:
        bars = daily_cache.setdefault(stock_code, _load_existing_daily_bars(stock_code))
        stock_proof = proof["per_stock"][stock_code]
        rows.append(
            _row_and_chart(stock_code, signal_date, bars, stock_proof, chart_root)
        )
    rows.sort(
        key=lambda row: (
            0 if row["deceleration_status"] == "PASS" else 1,
            row["signal_date"],
            row["stock_code"],
        )
    )
    if len(rows) != 15:
        raise ValueError(f"expected 15 breakout rows, got {len(rows)}")
    result = {
        "audit_version": "DOWN_BREAKOUT_15_VISUAL_AUDIT_V0.1",
        "network_calls": 0,
        "source_proof": DOWN_PROOF.as_posix(),
        "chart_window": {
            "focus": "breakout T",
            "pre_sessions": 60,
            "post_sessions": 20,
            "price_basis": "Daily adjusted signal OHLC",
            "raw_fill_policy": "metadata only",
        },
        "review_order": ["PASS", "FAIL"],
        "groups": {
            "PASS": sum(row["deceleration_status"] == "PASS" for row in rows),
            "FAIL": sum(row["deceleration_status"] == "FAIL" for row in rows),
            "ACTUAL_TRADE": sum(row["actual_trade"] for row in rows),
        },
        "rows": rows,
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
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    args = parser.parse_args()
    result = run_down_visual_audit(output=args.output, chart_root=args.chart_root)
    print(json.dumps({"output": args.output.as_posix(), **result["groups"]}, indent=2))


if __name__ == "__main__":
    main()
