"""Offline Daily execution proof and V0.3 audit for DOWN_BOX_REVERSAL.

This proof intentionally uses no minute data.  Daily adjusted bars are the
signal series and the cached ka10081 RAW Daily open is the execution series.
Floor reclaim and UP-transition rows are observations only; they never create
orders or alter the frozen V0.2 signal rules.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from pathlib import Path
from statistics import median
from typing import Any

from src.backtest_engine.down_box_strategy import (
    BoxEventType,
    BoxSetup,
    BoxSetupState,
    BoxSignal,
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
    ExplicitTradingCalendar,
    TradingCalendar,
)
from src.backtest_engine.validation import validate_daily_bars

# Reuse the existing immutable artifact loader for the DailyBar bridge.  It
# reads both RAW and ADJUSTED pages and never writes them.
from src.kiwoom_minute.small_up_path_proof import _load_existing_daily_bars

from .models import PriceBasis

PROOF_VERSION = "DOWN_BOX_REVERSAL_V0_3_DAILY_EXECUTION_PROOF"
OUTPUT_PATH = Path(
    "data/processed/kiwoom/down_box_reversal_v0_3_daily_execution_proof.json"
)
CANDIDATE_CSV_PATH = Path(
    "data/processed/kiwoom/down_box_reversal_v0_3_candidate_review.csv"
)
CHART_ROOT = Path("data/processed/strategy_charts/down_box_reversal_v0_3_daily")
RESEARCH_START = date(2023, 9, 1)
RESEARCH_END = date(2026, 8, 28)
DAILY_ARTIFACT_BASE_DATE = "20260831"
INITIAL_CAPITAL = Decimal(100000000)
STOCK_FULL_WEIGHT = Decimal("0.10")


class DailyProofAction(str, Enum):
    INITIAL_ENTRY = "INITIAL_ENTRY"
    HALF_EXIT = "HALF_EXIT"
    FULL_EXIT_FLOOR_BREAK = "FULL_EXIT_FLOOR_BREAK"
    FULL_TAKE_PROFIT_UPPER = "FULL_TAKE_PROFIT_UPPER"
    BREAKOUT_REENTRY = "BREAKOUT_REENTRY"

    @property
    def persistent(self) -> bool:
        return self in {
            DailyProofAction.FULL_EXIT_FLOOR_BREAK,
            DailyProofAction.FULL_TAKE_PROFIT_UPPER,
        }


@dataclass(frozen=True, slots=True)
class DailyProofIntent:
    intent_id: str
    stock_code: str
    action: DailyProofAction
    signal_date: date
    activation_date: date
    setup: BoxSetup
    signal_id: str
    equity_at_decision: Decimal | None = None
    entry_type: str | None = None
    breakout_reentry_setup: BoxSetup | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DailyFillOutcome:
    action: DailyProofAction
    setup: BoxSetup
    fill: Mapping[str, Any]
    next_setup: BoxSetup | None


def _require_code(stock_code: str) -> None:
    if len(stock_code) != 6 or not stock_code.isascii() or not stock_code.isdigit():
        raise ValueError("stock_code must be exactly six ASCII digits")


class ZeroCostDailyAccount:
    """Proof-local Daily RAW-open accounting with immutable transition rows."""

    def __init__(
        self,
        *,
        stock_code: str,
        initial_capital: Decimal,
        stock_full_weight: Decimal,
        corporate_action_ambiguous: Sequence[str] = (),
    ) -> None:
        _require_code(stock_code)
        if not isinstance(initial_capital, Decimal) or initial_capital <= 0:
            raise ValueError("initial_capital must be a positive Decimal")
        if not isinstance(stock_full_weight, Decimal) or not (
            Decimal(0) < stock_full_weight <= Decimal(1)
        ):
            raise ValueError("stock_full_weight must be Decimal in (0, 1]")
        self.stock_code = stock_code
        self.initial_capital = initial_capital
        self.stock_full_weight = stock_full_weight
        self.cash = initial_capital
        self.quantity = 0
        self.state = "FLAT"
        self.pending: DailyProofIntent | None = None
        self.current_trade: dict[str, Any] | None = None
        self.fills: list[dict[str, Any]] = []
        self.transitions: list[dict[str, Any]] = []
        self.completed_trades: list[dict[str, Any]] = []
        self.expirations: list[dict[str, Any]] = []
        self.corporate_action_exclusions: list[dict[str, Any]] = []
        self.half_exit_consumed = False
        self.half_exit_done = False
        self._ambiguous_setup_ids = frozenset(corporate_action_ambiguous)

    def schedule(self, intent: DailyProofIntent) -> None:
        if intent.stock_code != self.stock_code:
            raise ValueError("intent stock does not match account")
        if self.pending is not None:
            raise ValueError("only one pending Daily action is allowed")
        if intent.action in {
            DailyProofAction.INITIAL_ENTRY,
            DailyProofAction.BREAKOUT_REENTRY,
        }:
            expected = (
                "FLAT"
                if intent.action is DailyProofAction.INITIAL_ENTRY
                else "BREAKOUT_REENTRY_WAIT"
            )
            if self.state != expected or self.quantity != 0:
                raise ValueError("entry intent requires its matching flat state")
            if (
                not isinstance(intent.equity_at_decision, Decimal)
                or not intent.equity_at_decision.is_finite()
                or intent.equity_at_decision <= 0
            ):
                raise ValueError("entry intent requires decision-time Decimal equity")
        elif self.state not in {"FULL_POSITION", "HALF_POSITION"} or self.quantity <= 0:
            raise ValueError("exit intent requires an open BOX position")
        if intent.action is DailyProofAction.HALF_EXIT:
            if self.half_exit_consumed:
                raise ValueError("half exit may be signalled only once")
            self.half_exit_consumed = True
        self.pending = intent
        self.transitions.append(
            {
                "transition": "ACTION_SCHEDULED",
                "intent_id": intent.intent_id,
                "action": intent.action.value,
                "signal_date": intent.signal_date,
                "activation_date": intent.activation_date,
                "persistent": intent.action.persistent,
            }
        )

    def expire_if_due_without_bar(self, day: date) -> None:
        intent = self.pending
        if intent is None or intent.action.persistent or day != intent.activation_date:
            return
        self.expirations.append(
            {
                "intent_id": intent.intent_id,
                "action": intent.action.value,
                "signal_date": intent.signal_date,
                "activation_date": intent.activation_date,
                "reason": "NO_RAW_DAILY_BAR_ON_T_PLUS_1",
            }
        )
        self.transitions.append(
            {
                "transition": "ACTION_EXPIRED",
                "intent_id": intent.intent_id,
                "action": intent.action.value,
                "trade_date": day,
            }
        )
        self.pending = None

    def fill_daily_open(
        self,
        day: date,
        daily_bar: DailyBar,
        *,
        known_days: Sequence[date],
    ) -> DailyFillOutcome | None:
        intent = self.pending
        if intent is None or day < intent.activation_date:
            return None
        if not intent.action.persistent and day != intent.activation_date:
            raise ValueError("non-persistent action cannot fill after T+1")
        if daily_bar.trade_date != day or daily_bar.raw.open <= 0:
            raise ValueError("RAW Daily open must be positive and on the fill date")
        price = daily_bar.raw.open
        quantity_before = self.quantity
        cash_before = self.cash
        next_setup: BoxSetup | None = None
        if intent.action in {
            DailyProofAction.INITIAL_ENTRY,
            DailyProofAction.BREAKOUT_REENTRY,
        }:
            if intent.equity_at_decision is None:
                raise ValueError("entry intent lost decision-time equity")
            budget = intent.equity_at_decision * self.stock_full_weight
            fill_quantity = int(
                (budget / price).to_integral_value(rounding=ROUND_FLOOR)
            )
            if fill_quantity <= 0 or price * fill_quantity > self.cash:
                raise ValueError("FULL entry cannot purchase one integer share")
            self.cash -= price * Decimal(fill_quantity)
            self.quantity = fill_quantity
            if intent.action is DailyProofAction.INITIAL_ENTRY:
                self.state = "FULL_POSITION"
                self.half_exit_consumed = False
                self.half_exit_done = False
                self.current_trade = {
                    "stock_code": self.stock_code,
                    "setup_id": intent.setup.setup_id,
                    "entry_type": intent.entry_type,
                    "entry_daily_signal_date": intent.signal_date,
                    "entry_daily_signal_close": intent.metadata.get(
                        "entry_signal_close"
                    )
                    if intent.metadata
                    else None,
                    "entry_fill_date": day,
                    "entry_fill_source": "KA10081_RAW_DAILY_OPEN",
                    "entry_raw_price": price,
                    "initial_quantity": fill_quantity,
                    "initial_invested_capital": price * Decimal(fill_quantity),
                    "realized_pnl": Decimal(0),
                    "half_exit": None,
                    **dict(intent.metadata or {}),
                }
                next_setup = replace(intent.setup, state=BoxSetupState.BOX_POSITION)
            else:
                self.state = "HANDOFF_TO_UP_FILLED"
                self.transitions.append(
                    {
                        "transition": "HANDOFF_TO_UP_FILLED",
                        "intent_id": intent.intent_id,
                        "trade_date": day,
                    }
                )
        elif intent.action is DailyProofAction.HALF_EXIT:
            if self.current_trade is None:
                raise ValueError("half exit requires an active trade")
            fill_quantity = self.current_trade["initial_quantity"] // 2
            if fill_quantity <= 0 or fill_quantity >= self.quantity:
                raise ValueError("position cannot support a deterministic half exit")
            self.cash += price * Decimal(fill_quantity)
            self.quantity -= fill_quantity
            self.state = "HALF_POSITION"
            self.half_exit_done = True
            realized = (price - self.current_trade["entry_raw_price"]) * Decimal(
                fill_quantity
            )
            self.current_trade["realized_pnl"] += realized
            self.current_trade["half_exit"] = {
                "signal_date": intent.signal_date,
                "fill_date": day,
                "raw_price": price,
                "quantity": fill_quantity,
                "unrealized_pnl_before_fill": (
                    price - self.current_trade["entry_raw_price"]
                )
                * Decimal(self.current_trade["initial_quantity"]),
            }
        else:
            if self.current_trade is None:
                raise ValueError("full exit requires an active trade")
            fill_quantity = self.quantity
            if fill_quantity <= 0:
                raise ValueError("full exit has no remaining quantity")
            self.cash += price * Decimal(fill_quantity)
            final_leg = (price - self.current_trade["entry_raw_price"]) * Decimal(
                fill_quantity
            )
            total_pnl = self.current_trade["realized_pnl"] + final_leg
            if intent.setup.setup_id in self._ambiguous_setup_ids:
                self.corporate_action_exclusions.append(
                    {
                        "setup_id": intent.setup.setup_id,
                        "signal_date": intent.signal_date,
                        "reason": "CORPORATE_ACTION_AMBIGUOUS",
                    }
                )
            else:
                self.completed_trades.append(
                    {
                        **self.current_trade,
                        "exit_action": intent.action.value,
                        "exit_daily_signal_date": intent.signal_date,
                        "exit_fill_date": day,
                        "exit_fill_source": "KA10081_RAW_DAILY_OPEN",
                        "exit_raw_price": price,
                        "exit_quantity": fill_quantity,
                        "pnl_amount": total_pnl,
                        "pnl_pct": total_pnl
                        / self.current_trade["initial_invested_capital"]
                        * Decimal(100),
                        "holding_sessions": _holding_sessions(
                            known_days, self.current_trade["entry_fill_date"], day
                        ),
                    }
                )
            self.current_trade = None
            self.quantity = 0
            if intent.breakout_reentry_setup is not None:
                self.state = "BREAKOUT_REENTRY_WAIT"
                next_setup = intent.breakout_reentry_setup
            else:
                self.state = "FLAT"
        fill = {
            "fill_id": stable_id(
                "down_box_daily_fill",
                intent.intent_id,
                day,
                price,
                fill_quantity,
            ),
            "intent_id": intent.intent_id,
            "action": intent.action.value,
            "signal_date": intent.signal_date,
            "filled_at": day,
            "raw_price": price,
            "quantity": fill_quantity,
            "price_basis": PriceBasis.RAW.value,
            "source": "KIWOOM_KA10081_RAW",
            "commission": Decimal(0),
            "tax": Decimal(0),
            "slippage": Decimal(0),
        }
        self.fills.append(fill)
        self.transitions.append(
            {
                "transition": "ACTION_FILLED",
                "intent_id": intent.intent_id,
                "action": intent.action.value,
                "trade_date": day,
                "quantity_before": quantity_before,
                "quantity_after": self.quantity,
                "cash_before": cash_before,
                "cash_after": self.cash,
            }
        )
        self.pending = None
        return DailyFillOutcome(intent.action, intent.setup, fill, next_setup)


def _holding_sessions(known_days: Sequence[date], start: date, end: date) -> int:
    return sum(start <= day <= end for day in known_days)


def _make_intent(
    *,
    signal: BoxSignal,
    action: DailyProofAction,
    setup: BoxSetup,
    activation_date: date,
    equity_at_decision: Decimal | None = None,
    entry_type: str | None = None,
    breakout_reentry_setup: BoxSetup | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> DailyProofIntent:
    return DailyProofIntent(
        stable_id(
            "down_box_daily_intent",
            signal.stock_code,
            action.value,
            signal.signal_date,
            activation_date,
            setup.setup_id,
            signal.signal_id,
        ),
        signal.stock_code,
        action,
        signal.signal_date,
        activation_date,
        setup,
        signal.signal_id,
        equity_at_decision,
        entry_type,
        breakout_reentry_setup,
        dict(metadata or {}),
    )


def _position_signal(signals: Sequence[BoxSignal]) -> BoxSignal | None:
    priority = {
        BoxSignalType.FULL_EXIT_FLOOR_BREAK: 0,
        BoxSignalType.FULL_TAKE_PROFIT_UPPER: 1,
        BoxSignalType.HALF_EXIT_SIGNAL: 2,
    }
    candidates = [signal for signal in signals if signal.signal_type in priority]
    return min(
        candidates, key=lambda signal: priority[signal.signal_type], default=None
    )


def _entry_type(signal: BoxSignal) -> str:
    return {
        BoxSignalType.ENTRY_CANDIDATE_MA5_TURN: "MA5_TURN",
        BoxSignalType.ENTRY_CANDIDATE_SMA10_REBREAK: "SMA10_REBREAK",
        BoxSignalType.ENTRY_CANDIDATE_MA5_TURN_AND_SMA10_REBREAK: "BOTH",
    }[signal.signal_type]


def _entry_metadata(
    setup: BoxSetup, bars: Sequence[DailyBar], index: int
) -> dict[str, Any]:
    bar = bars[index]
    width = setup.box_upper - setup.box_floor
    close_position = (bar.signal.close - setup.box_floor) / width
    low_position = (bar.signal.low - setup.box_floor) / width
    dates = {item.trade_date: offset for offset, item in enumerate(bars)}
    origin_index = dates[setup.setup_origin_date]
    lower_upper = setup.box_floor * Decimal("1.03")
    touches = [
        item.trade_date
        for item in bars[max(0, origin_index - 10) : index + 1]
        if setup.box_floor <= item.signal.low <= lower_upper
        or setup.box_floor <= item.signal.close <= lower_upper
    ]
    location = (
        "LOWER"
        if close_position < Decimal(1) / Decimal(3)
        else "MIDDLE"
        if close_position < Decimal(2) / Decimal(3)
        else "UPPER"
    )
    return {
        "box_floor": setup.box_floor,
        "box_upper": setup.box_upper,
        "box_position_close": close_position,
        "box_position_low": low_position,
        "entry_location": location,
        "lower_zone_touch_dates": touches,
        "last_lower_zone_touch_date": touches[-1] if touches else None,
        "sessions_since_lower_zone_touch": (
            index - dates[touches[-1]] if touches else None
        ),
        "entry_signal_close": bar.signal.close,
    }


def run_down_box_daily_execution_proof(
    *,
    daily_bars: Sequence[DailyBar],
    calendar: TradingCalendar,
    research_start: date = RESEARCH_START,
    research_end: date = RESEARCH_END,
    stock_full_weight: Decimal = STOCK_FULL_WEIGHT,
    initial_capital: Decimal = INITIAL_CAPITAL,
    corporate_action_ambiguous: Sequence[str] = (),
) -> dict[str, Any]:
    if research_start > research_end:
        raise ValueError("research_start must not follow research_end")
    canonical = tuple(sorted(daily_bars, key=lambda bar: bar.trade_date))
    if not canonical:
        raise ValueError("daily_bars must not be empty")
    validate_daily_bars(canonical, calendar)
    stock_codes = {bar.stock_code for bar in canonical}
    if len(stock_codes) != 1:
        raise ValueError("proof supports exactly one stock")
    stock_code = next(iter(stock_codes))
    points = tuple(calculate_daily_indicators(canonical, calendar))
    by_date = {bar.trade_date: bar for bar in canonical}
    known_days = tuple(bar.trade_date for bar in canonical)
    research_days = tuple(
        day for day in known_days if research_start <= day <= research_end
    )
    config = DownBoxStrategyConfig()
    account = ZeroCostDailyAccount(
        stock_code=stock_code,
        initial_capital=initial_capital,
        stock_full_weight=stock_full_weight,
        corporate_action_ambiguous=corporate_action_ambiguous,
    )
    active: BoxSetup | None = None
    events: list[dict[str, Any]] = []
    reentry_candidates: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for day in research_days:
        bar = by_date[day]
        outcome = account.fill_daily_open(day, bar, known_days=known_days)
        if outcome is not None:
            counts[f"{outcome.action.value}_FILLS"] += 1
            if outcome.action is DailyProofAction.INITIAL_ENTRY or outcome.action in {
                DailyProofAction.FULL_EXIT_FLOOR_BREAK,
                DailyProofAction.FULL_TAKE_PROFIT_UPPER,
            }:
                active = outcome.next_setup
            elif outcome.action is DailyProofAction.BREAKOUT_REENTRY:
                active = None
        equity_curve.append(
            {
                "trade_date": day,
                "equity": account.cash + Decimal(account.quantity) * bar.raw.close,
                "holding": account.quantity > 0,
            }
        )
        if account.pending is not None:
            continue
        index = {item.trade_date: offset for offset, item in enumerate(canonical)}[day]
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
            breakout = next(
                (
                    event
                    for event in decision.events
                    if event.event_type is BoxEventType.BOX_BREAKOUT_CONFIRMED
                ),
                None,
            )
            if breakout is not None:
                counts["BOX_BREAKOUT_CONFIRMED"] += 1
                active = decision.setup_after
                account.state = "BREAKOUT_REENTRY_WAIT"
                continue
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
                _schedule(
                    account,
                    signal=signal,
                    action=DailyProofAction.INITIAL_ENTRY,
                    setup=active,
                    calendar=calendar,
                    equity_at_decision=account.cash
                    + Decimal(account.quantity) * bar.raw.close,
                    entry_type=_entry_type(signal),
                    metadata=_entry_metadata(active, canonical, index),
                )
                active = replace(
                    decision.setup_after, state=BoxSetupState.ENTRY_SIGNALLED
                )
            elif decision.setup_after.state in {
                BoxSetupState.EXPIRED,
                BoxSetupState.INVALIDATED,
            }:
                if decision.setup_after.state is BoxSetupState.EXPIRED:
                    counts["REVERSAL_EXPIRED"] += 1
                elif any(
                    event.event_type is BoxEventType.FLOOR_BREAK
                    for event in decision.events
                ):
                    counts["REVERSAL_FLOOR_INVALIDATED"] += 1
                else:
                    counts["REVERSAL_UPPER_INVALIDATED"] += 1
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
            breakout_setup = (
                decision.setup_after
                if decision.setup_after.state is BoxSetupState.BREAKOUT_REENTRY_WAIT
                else None
            )
            if signal.signal_type is BoxSignalType.FULL_EXIT_FLOOR_BREAK:
                action = DailyProofAction.FULL_EXIT_FLOOR_BREAK
                counts["FLOOR_EXIT_SIGNALS"] += 1
            elif signal.signal_type is BoxSignalType.FULL_TAKE_PROFIT_UPPER:
                action = DailyProofAction.FULL_TAKE_PROFIT_UPPER
                counts["UPPER_EXIT_SIGNALS"] += 1
                if breakout_setup is not None:
                    counts["HOLDING_BREAKOUT_CONFIRMED"] += 1
            else:
                action = DailyProofAction.HALF_EXIT
                counts["HALF_EXIT_SIGNALS"] += 1
            _schedule(
                account,
                signal=signal,
                action=action,
                setup=active,
                calendar=calendar,
                breakout_reentry_setup=breakout_setup,
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
                # V0.3 is a Daily execution-source proof only.  A breakout
                # re-entry is therefore recorded as an observation and never
                # scheduled or filled; a later strategy phase may define its
                # own handoff/execution contract.
                reentry_candidates.append(
                    {
                        "stock_code": signal.stock_code,
                        "setup_id": signal.setup_id,
                        "signal_id": signal.signal_id,
                        "signal_date": signal.signal_date,
                        "reason": signal.reason,
                        "details": dict(signal.details),
                    }
                )
                active = None
                account.state = "REENTRY_CANDIDATE_REPORTED"
            elif decision.setup_after.state is BoxSetupState.INVALIDATED:
                counts["BREAKOUT_FAILED"] += 1
                active = None
                account.state = "FLAT"
            else:
                active = decision.setup_after
    for intent in (account.pending,):
        if intent is not None and not intent.action.persistent:
            account.expire_if_due_without_bar(intent.activation_date)
            counts[f"{intent.action.value}_EXPIRATIONS"] += 1
            active = None
    return {
        "proof_version": PROOF_VERSION,
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
            "half_exit_rounding": "floor(initial_quantity / 2)",
        },
        "counts": dict(sorted(counts.items())),
        "completed_trades": account.completed_trades,
        "fills": account.fills,
        "expirations": account.expirations,
        "corporate_action_exclusions": account.corporate_action_exclusions,
        "transitions": account.transitions,
        "daily_events": events,
        "reentry_candidates": reentry_candidates,
        "state_at_end": account.state,
        "open_quantity": account.quantity,
        "pending_action_at_end": account.pending.action.value
        if account.pending
        else None,
        "active_setup_id_at_end": active.setup_id if active else None,
        "metrics": _metrics(
            account,
            equity_curve,
            initial_capital,
            canonical,
            research_start,
            research_end,
        ),
    }


def _schedule(
    account: ZeroCostDailyAccount,
    *,
    signal: BoxSignal,
    action: DailyProofAction,
    setup: BoxSetup,
    calendar: TradingCalendar,
    equity_at_decision: Decimal | None = None,
    entry_type: str | None = None,
    breakout_reentry_setup: BoxSetup | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    account.schedule(
        _make_intent(
            signal=signal,
            action=action,
            setup=setup,
            activation_date=calendar.next_trading_day(signal.signal_date),
            equity_at_decision=equity_at_decision,
            entry_type=entry_type,
            breakout_reentry_setup=breakout_reentry_setup,
            metadata=metadata,
        )
    )


def _event_rows(events: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "event": event.event_type.value,
            "event_date": event.event_date,
            "setup_id": event.setup_id,
            "reason": event.reason,
            "details": dict(event.details),
        }
        for event in events
    ]


def _metrics(
    account: ZeroCostDailyAccount,
    equity_curve: Sequence[Mapping[str, Any]],
    initial: Decimal,
    bars: Sequence[DailyBar],
    research_start: date,
    research_end: date,
) -> dict[str, Any]:
    in_range = [bar for bar in bars if research_start <= bar.trade_date <= research_end]
    last_price = in_range[-1].raw.close if in_range else Decimal(0)
    final_equity = account.cash + Decimal(account.quantity) * last_price
    returns = (final_equity / initial - Decimal(1)) * Decimal(100)
    peak: Decimal | None = None
    mdd = Decimal(0)
    for row in equity_curve:
        equity = row["equity"]
        peak = equity if peak is None else max(peak, equity)
        mdd = min(mdd, (equity / peak - Decimal(1)) * Decimal(100))
    trades = account.completed_trades
    wins = [row for row in trades if row["pnl_amount"] > 0]
    losses = [row for row in trades if row["pnl_amount"] < 0]
    gp = sum((row["pnl_amount"] for row in wins), Decimal(0))
    gl = -sum((row["pnl_amount"] for row in losses), Decimal(0))
    aw = sum((row["pnl_pct"] for row in wins), Decimal(0)) / len(wins) if wins else None
    al = (
        sum((row["pnl_pct"] for row in losses), Decimal(0)) / len(losses)
        if losses
        else None
    )
    return {
        "final_equity": final_equity,
        "cumulative_return_pct": returns,
        "mdd_pct": mdd,
        "win_rate_pct": Decimal(len(wins)) / Decimal(len(trades)) * Decimal(100)
        if trades
        else None,
        "average_win_pct": aw,
        "average_loss_pct": al,
        "payoff_ratio": aw / -al
        if aw is not None and al not in {None, Decimal(0)}
        else None,
        "profit_factor": gp / gl if gl else None,
        "average_holding_sessions": (
            Decimal(sum(row["holding_sessions"] for row in trades)) / len(trades)
            if trades
            else None
        ),
        "exposure": Decimal(sum(bool(row["holding"]) for row in equity_curve))
        / Decimal(len(equity_curve))
        if equity_curve
        else Decimal(0),
        "turnover": sum(
            (row["raw_price"] * Decimal(row["quantity"]) for row in account.fills),
            Decimal(0),
        )
        / initial,
    }


def _aggregate_metrics(
    per_stock: Mapping[str, Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return deterministic cross-stock summary metrics for this proof."""

    wins = [row for row in trades if row["pnl_amount"] > 0]
    losses = [row for row in trades if row["pnl_amount"] < 0]
    gross_profit = sum((row["pnl_amount"] for row in wins), Decimal(0))
    gross_loss = -sum((row["pnl_amount"] for row in losses), Decimal(0))
    average_win = (
        sum((row["pnl_pct"] for row in wins), Decimal(0)) / len(wins) if wins else None
    )
    average_loss = (
        sum((row["pnl_pct"] for row in losses), Decimal(0)) / len(losses)
        if losses
        else None
    )
    stock_metrics = [
        result["metrics"] for result in per_stock.values() if result["metrics"]
    ]
    mdds = [metric["mdd_pct"] for metric in stock_metrics]
    exposures = [metric["exposure"] for metric in stock_metrics]
    turnovers = [metric["turnover"] for metric in stock_metrics]
    holdings = [
        metric["average_holding_sessions"]
        for metric in stock_metrics
        if metric["average_holding_sessions"] is not None
    ]

    def mean(values: Sequence[Decimal]) -> Decimal | None:
        return sum(values, Decimal(0)) / len(values) if values else None

    return {
        "trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": (
            Decimal(len(wins)) / Decimal(len(trades)) * Decimal(100) if trades else None
        ),
        "total_realized_pnl": sum((row["pnl_amount"] for row in trades), Decimal(0)),
        "average_win_pct": average_win,
        "average_loss_pct": average_loss,
        "payoff_ratio": (
            average_win / -average_loss
            if average_win is not None and average_loss not in {None, Decimal(0)}
            else None
        ),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_holding_sessions": mean(holdings),
        "mean_mdd_pct": mean(mdds),
        "median_mdd_pct": median(mdds) if mdds else None,
        "mean_exposure": mean(exposures),
        "mean_turnover": mean(turnovers),
    }


