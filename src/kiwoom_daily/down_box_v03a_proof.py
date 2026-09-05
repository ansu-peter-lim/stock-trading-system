"""Proof-only DOWN_BOX_REVERSAL V0.3-A post-floor rescue replay.

This module is deliberately separate from the frozen V0.2 Daily proof.  It
replays each stock timeline from the cached Daily RAW/ADJUSTED artifacts and
adds at most one report-only rescue after an actual floor-stop fill.  No
production strategy defaults, source artifacts, or V0.2 rules are changed.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from src.backtest_engine.down_box_strategy import (
    BoxSetup,
    BoxSetupState,
    BoxSignalType,
    DownBoxStrategyConfig,
    analyze_box_origin,
    evaluate_box_position,
    evaluate_breakout_reentry,
    evaluate_reversal_wait,
)
from src.backtest_engine.events import stable_id
from src.backtest_engine.indicators import (
    DailyIndicatorPoint,
    calculate_daily_indicators,
    simple_moving_average,
)
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import (
    CalendarRangeError,
    ExplicitTradingCalendar,
    TradingCalendar,
)
from src.backtest_engine.validation import validate_daily_bars

from .down_box_daily_execution_proof import (
    INITIAL_CAPITAL,
    RESEARCH_END,
    RESEARCH_START,
    STOCK_FULL_WEIGHT,
    DailyFillOutcome,
    DailyProofAction,
    DailyProofIntent,
    ZeroCostDailyAccount,
    _aggregate_metrics,
    _entry_metadata,
    _entry_type,
    _event_rows,
    _load_stock,
    _metrics,
    _position_signal,
)

PROOF_VERSION = "DOWN_BOX_REVERSAL_V0_3_A_POST_FLOOR_SECOND_TURN_REENTRY"
OUTPUT_PATH = Path("data/processed/kiwoom/down_box_reversal_v0_3_a_stateful_ab.json")
CHART_ROOT = Path("data/processed/strategy_charts/down_box_reversal_v0_3_a_stateful_ab")
STOCKS = (
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


@dataclass(frozen=True, slots=True)
class RescueWait:
    """Immutable snapshot of one eligible original setup awaiting rescue."""

    setup: BoxSetup
    original_trade: Mapping[str, Any]
    original_entry_type: str
    floor_exit_fill_date: date
    floor_exit_fill_index: int
    turn_dates: tuple[date, ...]
    turn_indexes: tuple[int, ...]
    status: str = "WAITING"
    candidate: Mapping[str, Any] | None = None


class V03AAccount(ZeroCostDailyAccount):
    """V0.2 proof account with a single proof-local rescue lifecycle."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rescue_wait: RescueWait | None = None
        self.rescue_used_setup_ids: set[str] = set()
        self.rescue_candidates: list[dict[str, Any]] = []
        self.rescue_fills: list[dict[str, Any]] = []
        self.rescue_wait_history: list[dict[str, Any]] = []

    def fill_daily_open(
        self,
        day: date,
        daily_bar: DailyBar,
        *,
        known_days: Sequence[date],
    ) -> DailyFillOutcome | None:
        intent = self.pending
        if intent is None:
            return None
        trade_kind = str((intent.metadata or {}).get("trade_kind", "ORIGINAL"))
        current_kind = str((self.current_trade or {}).get("trade_kind", "ORIGINAL"))
        is_original_floor = (
            intent.action is DailyProofAction.FULL_EXIT_FLOOR_BREAK
            and current_kind == "ORIGINAL"
        )
        is_rescue_entry = (
            intent.action is DailyProofAction.INITIAL_ENTRY and trade_kind == "RESCUE"
        )
        is_rescue_exit = (
            intent.action
            in {
                DailyProofAction.HALF_EXIT,
                DailyProofAction.FULL_EXIT_FLOOR_BREAK,
                DailyProofAction.FULL_TAKE_PROFIT_UPPER,
            }
            and current_kind == "RESCUE"
        )
        before_completed = len(self.completed_trades)
        outcome = super().fill_daily_open(day, daily_bar, known_days=known_days)
        if outcome is None:
            return None
        if self.fills:
            self.fills[-1] = {**self.fills[-1], "trade_leg": trade_kind}
        if is_rescue_entry:
            self.rescue_fills.append({**self.fills[-1], "trade_leg": "RESCUE"})
            self.transitions.append(
                {
                    "transition": "POST_FLOOR_REENTRY_FILLED",
                    "trade_date": day,
                    "setup_id": outcome.setup.setup_id,
                }
            )
            if self.rescue_wait is not None:
                self.rescue_wait = replace(self.rescue_wait, status="RESCUE_ACTIVE")
        if trade_kind == "ORIGINAL" and len(self.completed_trades) > before_completed:
            original = {
                **self.completed_trades[-1],
                "trade_leg": "ORIGINAL",
            }
            self.completed_trades[-1] = original
            if is_original_floor:
                wait = self.rescue_wait
                if wait is not None:
                    wait = replace(wait, status="FLOOR_EXIT_FILLED")
                    self.rescue_wait = wait
        if is_rescue_exit and len(self.completed_trades) > before_completed:
            rescue = {
                **self.completed_trades[-1],
                "trade_leg": "RESCUE",
                "status": "COMPLETED",
                "rescue_parent_setup_id": (
                    self.rescue_wait.setup.setup_id if self.rescue_wait else None
                ),
            }
            self.completed_trades[-1] = rescue
            if self.rescue_wait is not None:
                self.rescue_wait = replace(self.rescue_wait, status="COMPLETED")
        return outcome

    def mark_rescue_candidate(self, row: Mapping[str, Any]) -> None:
        candidate = dict(row)
        self.rescue_candidates.append(candidate)
        if self.rescue_wait is not None:
            self.rescue_wait = replace(
                self.rescue_wait, status="SIGNALLED", candidate=candidate
            )

    def mark_rescue_wait_terminal(self, status: str, day: date) -> None:
        if self.rescue_wait is None:
            return
        self.rescue_wait_history.append(
            {
                "setup_id": self.rescue_wait.setup.setup_id,
                "status": status,
                "terminal_date": day,
                "floor_exit_fill_date": self.rescue_wait.floor_exit_fill_date,
            }
        )
        self.rescue_wait = replace(self.rescue_wait, status=status)


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _has_ma5_component(entry_type: str | None) -> bool:
    return entry_type in {"MA5_TURN", "BOTH"}


