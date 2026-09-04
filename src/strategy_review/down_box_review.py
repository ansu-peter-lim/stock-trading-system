"""Offline visual proof for ``DOWN_BOX_REVERSAL_V0_1``.

The review layer consumes only the Daily artifacts already present in the
workspace.  It deliberately stops at signal/candidate evidence: no minute
bars, fills, position accounting, or PnL are inferred here.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.down_box_strategy import (
    BoxEvent,
    BoxEventType,
    BoxSetup,
    BoxSignal,
    BoxSignalType,
    run_down_box_signal_proof,
)
from src.backtest_engine.indicators import (
    calculate_daily_indicators,
    simple_moving_average,
)
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

UNIVERSE = (
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
OUTPUT_PATH = Path("data/processed/strategy_review/down_box_reversal_v0_1_proof.json")
CHART_ROOT = Path("data/processed/strategy_charts/down_box_reversal_v0_1")

_EVENT_MAP: dict[BoxEventType, tuple[ReviewEventType, str]] = {
    BoxEventType.LOWER_ZONE_TOUCH: (ReviewEventType.LOWER_ZONE_TOUCH, "LOWER ZONE"),
    BoxEventType.FIRST_SMA10_BREAKOUT: (
        ReviewEventType.FIRST_SMA10_BREAKOUT,
        "BREAKOUT",
    ),
    BoxEventType.REVERSAL_WAIT: (ReviewEventType.REVERSAL_WAIT, "WAIT"),
    BoxEventType.ENTRY_SIGNALLED: (ReviewEventType.ENTRY_SIGNALLED, "ENTRY SIGNALLED"),
    BoxEventType.SETUP_EXPIRED: (ReviewEventType.EXPIRED, "EXPIRED"),
    BoxEventType.MA5_TURN: (ReviewEventType.MA5_TURN, "MA5 TURN"),
    BoxEventType.MA5_BAND_TOUCH: (ReviewEventType.MA5_BAND_TOUCH, "MA5 TOUCH"),
    BoxEventType.SMA10_REBREAK: (ReviewEventType.SMA10_REBREAK, "SMA10 REBREAK"),
    BoxEventType.HALF_EXIT_SIGNAL: (ReviewEventType.HALF_EXIT_SIGNAL, "HALF EXIT"),
    BoxEventType.FLOOR_BREAK: (ReviewEventType.FLOOR_BREAK, "FLOOR BREAK"),
    BoxEventType.UPPER_TAKE_PROFIT: (
        ReviewEventType.UPPER_TAKE_PROFIT,
        "UPPER TAKE PROFIT",
    ),
    BoxEventType.BOX_BREAKOUT: (ReviewEventType.BOX_BREAKOUT, "BOX BREAKOUT"),
    BoxEventType.BREAKOUT_REENTRY_WAIT: (
        ReviewEventType.BREAKOUT_REENTRY_WAIT,
        "REENTRY WAIT",
    ),
    BoxEventType.BREAKOUT_FAILED: (ReviewEventType.BREAKOUT_FAILED, "BREAKOUT FAILED"),
    BoxEventType.BREAKOUT_REENTRY_CANDIDATE: (
        ReviewEventType.BREAKOUT_REENTRY_CANDIDATE,
        "REENTRY",
    ),
}


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal)):
        return str(value)
    if hasattr(value, "value"):
        return str(value.value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _event_price(
    event: BoxEvent,
    bars_by_date: Mapping[date, Any],
    setup: BoxSetup,
) -> Decimal:
    bar = bars_by_date[event.event_date]
    if event.event_type is BoxEventType.FLOOR_BREAK:
        return setup.box_floor
    if (
        event.event_type is BoxEventType.SETUP_INVALIDATED
        and event.reason == "HIGH_IN_UPPER_SELL_ZONE"
    ):
        return setup.box_upper
    if event.event_type in {
        BoxEventType.UPPER_TAKE_PROFIT,
        BoxEventType.BOX_BREAKOUT,
        BoxEventType.BREAKOUT_REENTRY_WAIT,
        BoxEventType.BREAKOUT_FAILED,
        BoxEventType.BREAKOUT_REENTRY_CANDIDATE,
    }:
        return setup.box_upper
    return bar.signal.close


def _signal_review_event(
    signal: BoxSignal, bars_by_date: Mapping[date, Any]
) -> ReviewEvent:
    label = {
        BoxSignalType.ENTRY_CANDIDATE_MA5_TURN: "BUY MA5 TURN",
        BoxSignalType.ENTRY_CANDIDATE_SMA10_REBREAK: "BUY REBREAK",
        BoxSignalType.ENTRY_CANDIDATE_MA5_TURN_AND_SMA10_REBREAK: "BUY BOTH",
        BoxSignalType.HALF_EXIT_SIGNAL: "HALF EXIT",
        BoxSignalType.FULL_EXIT_FLOOR_BREAK: "FLOOR EXIT",
        BoxSignalType.FULL_TAKE_PROFIT_UPPER: "UPPER EXIT",
        BoxSignalType.BREAKOUT_REENTRY_CANDIDATE: "REENTRY BUY",
    }[signal.signal_type]
    event_type = {
        BoxSignalType.HALF_EXIT_SIGNAL: ReviewEventType.HALF_EXIT_SIGNAL,
        BoxSignalType.FULL_EXIT_FLOOR_BREAK: ReviewEventType.FLOOR_BREAK,
        BoxSignalType.FULL_TAKE_PROFIT_UPPER: ReviewEventType.UPPER_TAKE_PROFIT,
        BoxSignalType.BREAKOUT_REENTRY_CANDIDATE: ReviewEventType.BREAKOUT_REENTRY_CANDIDATE,
    }.get(signal.signal_type, ReviewEventType.BOX_BUY_CANDIDATE)
    return ReviewEvent(
        event_type,
        signal.signal_date,
        label,
        adjusted_plot_price=bars_by_date[signal.signal_date].signal.close,
        details={"signal_type": signal.signal_type.value, **dict(signal.details)},
    )


def _setup_map(result: Mapping[str, Any]) -> dict[str, BoxSetup]:
    setups: dict[str, BoxSetup] = {}
    for event in result["events"]:
        if event.event_type is not BoxEventType.REVERSAL_SETUP_CREATED:
            continue
        origin = next(
            item
            for item in result["setup_origins"]
            if item["trade_date"] == event.event_date and item["issue"] is None
        )
        setups[event.setup_id] = BoxSetup(
            setup_id=event.setup_id,
            stock_code=event.stock_code,
            setup_origin_date=event.event_date,
            box_floor=origin["box_floor"],
            box_upper=origin["box_upper"],
            floor_pivot_date=origin["floor_pivot_date"],
            upper_pivot_date=origin["upper_pivot_date"],
        )
    return setups


def _review_events(
    result: Mapping[str, Any],
    setup: BoxSetup,
    bars: Sequence[Any],
) -> tuple[ReviewEvent, ...]:
    bars_by_date = {bar.trade_date: bar for bar in bars}
    events: list[ReviewEvent] = []
    for event in result["events"]:
        if event.setup_id != setup.setup_id:
            continue
        if (
            event.event_type is BoxEventType.SETUP_INVALIDATED
            and event.reason == "HIGH_IN_UPPER_SELL_ZONE"
        ):
            events.append(
                ReviewEvent(
                    ReviewEventType.UPPER_INVALIDATED,
                    event.event_date,
                    "UPPER INVALID",
                    adjusted_plot_price=_event_price(event, bars_by_date, setup),
                    details={"reason": event.reason},
                )
            )
            continue
        if event.event_type not in _EVENT_MAP:
            continue
        event_type, label = _EVENT_MAP[event.event_type]
        if event.event_type is BoxEventType.REVERSAL_WAIT and any(
            item.event_type is ReviewEventType.REVERSAL_WAIT for item in events
        ):
            continue
        events.append(
            ReviewEvent(
                event_type,
                event.event_date,
                label,
                adjusted_plot_price=_event_price(event, bars_by_date, setup),
                details={"reason": event.reason, **dict(event.details)},
            )
        )
    for signal in result["signals"]:
        if signal.setup_id == setup.setup_id:
            events.append(_signal_review_event(signal, bars_by_date))
    return tuple(events)


def _setup_summary(
    setup: BoxSetup,
    result: Mapping[str, Any],
    bars: Sequence[Any],
    points: Sequence[Any],
    chart_path: str | None,
) -> dict[str, Any]:
    index_by_date = {bar.trade_date: index for index, bar in enumerate(bars)}
    origin_index = index_by_date[setup.setup_origin_date]
    origin_point = points[origin_index]
    sma5 = simple_moving_average([bar.signal.close for bar in bars], 5)[origin_index]
    setup_events = [
        event for event in result["events"] if event.setup_id == setup.setup_id
    ]
    setup_signals = [
        signal for signal in result["signals"] if signal.setup_id == setup.setup_id
    ]
    lifecycle = next(
        (
            item
            for item in result.get("setup_lifecycle", ())
            if item["setup_id"] == setup.setup_id
        ),
        {},
    )
    event_dates = {event.event_type: event.event_date for event in setup_events}
    signal_dates = {signal.signal_type: signal.signal_date for signal in setup_signals}
    candidate = next(
        (
            signal
            for signal in setup_signals
            if signal.signal_type.name.startswith("ENTRY_CANDIDATE")
        ),
        None,
    )
    signal_point = (
        points[index_by_date[candidate.signal_date]] if candidate is not None else None
    )
    full_sma5 = simple_moving_average([bar.signal.close for bar in bars], 5)
    signal_sma5 = (
        full_sma5[index_by_date[candidate.signal_date]]
        if candidate is not None
        else None
    )
    invalidation = next(
        (
            event.reason
            for event in setup_events
            if event.event_type is BoxEventType.SETUP_INVALIDATED
        ),
        None,
    )
    expiry = event_dates.get(BoxEventType.SETUP_EXPIRED)
    return {
        "setup_id": setup.setup_id,
        "stock_code": setup.stock_code,
        "setup_origin_date": setup.setup_origin_date,
        "box_floor": setup.box_floor,
        "lower_zone_upper": setup.box_floor * Decimal("1.03"),
        "box_upper": setup.box_upper,
        "upper_sell_level": setup.box_upper * Decimal("0.97"),
        "floor_pivot_date": setup.floor_pivot_date,
        "upper_pivot_date": setup.upper_pivot_date,
        "lower_zone_touch_dates": [
            event.event_date
            for event in setup_events
            if event.event_type is BoxEventType.LOWER_ZONE_TOUCH
        ],
        "below_sma10_context_start": (
            bars[max(0, origin_index - 10)].trade_date if origin_index >= 10 else None
        ),
        "below_sma10_context_end": (
            bars[origin_index - 1].trade_date if origin_index >= 1 else None
        ),
        "first_breakout_date": event_dates.get(
            BoxEventType.FIRST_SMA10_BREAKOUT, setup.setup_origin_date
        ),
        "entry_type": candidate.signal_type.value if candidate else None,
        "entry_signal_date": candidate.signal_date if candidate else None,
        "sma5": sma5,
        "sma10": origin_point.sma10,
        "sma20": origin_point.sma20,
        "sma60": origin_point.sma60,
        "signal_sma5": signal_sma5,
        "signal_sma10": signal_point.sma10 if signal_point is not None else None,
        "signal_sma20": signal_point.sma20 if signal_point is not None else None,
        "signal_sma60": signal_point.sma60 if signal_point is not None else None,
        "trend_state": "DOWN",
        "half_exit_signal_date": signal_dates.get(BoxSignalType.HALF_EXIT_SIGNAL),
        "floor_break_date": event_dates.get(BoxEventType.FLOOR_BREAK),
        "upper_take_profit_date": event_dates.get(BoxEventType.UPPER_TAKE_PROFIT),
        "box_breakout_date": event_dates.get(BoxEventType.BOX_BREAKOUT),
        "reentry_candidate_date": signal_dates.get(
            BoxSignalType.BREAKOUT_REENTRY_CANDIDATE
        ),
        "setup_expiry_date": expiry,
        "invalidation_reason": invalidation,
        "terminal_outcome": lifecycle.get("terminal_outcome"),
        "terminal_date": lifecycle.get("terminal_date"),
        "terminal_reason": lifecycle.get("terminal_reason"),
        "daily_candidate": candidate is not None,
        "actual_trade": False,
        "trade_pnl": None,
        "chart_path": chart_path,
    }


def _funnel(result: Mapping[str, Any]) -> dict[str, int]:
    origins = list(result["setup_origins"])
    valid_floor = sum(item["issue"] not in {"NO_VALID_BOX_FLOOR"} for item in origins)
    valid_upper = sum(
        item["issue"] not in {"NO_VALID_BOX_FLOOR", "NO_VALID_BOX_UPPER"}
        for item in origins
    )
    events = list(result["events"])
    signals = list(result["signals"])
    return {
        "down_sessions": len(origins),
        "valid_box_floor": valid_floor,
        "valid_box_upper": valid_upper,
        "recent_lower_touch": sum(
            item["issue"] not in {"NO_RECENT_LOWER_ZONE_TOUCH"} for item in origins
        ),
        "below_sma10_context": sum(
            item["issue"] not in {"NO_FIRST_SMA10_BREAKOUT"} for item in origins
        ),
        "first_sma10_breakout": sum(
            item["issue"] not in {"NO_FIRST_SMA10_BREAKOUT"} for item in origins
        ),
        "reversal_setup": int(result["setups_created"]),
        "ma5_touch": sum(
            event.event_type is BoxEventType.MA5_BAND_TOUCH for event in events
        ),
        "ma5_turn": sum(event.event_type is BoxEventType.MA5_TURN for event in events),
        "sma10_rebreak": sum(
            event.event_type is BoxEventType.SMA10_REBREAK for event in events
        ),
        "buy_candidate": sum(
            signal.signal_type.name.startswith("ENTRY_CANDIDATE") for signal in signals
        ),
        "expired": sum(
            event.event_type is BoxEventType.SETUP_EXPIRED for event in events
        ),
        "floor_invalidation": sum(
            event.event_type is BoxEventType.FLOOR_BREAK for event in events
        ),
        "upper_invalidation": sum(
            event.reason == "HIGH_IN_UPPER_SELL_ZONE" for event in events
        ),
        "box_breakout_confirmed": sum(
            event.event_type is BoxEventType.BOX_BREAKOUT for event in events
        ),
        "breakout_reentry_wait": sum(
            event.event_type is BoxEventType.BREAKOUT_REENTRY_WAIT for event in events
        ),
        "breakout_failed": sum(
            event.event_type is BoxEventType.BREAKOUT_FAILED for event in events
        ),
        "breakout_reentry_candidate": sum(
            signal.signal_type is BoxSignalType.BREAKOUT_REENTRY_CANDIDATE
            for signal in signals
        ),
    }


def _chart_category(summary: Mapping[str, Any]) -> str:
    entry = summary["entry_type"]
    if entry == BoxSignalType.ENTRY_CANDIDATE_MA5_TURN.value:
        return "MA5_TURN"
    if entry == BoxSignalType.ENTRY_CANDIDATE_SMA10_REBREAK.value:
        return "SMA10_REBREAK"
    if entry == BoxSignalType.ENTRY_CANDIDATE_MA5_TURN_AND_SMA10_REBREAK.value:
        return "BOTH"
    if summary["invalidation_reason"] == "CLOSE_BELOW_BOX_FLOOR":
        return "FLOOR_INVALIDATION"
    if summary["invalidation_reason"] == "HIGH_IN_UPPER_SELL_ZONE":
        return "UPPER_INVALIDATION"
    if summary["box_breakout_date"] is not None:
        return "BOX_BREAKOUT"
    return "EXPIRED"


def _select_representative_charts(
    candidates: Sequence[tuple[str, str, BoxSetup, dict[str, Any], tuple[Any, ...]]],
) -> list[tuple[str, str, BoxSetup, dict[str, Any], tuple[Any, ...]]]:
    """Select the bounded eight-chart lifecycle review set deterministically."""

    targets = (
        ("MA5_TURN", 2),
        ("SMA10_REBREAK", 1),
        ("BOTH", 1),
        ("FLOOR_INVALIDATION", 2),
        ("UPPER_INVALIDATION", 1),
        ("EXPIRED", 1),
    )
    selected: list[tuple[str, str, BoxSetup, dict[str, Any], tuple[Any, ...]]] = []
    selected_ids: set[str] = set()
    selected_stocks: set[str] = set()
    for category, target_count in targets:
        rows = [item for item in candidates if item[1] == category]
        for prefer_new_stock in (True, False):
            for item in rows:
                if len([row for row in selected if row[1] == category]) >= target_count:
                    break
                if item[2].setup_id in selected_ids:
                    continue
                if prefer_new_stock and item[0] in selected_stocks:
                    continue
                selected.append(item)
                selected_ids.add(item[2].setup_id)
                selected_stocks.add(item[0])
    if len(selected) < 8:
        for item in candidates:
            if len(selected) >= 8:
                break
            if item[2].setup_id in selected_ids:
                continue
            selected.append(item)
            selected_ids.add(item[2].setup_id)
    return selected[:8]


def run_down_box_review_proof(
    *,
    output: Path = OUTPUT_PATH,
    chart_root: Path = CHART_ROOT,
    stocks: Sequence[str] = UNIVERSE,
) -> dict[str, Any]:
    """Run the bounded, deterministic, no-network visual review."""

    daily_cache: dict[str, tuple[Any, ...]] = {}
    proof_cache: dict[str, Mapping[str, Any]] = {}
    stock_rows: dict[str, dict[str, Any]] = {}
    chart_candidates: list[
        tuple[str, str, BoxSetup, dict[str, Any], tuple[Any, ...]]
    ] = []
    for stock_code in sorted(stocks):
        bars = daily_cache.setdefault(stock_code, _load_existing_daily_bars(stock_code))
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        points = tuple(calculate_daily_indicators(bars, calendar))
        result = run_down_box_signal_proof(bars, calendar=calendar)
        proof_cache[stock_code] = result
        setups = _setup_map(result)
        rows = []
        for setup in setups.values():
            summary = _setup_summary(setup, result, bars, points, None)
            chart_candidates.append(
                (stock_code, _chart_category(summary), setup, summary, bars)
            )
            rows.append(summary)
        stock_rows[stock_code] = {
            "funnel": _funnel(result),
            "setups": rows,
            "setup_rejections": result["setup_rejections"],
            "signal_count": len(result["signals"]),
        }

    category_order = {
        "MA5_TURN": 0,
        "SMA10_REBREAK": 1,
        "BOTH": 2,
        "EXPIRED": 3,
        "FLOOR_INVALIDATION": 4,
        "UPPER_INVALIDATION": 5,
        "BOX_BREAKOUT": 6,
        "BREAKOUT_REENTRY": 7,
    }
    chart_candidates.sort(
        key=lambda item: (
            category_order.get(item[1], 99),
            item[0],
            item[2].setup_origin_date,
            item[2].setup_id,
        )
    )
    selected = _select_representative_charts(chart_candidates)
    for stock_code, category, setup, summary, bars in selected:
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        result = proof_cache[stock_code]
        events = _review_events(result, setup, bars)
        origin_index = next(
            index
            for index, bar in enumerate(bars)
            if bar.trade_date == setup.setup_origin_date
        )
        window_start = bars[max(0, origin_index - 60)].trade_date
        window_end = bars[min(len(bars) - 1, origin_index + 30)].trade_date
        events = tuple(
            event for event in events if window_start <= event.event_date <= window_end
        )
        levels = {
            "BOX_FLOOR": setup.box_floor,
            "LOWER_ZONE_UPPER": setup.box_floor * Decimal("1.03"),
            "BOX_UPPER": setup.box_upper,
            "UPPER_SELL_LEVEL": setup.box_upper * Decimal("0.97"),
        }
        filename = deterministic_chart_filename(
            stock_code,
            ChartType.EVENT_REVIEW,
            setup.setup_origin_date,
            slug=f"down-box-{category.casefold()}",
        )
        chart_path = chart_root / category.casefold() / stock_code / filename
        prepared = prepare_review_chart(
            bars,
            chart_type=ChartType.EVENT_REVIEW,
            events=events,
            calendar=calendar,
            focus_date=setup.setup_origin_date,
            pre_sessions=60,
            post_sessions=30,
            show_sma5=True,
            shade_below_sma10_context=True,
            horizontal_levels=levels,
        )
        artifact = render_review_chart(
            prepared,
            chart_path,
            strategy_policy="DOWN_BOX_REVERSAL_V0_1_SIGNAL_ONLY",
            summary={**summary, "review_category": category},
        )
        summary["chart_path"] = artifact.png_path.as_posix()
        summary["metadata_path"] = artifact.metadata_path.as_posix()
        summary["review_category"] = category

    all_funnels = Counter()
    for row in stock_rows.values():
        all_funnels.update(row["funnel"])
    selected_rows = [item[3] for item in selected]
    result = {
        "proof_version": "DOWN_BOX_REVERSAL_V0_1_VISUAL_PROOF",
        "strategy_id": "DOWN_BOX_REVERSAL_V0_1",
        "network_calls": 0,
        "execution": {"five_minute": False, "fills": False, "pnl": False},
        "universe": sorted(stocks),
        "chart_window": {
            "pre_sessions": 60,
            "post_sessions": 30,
            "price_basis": "Daily adjusted signal OHLC",
        },
        "overall_funnel": dict(sorted(all_funnels.items())),
        "per_stock": stock_rows,
        "review_order": [
            "MA5_TURN",
            "SMA10_REBREAK",
            "BOTH",
            "EXPIRED",
            "FLOOR_INVALIDATION",
            "UPPER_INVALIDATION",
            "BOX_BREAKOUT",
            "BREAKOUT_REENTRY",
        ],
        "selected_charts": selected_rows,
        "selected_chart_count": len(selected_rows),
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
    result = run_down_box_review_proof(output=args.output, chart_root=args.chart_root)
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "selected_chart_count": result["selected_chart_count"],
                "overall_funnel": result["overall_funnel"],
            },
            indent=2,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