def _sma20_persistent(
    points: Sequence[DailyIndicatorPoint], d3_index: int
) -> tuple[bool, Decimal | None, Decimal | None]:
    if d3_index < 10:
        return False, None, None
    now = points[d3_index].sma20
    five = points[d3_index - 5].sma20
    ten = points[d3_index - 10].sma20
    if now is None or five is None or ten is None:
        return False, None, None
    return now > five > ten, now / five - Decimal(1), now / ten - Decimal(1)


def _sma60_flatness(
    points: Sequence[DailyIndicatorPoint], d3_index: int
) -> tuple[Decimal | None, Decimal | None]:
    if d3_index < 5:
        return None, None
    now = points[d3_index].sma60
    old = points[d3_index - 5].sma60
    if now is None or old in {None, Decimal(0)}:
        return None, None
    signed = now / old - Decimal(1)
    return abs(signed), signed


def _future_return(
    closes: Sequence[Decimal], index: int, sessions: int
) -> Decimal | None:
    target = index + sessions
    if not 0 <= index < len(closes) or target >= len(closes) or closes[index] == 0:
        return None
    return closes[target] / closes[index] - Decimal(1)


def _three_day_support(
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
    indexes: Sequence[int],
) -> tuple[bool, ...]:
    # DailyIndicatorPoint intentionally exposes only the V1 daily indicators
    # (SMA10/20/60).  The V0.2 audit's SMA5 support is the same trailing
    # signal-close SMA used by the frozen strategy, so derive it locally from
    # the complete adjusted series rather than adding a parallel model field.
    sma5 = simple_moving_average([bar.signal.close for bar in bars], 5)
    return tuple(
        sma5[index] is not None and bars[index].signal.close >= sma5[index]
        for index in indexes
    )