def _exact_turn_indexes(
    bars: Sequence[DailyBar], origin_index: int, sma5: Sequence[Decimal | None]
) -> tuple[int, ...]:
    """Return exact V0.2 MA5 turns only inside setup D1..D10."""

    end = min(origin_index + 10, len(bars) - 1)
    return tuple(
        index
        for index in range(origin_index + 1, end + 1)
        if index >= 2
        and sma5[index - 2] is not None
        and sma5[index - 1] is not None
        and sma5[index] is not None
        and sma5[index - 1] <= sma5[index - 2]
        and sma5[index] > sma5[index - 1]
    )


def _box_touch(bar: DailyBar, setup: BoxSetup, *, close_only: bool = False) -> bool:
    upper = setup.box_floor * Decimal("1.03")
    if close_only:
        return setup.box_floor <= bar.signal.close <= upper
    return (
        setup.box_floor <= bar.signal.low <= upper
        or setup.box_floor <= bar.signal.close <= upper
    )


def _rescue_metadata(
    *,
    setup: BoxSetup,
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
    sma5: Sequence[Decimal | None],
    index: int,
    turn_ordinal: int,
) -> dict[str, Any]:
    bar = bars[index]
    width = setup.box_upper - setup.box_floor
    lower_upper = setup.box_floor * Decimal("1.03")
    close = bar.signal.close
    return {
        "turn_ordinal": turn_ordinal,
        "turn_date": bar.trade_date,
        "setup_origin_date": setup.setup_origin_date,
        "setup_relative_day": index
        - next(
            i
            for i, item in enumerate(bars)
            if item.trade_date == setup.setup_origin_date
        ),
        "close": close,
        "low": bar.signal.low,
        "sma5": sma5[index],
        "sma10": points[index].sma10,
        "sma20": points[index].sma20,
        "sma60": points[index].sma60,
        "box_position_close": (close - setup.box_floor) / width,
        "box_position_low": (bar.signal.low - setup.box_floor) / width,
        "close_vs_box_floor": close / setup.box_floor - Decimal(1),
        "close_vs_lower_zone_upper": close / lower_upper - Decimal(1),
        "floor_reclaim": close >= setup.box_floor,
        "lower_zone_touch": _box_touch(bar, setup),
        "lower_zone_close_touch": _box_touch(bar, setup, close_only=True),
        "box_floor": setup.box_floor,
        "box_upper": setup.box_upper,
        "upper_sell_level": setup.box_upper * Decimal("0.97"),
    }


def _next_trading_day_or_none(calendar: TradingCalendar, day: date) -> date | None:
    try:
        return calendar.next_trading_day(day)
    except CalendarRangeError:
        return None


def _schedule_rescue(
    account: V03AAccount,
    *,
    wait: RescueWait,
    signal_date: date,
    activation_date: date,
    equity_at_decision: Decimal,
    metadata: Mapping[str, Any],
) -> None:
    signal_id = stable_id(
        "down_box_v03a_rescue_signal",
        wait.setup.stock_code,
        wait.setup.setup_id,
        signal_date,
    )
    intent = DailyProofIntent(
        intent_id=stable_id(
            "down_box_v03a_rescue_intent",
            wait.setup.stock_code,
            wait.setup.setup_id,
            signal_date,
            activation_date,
        ),
        stock_code=wait.setup.stock_code,
        action=DailyProofAction.INITIAL_ENTRY,
        signal_date=signal_date,
        activation_date=activation_date,
        setup=wait.setup,
        signal_id=signal_id,
        equity_at_decision=equity_at_decision,
        entry_type="POST_FLOOR_SECOND_MA5_TURN_REENTRY",
        metadata={
            **dict(metadata),
            "trade_kind": "RESCUE",
            "rescue_parent_setup_id": wait.setup.setup_id,
            "rescue_signal_date": signal_date,
        },
    )
    account.schedule(intent)


def _candidate_conditions(
    *,
    wait: RescueWait,
    index: int,
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
    sma5: Sequence[Decimal | None],
) -> tuple[bool, dict[str, Any]]:
    """Evaluate the frozen, report-only A-F rescue conditions."""

    if len(wait.turn_indexes) < 2 or index != wait.turn_indexes[1]:
        return False, {"reason": "NOT_SECOND_TURN"}
    if index <= wait.floor_exit_fill_index:
        return False, {"reason": "TURN2_BEFORE_FLOOR_EXIT_FILL"}
    setup = wait.setup
    metadata = _rescue_metadata(
        setup=setup,
        bars=bars,
        points=points,
        sma5=sma5,
        index=index,
        turn_ordinal=2,
    )
    metadata.update(
        {
            "stock_code": setup.stock_code,
            "setup_id": setup.setup_id,
            "original_entry_type": wait.original_entry_type,
            "floor_exit_fill_date": wait.floor_exit_fill_date,
            "candidate": bool(
                _has_ma5_component(wait.original_entry_type)
                and metadata["floor_reclaim"]
                and metadata["close"] < metadata["upper_sell_level"]
            ),
        }
    )
    if not _has_ma5_component(wait.original_entry_type):
        metadata["rejection_reason"] = "ORIGINAL_ENTRY_HAS_NO_MA5_COMPONENT"
        return False, metadata
    if not metadata["floor_reclaim"]:
        metadata["rejection_reason"] = "TURN2_CLOSE_BELOW_FLOOR"
        return False, metadata
    if metadata["close"] >= metadata["upper_sell_level"]:
        metadata["rejection_reason"] = "TURN2_CLOSE_IN_UPPER_SELL_ZONE"
        return False, metadata
    return True, metadata


