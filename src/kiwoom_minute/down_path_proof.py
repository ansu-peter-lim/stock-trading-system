"""Stateful ZERO_COST DOWN-path proof on opaque ka10080 source order."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from src.backtest_engine.core_strategy import DailyCoreSignalType
from src.backtest_engine.down_strategy import (
    DownBlockReason,
    DownDailySignalGenerator,
    DownEntryBranch,
    DownRiseBranch,
    SurgeSetupEventType,
    analyze_down_entry,
)
from src.backtest_engine.indicators import calculate_daily_indicators
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import TradingCalendar

from .pipeline import ASSUMPTION_ID, MinuteSourceBar
from .proof import (
    _holding_sessions,
    _metrics,
    _next_global_bar,
    _schedule,
    _source_indicators,
    _validate_source_sequence,
)


def run_down_path_sequence_proof(
    *,
    daily_bars: Sequence[DailyBar],
    source_bars: Sequence[MinuteSourceBar],
    calendar: TradingCalendar,
    research_start: date,
    research_end: date,
    stock_full_weight: Decimal,
    initial_capital: Decimal,
) -> dict[str, Any]:
    """Run only the fixed Strategy V1 DOWN Core path and ExitPolicy C."""

    if research_start > research_end:
        raise ValueError("research_start must not follow research_end")
    if not source_bars:
        raise ValueError("source_bars must not be empty")
    stock_codes = {bar.stock_code for bar in daily_bars} | {
        bar.stock_code for bar in source_bars
    }
    if len(stock_codes) != 1:
        raise ValueError("proof supports exactly one stock")
    stock_code = next(iter(stock_codes))
    if not Decimal(0) < stock_full_weight <= Decimal(1):
        raise ValueError("stock_full_weight must be in (0, 1]")
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")

    canonical_daily = tuple(sorted(daily_bars, key=lambda bar: bar.trade_date))
    canonical_source = tuple(
        sorted(source_bars, key=lambda bar: (bar.source_label, bar.source_bar_id))
    )
    _validate_source_sequence(canonical_source)
    daily_points = tuple(calculate_daily_indicators(canonical_daily, calendar))
    daily_index = {bar.trade_date: index for index, bar in enumerate(canonical_daily)}
    source_points = _source_indicators(canonical_source)
    by_date: dict[date, list[tuple[MinuteSourceBar, dict[str, Any]]]] = defaultdict(
        list
    )
    for bar, point in zip(canonical_source, source_points, strict=True):
        by_date[bar.trading_date].append((bar, point))

    generator = DownDailySignalGenerator(calendar)
    cash = initial_capital
    quantity = 0
    pending: dict[str, Any] | None = None
    scheduled: dict[str, Any] | None = None
    current_trade: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    counts = {
        "down_sessions": 0,
        "prior_ten_context_satisfied": 0,
        "sma10_breakouts": 0,
        "rise_below_five": 0,
        "rise_five_to_ten": 0,
        "rise_above_ten": 0,
        "steep_slope_blocked": 0,
        "ma20_resistance_blocked": 0,
        "ma60_resistance_blocked": 0,
        "soldiers_accepted": 0,
        "surge_setups_created": 0,
        "surge_setups_superseded": 0,
        "surge_pullback_touches": 0,
        "surge_setup_entries": 0,
        "surge_setups_expired": 0,
        "daily_buy_candidates": 0,
        "reversal_candidates": 0,
        "soldiers_candidates": 0,
        "pending_entries": 0,
        "entry_execution_signals": 0,
        "entry_fills": 0,
        "entry_expirations": 0,
        "daily_full_exit_signals": 0,
        "exit_c_triggers": 0,
        "exit_fills": 0,
    }

    known_days = tuple(bar.trade_date for bar in canonical_daily)
    research_days = [day for day in known_days if research_start <= day <= research_end]
    for day in research_days:
        if (
            pending is not None
            and pending["status"] == "PENDING"
            and pending["activation"] == day
        ):
            pending["status"] = "ARMED"

        day_rows = by_date.get(day, [])
        for day_index, (bar, point) in enumerate(day_rows):
            if scheduled is not None and scheduled["fill_bar_id"] == bar.source_bar_id:
                if not (
                    bar.source_bar_sequence > scheduled["signal_sequence"]
                    and bar.source_bar_id != scheduled["signal_bar_id"]
                ):
                    raise ValueError("source-sequence fill causality violated")
                price = bar.raw.open
                if scheduled["side"] == "BUY":
                    budget = (
                        scheduled["equity_at_decision"]
                        * stock_full_weight
                        * Decimal("0.90")
                    )
                    fill_quantity = int(
                        (budget / price).to_integral_value(rounding=ROUND_FLOOR)
                    )
                    if fill_quantity <= 0 or price * fill_quantity > cash:
                        raise ValueError("entry cannot purchase one integer share")
                    cash -= price * fill_quantity
                    quantity = fill_quantity
                    counts["entry_fills"] += 1
                    current_trade = {
                        "entry_daily_signal_date": scheduled[
                            "daily_signal"
                        ].generated_trade_date,
                        "entry_daily": scheduled["daily_snapshot"],
                        "entry_branch": scheduled["daily_signal"].entry_branch.value,
                        "entry_execution_condition": scheduled["condition"],
                        "entry_execution_source_label": scheduled["signal_label"],
                        "entry_fill_source_label": bar.source_label,
                        "entry_fill_sequence": bar.source_bar_sequence,
                        "entry_fill_date": day,
                        "entry_raw_price": price,
                        "quantity": fill_quantity,
                        "mae_price": price,
                        "mfe_price": price,
                    }
                else:
                    if current_trade is None or quantity <= 0:
                        raise ValueError("exit fill requires an open Core position")
                    exit_quantity = quantity
                    cash += price * exit_quantity
                    quantity = 0
                    counts["exit_fills"] += 1
                    trade = dict(current_trade)
                    trade.update(
                        {
                            "exit_daily_signal_date": scheduled[
                                "daily_signal"
                            ].generated_trade_date,
                            "exit_daily": scheduled["daily_snapshot"],
                            "exit_c_source_label": scheduled["signal_label"],
                            "exit_fill_source_label": bar.source_label,
                            "exit_fill_sequence": bar.source_bar_sequence,
                            "exit_fill_date": day,
                            "exit_raw_price": price,
                            "pnl_amount": (price - trade["entry_raw_price"])
                            * Decimal(exit_quantity),
                            "pnl_pct": (price / trade["entry_raw_price"] - Decimal(1))
                            * Decimal(100),
                            "holding_sessions": _holding_sessions(
                                known_days, trade["entry_fill_date"], day
                            ),
                            "holding_source_rows": (
                                bar.source_bar_sequence - trade["entry_fill_sequence"]
                            ),
                            "mae_pct": (
                                trade["mae_price"] / trade["entry_raw_price"]
                                - Decimal(1)
                            )
                            * Decimal(100),
                            "mfe_pct": (
                                trade["mfe_price"] / trade["entry_raw_price"]
                                - Decimal(1)
                            )
                            * Decimal(100),
                            "exit_delay_sessions": _holding_sessions(
                                known_days,
                                scheduled["daily_signal"].generated_trade_date,
                                day,
                            )
                            - 1,
                            "exit_delay_effect_pct": (
                                price / scheduled["daily_raw_close"] - Decimal(1)
                            )
                            * Decimal(100),
                        }
                    )
                    trades.append(trade)
                    current_trade = None
                fills.append(
                    {
                        "side": scheduled["side"],
                        "source_label": bar.source_label,
                        "source_bar_sequence": bar.source_bar_sequence,
                        "raw_price": price,
                        "quantity": scheduled.get("quantity", quantity),
                    }
                )
                pending = None
                scheduled = None

            if current_trade is not None:
                current_trade["mae_price"] = min(
                    current_trade["mae_price"], bar.raw.low
                )
                current_trade["mfe_price"] = max(
                    current_trade["mfe_price"], bar.raw.high
                )

            if (
                pending is not None
                and pending["status"] == "ARMED"
                and scheduled is None
            ):
                condition: str | None = None
                if pending["kind"] == "ENTER" and pending["activation"] == day:
                    if point["sma20"] is not None and point["sma60"] is not None:
                        if day_index == 0 and point["sma20"] > point["sma60"]:
                            condition = "FIRST_SOURCE_BAR_MA20_ABOVE_MA60"
                        elif day_index > 0 and point["golden_cross"]:
                            condition = "NEW_SOURCE_BAR_MA20_MA60_GOLDEN_CROSS"
                    if condition is not None:
                        counts["entry_execution_signals"] += 1
                        next_bar = (
                            day_rows[day_index + 1][0]
                            if day_index + 1 < len(day_rows)
                            else None
                        )
                        if next_bar is not None:
                            scheduled = _schedule(
                                pending, bar, next_bar, "BUY", condition
                            )
                            pending["status"] = "ORDER_SCHEDULED"
                elif (
                    pending["kind"] == "FULL_EXIT"
                    and day >= pending["activation"]
                    and point["sma60"] is not None
                    and bar.signal.close < point["sma60"]
                ):
                    counts["exit_c_triggers"] += 1
                    next_bar = _next_global_bar(
                        canonical_source, bar.source_bar_sequence
                    )
                    if next_bar is not None and next_bar.trading_date <= research_end:
                        scheduled = _schedule(
                            pending,
                            bar,
                            next_bar,
                            "SELL",
                            "EXIT_C_SOURCE_CLOSE_BELOW_MA60",
                        )
                        scheduled["quantity"] = quantity
                        pending["status"] = "ORDER_SCHEDULED"

            equity_curve.append(
                {
                    "source_label": bar.source_label,
                    "equity": cash + Decimal(quantity) * bar.raw.close,
                    "holding": quantity > 0,
                }
            )

        if (
            pending is not None
            and pending["kind"] == "ENTER"
            and pending["activation"] == day
            and pending["status"] in {"ARMED", "ORDER_SCHEDULED"}
        ):
            counts["entry_expirations"] += 1
            pending = None
            scheduled = None

        index = daily_index[day]
        bar = canonical_daily[index]
        point = daily_points[index]
        facts = analyze_down_entry(
            canonical_daily, daily_points, index, generator.config
        )
        _count_funnel(facts, counts)
        if pending is not None:
            continue
        decision = generator.evaluate(
            canonical_daily,
            daily_points,
            index,
            holding_core=quantity > 0,
            entry_allowed=quantity == 0,
            stock_full_weight=stock_full_weight,
        )
        for event in decision.setup_events:
            _count_setup_event(event.event_type, counts)
            audits.append(
                {
                    "kind": f"SURGE_SETUP_{event.event_type.value}",
                    "trade_date": day,
                    "setup_id": event.setup_id,
                    "sessions_elapsed": event.sessions_elapsed,
                    "blocks": [reason.value for reason in event.block_reasons],
                    "snapshot": _daily_snapshot(bar, point, facts),
                }
            )
        signal = decision.signal
        if signal is None:
            if facts.origin_context_satisfied and facts.block_reasons:
                audits.append(
                    {
                        "kind": "BLOCKED_ORIGIN",
                        "trade_date": day,
                        "blocks": [reason.value for reason in facts.block_reasons],
                        "snapshot": _daily_snapshot(bar, point, facts),
                    }
                )
            continue
        snapshot = _daily_snapshot(bar, point, facts)
        if signal.signal_type is DailyCoreSignalType.ENTER:
            counts["daily_buy_candidates"] += 1
            counts["pending_entries"] += 1
            if signal.entry_branch is DownEntryBranch.REVERSAL:
                counts["reversal_candidates"] += 1
            elif signal.entry_branch is DownEntryBranch.RED_THREE_SOLDIERS:
                counts["soldiers_candidates"] += 1
            kind = "ENTER"
        else:
            counts["daily_full_exit_signals"] += 1
            kind = "FULL_EXIT"
        audits.append(
            {
                "kind": f"DAILY_{kind}_SIGNAL",
                "trade_date": day,
                "entry_branch": signal.entry_branch.value
                if signal.entry_branch
                else None,
                "snapshot": snapshot,
            }
        )
        pending = {
            "kind": kind,
            "status": "PENDING",
            "activation": signal.activation_trade_date,
            "daily_signal": signal,
            "daily_snapshot": snapshot,
            "daily_raw_close": bar.raw.close,
            "equity_at_decision": cash + Decimal(quantity) * bar.raw.close,
        }

    return {
        "proof_version": "DOWN_PATH_SEQUENCE_PROOF_V1",
        "assumption_id": ASSUMPTION_ID,
        "stock_code": stock_code,
        "research_start": research_start.isoformat(),
        "research_end": research_end.isoformat(),
        "stock_full_weight": stock_full_weight,
        "core_fraction_of_full": Decimal("0.90"),
        "initial_capital": initial_capital,
        "counts": counts,
        "completed_trades": trades,
        "fills": fills,
        "audits": audits,
        "active_setup_at_end": generator.active_setup,
        "open_position_at_end": quantity > 0,
        "open_quantity": quantity,
        "final_cash": cash,
        "metrics": _metrics(
            initial_capital,
            cash,
            quantity,
            canonical_source,
            equity_curve,
            trades,
            fills,
        ),
    }


def _count_funnel(facts: Any, counts: dict[str, int]) -> None:
    if not facts.down_trend:
        return
    counts["down_sessions"] += 1
    if not facts.prior_ten_below_sma10:
        return
    counts["prior_ten_context_satisfied"] += 1
    if not facts.sma10_breakout:
        return
    counts["sma10_breakouts"] += 1
    branch_key = {
        DownRiseBranch.BELOW_FIVE: "rise_below_five",
        DownRiseBranch.FIVE_TO_TEN: "rise_five_to_ten",
        DownRiseBranch.ABOVE_TEN: "rise_above_ten",
    }.get(facts.rise_branch)
    if branch_key is not None:
        counts[branch_key] += 1
    if DownBlockReason.STEEP_MA20 in facts.block_reasons:
        counts["steep_slope_blocked"] += 1
    if DownBlockReason.MA20_RESISTANCE in facts.block_reasons:
        counts["ma20_resistance_blocked"] += 1
    if DownBlockReason.MA60_RESISTANCE in facts.block_reasons:
        counts["ma60_resistance_blocked"] += 1
    if facts.rise_branch is DownRiseBranch.BELOW_FIVE and facts.red_three_soldiers:
        counts["soldiers_accepted"] += 1


def _count_setup_event(event_type: SurgeSetupEventType, counts: dict[str, int]) -> None:
    key = {
        SurgeSetupEventType.CREATED: "surge_setups_created",
        SurgeSetupEventType.SUPERSEDED: "surge_setups_superseded",
        SurgeSetupEventType.TOUCHED: "surge_pullback_touches",
        SurgeSetupEventType.ENTRY: "surge_setup_entries",
        SurgeSetupEventType.EXPIRED: "surge_setups_expired",
    }[event_type]
    counts[key] += 1


def _daily_snapshot(bar: Any, point: Any, facts: Any) -> dict[str, Any]:
    return {
        "signal_open": bar.signal.open,
        "signal_high": bar.signal.high,
        "signal_low": bar.signal.low,
        "signal_close": bar.signal.close,
        "signal_sma10": point.sma10,
        "signal_sma20": point.sma20,
        "signal_sma60": point.sma60,
        "ma20_slope_5_pct": point.ma20_slope_5,
        "ma60_slope_5_pct": point.ma60_slope_5,
        "rise_pct": facts.rise_pct,
        "prior_ten_below_sma10": facts.prior_ten_below_sma10,
        "sma10_breakout": facts.sma10_breakout,
        "red_three_soldiers": facts.red_three_soldiers,
        "rise_branch": facts.rise_branch.value if facts.rise_branch else None,
        "block_reasons": [reason.value for reason in facts.block_reasons],
    }