def audit_floor_reclaim(
    trade: Mapping[str, Any],
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
) -> dict[str, Any]:
    date_to_index = {bar.trade_date: index for index, bar in enumerate(bars)}
    exit_signal_date = trade["exit_daily_signal_date"]
    exit_fill_date = trade["exit_fill_date"]
    exit_index = date_to_index[exit_fill_date]
    entry_close = trade["entry_daily_signal_close"]
    floor = trade["box_floor"]
    rows = []
    closes = [bar.signal.close for bar in bars]
    reclaim = None
    for offset in range(3):
        index = exit_index + offset
        if index >= len(bars):
            break
        bar = bars[index]
        point = points[index]
        if reclaim is None and bar.signal.close >= entry_close:
            reclaim = {
                "reclaim_date": bar.trade_date,
                "sessions_since_floor_exit": offset,
            }
        rows.append(
            {
                "day_number": offset + 1,
                "trade_date": bar.trade_date,
                "close_vs_floor": bar.signal.close / floor - Decimal(1),
                "close_vs_original_entry_signal": bar.signal.close / entry_close
                - Decimal(1),
                "distance_to_sma20": (
                    bar.signal.close / point.sma20 - Decimal(1) if point.sma20 else None
                ),
                "distance_to_sma60": (
                    bar.signal.close / point.sma60 - Decimal(1) if point.sma60 else None
                ),
            }
        )
    reclaim_row = None
    if reclaim is not None:
        index = date_to_index[reclaim["reclaim_date"]]
        reclaim_row = {
            **reclaim,
            "distance_to_sma20": rows[reclaim["sessions_since_floor_exit"]][
                "distance_to_sma20"
            ],
            "distance_to_sma60": rows[reclaim["sessions_since_floor_exit"]][
                "distance_to_sma60"
            ],
            "future_5_session_return": _future_return(closes, index, 5),
            "future_10_session_return": _future_return(closes, index, 10),
            "future_20_session_return": _future_return(closes, index, 20),
        }
    return {
        "stock_code": trade["stock_code"],
        "setup_id": trade["setup_id"],
        "original_box_floor": floor,
        "original_entry_signal_close": entry_close,
        "original_entry_signal_date": trade["entry_daily_signal_date"],
        "floor_exit_signal_date": exit_signal_date,
        "floor_exit_fill_date": exit_fill_date,
        "d1_d3": rows,
        "fast_reclaim_observed": reclaim_row is not None,
        "fast_reclaim": reclaim_row,
    }