def _make_wait(
    *,
    setup: BoxSetup,
    original_trade: Mapping[str, Any],
    floor_exit_fill_date: date,
    floor_exit_fill_index: int,
    turn_indexes: Sequence[int],
    bars: Sequence[DailyBar],
) -> RescueWait | None:
    entry_type = str(original_trade.get("entry_type"))
    if not _has_ma5_component(entry_type):
        return None
    return RescueWait(
        setup=setup,
        original_trade=dict(original_trade),
        original_entry_type=entry_type,
        floor_exit_fill_date=floor_exit_fill_date,
        floor_exit_fill_index=floor_exit_fill_index,
        turn_dates=tuple(bars[index].trade_date for index in turn_indexes),
        turn_indexes=tuple(turn_indexes),
    )


def _run_variant_b(
    *,
    daily_bars: Sequence[DailyBar],
    calendar: TradingCalendar,
    research_start: date = RESEARCH_START,
    research_end: date = RESEARCH_END,
    stock_full_weight: Decimal = STOCK_FULL_WEIGHT,
    initial_capital: Decimal = INITIAL_CAPITAL,
) -> dict[str, Any]:
    """Replay one stock timeline with exactly one post-floor rescue."""

    if research_start > research_end:
        raise ValueError("research_start must not follow research_end")
    canonical = tuple(sorted(daily_bars, key=lambda bar: bar.trade_date))
    if not canonical:
        raise ValueError("daily_bars must not be empty")
    validate_daily_bars(canonical, calendar)
    if len({bar.stock_code for bar in canonical}) != 1:
        raise ValueError("V0.3-A accepts one stock at a time")
    stock_code = canonical[0].stock_code
    points = tuple(calculate_daily_indicators(canonical, calendar))
    sma5 = simple_moving_average([bar.signal.close for bar in canonical], 5)
    by_date = {bar.trade_date: bar for bar in canonical}
    indexes = {bar.trade_date: index for index, bar in enumerate(canonical)}
    known_days = tuple(bar.trade_date for bar in canonical)
    research_days = tuple(
        day for day in known_days if research_start <= day <= research_end
    )
    account = V03AAccount(
        stock_code=stock_code,
        initial_capital=initial_capital,
        stock_full_weight=stock_full_weight,
    )
    active: BoxSetup | None = None
    events: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    collision_rows: list[dict[str, Any]] = []
    config = DownBoxStrategyConfig()

    for day in research_days:
        index = indexes[day]
        bar = by_date[day]
        outcome = account.fill_daily_open(day, bar, known_days=known_days)
        if outcome is not None:
            counts[f"{outcome.action.value}_FILLS"] += 1
            if outcome.action is DailyProofAction.INITIAL_ENTRY:
                next_setup = outcome.next_setup
                if (
                    account.current_trade
                    and account.current_trade.get("trade_kind") == "RESCUE"
                ):
                    active = next_setup
                    counts["RESCUE_ENTRY_FILLS"] += 1
                else:
                    active = next_setup
            elif outcome.action is DailyProofAction.FULL_EXIT_FLOOR_BREAK:
                active = outcome.next_setup
                if (
                    account.completed_trades
                    and account.completed_trades[-1].get("trade_leg") == "ORIGINAL"
                ):
                    original = account.completed_trades[-1]
                    origin_date = next(
                        event["event_date"]
                        for event in events
                        if event.get("setup_id") == original["setup_id"]
                        and event.get("event") == "REVERSAL_SETUP_CREATED"
                    )
                    origin_index = indexes[origin_date]
                    # The strategy only permits rescue while original D10 is
                    # still ahead; otherwise the completed floor trade is
                    # ordinary V0.2 lifecycle.
                    turns = _exact_turn_indexes(canonical, origin_index, sma5)
                    wait = _make_wait(
                        setup=outcome.setup,
                        original_trade=original,
                        floor_exit_fill_date=day,
                        floor_exit_fill_index=index,
                        turn_indexes=turns,
                        bars=canonical,
                    )
                    if wait is not None and index < origin_index + 10:
                        account.rescue_wait = wait
                        counts["POST_FLOOR_WAIT_STARTED"] += 1
                        events.append(
                            {
                                "event": "POST_FLOOR_REENTRY_WAIT",
                                "event_date": day,
                                "setup_id": wait.setup.setup_id,
                                "reason": "ACTUAL_FLOOR_EXIT_FILL",
                            }
                        )
                    elif wait is not None:
                        account.rescue_wait_history.append(
                            {
                                "setup_id": wait.setup.setup_id,
                                "status": "D10_ALREADY_PASSED",
                                "terminal_date": day,
                                "floor_exit_fill_date": day,
                            }
                        )
            elif outcome.action is DailyProofAction.FULL_TAKE_PROFIT_UPPER:
                active = outcome.next_setup
        equity_curve.append(
            {
                "trade_date": day,
                "equity": account.cash + Decimal(account.quantity) * bar.raw.close,
                "holding": account.quantity > 0,
            }
        )
        if account.pending is not None:
            continue

        wait = account.rescue_wait
        if wait is not None and wait.status in {"FLOOR_EXIT_FILLED", "WAITING"}:
            # Suppress independent setup creation while rescue wait is active,
            # but record a deterministic collision observation if one exists.
            origin_probe = analyze_box_origin(canonical, points, index, config)
            if origin_probe.setup is not None:
                collision_rows.append(
                    {
                        "stock_code": stock_code,
                        "date": day,
                        "waiting_setup_id": wait.setup.setup_id,
                        "colliding_setup_id": origin_probe.setup.setup_id,
                        "status": "SETUP_COLLISION_REVIEW_REQUIRED",
                    }
                )
            if (
                wait.setup.setup_id not in account.rescue_used_setup_ids
                and index > wait.floor_exit_fill_index
                and index <= wait.floor_exit_fill_index + 10
                and index <= indexes[wait.setup.setup_origin_date] + 10
            ):
                valid, metadata = _candidate_conditions(
                    wait=wait,
                    index=index,
                    bars=canonical,
                    points=points,
                    sma5=sma5,
                )
                if valid:
                    account.rescue_used_setup_ids.add(wait.setup.setup_id)
                    activation = _next_trading_day_or_none(calendar, day)
                    candidate = {
                        **metadata,
                        "signal_date": day,
                        "activation_date": activation,
                        "status": "SIGNALLED" if activation else "EXPIRED",
                    }
                    account.mark_rescue_candidate(candidate)
                    events.append(
                        {
                            "event": "POST_FLOOR_SECOND_MA5_TURN_CANDIDATE",
                            "event_date": day,
                            "setup_id": wait.setup.setup_id,
                            "reason": "SECOND_TURN_FLOOR_RECLAIM",
                            "details": candidate,
                        }
                    )
                    counts["RESCUE_CANDIDATE_SIGNALS"] += 1
                    if activation is not None:
                        _schedule_rescue(
                            account,
                            wait=wait,
                            signal_date=day,
                            activation_date=activation,
                            equity_at_decision=account.cash,
                            metadata=candidate,
                        )
                    else:
                        account.mark_rescue_wait_terminal("EXPIRED", day)
                elif metadata.get("reason") == "NOT_SECOND_TURN":
                    pass
            if account.rescue_wait is not None and account.rescue_wait.status in {
                "FLOOR_EXIT_FILLED",
                "WAITING",
            }:
                origin = indexes[wait.setup.setup_origin_date]
                if index >= origin + 10:
                    account.mark_rescue_wait_terminal("D10_EXPIRED", day)
            continue

        if active is None and account.state == "FLAT":
            origin = analyze_box_origin(canonical, points, index, config)
            if origin.setup is not None:
                active = origin.setup
                counts["DAILY_SETUPS"] += 1
                events.append(
                    {
                        "event": "REVERSAL_SETUP_CREATED",
                        "event_date": day,
                        "setup_id": active.setup_id,
                    }
                )
            continue
        if active is None:
            continue

        if active.state is BoxSetupState.REVERSAL_WAIT:
            decision = evaluate_reversal_wait(active, canonical, points, index, config)
            events.extend(_event_rows(decision.events))
            signal = next(
                (
                    item
                    for item in decision.signals
                    if item.signal_type.name.startswith("ENTRY_CANDIDATE")
                ),
                None,
            )
            if signal is not None:
                counts["ENTRY_SIGNALS"] += 1
                metadata = _entry_metadata(active, canonical, index)
                metadata["trade_kind"] = "ORIGINAL"
                try:
                    activation = calendar.next_trading_day(day)
                except CalendarRangeError:
                    activation = None
                if activation is not None:
                    account.schedule(
                        DailyProofIntent(
                            intent_id=stable_id(
                                "down_box_v03a_original_intent",
                                stock_code,
                                signal.signal_date,
                                active.setup_id,
                                activation,
                            ),
                            stock_code=stock_code,
                            action=DailyProofAction.INITIAL_ENTRY,
                            signal_date=signal.signal_date,
                            activation_date=activation,
                            setup=active,
                            signal_id=signal.signal_id,
                            equity_at_decision=account.cash
                            + Decimal(account.quantity) * bar.raw.close,
                            entry_type=_entry_type(signal),
                            metadata=metadata,
                        )
                    )
                    active = replace(
                        decision.setup_after, state=BoxSetupState.ENTRY_SIGNALLED
                    )
            elif decision.setup_after.state in {
                BoxSetupState.EXPIRED,
                BoxSetupState.INVALIDATED,
            }:
                active = None
            else:
                active = decision.setup_after
            continue

        if active.state is BoxSetupState.BOX_POSITION:
            decision = evaluate_box_position(active, canonical, points, index, config)
            events.extend(_event_rows(decision.events))
            signal = _position_signal(decision.signals)
            if signal is None:
                active = decision.setup_after
                continue
            if signal.signal_type is BoxSignalType.FULL_EXIT_FLOOR_BREAK:
                action = DailyProofAction.FULL_EXIT_FLOOR_BREAK
            elif signal.signal_type is BoxSignalType.FULL_TAKE_PROFIT_UPPER:
                action = DailyProofAction.FULL_TAKE_PROFIT_UPPER
            else:
                action = DailyProofAction.HALF_EXIT
            try:
                activation = calendar.next_trading_day(day)
            except CalendarRangeError:
                activation = None
            if activation is not None:
                is_rescue_trade = bool(
                    account.current_trade
                    and account.current_trade.get("trade_kind") == "RESCUE"
                )
                breakout_setup = (
                    decision.setup_after
                    if (
                        not is_rescue_trade
                        and action is DailyProofAction.FULL_TAKE_PROFIT_UPPER
                        and decision.setup_after.state
                        is BoxSetupState.BREAKOUT_REENTRY_WAIT
                    )
                    else None
                )
                account.schedule(
                    DailyProofIntent(
                        intent_id=stable_id(
                            "down_box_v03a_position_intent",
                            stock_code,
                            action.value,
                            signal.signal_date,
                            active.setup_id,
                        ),
                        stock_code=stock_code,
                        action=action,
                        signal_date=signal.signal_date,
                        activation_date=activation,
                        setup=active,
                        signal_id=signal.signal_id,
                        breakout_reentry_setup=breakout_setup,
                        metadata={"trade_kind": "RESCUE"} if is_rescue_trade else None,
                    )
                )
            active = decision.setup_after
            continue

        if active.state is BoxSetupState.BREAKOUT_REENTRY_WAIT:
            decision = evaluate_breakout_reentry(
                active, canonical, points, index, config
            )
            events.extend(_event_rows(decision.events))
            signal = next(iter(decision.signals), None)
            if signal is not None:
                counts["BREAKOUT_REENTRY_SIGNALS"] += 1
                active = None
                account.state = "REENTRY_CANDIDATE_REPORTED"
            elif decision.setup_after.state is BoxSetupState.INVALIDATED:
                counts["BREAKOUT_FAILED"] += 1
                active = None
                account.state = "FLAT"
            else:
                active = decision.setup_after

    if account.pending is not None and not account.pending.action.persistent:
        pending = account.pending
        account.expire_if_due_without_bar(pending.activation_date)
        if (pending.metadata or {}).get("trade_kind") == "RESCUE":
            account.mark_rescue_wait_terminal(
                "POST_FLOOR_REENTRY_EXPIRED", pending.signal_date
            )
            if account.rescue_candidates:
                account.rescue_candidates[-1]["status"] = "EXPIRED"
        counts[f"{pending.action.value}_EXPIRATIONS"] += 1

    rescue_trades = [
        trade
        for trade in account.completed_trades
        if trade.get("trade_leg") == "RESCUE"
    ]
    return {
        "proof_version": PROOF_VERSION,
        "variant": "B",
        "stock_code": stock_code,
        "research_start": research_start,
        "research_end": research_end,
        "coverage": {
            "daily_first_date": canonical[0].trade_date,
            "daily_last_date": canonical[-1].trade_date,
            "daily_row_count": len(canonical),
            "research_row_count": len(research_days),
        },
        "accounting": {
            "cost_profile": "ZERO_COST",
            "stock_full_weight": stock_full_weight,
            "signal_price_basis": "DAILY_ADJUSTED",
            "execution_price_basis": "KA10081_RAW_DAILY_OPEN",
            "entry_size": "1.0 STOCK FULL",
            "rescue_entry_size": "1.0 STOCK FULL",
            "completed_trade_pnl_basis": "REALIZED_ONLY",
            "cumulative_return_basis": "RESEARCH_END_MARK_TO_MARKET_RAW_CLOSE",
            "mdd_basis": "DAILY_MARK_TO_MARKET_RAW_CLOSE",
            "open_position_included_in_metrics": True,
        },
        "counts": dict(sorted(counts.items())),
        "completed_trades": account.completed_trades,
        "open_trade": account.current_trade,
        "rescue_trades": rescue_trades,
        "fills": account.fills,
        "rescue_fills": account.rescue_fills,
        "expirations": account.expirations,
        "transitions": account.transitions,
        "daily_events": events,
        "rescue_candidates": account.rescue_candidates,
        "rescue_wait_history": account.rescue_wait_history,
        "setup_collision_audit": collision_rows,
        "state_at_end": account.state,
        "open_quantity": account.quantity,
        "metrics": _metrics(
            account,
            equity_curve,
            initial_capital,
            canonical,
            research_start,
            research_end,
        ),
    }


