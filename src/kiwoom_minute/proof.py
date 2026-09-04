"""Sequence-causal, ZERO_COST UP-path proof for opaque ka10080 labels."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from typing import Any

from src.backtest_engine.core_strategy import (
    DailyCoreSignal,
    DailyCoreSignalGenerator,
    DailyTrendClassifier,
)
from src.backtest_engine.indicators import (
    DailyIndicatorPoint,
    calculate_daily_indicators,
    is_golden_cross,
    simple_moving_average,
)
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import TradingCalendar

from .pipeline import ASSUMPTION_ID, MinuteSourceBar


class UpEntryPolicy(str, Enum):
    """The two explicitly authorized UP-entry policies for this experiment."""

    BASELINE = "BASELINE_LOW_OR_CLOSE"
    LOW_REQUIRED = "VARIANT_LOW_REQUIRED"
    LOW_REQUIRED_MA10_3D_NON_DOWN = "VARIANT_LOW_REQUIRED_MA10_3D_NON_DOWN"


def run_up_path_sequence_proof(
    *,
    daily_bars: Sequence[DailyBar],
    source_bars: Sequence[MinuteSourceBar],
    calendar: TradingCalendar,
    research_start: date,
    research_end: date,
    stock_full_weight: Decimal,
    initial_capital: Decimal,
    entry_policy: UpEntryPolicy = UpEntryPolicy.BASELINE,
) -> dict[str, Any]:
    """Run only the already-defined V1 UP Core path on source-bar order.

    ``source_label_at`` is used for deterministic ordering and reporting only;
    it is not represented as an interval START or END timestamp.
    """

    if not isinstance(entry_policy, UpEntryPolicy):
        raise TypeError("entry_policy must be an UpEntryPolicy")
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
    daily_points = calculate_daily_indicators(canonical_daily, calendar)
    daily_sma10 = simple_moving_average(
        [bar.signal.close for bar in canonical_daily], 10
    )
    ma10_slope_3 = [
        None
        if index < 3
        or daily_sma10[index] is None
        or daily_sma10[index - 3] in (None, Decimal(0))
        else (daily_sma10[index] / daily_sma10[index - 3] - Decimal(1)) * Decimal(100)
        for index in range(len(canonical_daily))
    ]
    daily_by_date = {bar.trade_date: bar for bar in canonical_daily}
    daily_index = {bar.trade_date: index for index, bar in enumerate(canonical_daily)}
    point_by_date = {point.trade_date: point for point in daily_points}
    source_points = _source_indicators(canonical_source)
    by_date: dict[date, list[tuple[MinuteSourceBar, dict[str, Any]]]] = defaultdict(
        list
    )
    for bar, point in zip(canonical_source, source_points, strict=True):
        by_date[bar.trading_date].append((bar, point))

    generator = DailyCoreSignalGenerator(calendar)
    classifier = DailyTrendClassifier()
    cash = initial_capital
    quantity = 0
    pending: dict[str, Any] | None = None
    scheduled: dict[str, Any] | None = None
    current_trade: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    counts = {
        "daily_up_candidates": 0,
        "candidate_suppressed_holding_or_pending": 0,
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
                        "entry_condition": scheduled["condition"],
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

        daily_bar = daily_by_date.get(day)
        daily_point = point_by_date.get(day)
        if daily_bar is None or daily_point is None:
            continue
        potential = generator.evaluate(
            daily_bar,
            daily_point,
            holding_core=False,
            stock_full_weight=stock_full_weight,
        )
        potential = _apply_entry_policy(
            potential,
            daily_bar,
            daily_point,
            entry_policy,
            generator.config.uptrend_ma20_band_pct,
            ma10_slope_3[daily_index[day]] if day in daily_index else None,
        )
        if (
            potential is not None
            and potential.signal_type.value == "DAILY_ENTRY_SIGNAL"
        ):
            counts["daily_up_candidates"] += 1
            if quantity > 0 or pending is not None:
                counts["candidate_suppressed_holding_or_pending"] += 1
        if pending is not None:
            continue
        actual = generator.evaluate(
            daily_bar,
            daily_point,
            holding_core=quantity > 0,
            stock_full_weight=stock_full_weight,
        )
        actual = _apply_entry_policy(
            actual,
            daily_bar,
            daily_point,
            entry_policy,
            generator.config.uptrend_ma20_band_pct,
            ma10_slope_3[daily_index[day]] if day in daily_index else None,
        )
        if actual is None:
            continue
        snapshot = _daily_snapshot(daily_bar, daily_point, classifier)
        if actual.signal_type.value == "DAILY_ENTRY_SIGNAL":
            counts["pending_entries"] += 1
            kind = "ENTER"
        else:
            counts["daily_full_exit_signals"] += 1
            kind = "FULL_EXIT"
        pending = {
            "kind": kind,
            "status": "PENDING",
            "activation": actual.activation_trade_date,
            "daily_signal": actual,
            "daily_snapshot": snapshot,
            "daily_raw_close": daily_bar.raw.close,
            "equity_at_decision": cash + Decimal(quantity) * daily_bar.raw.close,
        }

    return {
        "assumption_id": ASSUMPTION_ID,
        "entry_policy": entry_policy.value,
        "stock_code": stock_code,
        "research_start": research_start.isoformat(),
        "research_end": research_end.isoformat(),
        "stock_full_weight": stock_full_weight,
        "core_fraction_of_full": Decimal("0.90"),
        "initial_capital": initial_capital,
        "counts": counts,
        "completed_trades": trades,
        "fills": fills,
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


def _source_indicators(bars: Sequence[MinuteSourceBar]) -> list[dict[str, Any]]:
    closes = [bar.signal.close for bar in bars]
    sma10 = simple_moving_average(closes, 10)
    sma20 = simple_moving_average(closes, 20)
    sma60 = simple_moving_average(closes, 60)
    return [
        {
            "sma10": sma10[index],
            "sma20": sma20[index],
            "sma60": sma60[index],
            "golden_cross": index > 0
            and is_golden_cross(
                sma20[index - 1], sma60[index - 1], sma20[index], sma60[index]
            ),
        }
        for index in range(len(bars))
    ]


def _validate_source_sequence(bars: Sequence[MinuteSourceBar]) -> None:
    ids: set[str] = set()
    labels: set[str] = set()
    previous = None
    for expected, bar in enumerate(bars):
        if bar.assumption_id != ASSUMPTION_ID:
            raise ValueError("unexpected source semantics")
        if bar.source_bar_sequence != expected:
            raise ValueError("source_bar_sequence must be continuous")
        if bar.source_bar_id in ids or bar.source_label in labels:
            raise ValueError("duplicate canonical source bar")
        if previous is not None and bar.source_label <= previous:
            raise ValueError("source labels must be strictly increasing")
        ids.add(bar.source_bar_id)
        labels.add(bar.source_label)
        previous = bar.source_label


def _schedule(
    pending: dict[str, Any],
    signal_bar: MinuteSourceBar,
    fill_bar: MinuteSourceBar,
    side: str,
    condition: str,
) -> dict[str, Any]:
    if fill_bar.source_bar_sequence <= signal_bar.source_bar_sequence:
        raise ValueError("fill source sequence must follow signal sequence")
    if fill_bar.source_bar_id == signal_bar.source_bar_id:
        raise ValueError("fill source bar must differ from signal source bar")
    return {
        **pending,
        "side": side,
        "condition": condition,
        "signal_label": signal_bar.source_label,
        "signal_sequence": signal_bar.source_bar_sequence,
        "signal_bar_id": signal_bar.source_bar_id,
        "fill_bar_id": fill_bar.source_bar_id,
    }


def _next_global_bar(
    bars: Sequence[MinuteSourceBar], source_bar_sequence: int
) -> MinuteSourceBar | None:
    candidate = source_bar_sequence + 1
    return bars[candidate] if candidate < len(bars) else None


def _daily_snapshot(
    bar: DailyBar, point: DailyIndicatorPoint, classifier: DailyTrendClassifier
) -> dict[str, Any]:
    return {
        "trend": classifier.classify(point).value,
        "signal_low": bar.signal.low,
        "signal_close": bar.signal.close,
        "signal_sma20": point.sma20,
        "signal_sma60": point.sma60,
        "ma20_slope_5": point.ma20_slope_5,
        "ma60_slope_5": point.ma60_slope_5,
        "daily_return": point.daily_return,
    }


def _apply_entry_policy(
    signal: DailyCoreSignal | None,
    bar: DailyBar,
    point: DailyIndicatorPoint,
    entry_policy: UpEntryPolicy,
    band_pct: Decimal,
    ma10_slope_3: Decimal | None = None,
) -> DailyCoreSignal | None:
    """Filter entries only; holding exits and all other V1 rules stay unchanged."""

    if signal is None or signal.signal_type.value != "DAILY_ENTRY_SIGNAL":
        return signal
    if entry_policy is UpEntryPolicy.BASELINE:
        return signal
    if point.sma20 is None:
        return None
    ratio = signal.signal_sma20 * band_pct / Decimal(100)
    lower = signal.signal_sma20 - ratio
    upper = signal.signal_sma20 + ratio
    if not lower <= bar.signal.low <= upper:
        return None
    if entry_policy is UpEntryPolicy.LOW_REQUIRED_MA10_3D_NON_DOWN and (
        ma10_slope_3 is None or ma10_slope_3 < 0
    ):
        return None
    return signal


def _holding_sessions(known_days: Sequence[date], start: date, end: date) -> int:
    return sum(start <= day <= end for day in known_days)


def _metrics(
    initial: Decimal,
    cash: Decimal,
    quantity: int,
    bars: Sequence[MinuteSourceBar],
    equity_curve: Sequence[dict[str, Any]],
    trades: Sequence[dict[str, Any]],
    fills: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    last_price = bars[-1].raw.close
    final_equity = cash + Decimal(quantity) * last_price
    cumulative = (final_equity / initial - Decimal(1)) * Decimal(100)
    peak: Decimal | None = None
    mdd = Decimal(0)
    for row in equity_curve:
        equity = row["equity"]
        peak = equity if peak is None else max(peak, equity)
        drawdown = (equity / peak - Decimal(1)) * Decimal(100)
        mdd = min(mdd, drawdown)
    wins = [trade for trade in trades if trade["pnl_amount"] > 0]
    losses = [trade for trade in trades if trade["pnl_amount"] < 0]
    average_win = (
        sum((trade["pnl_pct"] for trade in wins), Decimal(0)) / len(wins)
        if wins
        else None
    )
    average_loss = (
        sum((trade["pnl_pct"] for trade in losses), Decimal(0)) / len(losses)
        if losses
        else None
    )
    gross_profit = sum((trade["pnl_amount"] for trade in wins), Decimal(0))
    gross_loss = -sum((trade["pnl_amount"] for trade in losses), Decimal(0))
    turnover = (
        sum((row["raw_price"] * Decimal(row["quantity"]) for row in fills), Decimal(0))
        / initial
    )
    return {
        "final_equity": final_equity,
        "cumulative_return_pct": cumulative,
        "mdd_pct": mdd,
        "win_rate_pct": Decimal(len(wins)) / Decimal(len(trades)) * Decimal(100)
        if trades
        else None,
        "average_win_pct": average_win,
        "average_loss_pct": average_loss,
        "payoff_ratio": average_win / -average_loss
        if average_win is not None and average_loss not in (None, Decimal(0))
        else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_holding_sessions": (
            Decimal(sum(trade["holding_sessions"] for trade in trades)) / len(trades)
            if trades
            else None
        ),
        "exposure_source_row_fraction": (
            Decimal(sum(bool(row["holding"]) for row in equity_curve))
            / Decimal(len(equity_curve))
            if equity_curve
            else Decimal(0)
        ),
        "turnover_multiple": turnover,
    }