def audit_upper_transition(
    trade: Mapping[str, Any],
    bars: Sequence[DailyBar],
    points: Sequence[DailyIndicatorPoint],
) -> dict[str, Any]:
    dates = {bar.trade_date: index for index, bar in enumerate(bars)}
    exit_signal_date = trade["exit_daily_signal_date"]
    e_index = dates[exit_signal_date]
    x_index = dates[trade["exit_fill_date"]]
    d3_indexes = [x_index + offset for offset in range(3)]
    available = d3_indexes[-1] < len(bars)
    if not available:
        d3_indexes = [index for index in d3_indexes if index < len(bars)]
    d1_d3 = _three_day_support(bars, points, d3_indexes)
    d3_index = d3_indexes[-1] if len(d3_indexes) == 3 else None
    persistent, change5, change10 = (
        _sma20_persistent(points, d3_index)
        if d3_index is not None
        else (False, None, None)
    )
    flatness, signed_flatness = (
        _sma60_flatness(points, d3_index) if d3_index is not None else (None, None)
    )
    closes = [bar.signal.close for bar in bars]
    close_e = bars[e_index].signal.close
    d3_close = bars[d3_index].signal.close if d3_index is not None else None
    d3_run_signal = d3_close / close_e - Decimal(1) if d3_close is not None else None
    d3_run_fill = (
        d3_close / trade["_exit_fill_raw_open"] - Decimal(1)
        if d3_close is not None
        else None
    )
    rises = []
    if d3_index is not None:
        prior = close_e
        for index in d3_indexes:
            rises.append(closes[index] / prior - Decimal(1))
            prior = closes[index]
    structural = available and all(d1_d3) and persistent
    forward = {
        "future_5_session_return": _future_return(closes, d3_index, 5)
        if d3_index is not None
        else None,
        "future_10_session_return": _future_return(closes, d3_index, 10)
        if d3_index is not None
        else None,
        "future_20_session_return": _future_return(closes, d3_index, 20)
        if d3_index is not None
        else None,
    }
    excursion = _forward_excursion(closes, d3_index) if d3_index is not None else {}
    return {
        "stock_code": trade["stock_code"],
        "setup_id": trade["setup_id"],
        "entry_type": trade["entry_type"],
        "upper_exit_signal_date": exit_signal_date,
        "upper_exit_fill_date": trade["exit_fill_date"],
        "box_floor": trade["box_floor"],
        "box_upper": trade["box_upper"],
        "d1_d3_dates": [bars[index].trade_date for index in d3_indexes],
        "d1_close_above_sma5": d1_d3[0] if len(d1_d3) > 0 else None,
        "d2_close_above_sma5": d1_d3[1] if len(d1_d3) > 1 else None,
        "d3_close_above_sma5": d1_d3[2] if len(d1_d3) > 2 else None,
        "holds_sma5_all_3": len(d1_d3) == 3 and all(d1_d3),
        "sma20_persistent_up": persistent,
        "sma20_change_5": change5,
        "sma20_change_10": change10,
        "sma60_flatness_5": flatness,
        "sma60_change_5": signed_flatness,
        "d3_runup_from_exit_signal": d3_run_signal,
        "d3_runup_from_exit_fill": d3_run_fill,
        "rise_d1": rises[0] if len(rises) > 0 else None,
        "rise_d2": rises[1] if len(rises) > 1 else None,
        "rise_d3": rises[2] if len(rises) > 2 else None,
        "max_daily_rise_d1_d3": max(rises) if rises else None,
        "d3_distance_from_old_box_upper": (
            d3_close / trade["box_upper"] - Decimal(1) if d3_close is not None else None
        ),
        "structural_up_transition": structural,
        **forward,
        **excursion,
    }


