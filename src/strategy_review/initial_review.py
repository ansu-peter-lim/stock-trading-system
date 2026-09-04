"""Generate the first offline UP/DOWN Strategy V1 review-chart set."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import calculate_daily_indicators
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.kiwoom_minute.small_up_path_proof import _load_existing_daily_bars

from .chart import (
    ChartType,
    ReviewEvent,
    ReviewEventType,
    deterministic_chart_filename,
    prepare_review_chart,
    render_review_chart,
)

OUTPUT_ROOT = Path("data/processed/strategy_charts")
UP_PROOF = Path("data/processed/kiwoom/small_up_path_sequence_proof.json")
DOWN_PROOF = Path("data/processed/kiwoom/ten_stock_down_path_sequence_proof.json")

UP_SELECTIONS = (
    ("005930", "2025-12-16", "large-winner"),
    ("005930", "2026-06-30", "close-only-worst"),
    ("000660", "2025-09-04", "large-winner"),
    ("000660", "2026-03-24", "close-only-worst"),
    ("005930", "2026-03-16", "close-only-removed"),
    ("005930", "2026-06-11", "close-only-removed"),
    ("000660", "2026-07-03", "close-only-removed"),
)
DOWN_TRADE_SELECTIONS = (("035720", "2026-05-14"), ("012450", "2026-07-23"))
DOWN_BLOCK_SELECTIONS = (("005380", "2026-07-23"), ("035720", "2026-04-08"))


def main() -> None:
    artifacts = generate_initial_review_set()
    print(json.dumps(artifacts, ensure_ascii=False, indent=2))


def generate_initial_review_set() -> list[dict[str, str]]:
    """Generate only the explicitly selected offline review examples."""

    up = _read_json(UP_PROOF)
    down = _read_json(DOWN_PROOF)
    output: list[dict[str, str]] = []
    daily_cache: dict[str, tuple[Any, ...]] = {}

    for stock_code, signal_text, slug in UP_SELECTIONS:
        bars = daily_cache.setdefault(stock_code, _load_existing_daily_bars(stock_code))
        trade = _find_trade(
            up["stocks"][stock_code]["primary"]["completed_trades"], signal_text
        )
        events, classification = _up_events(trade)
        output.append(
            _render(
                bars,
                events,
                date.fromisoformat(signal_text),
                date.fromisoformat(trade["exit_fill_date"]),
                OUTPUT_ROOT / stock_code,
                f"up-{slug}",
                "UP_BASELINE_WITH_LOW_REQUIRED_COMPARISON",
                _up_summary(bars, trade, classification),
            )
        )

    for stock_code, signal_text in DOWN_TRADE_SELECTIONS:
        bars = daily_cache.setdefault(stock_code, _load_existing_daily_bars(stock_code))
        trade = _find_trade(
            down["per_stock"][stock_code]["completed_trades"], signal_text
        )
        output.append(
            _render(
                bars,
                _down_trade_events(trade),
                date.fromisoformat(signal_text),
                date.fromisoformat(trade["exit_fill_date"]),
                OUTPUT_ROOT / "down" / stock_code,
                "down-completed-trade",
                "DOWN_REVERSAL_V1_ZERO_COST",
                _down_trade_summary(trade),
            )
        )

    for stock_code, event_text in DOWN_BLOCK_SELECTIONS:
        bars = daily_cache.setdefault(stock_code, _load_existing_daily_bars(stock_code))
        audit = _find_audit(down["per_stock"][stock_code]["audits"], event_text)
        output.append(
            _render(
                bars,
                _down_block_events(audit),
                date.fromisoformat(event_text),
                None,
                OUTPUT_ROOT / "down" / stock_code,
                "down-blocked",
                "DOWN_REVERSAL_V1_ZERO_COST",
                {"reason": audit["blocks"], **audit["snapshot"]},
            )
        )
    return output


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_trade(trades: list[dict[str, Any]], signal_text: str) -> dict[str, Any]:
    matches = [
        trade for trade in trades if trade["entry_daily_signal_date"] == signal_text
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one proof trade for {signal_text}")
    return matches[0]


def _find_audit(audits: list[dict[str, Any]], event_text: str) -> dict[str, Any]:
    matches = [
        audit
        for audit in audits
        if audit["kind"] == "BLOCKED_ORIGIN" and audit["trade_date"] == event_text
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one blocked proof event for {event_text}")
    return matches[0]


def _up_classification(trade: dict[str, Any]) -> tuple[str, bool]:
    snapshot = trade["entry_daily"]
    sma20 = Decimal(snapshot["signal_sma20"])
    lower, upper = sma20 * Decimal("0.97"), sma20 * Decimal("1.03")
    low_near = lower <= Decimal(snapshot["signal_low"]) <= upper
    close_near = lower <= Decimal(snapshot["signal_close"]) <= upper
    if low_near and close_near:
        return "LOW_AND_CLOSE", True
    if low_near:
        return "LOW_ONLY", True
    if close_near:
        return "CLOSE_ONLY", False
    raise ValueError("selected UP proof trade has neither configured MA20 approach")


def _up_events(trade: dict[str, Any]) -> tuple[tuple[ReviewEvent, ...], str]:
    signal_date = date.fromisoformat(trade["entry_daily_signal_date"])
    snapshot = trade["entry_daily"]
    classification, low_required = _up_classification(trade)
    events = [
        ReviewEvent(
            ReviewEventType.BASELINE_ENTRY_CANDIDATE,
            signal_date,
            "CLOSE_ONLY" if classification == "CLOSE_ONLY" else "UP BUY",
            adjusted_plot_price=Decimal(snapshot["signal_close"]),
            details={"classification": classification},
        )
    ]
    if low_required:
        events.append(
            ReviewEvent(
                ReviewEventType.LOW_REQUIRED_ENTRY_CANDIDATE,
                signal_date,
                "LOW_REQUIRED",
                adjusted_plot_price=Decimal(snapshot["signal_low"]),
                details={"classification": classification},
            )
        )
    events.extend(
        (
            ReviewEvent(
                ReviewEventType.ENTRY_FILL,
                date.fromisoformat(trade["entry_fill_date"]),
                "ENTRY",
                raw_fill_price=Decimal(trade["entry_raw_price"]),
                source_label=trade["entry_fill_source_label"],
            ),
            ReviewEvent(
                ReviewEventType.DAILY_FULL_EXIT,
                date.fromisoformat(trade["exit_daily_signal_date"]),
                "EXIT SIGNAL",
                adjusted_plot_price=Decimal(trade["exit_daily"]["signal_close"]),
            ),
            ReviewEvent(
                ReviewEventType.EXIT_FILL,
                date.fromisoformat(trade["exit_fill_date"]),
                "EXIT FILL",
                raw_fill_price=Decimal(trade["exit_raw_price"]),
                source_label=trade["exit_fill_source_label"],
            ),
        )
    )
    return tuple(events), classification


def _down_trade_events(trade: dict[str, Any]) -> tuple[ReviewEvent, ...]:
    signal_date = date.fromisoformat(trade["entry_daily_signal_date"])
    close = Decimal(trade["entry_daily"]["signal_close"])
    return (
        ReviewEvent(
            ReviewEventType.DOWN_CONTEXT_SATISFIED,
            signal_date,
            "DOWN CONTEXT",
            adjusted_plot_price=close,
        ),
        ReviewEvent(
            ReviewEventType.SMA10_BREAKOUT,
            signal_date,
            "DOWN BREAKOUT",
            adjusted_plot_price=close,
        ),
        ReviewEvent(
            ReviewEventType.DAILY_BUY_CANDIDATE,
            signal_date,
            "DOWN BUY",
            adjusted_plot_price=close,
        ),
        ReviewEvent(
            ReviewEventType.ENTRY_FILL,
            date.fromisoformat(trade["entry_fill_date"]),
            "ENTRY",
            raw_fill_price=Decimal(trade["entry_raw_price"]),
            source_label=trade["entry_fill_source_label"],
        ),
        ReviewEvent(
            ReviewEventType.DAILY_FULL_EXIT,
            date.fromisoformat(trade["exit_daily_signal_date"]),
            "EXIT SIGNAL",
            adjusted_plot_price=Decimal(trade["exit_daily"]["signal_close"]),
        ),
        ReviewEvent(
            ReviewEventType.EXIT_FILL,
            date.fromisoformat(trade["exit_fill_date"]),
            "EXIT FILL",
            raw_fill_price=Decimal(trade["exit_raw_price"]),
            source_label=trade["exit_fill_source_label"],
        ),
    )


def _down_block_events(audit: dict[str, Any]) -> tuple[ReviewEvent, ...]:
    event_date = date.fromisoformat(audit["trade_date"])
    close = Decimal(audit["snapshot"]["signal_close"])
    events = [
        ReviewEvent(
            ReviewEventType.DOWN_CONTEXT_SATISFIED,
            event_date,
            "DOWN CONTEXT",
            adjusted_plot_price=close,
        ),
        ReviewEvent(
            ReviewEventType.SMA10_BREAKOUT,
            event_date,
            "DOWN BREAKOUT",
            adjusted_plot_price=close,
        ),
    ]
    if audit["snapshot"]["red_three_soldiers"]:
        events.append(
            ReviewEvent(
                ReviewEventType.RED_THREE_SOLDIERS,
                event_date,
                "3 SOLDIERS",
                adjusted_plot_price=close,
            )
        )
    block_events = {
        "MA20_SLOPE_AT_OR_BELOW_MINUS_FIVE_PERCENT": (
            ReviewEventType.REJECTED_STEEP_MA20,
            "STEEP BLOCK",
        ),
        "HIGH_NEAR_MA20_AND_CLOSE_BELOW_MA20": (
            ReviewEventType.REJECTED_MA20_RESISTANCE,
            "MA20 RESIST",
        ),
        "HIGH_NEAR_MA60_AND_CLOSE_BELOW_MA60": (
            ReviewEventType.REJECTED_MA60_RESISTANCE,
            "MA60 RESIST",
        ),
    }
    for reason in audit["blocks"]:
        event_type, label = block_events[reason]
        events.append(
            ReviewEvent(
                event_type,
                event_date,
                label,
                adjusted_plot_price=close,
                details={"reason": reason},
            )
        )
    return tuple(events)


def _up_summary(
    bars: tuple[Any, ...], trade: dict[str, Any], classification: str
) -> dict[str, Any]:
    points = {
        point.trade_date: point
        for point in calculate_daily_indicators(
            bars, ExplicitTradingCalendar(bar.trade_date for bar in bars)
        )
    }
    signal_date = date.fromisoformat(trade["entry_daily_signal_date"])
    point = points[signal_date]
    return {
        "event_type": "UP_TRADE",
        "signal_date": signal_date,
        "trend": trade["entry_daily"]["trend"],
        "entry_classification": classification,
        "signal_close": trade["entry_daily"]["signal_close"],
        "sma10": point.sma10,
        "sma20": point.sma20,
        "sma60": point.sma60,
        "slope20": point.ma20_slope_5,
        "slope60": point.ma60_slope_5,
        "daily_return": point.daily_return,
        "entry_fill_at": trade["entry_fill_source_label"],
        "entry_raw_price": trade["entry_raw_price"],
        "exit_signal_date": trade["exit_daily_signal_date"],
        "exit_fill_at": trade["exit_fill_source_label"],
        "exit_raw_price": trade["exit_raw_price"],
        "trade_pnl": trade["pnl_pct"],
        "reason": trade["entry_condition"],
    }


def _down_trade_summary(trade: dict[str, Any]) -> dict[str, Any]:
    snapshot = trade["entry_daily"]
    return {
        "event_type": "DOWN_TRADE",
        "signal_date": trade["entry_daily_signal_date"],
        "trend": "DOWN",
        "signal_close": snapshot["signal_close"],
        "sma10": snapshot["signal_sma10"],
        "sma20": snapshot["signal_sma20"],
        "sma60": snapshot["signal_sma60"],
        "slope20": snapshot["ma20_slope_5_pct"],
        "slope60": snapshot["ma60_slope_5_pct"],
        "daily_return": snapshot["rise_pct"],
        "entry_fill_at": trade["entry_fill_source_label"],
        "entry_raw_price": trade["entry_raw_price"],
        "exit_signal_date": trade["exit_daily_signal_date"],
        "exit_fill_at": trade["exit_fill_source_label"],
        "exit_raw_price": trade["exit_raw_price"],
        "trade_pnl": trade["pnl_pct"],
        "reason": trade["entry_branch"],
    }


def _render(
    bars: tuple[Any, ...],
    events: tuple[ReviewEvent, ...],
    focus_date: date,
    event_end_date: date | None,
    output_dir: Path,
    slug: str,
    policy: str,
    summary: dict[str, Any],
) -> dict[str, str]:
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
    prepared = prepare_review_chart(
        bars,
        chart_type=ChartType.EVENT_REVIEW,
        events=events,
        calendar=calendar,
        focus_date=focus_date,
        event_end_date=event_end_date,
    )
    filename = deterministic_chart_filename(
        prepared.stock_code,
        prepared.chart_type,
        focus_date,
        slug=slug,
    )
    artifact = render_review_chart(
        prepared,
        output_dir / filename,
        strategy_policy=policy,
        summary=summary,
    )
    return {
        "png": artifact.png_path.as_posix(),
        "metadata": artifact.metadata_path.as_posix(),
        "backend": artifact.backend,
    }


if __name__ == "__main__":
    main()