def _metric_comparison(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Compare stock metrics with MDD interpreted as a higher-is-better value."""

    result: dict[str, Any] = {}
    for key in (
        "cumulative_return_pct",
        "mdd_pct",
        "win_rate_pct",
        "profit_factor",
        "average_holding_sessions",
        "exposure",
        "turnover",
    ):
        av, bv = a.get(key), b.get(key)
        if av is not None and not isinstance(av, Decimal):
            av = Decimal(str(av))
        if bv is not None and not isinstance(bv, Decimal):
            bv = Decimal(str(bv))
        delta = None if av is None or bv is None else bv - av
        result[key] = {"a": av, "b": bv, "delta": delta}
    directions = [
        "BETTER" if item["delta"] > 0 else "WORSE" if item["delta"] < 0 else "SAME"
        for item in result.values()
        if item["delta"] is not None
    ]
    better, worse = directions.count("BETTER"), directions.count("WORSE")
    result["overall"] = (
        "BETTER"
        if better >= 2 and better > worse
        else "WORSE"
        if worse >= 2 and worse > better
        else "SAME"
    )
    return result


def _campaign_classification(combined: Decimal | None, original: Decimal) -> str:
    if combined is None:
        return "INCOMPLETE"
    if combined > 0:
        return "FULL_RECOVERY"
    if combined > original:
        return "PARTIAL_RECOVERY"
    return "NO_IMPROVEMENT"


def _campaign_rows(
    a_results: Mapping[str, Mapping[str, Any]],
    b_results: Mapping[str, Mapping[str, Any]],
    *,
    initial_capital: Decimal,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stock_code in sorted(a_results):
        a_trades = {
            trade["setup_id"]: trade
            for trade in a_results[stock_code]["completed_trades"]
        }
        b_result = b_results[stock_code]
        b_trades = b_result["completed_trades"]
        rescues = [trade for trade in b_trades if trade.get("trade_leg") == "RESCUE"]
        for setup_id in sorted(a_trades):
            original = a_trades[setup_id]
            rescue = next(
                (
                    trade
                    for trade in rescues
                    if trade.get("rescue_parent_setup_id") == setup_id
                    or trade.get("setup_id") == setup_id
                ),
                None,
            )
            if rescue is None:
                open_trade = b_result.get("open_trade")
                if (
                    open_trade is not None
                    and open_trade.get("trade_kind") == "RESCUE"
                    and (
                        open_trade.get("rescue_parent_setup_id") == setup_id
                        or open_trade.get("setup_id") == setup_id
                    )
                ):
                    rescue = {
                        **open_trade,
                        "trade_leg": "RESCUE",
                        "rescue_parent_setup_id": setup_id,
                        "status": "INCOMPLETE",
                    }
            combined = (
                None
                if rescue is not None and rescue.get("pnl_amount") is None
                else original["pnl_amount"] + rescue["pnl_amount"]
                if rescue is not None
                else original["pnl_amount"]
            )
            rows.append(
                {
                    "stock_code": stock_code,
                    "setup_id": setup_id,
                    "original_trade": original,
                    "original_entry_type": original.get("entry_type"),
                    "first_entry_signal_date": original.get("entry_daily_signal_date"),
                    "first_entry_fill_date": original.get("entry_fill_date"),
                    "first_exit_signal_date": original.get("exit_daily_signal_date"),
                    "first_exit_fill_date": original.get("exit_fill_date"),
                    "first_exit_reason": (
                        "FLOOR"
                        if original.get("exit_action") == "FULL_EXIT_FLOOR_BREAK"
                        else "UPPER"
                    ),
                    "first_trade_pnl": original["pnl_amount"],
                    "first_trade_return_pct": original["pnl_pct"],
                    "rescue": rescue,
                    "combined_campaign_pnl": combined,
                    "combined_campaign_return_pct": (
                        combined / initial_capital * Decimal(100)
                        if combined is not None
                        else None
                    ),
                    "baseline_campaign_pnl": original["pnl_amount"],
                    "v03a_campaign_delta": (
                        combined - original["pnl_amount"]
                        if combined is not None
                        else None
                    ),
                    "recovery_classification": _campaign_classification(
                        combined, original["pnl_amount"]
                    ),
                }
            )
    return rows


def _aggregate_variant(
    results: Mapping[str, Mapping[str, Any]],
    campaigns: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    trades = [
        trade for result in results.values() for trade in result["completed_trades"]
    ]
    base = _aggregate_metrics(results, trades)
    wins = [trade for trade in trades if trade["pnl_amount"] > 0]
    losses = [trade for trade in trades if trade["pnl_amount"] < 0]
    returns = [
        result["metrics"]["cumulative_return_pct"] for result in results.values()
    ]
    rescue_trades = [trade for trade in trades if trade.get("trade_leg") == "RESCUE"]
    rescue_entry_fills = sum(
        1
        for result in results.values()
        for fill in result.get("rescue_fills", ())
        if fill["action"] == DailyProofAction.INITIAL_ENTRY.value
    )
    rescue_open = sum(
        result.get("open_trade") is not None
        and result["open_trade"].get("trade_kind") == "RESCUE"
        for result in results.values()
    )
    classifications = Counter(row["recovery_classification"] for row in campaigns)
    rescue_classifications = Counter(
        row["recovery_classification"]
        for row in campaigns
        if row.get("rescue_candidate") is not None
    )
    return {
        **base,
        "flat_count": len(trades) - len(wins) - len(losses),
        "gross_profit": sum((trade["pnl_amount"] for trade in wins), Decimal(0)),
        "gross_loss": -sum((trade["pnl_amount"] for trade in losses), Decimal(0)),
        "mean_trade_return_pct": (
            sum((trade["pnl_pct"] for trade in trades), Decimal(0)) / len(trades)
            if trades
            else None
        ),
        "median_trade_return_pct": median([trade["pnl_pct"] for trade in trades])
        if trades
        else None,
        "mean_stock_return_pct": sum(returns, Decimal(0)) / len(returns),
        "median_stock_return_pct": median(returns),
        "profitable_stock_count": sum(value > 0 for value in returns),
        "losing_stock_count": sum(value < 0 for value in returns),
        "rescue_trade_count": len(rescue_trades),
        "rescue_entry_fill_count": rescue_entry_fills,
        "rescue_open_count": rescue_open,
        "full_recovery_count": classifications["FULL_RECOVERY"],
        "partial_recovery_count": classifications["PARTIAL_RECOVERY"],
        "no_improvement_count": classifications["NO_IMPROVEMENT"],
        "incomplete_campaign_count": classifications["INCOMPLETE"],
        "rescue_full_recovery_count": rescue_classifications["FULL_RECOVERY"],
        "rescue_partial_recovery_count": rescue_classifications["PARTIAL_RECOVERY"],
        "rescue_no_improvement_count": rescue_classifications["NO_IMPROVEMENT"],
        "rescue_incomplete_campaign_count": rescue_classifications["INCOMPLETE"],
    }


def _large_winner_protection(
    a_results: Mapping[str, Mapping[str, Any]],
    b_results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for stock_code in sorted(a_results):
        a_entries = {
            trade["entry_daily_signal_date"]: trade
            for trade in a_results[stock_code]["completed_trades"]
        }
        b_entries = {
            trade["entry_daily_signal_date"]: trade
            for trade in b_results[stock_code]["completed_trades"]
            if trade.get("trade_leg") != "RESCUE"
        }
        for day, trade in sorted(a_entries.items()):
            if trade["pnl_pct"] >= Decimal(20):
                rows.append(
                    {
                        "stock_code": stock_code,
                        "signal_date": day,
                        "baseline_pnl_pct": trade["pnl_pct"],
                        "status": "PRESERVED" if day in b_entries else "REMOVED",
                    }
                )
    return {
        "large_winner_count_a": len(rows),
        "large_winner_preserved_count": sum(
            row["status"] == "PRESERVED" for row in rows
        ),
        "rows": rows,
    }


def _review_chart_events(
    campaign: Mapping[str, Any],
) -> tuple[Any, ...]:
    from src.strategy_review.chart import ReviewEvent, ReviewEventType

    original = campaign["original_trade"]
    rescue = campaign.get("rescue")
    events = [
        ReviewEvent(
            ReviewEventType.BOX_BUY_CANDIDATE,
            _as_date(original["entry_daily_signal_date"]),
            "ORIGINAL ENTRY",
        ),
        ReviewEvent(
            ReviewEventType.ENTRY_FILL,
            _as_date(original["entry_fill_date"]),
            "ORIGINAL ENTRY FILL",
            raw_fill_price=original["entry_raw_price"],
            source_label="KA10081 RAW DAILY OPEN",
        ),
    ]
    if original.get("half_exit"):
        half = original["half_exit"]
        events.append(
            ReviewEvent(
                ReviewEventType.HALF_EXIT_SIGNAL,
                _as_date(half["signal_date"]),
                "ORIGINAL HALF",
            )
        )
        events.append(
            ReviewEvent(
                ReviewEventType.EXIT_FILL,
                _as_date(half["fill_date"]),
                "ORIGINAL HALF FILL",
                raw_fill_price=half["raw_price"],
                source_label="KA10081 RAW DAILY OPEN",
            )
        )
    original_exit_type = (
        ReviewEventType.FLOOR_BREAK
        if original["exit_action"] == DailyProofAction.FULL_EXIT_FLOOR_BREAK.value
        else ReviewEventType.UPPER_TAKE_PROFIT
    )
    events.extend(
        (
            ReviewEvent(
                original_exit_type,
                _as_date(original["exit_daily_signal_date"]),
                "FLOOR EXIT"
                if original_exit_type is ReviewEventType.FLOOR_BREAK
                else "UPPER EXIT",
            ),
            ReviewEvent(
                ReviewEventType.EXIT_FILL,
                _as_date(original["exit_fill_date"]),
                "ORIGINAL EXIT FILL",
                raw_fill_price=original["exit_raw_price"],
                source_label="KA10081 RAW DAILY OPEN",
            ),
        )
    )
    candidate = campaign.get("rescue_candidate")
    if candidate:
        events.append(
            ReviewEvent(
                ReviewEventType.MA5_TURN,
                _as_date(candidate["signal_date"]),
                "TURN2 / FLOOR RECLAIM",
            )
        )
        if candidate.get("activation_date"):
            events.append(
                ReviewEvent(
                    ReviewEventType.BOX_BUY_CANDIDATE,
                    _as_date(candidate["signal_date"]),
                    "RESCUE ENTRY SIGNAL",
                )
            )
    if rescue:
        events.append(
            ReviewEvent(
                ReviewEventType.ENTRY_FILL,
                _as_date(rescue["entry_fill_date"]),
                "RESCUE ENTRY FILL",
                raw_fill_price=rescue["entry_raw_price"],
                source_label="KA10081 RAW DAILY OPEN",
            )
        )
        if rescue.get("half_exit"):
            half = rescue["half_exit"]
            events.append(
                ReviewEvent(
                    ReviewEventType.HALF_EXIT_SIGNAL,
                    _as_date(half["signal_date"]),
                    "RESCUE HALF",
                )
            )
            events.append(
                ReviewEvent(
                    ReviewEventType.EXIT_FILL,
                    _as_date(half["fill_date"]),
                    "RESCUE HALF FILL",
                    raw_fill_price=half["raw_price"],
                    source_label="KA10081 RAW DAILY OPEN",
                )
            )
        if rescue.get("exit_fill_date"):
            exit_type = (
                ReviewEventType.FLOOR_BREAK
                if rescue["exit_action"] == DailyProofAction.FULL_EXIT_FLOOR_BREAK.value
                else ReviewEventType.UPPER_TAKE_PROFIT
            )
            events.extend(
                (
                    ReviewEvent(
                        exit_type,
                        _as_date(rescue["exit_daily_signal_date"]),
                        "RESCUE FLOOR EXIT"
                        if exit_type is ReviewEventType.FLOOR_BREAK
                        else "RESCUE UPPER EXIT",
                    ),
                    ReviewEvent(
                        ReviewEventType.EXIT_FILL,
                        _as_date(rescue["exit_fill_date"]),
                        "RESCUE EXIT FILL",
                        raw_fill_price=rescue["exit_raw_price"],
                        source_label="KA10081 RAW DAILY OPEN",
                    ),
                )
            )
    return tuple(events)


def _generate_rescue_charts(
    campaigns: Sequence[Mapping[str, Any]],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
    chart_root: Path,
) -> list[dict[str, Any]]:
    from src.strategy_review.chart import (
        ChartType,
        deterministic_chart_filename,
        prepare_review_chart,
        render_review_chart,
    )

    artifacts: list[dict[str, Any]] = []
    for campaign in sorted(
        (row for row in campaigns if row.get("rescue_candidate")),
        key=lambda row: (row["stock_code"], row["setup_id"]),
    ):
        stock_code = campaign["stock_code"]
        original = campaign["original_trade"]
        rescue = campaign.get("rescue")
        bars = daily_by_stock[stock_code]
        dates = [
            _as_date(original["entry_daily_signal_date"]),
            _as_date(campaign["rescue_candidate"]["signal_date"]),
        ]
        if rescue and rescue.get("exit_fill_date"):
            dates.append(_as_date(rescue["exit_fill_date"]))
        end = max(dates)
        prepared = prepare_review_chart(
            bars,
            chart_type=ChartType.EVENT_REVIEW,
            events=_review_chart_events(campaign),
            calendar=ExplicitTradingCalendar(bar.trade_date for bar in bars),
            focus_date=_as_date(original["entry_daily_signal_date"]),
            event_end_date=end,
            pre_sessions=60,
            post_sessions=20,
            show_sma5=True,
            horizontal_levels={
                "BOX_FLOOR": Decimal(str(original["box_floor"])),
                "LOWER_ZONE_UPPER": Decimal(str(original["box_floor"]))
                * Decimal("1.03"),
                "BOX_UPPER": Decimal(str(original["box_upper"])),
                "UPPER_SELL_LEVEL": Decimal(str(original["box_upper"]))
                * Decimal("0.97"),
            },
        )
        filename = deterministic_chart_filename(
            stock_code,
            ChartType.EVENT_REVIEW,
            _as_date(original["entry_daily_signal_date"]),
            slug="down-box-v0-3-a-rescue",
        )
        artifact = render_review_chart(
            prepared,
            chart_root / stock_code / filename,
            strategy_policy=PROOF_VERSION,
            summary=campaign,
        )
        artifacts.append(
            {
                "stock_code": stock_code,
                "setup_id": campaign["setup_id"],
                "png_path": artifact.png_path.as_posix(),
                "metadata_path": artifact.metadata_path.as_posix(),
            }
        )
    return artifacts


def run_v03a_long_history_ab(
    *,
    output: Path = OUTPUT_PATH,
    chart_root: Path = CHART_ROOT,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    """Run baseline A and proof-only V0.3-A B over the cached universe."""

    from src.kiwoom_daily.down_box_daily_execution_proof import (
        run_down_box_daily_execution_proof,
    )

    a_results: dict[str, dict[str, Any]] = {}
    b_results: dict[str, dict[str, Any]] = {}
    daily_by_stock: dict[str, tuple[DailyBar, ...]] = {}
    provenance: dict[str, Any] = {}
    for stock_code in sorted(stocks):
        bars, source_provenance = _load_stock(stock_code)
        daily_by_stock[stock_code] = bars
        provenance[stock_code] = source_provenance
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        a_results[stock_code] = run_down_box_daily_execution_proof(
            daily_bars=bars,
            calendar=calendar,
            research_start=RESEARCH_START,
            research_end=RESEARCH_END,
        )
        b_results[stock_code] = _run_variant_b(
            daily_bars=bars,
            calendar=calendar,
            research_start=RESEARCH_START,
            research_end=RESEARCH_END,
        )
    campaigns = _campaign_rows(a_results, b_results, initial_capital=INITIAL_CAPITAL)
    # Attach the exact candidate row to each campaign for chart/inspection.
    for campaign in campaigns:
        b = b_results[campaign["stock_code"]]
        candidate = next(
            (
                row
                for row in b["rescue_candidates"]
                if row.get("setup_id") == campaign["setup_id"]
            ),
            None,
        )
        campaign["rescue_candidate"] = candidate
        if candidate is not None:
            campaign["setup_origin_date"] = candidate["setup_origin_date"]
        if campaign.get("rescue") is not None:
            campaign["rescue"] = dict(campaign["rescue"])
    charts = _generate_rescue_charts(campaigns, daily_by_stock, chart_root)
    for campaign in campaigns:
        campaign["chart"] = next(
            (
                chart
                for chart in charts
                if chart["stock_code"] == campaign["stock_code"]
                and chart["setup_id"] == campaign["setup_id"]
            ),
            None,
        )
    stock_comparison = {
        stock_code: _metric_comparison(
            a_results[stock_code]["metrics"], b_results[stock_code]["metrics"]
        )
        for stock_code in sorted(a_results)
    }
    aggregate_a = _aggregate_variant(a_results, campaigns)
    aggregate_b = _aggregate_variant(b_results, campaigns)
    large = _large_winner_protection(a_results, b_results)
    result = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "baseline_variant": "DOWN_BOX_REVERSAL_V0_2_BASELINE",
        "comparison_variant": "DOWN_BOX_REVERSAL_V0_3_A_POST_FLOOR_SECOND_TURN",
        "universe": sorted(stocks),
        "research_period": {
            "start": RESEARCH_START,
            "end": RESEARCH_END,
        },
        "policy": {
            "original_setup_window": "D1..D10; D11+ cannot rescue original setup",
            "eligible_original_entry_types": ["MA5_TURN", "BOTH"],
            "excluded_original_entry_type": "SMA10_REBREAK",
            "ma5_turn": "SMA5[t-1] <= SMA5[t-2] and SMA5[t] > SMA5[t-1]",
            "rescue_limit": "one per original setup; TURN3 prohibited",
            "rescue_fill": "T+1 RAW Daily open; no retry",
            "frozen_box": True,
            "lower_zone_required": False,
            "price_basis": {
                "signal": "DAILY_ADJUSTED",
                "execution": "KA10081_RAW_DAILY_OPEN",
            },
        },
        "setup_collision_audit": [
            row
            for stock in b_results.values()
            for row in stock["setup_collision_audit"]
        ],
        "a": {
            "per_stock": a_results,
            "aggregate": aggregate_a,
        },
        "b": {
            "per_stock": b_results,
            "aggregate": aggregate_b,
        },
        "campaigns": campaigns,
        "stock_level_comparison": stock_comparison,
        "large_winner_protection": large,
        "charts": charts,
        "artifact_provenance": provenance,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=PROOF_VERSION)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    parser.add_argument("--stocks", nargs="*", default=list(STOCKS))
    args = parser.parse_args()
    result = run_v03a_long_history_ab(
        output=args.output,
        chart_root=args.chart_root,
        stocks=tuple(args.stocks),
    )
    print(
        json.dumps(
            {
                "proof_version": result["proof_version"],
                "candidate_count": sum(
                    campaign.get("rescue_candidate") is not None
                    for campaign in result["campaigns"]
                ),
                "rescue_fill_count": result["b"]["aggregate"][
                    "rescue_entry_fill_count"
                ],
                "aggregate_a": result["a"]["aggregate"],
                "aggregate_b": result["b"]["aggregate"],
            },
            ensure_ascii=False,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