def _forward_excursion(
    closes: Sequence[Decimal], index: int
) -> dict[str, Decimal | None]:
    if index is None or index >= len(closes):
        return {"maximum_favorable_excursion": None, "maximum_adverse_excursion": None}
    window = closes[index : min(len(closes), index + 21)]
    base = closes[index]
    if not window or base == 0:
        return {"maximum_favorable_excursion": None, "maximum_adverse_excursion": None}
    changes = [value / base - Decimal(1) for value in window]
    return {
        "maximum_favorable_excursion": max(changes),
        "maximum_adverse_excursion": min(changes),
    }


def _distribution(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    values = sorted(value for row in rows if (value := row.get(field)) is not None)
    if not values:
        return {
            "count": 0,
            "min": None,
            "p25": None,
            "median": None,
            "p75": None,
            "max": None,
        }

    def percentile(q: Decimal) -> Decimal:
        position = (len(values) - 1) * q
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        fraction = position - Decimal(lower)
        return values[lower] + (values[upper] - values[lower]) * fraction

    return {
        "count": len(values),
        "min": values[0],
        "p25": percentile(Decimal("0.25")),
        "median": percentile(Decimal("0.50")),
        "p75": percentile(Decimal("0.75")),
        "max": values[-1],
    }


def _artifact_provenance(stock_code: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "stock_code": stock_code,
        "base_date": DAILY_ARTIFACT_BASE_DATE,
    }
    for basis in (PriceBasis.RAW, PriceBasis.ADJUSTED):
        directory = (
            Path("data/raw/kiwoom/daily")
            / stock_code
            / basis.value.lower()
            / DAILY_ARTIFACT_BASE_DATE
        )
        files = sorted(directory.glob("page-*.json"))
        digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in files]
        result[basis.value.lower()] = {
            "provider": "KIWOOM",
            "service_name": "ka10081",
            "price_basis": basis.value,
            "raw_file_paths": [path.as_posix() for path in files],
            "raw_file_sha256": digests,
            "row_count": None,
        }
    return result


def _load_stock(stock_code: str) -> tuple[tuple[DailyBar, ...], dict[str, Any]]:
    bars = _load_existing_daily_bars(stock_code)
    provenance = _artifact_provenance(stock_code)
    # This is the canonical row count after RAW/ADJUSTED date alignment.  The
    # original page JSON and its SHA-256 remain untouched and are still listed
    # separately for provenance.
    for basis in (PriceBasis.RAW, PriceBasis.ADJUSTED):
        provenance[basis.value.lower()]["row_count"] = len(bars)
    return bars, provenance


def _review_events(
    row: Mapping[str, Any],
    bars: Sequence[DailyBar],
    *,
    category: str,
) -> list[Any]:
    from src.strategy_review.chart import ReviewEvent, ReviewEventType

    events: list[Any] = [
        ReviewEvent(
            ReviewEventType.BOX_BUY_CANDIDATE,
            row["entry_daily_signal_date"],
            "ENTRY",
        ),
        ReviewEvent(
            ReviewEventType.ENTRY_FILL,
            row["entry_fill_date"],
            "ENTRY",
            raw_fill_price=row["entry_raw_price"],
            source_label="KA10081 RAW DAILY OPEN",
        ),
    ]
    if row.get("half_exit") is not None:
        half = row["half_exit"]
        events.append(
            ReviewEvent(
                ReviewEventType.EXIT_FILL,
                half["fill_date"],
                "HALF EXIT",
                raw_fill_price=half["raw_price"],
                source_label="KA10081 RAW DAILY OPEN",
            )
        )
    event_type = (
        ReviewEventType.FLOOR_BREAK
        if row["exit_action"] == DailyProofAction.FULL_EXIT_FLOOR_BREAK.value
        else ReviewEventType.UPPER_TAKE_PROFIT
    )
    events.append(ReviewEvent(event_type, row["exit_daily_signal_date"], "FULL EXIT"))
    events.append(
        ReviewEvent(
            ReviewEventType.EXIT_FILL,
            row["exit_fill_date"],
            "EXIT",
            raw_fill_price=row["exit_raw_price"],
            source_label="KA10081 RAW DAILY OPEN",
        )
    )
    audit = row.get("audit") or {}
    dates = audit.get("d1_d3_dates", [])
    for offset, day in enumerate(dates, 1):
        events.append(
            ReviewEvent(ReviewEventType.BREAKOUT_REENTRY_WAIT, day, f"D{offset}")
        )
    if audit.get("fast_reclaim_observed") or audit.get("structural_up_transition"):
        marker_date = (
            audit.get("reclaim", {}).get("reclaim_date")
            if audit.get("reclaim")
            else dates[-1]
            if dates
            else row["exit_fill_date"]
        )
        events.append(
            ReviewEvent(
                ReviewEventType.BREAKOUT_REENTRY_CANDIDATE,
                marker_date,
                "RECLAIM" if audit.get("fast_reclaim_observed") else "STRUCTURAL UP",
            )
        )
    return events


def _generate_charts(
    all_trades: Sequence[Mapping[str, Any]],
    floor_rows: Sequence[Mapping[str, Any]],
    upper_rows: Sequence[Mapping[str, Any]],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
    chart_root: Path,
) -> list[dict[str, Any]]:
    from src.strategy_review.chart import (
        ChartType,
        deterministic_chart_filename,
        prepare_review_chart,
        render_review_chart,
    )

    # Keep the visual review set representative and bounded.  The complete
    # audit rows remain in the proof JSON/CSV; charts are capped per category
    # using a stable stock/date/setup ordering only.
    grouped: dict[str, list[tuple[Mapping[str, Any], str]]] = {
        "floor_reclaim": [],
        "floor_no_reclaim": [],
        "structural_up_transition": [],
        "upper_trend_failure": [],
    }
    for row in floor_rows:
        grouped[
            "floor_reclaim" if row["fast_reclaim_observed"] else "floor_no_reclaim"
        ].append(
            (
                row,
                "floor_reclaim" if row["fast_reclaim_observed"] else "floor_no_reclaim",
            )
        )
    for row in upper_rows:
        grouped[
            "structural_up_transition"
            if row["structural_up_transition"]
            else "upper_trend_failure"
        ].append(
            (
                row,
                "structural_up_transition"
                if row["structural_up_transition"]
                else "upper_trend_failure",
            )
        )
    caps = {
        "floor_reclaim": 5,
        "floor_no_reclaim": 5,
        "structural_up_transition": 6,
        "upper_trend_failure": 5,
    }
    selected: dict[tuple[str, str], tuple[Mapping[str, Any], str]] = {}
    for category, rows in grouped.items():
        ordered = sorted(
            rows,
            key=lambda item: (
                item[0]["stock_code"],
                item[0].get(
                    "floor_exit_fill_date", item[0].get("upper_exit_fill_date")
                ),
                item[0]["setup_id"],
            ),
        )
        for row, row_category in ordered[: caps[category]]:
            selected[(row["stock_code"], row["setup_id"])] = (row, row_category)
    artifacts = []
    trade_by_key = {(row["stock_code"], row["setup_id"]): row for row in all_trades}
    for key, (audit, category) in sorted(selected.items()):
        trade = trade_by_key[key]
        merged = {**trade, "audit": audit}
        bars = daily_by_stock[audit["stock_code"]]
        focus = trade["entry_daily_signal_date"]
        end = trade["exit_fill_date"]
        events = _review_events(merged, bars, category=category)
        prepared = prepare_review_chart(
            bars,
            chart_type=ChartType.EVENT_REVIEW,
            events=events,
            calendar=ExplicitTradingCalendar(bar.trade_date for bar in bars),
            focus_date=focus,
            event_end_date=end,
            pre_sessions=60,
            post_sessions=20,
            show_sma5=True,
            horizontal_levels={
                "BOX_FLOOR": trade["box_floor"],
                "BOX_UPPER": trade["box_upper"],
                "UPPER_SELL_LEVEL": trade["box_upper"] * Decimal("0.97"),
            },
        )
        filename = deterministic_chart_filename(
            audit["stock_code"],
            ChartType.EVENT_REVIEW,
            focus,
            slug=f"down-box-v0-3-{category}",
        )
        artifact = render_review_chart(
            prepared,
            chart_root / category / audit["stock_code"] / filename,
            strategy_policy=PROOF_VERSION,
            summary=merged,
        )
        artifacts.append(
            {
                "stock_code": audit["stock_code"],
                "setup_id": audit["setup_id"],
                "category": category,
                "png_path": artifact.png_path.as_posix(),
                "metadata_path": artifact.metadata_path.as_posix(),
            }
        )
    return artifacts


def run_long_history_daily_proof(
    *,
    output: Path = OUTPUT_PATH,
    candidate_csv: Path = CANDIDATE_CSV_PATH,
    chart_root: Path = CHART_ROOT,
    stocks: Sequence[str] = (
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
    ),
) -> dict[str, Any]:
    per_stock: dict[str, Any] = {}
    daily_by_stock: dict[str, tuple[DailyBar, ...]] = {}
    all_trades: list[dict[str, Any]] = []
    all_floor: list[dict[str, Any]] = []
    all_upper: list[dict[str, Any]] = []
    for stock_code in sorted(stocks):
        bars, provenance = _load_stock(stock_code)
        daily_by_stock[stock_code] = bars
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        result = run_down_box_daily_execution_proof(
            daily_bars=bars,
            calendar=calendar,
            research_start=RESEARCH_START,
            research_end=RESEARCH_END,
        )
        points = tuple(calculate_daily_indicators(bars, calendar))
        floor_rows = []
        upper_rows = []
        for trade in result["completed_trades"]:
            if trade["exit_action"] == DailyProofAction.FULL_EXIT_FLOOR_BREAK.value:
                row = audit_floor_reclaim(trade, bars, points)
                floor_rows.append(row)
                all_floor.append(row)
            elif trade["exit_action"] == DailyProofAction.FULL_TAKE_PROFIT_UPPER.value:
                # Preserve the raw fill open explicitly for the two runup definitions.
                enriched = {**trade, "_exit_fill_raw_open": trade["exit_raw_price"]}
                row = audit_upper_transition(enriched, bars, points)
                upper_rows.append(row)
                all_upper.append(row)
        result["artifact_provenance"] = provenance
        result["floor_reclaim_audit"] = floor_rows
        result["upper_transition_audit"] = upper_rows
        per_stock[stock_code] = result
        all_trades.extend(result["completed_trades"])
    charts = _generate_charts(
        all_trades, all_floor, all_upper, daily_by_stock, chart_root
    )
    structural = [row for row in all_upper if row["structural_up_transition"]]
    distributions = {
        "all_upper_exit_cases": {
            field: _distribution(all_upper, field)
            for field in (
                "sma60_flatness_5",
                "sma60_change_5",
                "d3_runup_from_exit_signal",
                "d3_runup_from_exit_fill",
                "max_daily_rise_d1_d3",
                "d3_distance_from_old_box_upper",
            )
        },
        "structural_up_transition": {
            field: _distribution(structural, field)
            for field in (
                "sma60_flatness_5",
                "sma60_change_5",
                "d3_runup_from_exit_signal",
                "d3_runup_from_exit_fill",
                "max_daily_rise_d1_d3",
                "d3_distance_from_old_box_upper",
            )
        },
    }
    counts = Counter()
    for result in per_stock.values():
        counts.update(result["counts"])
    independent_returns = [
        result["metrics"]["cumulative_return_pct"] for result in per_stock.values()
    ]
    result = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "universe": sorted(stocks),
        "research_period": {
            "start": RESEARCH_START,
            "end": RESEARCH_END,
            "daily_indicator_warmup": "available history before research_start",
        },
        "execution_contract": {
            "daily_signal": "ADJUSTED ka10081 Daily",
            "execution_and_accounting": "RAW ka10081 Daily open",
            "entry": "T+1 RAW Daily open; no same-day fill; no retry if absent",
            "half_exit": "T+1 RAW Daily open; floor(initial_quantity / 2); no retry",
            "full_exit": "T+1 RAW Daily open; persistent until next valid RAW Daily bar",
            "breakout_reentry": "report-only candidate; no V0.3 reentry order or fill",
            "cost_profile": "ZERO_COST",
            "corporate_action": "ambiguous lifecycles excluded; no share adjustment inference",
        },
        "funnel": {
            "counts": dict(sorted(counts.items())),
            "completed_trade_count": len(all_trades),
            "floor_exit_count": len(all_floor),
            "upper_exit_count": len(all_upper),
            "fast_reclaim_observed_count": sum(
                row["fast_reclaim_observed"] for row in all_floor
            ),
            "structural_up_transition_count": len(structural),
            "corporate_action_ambiguous_count": sum(
                len(result["corporate_action_exclusions"])
                for result in per_stock.values()
            ),
        },
        "baseline_metrics": {
            **_aggregate_metrics(per_stock, all_trades),
            "mean_trade_pnl_pct": (
                sum((row["pnl_pct"] for row in all_trades), Decimal(0))
                / len(all_trades)
                if all_trades
                else None
            ),
            "mean_independent_stock_return_pct": sum(independent_returns, Decimal(0))
            / len(independent_returns),
            "median_independent_stock_return_pct": median(independent_returns),
        },
        "floor_reclaim_audit": all_floor,
        "upper_transition_audit": all_upper,
        "candidate_review_table": [
            {
                "stock_code": row["stock_code"],
                "exit_date": row["upper_exit_fill_date"],
                "sma60_change_5": row["sma60_change_5"],
                "sma60_flatness_5": row["sma60_flatness_5"],
                "d3_runup_from_exit_signal": row["d3_runup_from_exit_signal"],
                "d3_runup_from_exit_fill": row["d3_runup_from_exit_fill"],
                "max_daily_rise": row["max_daily_rise_d1_d3"],
                "distance_from_old_upper": row["d3_distance_from_old_box_upper"],
                "future_5d": row["future_5_session_return"],
                "future_10d": row["future_10_session_return"],
                "future_20d": row["future_20_session_return"],
            }
            for row in all_upper
        ],
        "threshold_discovery": distributions,
        "representative_charts": charts,
        "per_stock": per_stock,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    _write_candidate_csv(candidate_csv, result["candidate_review_table"])
    return result


def _write_candidate_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "stock_code",
        "exit_date",
        "sma60_change_5",
        "sma60_flatness_5",
        "d3_runup_from_exit_signal",
        "d3_runup_from_exit_fill",
        "max_daily_rise",
        "distance_from_old_upper",
        "future_5d",
        "future_10d",
        "future_20d",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, Decimal, Enum)):
        return _json_default(value)
    return str(value)


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
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--candidate-csv", type=Path, default=CANDIDATE_CSV_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    args = parser.parse_args()
    result = run_long_history_daily_proof(
        output=args.output,
        candidate_csv=args.candidate_csv,
        chart_root=args.chart_root,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "network_calls": result["network_calls"],
                "trades": result["baseline_metrics"]["trade_count"],
                "floor_reclaim": result["funnel"]["fast_reclaim_observed_count"],
                "structural_up_transition": result["funnel"][
                    "structural_up_transition_count"
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
