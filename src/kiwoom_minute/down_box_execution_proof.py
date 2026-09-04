"""Offline 5-minute execution proof for ``DOWN_BOX_REVERSAL_V0_2``.

The module deliberately remains a bounded research proof.  Daily adjusted
prices create signals; aligned ka10080 RAW source-bar opens provide fills and
all cash accounting.  No source timestamp interval meaning is inferred.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, time
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
from src.backtest_engine.indicators import calculate_daily_indicators
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import (
    ExplicitTradingCalendar,
    TradingCalendar,
)
from src.backtest_engine.validation import validate_daily_bars

from .pipeline import (
    ASSUMPTION_ID,
    MinuteCollectionRequest,
    MinutePriceBasis,
    MinuteSourceBar,
    align_source_bars,
)
from .proof import _holding_sessions, _validate_source_sequence
from .small_up_path_proof import (
    INITIAL_CAPITAL,
    MINUTE_REQUIRED_START,
    RESEARCH_END,
    RESEARCH_START,
    STOCK_FULL_WEIGHT,
    _load_cached_minute_series,
    _load_existing_daily_bars,
)

PROOF_VERSION = "DOWN_BOX_REVERSAL_V0_2_5M_EXECUTION_PROOF"
OUTPUT_PATH = Path("data/processed/kiwoom/down_box_reversal_v0_2_execution_proof.json")
CHART_ROOT = Path("data/processed/strategy_charts/down_box_reversal_v0_2_execution")
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


class BoxProofPositionState(str, Enum):
    FLAT = "FLAT"
    FULL_POSITION = "FULL_POSITION"
    HALF_POSITION = "HALF_POSITION"
    BREAKOUT_REENTRY_WAIT = "BREAKOUT_REENTRY_WAIT"
    HANDOFF_TO_UP_FILLED = "HANDOFF_TO_UP_FILLED"


class BoxProofAction(str, Enum):
    INITIAL_ENTRY = "INITIAL_ENTRY"
    HALF_EXIT = "HALF_EXIT"
    FULL_EXIT_FLOOR_BREAK = "FULL_EXIT_FLOOR_BREAK"
    FULL_TAKE_PROFIT_UPPER = "FULL_TAKE_PROFIT_UPPER"
    BREAKOUT_REENTRY = "BREAKOUT_REENTRY"

    @property
    def persistent(self) -> bool:
        return self in {
            BoxProofAction.FULL_EXIT_FLOOR_BREAK,
            BoxProofAction.FULL_TAKE_PROFIT_UPPER,
        }


@dataclass(frozen=True, slots=True)
class BoxProofIntent:
    intent_id: str
    stock_code: str
    action: BoxProofAction
    signal_date: date
    activation_date: date
    setup: BoxSetup
    signal_id: str
    equity_at_decision: Decimal | None = None
    entry_type: str | None = None
    breakout_reentry_setup: BoxSetup | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class BoxProofFillOutcome:
    action: BoxProofAction
    setup: BoxSetup
    fill: Mapping[str, Any]
    next_setup: BoxSetup | None


def _require_stock_code(value: str) -> None:
    if len(value) != 6 or not value.isascii() or not value.isdigit():
        raise ValueError("stock_code must be exactly six ASCII digits")


def _make_intent(
    *,
    stock_code: str,
    action: BoxProofAction,
    signal_date: date,
    activation_date: date,
    setup: BoxSetup,
    signal_id: str,
    equity_at_decision: Decimal | None = None,
    entry_type: str | None = None,
    breakout_reentry_setup: BoxSetup | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> BoxProofIntent:
    return BoxProofIntent(
        stable_id(
            "down_box_proof_intent",
            stock_code,
            action.value,
            signal_date,
            activation_date,
            setup.setup_id,
            signal_id,
        ),
        stock_code,
        action,
        signal_date,
        activation_date,
        setup,
        signal_id,
        equity_at_decision,
        entry_type,
        breakout_reentry_setup,
        dict(metadata or {}),
    )


class ZeroCostBoxProofAccount:
    """Proof-local integer-share accounting with one pending action."""

    def __init__(
        self,
        *,
        stock_code: str,
        initial_capital: Decimal,
        stock_full_weight: Decimal,
    ) -> None:
        _require_stock_code(stock_code)
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
        self.state = BoxProofPositionState.FLAT
        self.pending: BoxProofIntent | None = None
        self.current_trade: dict[str, Any] | None = None
        self.fills: list[dict[str, Any]] = []
        self.transitions: list[dict[str, Any]] = []
        self.completed_trades: list[dict[str, Any]] = []
        self.expirations: list[dict[str, Any]] = []
        self.handoffs: list[dict[str, Any]] = []
        self.half_exit_consumed = False
        self.half_exit_done = False

    def schedule(self, intent: BoxProofIntent) -> None:
        if intent.stock_code != self.stock_code:
            raise ValueError("intent stock does not match account")
        if self.pending is not None:
            raise ValueError("only one pending proof action is allowed")
        if intent.action in {
            BoxProofAction.INITIAL_ENTRY,
            BoxProofAction.BREAKOUT_REENTRY,
        }:
            expected = (
                BoxProofPositionState.FLAT
                if intent.action is BoxProofAction.INITIAL_ENTRY
                else BoxProofPositionState.BREAKOUT_REENTRY_WAIT
            )
            if self.state is not expected or self.quantity != 0:
                raise ValueError("entry intent requires its matching flat state")
            if (
                not isinstance(intent.equity_at_decision, Decimal)
                or not intent.equity_at_decision.is_finite()
                or intent.equity_at_decision <= 0
            ):
                raise ValueError("entry intent requires decision-time Decimal equity")
        else:
            if (
                self.state
                not in {
                    BoxProofPositionState.FULL_POSITION,
                    BoxProofPositionState.HALF_POSITION,
                }
                or self.quantity <= 0
            ):
                raise ValueError("exit intent requires an open BOX position")
        if intent.action is BoxProofAction.HALF_EXIT:
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

    def expire_if_due_without_row(self, day: date) -> None:
        intent = self.pending
        if intent is None or intent.action.persistent or day != intent.activation_date:
            return
        self.expirations.append(
            {
                "intent_id": intent.intent_id,
                "action": intent.action.value,
                "signal_date": intent.signal_date,
                "activation_date": intent.activation_date,
                "reason": "NO_TRADABLE_INCLUDED_MINUTE_ROW_ON_T_PLUS_1",
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

    def fill_session_open(
        self,
        day: date,
        bar: MinuteSourceBar,
        *,
        known_days: Sequence[date],
    ) -> BoxProofFillOutcome | None:
        intent = self.pending
        if intent is None or day < intent.activation_date:
            return None
        if not intent.action.persistent and day != intent.activation_date:
            raise ValueError("non-persistent action cannot fill after T+1")
        if bar.trading_date != day:
            raise ValueError("fill bar does not belong to the execution session")
        if bar.raw.open <= 0:
            raise ValueError("fill requires a positive RAW open")
        price = bar.raw.open
        quantity_before = self.quantity
        cash_before = self.cash
        next_setup: BoxSetup | None = None
        if intent.action in {
            BoxProofAction.INITIAL_ENTRY,
            BoxProofAction.BREAKOUT_REENTRY,
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
            if intent.action is BoxProofAction.INITIAL_ENTRY:
                self.state = BoxProofPositionState.FULL_POSITION
                self.half_exit_consumed = False
                self.half_exit_done = False
                self.current_trade = {
                    "stock_code": self.stock_code,
                    "setup_id": intent.setup.setup_id,
                    "entry_type": intent.entry_type,
                    "entry_daily_signal_date": intent.signal_date,
                    "entry_fill_date": day,
                    "entry_fill_source_label": bar.source_label,
                    "entry_raw_price": price,
                    "initial_quantity": fill_quantity,
                    "initial_invested_capital": price * Decimal(fill_quantity),
                    "realized_pnl": Decimal(0),
                    "half_exit": None,
                    **dict(intent.metadata or {}),
                }
                next_setup = replace(intent.setup, state=BoxSetupState.BOX_POSITION)
            else:
                self.state = BoxProofPositionState.HANDOFF_TO_UP_FILLED
                handoff = {
                    "stock_code": self.stock_code,
                    "setup_id": intent.setup.setup_id,
                    "parent_setup_id": intent.setup.parent_setup_id,
                    "signal_date": intent.signal_date,
                    "fill_date": day,
                    "fill_source_label": bar.source_label,
                    "raw_price": price,
                    "quantity": fill_quantity,
                    "status": "HANDOFF_TO_UP_FILLED",
                    "post_handoff_pnl_calculated": False,
                }
                self.handoffs.append(handoff)
        elif intent.action is BoxProofAction.HALF_EXIT:
            if self.current_trade is None:
                raise ValueError("half exit requires an active trade")
            fill_quantity = self.current_trade["initial_quantity"] // 2
            if fill_quantity <= 0 or fill_quantity >= self.quantity:
                raise ValueError("position cannot support a deterministic half exit")
            self.cash += price * Decimal(fill_quantity)
            self.quantity -= fill_quantity
            self.state = BoxProofPositionState.HALF_POSITION
            self.half_exit_done = True
            realized = (price - self.current_trade["entry_raw_price"]) * Decimal(
                fill_quantity
            )
            self.current_trade["realized_pnl"] += realized
            self.current_trade["half_exit"] = {
                "signal_date": intent.signal_date,
                "fill_date": day,
                "fill_source_label": bar.source_label,
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
            final_leg_pnl = (price - self.current_trade["entry_raw_price"]) * Decimal(
                fill_quantity
            )
            total_pnl = self.current_trade["realized_pnl"] + final_leg_pnl
            trade = {
                **self.current_trade,
                "exit_action": intent.action.value,
                "exit_daily_signal_date": intent.signal_date,
                "exit_fill_date": day,
                "exit_fill_source_label": bar.source_label,
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
            self.completed_trades.append(trade)
            self.current_trade = None
            self.quantity = 0
            if intent.breakout_reentry_setup is not None:
                self.state = BoxProofPositionState.BREAKOUT_REENTRY_WAIT
                next_setup = intent.breakout_reentry_setup
            else:
                self.state = BoxProofPositionState.FLAT
        fill = {
            "fill_id": stable_id(
                "down_box_proof_fill",
                intent.intent_id,
                bar.source_bar_id,
                price,
                fill_quantity,
            ),
            "intent_id": intent.intent_id,
            "action": intent.action.value,
            "signal_date": intent.signal_date,
            "filled_at_source_label": bar.source_label,
            "fill_date": day,
            "source_bar_id": bar.source_bar_id,
            "source_bar_sequence": bar.source_bar_sequence,
            "raw_price": price,
            "quantity": fill_quantity,
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
                "source_label": bar.source_label,
                "quantity_before": quantity_before,
                "quantity_after": self.quantity,
                "cash_before": cash_before,
                "cash_after": self.cash,
            }
        )
        self.pending = None
        return BoxProofFillOutcome(intent.action, intent.setup, fill, next_setup)


def _next_session(calendar: TradingCalendar, day: date) -> date:
    return calendar.next_trading_day(day)


def _entry_type(signal: BoxSignal) -> str:
    return {
        BoxSignalType.ENTRY_CANDIDATE_MA5_TURN: "MA5_TURN",
        BoxSignalType.ENTRY_CANDIDATE_SMA10_REBREAK: "SMA10_REBREAK",
        BoxSignalType.ENTRY_CANDIDATE_MA5_TURN_AND_SMA10_REBREAK: "BOTH",
    }[signal.signal_type]


def _entry_location(value: Decimal) -> str:
    if value < Decimal(1) / Decimal(3):
        return "LOWER"
    if value < Decimal(2) / Decimal(3):
        return "MIDDLE"
    return "UPPER"


def _entry_metadata(
    setup: BoxSetup,
    bars: Sequence[DailyBar],
    index: int,
) -> dict[str, Any]:
    bar = bars[index]
    width = setup.box_upper - setup.box_floor
    close_position = (bar.signal.close - setup.box_floor) / width
    low_position = (bar.signal.low - setup.box_floor) / width
    by_date = {item.trade_date: offset for offset, item in enumerate(bars)}
    origin_index = by_date[setup.setup_origin_date]
    lower_upper = setup.box_floor * Decimal("1.03")
    touch_dates = [
        item.trade_date
        for item in bars[max(0, origin_index - 10) : index + 1]
        if setup.box_floor <= item.signal.low <= lower_upper
        or setup.box_floor <= item.signal.close <= lower_upper
    ]
    last_touch = touch_dates[-1] if touch_dates else None
    return {
        "box_floor": setup.box_floor,
        "box_upper": setup.box_upper,
        "box_position_close": close_position,
        "box_position_low": low_position,
        "entry_location": _entry_location(close_position),
        "lower_zone_touch_dates": touch_dates,
        "last_lower_zone_touch_date": last_touch,
        "sessions_since_lower_zone_touch": (
            index - by_date[last_touch] if last_touch is not None else None
        ),
    }


def _schedule_for_signal(
    account: ZeroCostBoxProofAccount,
    *,
    action: BoxProofAction,
    signal: BoxSignal,
    setup: BoxSetup,
    calendar: TradingCalendar,
    equity_at_decision: Decimal | None = None,
    entry_type: str | None = None,
    breakout_reentry_setup: BoxSetup | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    account.schedule(
        _make_intent(
            stock_code=signal.stock_code,
            action=action,
            signal_date=signal.signal_date,
            activation_date=_next_session(calendar, signal.signal_date),
            setup=setup,
            signal_id=signal.signal_id,
            equity_at_decision=equity_at_decision,
            entry_type=entry_type,
            breakout_reentry_setup=breakout_reentry_setup,
            metadata=metadata,
        )
    )


def _position_action(signals: Sequence[BoxSignal]) -> BoxSignal | None:
    """Return the V0.2 full-before-half priority without changing signals."""

    priority = {
        BoxSignalType.FULL_EXIT_FLOOR_BREAK: 0,
        BoxSignalType.FULL_TAKE_PROFIT_UPPER: 1,
        BoxSignalType.HALF_EXIT_SIGNAL: 2,
    }
    candidates = [signal for signal in signals if signal.signal_type in priority]
    return min(candidates, key=lambda item: priority[item.signal_type], default=None)


def run_down_box_execution_proof(
    *,
    daily_bars: Sequence[DailyBar],
    source_bars: Sequence[MinuteSourceBar],
    calendar: TradingCalendar,
    research_start: date,
    research_end: date,
    stock_full_weight: Decimal,
    initial_capital: Decimal,
) -> dict[str, Any]:
    """Replay one stock with Daily V0.2 signals and RAW first-row fills."""

    if research_start > research_end:
        raise ValueError("research_start must not follow research_end")
    canonical_daily = tuple(sorted(daily_bars, key=lambda bar: bar.trade_date))
    canonical_source = tuple(
        sorted(source_bars, key=lambda bar: (bar.source_label, bar.source_bar_id))
    )
    validate_daily_bars(canonical_daily, calendar)
    _validate_source_sequence(canonical_source)
    stock_codes = {bar.stock_code for bar in canonical_daily} | {
        bar.stock_code for bar in canonical_source
    }
    if len(stock_codes) != 1:
        raise ValueError("proof supports exactly one stock")
    stock_code = next(iter(stock_codes))
    points = tuple(calculate_daily_indicators(canonical_daily, calendar))
    config = DownBoxStrategyConfig()
    account = ZeroCostBoxProofAccount(
        stock_code=stock_code,
        initial_capital=initial_capital,
        stock_full_weight=stock_full_weight,
    )
    by_date: dict[date, list[MinuteSourceBar]] = defaultdict(list)
    for bar in canonical_source:
        if research_start <= bar.trading_date <= research_end:
            by_date[bar.trading_date].append(bar)
    for rows in by_date.values():
        rows.sort(key=lambda bar: (bar.source_label, bar.source_bar_id))
    daily_index = {bar.trade_date: index for index, bar in enumerate(canonical_daily)}
    known_days = tuple(bar.trade_date for bar in canonical_daily)
    research_days = tuple(
        day for day in known_days if research_start <= day <= research_end
    )
    active: BoxSetup | None = None
    events: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    counts = Counter()
    stopped_at_handoff = False

    for day in research_days:
        day_rows = by_date.get(day, [])
        if day_rows:
            outcome = account.fill_session_open(
                day,
                day_rows[0],
                known_days=known_days,
            )
            if outcome is not None:
                counts[f"{outcome.action.value}_FILLS"] += 1
                if outcome.action is BoxProofAction.INITIAL_ENTRY or outcome.action in {
                    BoxProofAction.FULL_EXIT_FLOOR_BREAK,
                    BoxProofAction.FULL_TAKE_PROFIT_UPPER,
                }:
                    active = outcome.next_setup
                elif outcome.action is BoxProofAction.BREAKOUT_REENTRY:
                    active = None
                    stopped_at_handoff = True
        else:
            pending_action = account.pending.action if account.pending else None
            account.expire_if_due_without_row(day)
            if pending_action is not None and account.pending is None:
                counts[f"{pending_action.value}_EXPIRATIONS"] += 1
                if pending_action in {
                    BoxProofAction.INITIAL_ENTRY,
                    BoxProofAction.BREAKOUT_REENTRY,
                }:
                    active = None
        for source_bar in day_rows:
            equity_curve.append(
                {
                    "source_label": source_bar.source_label,
                    "equity": account.cash
                    + Decimal(account.quantity) * source_bar.raw.close,
                    "holding": account.quantity > 0,
                }
            )
        if stopped_at_handoff:
            break
        if account.pending is not None:
            continue
        index = daily_index[day]
        daily_bar = canonical_daily[index]
        if active is None and account.state is BoxProofPositionState.FLAT:
            origin = analyze_box_origin(canonical_daily, points, index, config)
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
            decision = evaluate_reversal_wait(
                active, canonical_daily, points, index, config
            )
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
                account.state = BoxProofPositionState.BREAKOUT_REENTRY_WAIT
                continue
            entry = next(
                (
                    signal
                    for signal in decision.signals
                    if signal.signal_type
                    in {
                        BoxSignalType.ENTRY_CANDIDATE_MA5_TURN,
                        BoxSignalType.ENTRY_CANDIDATE_SMA10_REBREAK,
                        BoxSignalType.ENTRY_CANDIDATE_MA5_TURN_AND_SMA10_REBREAK,
                    }
                ),
                None,
            )
            if entry is not None:
                counts["ENTRY_SIGNALS"] += 1
                metadata = _entry_metadata(active, canonical_daily, index)
                _schedule_for_signal(
                    account,
                    action=BoxProofAction.INITIAL_ENTRY,
                    signal=entry,
                    setup=active,
                    calendar=calendar,
                    equity_at_decision=account.cash
                    + Decimal(account.quantity) * daily_bar.raw.close,
                    entry_type=_entry_type(entry),
                    metadata=metadata,
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
            decision = evaluate_box_position(
                active, canonical_daily, points, index, config
            )
            events.extend(_event_rows(decision.events))
            signal = _position_action(decision.signals)
            if signal is None:
                active = decision.setup_after
                continue
            breakout_setup = (
                decision.setup_after
                if decision.setup_after.state is BoxSetupState.BREAKOUT_REENTRY_WAIT
                else None
            )
            if signal.signal_type is BoxSignalType.FULL_EXIT_FLOOR_BREAK:
                action = BoxProofAction.FULL_EXIT_FLOOR_BREAK
                counts["FLOOR_EXIT_SIGNALS"] += 1
            elif signal.signal_type is BoxSignalType.FULL_TAKE_PROFIT_UPPER:
                action = BoxProofAction.FULL_TAKE_PROFIT_UPPER
                counts["UPPER_EXIT_SIGNALS"] += 1
                if breakout_setup is not None:
                    counts["HOLDING_BREAKOUT_COLLISIONS"] += 1
                    counts["BOX_BREAKOUT_CONFIRMED"] += 1
            else:
                action = BoxProofAction.HALF_EXIT
                counts["HALF_EXIT_SIGNALS"] += 1
            _schedule_for_signal(
                account,
                action=action,
                signal=signal,
                setup=active,
                calendar=calendar,
                breakout_reentry_setup=breakout_setup,
            )
            active = decision.setup_after
            continue
        if active.state is BoxSetupState.BREAKOUT_REENTRY_WAIT:
            decision = evaluate_breakout_reentry(
                active, canonical_daily, points, index, config
            )
            events.extend(_event_rows(decision.events))
            reentry = next(iter(decision.signals), None)
            if reentry is not None:
                counts["BREAKOUT_REENTRY_SIGNALS"] += 1
                _schedule_for_signal(
                    account,
                    action=BoxProofAction.BREAKOUT_REENTRY,
                    signal=reentry,
                    setup=active,
                    calendar=calendar,
                    equity_at_decision=account.cash
                    + Decimal(account.quantity) * daily_bar.raw.close,
                )
                active = decision.setup_after
            elif decision.setup_after.state is BoxSetupState.INVALIDATED:
                counts["BREAKOUT_FAILED"] += 1
                active = None
                account.state = BoxProofPositionState.FLAT
            else:
                active = decision.setup_after

    if account.pending is not None and not account.pending.action.persistent:
        pending = account.pending
        account.expire_if_due_without_row(pending.activation_date)
        counts[f"{pending.action.value}_EXPIRATIONS"] += 1
        if pending.action in {
            BoxProofAction.INITIAL_ENTRY,
            BoxProofAction.BREAKOUT_REENTRY,
        }:
            active = None

    metrics = _proof_metrics(
        initial_capital,
        account,
        canonical_source,
        equity_curve,
        known_days,
        research_start,
        research_end,
    )
    return {
        "proof_version": PROOF_VERSION,
        "assumption_id": ASSUMPTION_ID,
        "stock_code": stock_code,
        "research_start": research_start,
        "research_end": research_end,
        "initial_capital": initial_capital,
        "stock_full_weight": stock_full_weight,
        "accounting": {
            "cost_profile": "ZERO_COST",
            "entry_size": "1.0 STOCK FULL",
            "integer_half_exit_rounding": "floor(initial_quantity / 2)",
            "signal_price_basis": "DAILY_ADJUSTED",
            "fill_price_basis": "KA10080_RAW_OPEN",
        },
        "counts": dict(sorted(counts.items())),
        "completed_trades": account.completed_trades,
        "fills": account.fills,
        "expirations": account.expirations,
        "handoffs": account.handoffs,
        "transitions": account.transitions,
        "daily_events": events,
        "state_at_end": account.state.value,
        "open_quantity": account.quantity,
        "pending_action_at_end": (
            account.pending.action.value if account.pending else None
        ),
        "active_setup_id_at_end": active.setup_id if active else None,
        "metrics": metrics,
    }


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


def _proof_metrics(
    initial: Decimal,
    account: ZeroCostBoxProofAccount,
    source_bars: Sequence[MinuteSourceBar],
    equity_curve: Sequence[Mapping[str, Any]],
    known_days: Sequence[date],
    research_start: date,
    research_end: date,
) -> dict[str, Any]:
    if account.state is BoxProofPositionState.HANDOFF_TO_UP_FILLED:
        final_equity = None
        cumulative = None
    else:
        in_range = [
            bar
            for bar in source_bars
            if research_start <= bar.trading_date <= research_end
        ]
        last_price = in_range[-1].raw.close if in_range else Decimal(0)
        final_equity = account.cash + Decimal(account.quantity) * last_price
        cumulative = (final_equity / initial - Decimal(1)) * Decimal(100)
    peak: Decimal | None = None
    mdd = Decimal(0)
    for row in equity_curve:
        equity = row["equity"]
        peak = equity if peak is None else max(peak, equity)
        mdd = min(mdd, (equity / peak - Decimal(1)) * Decimal(100))
    trades = account.completed_trades
    wins = [trade for trade in trades if trade["pnl_amount"] > 0]
    losses = [trade for trade in trades if trade["pnl_amount"] < 0]
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
    return {
        "final_equity": final_equity,
        "cumulative_return_pct": cumulative,
        "mdd_pct": mdd,
        "win_rate_pct": (
            Decimal(len(wins)) / Decimal(len(trades)) * Decimal(100) if trades else None
        ),
        "average_win_pct": average_win,
        "average_loss_pct": average_loss,
        "payoff_ratio": (
            average_win / -average_loss
            if average_win is not None and average_loss not in {None, Decimal(0)}
            else None
        ),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_holding_sessions": (
            Decimal(sum(row["holding_sessions"] for row in trades)) / len(trades)
            if trades
            else None
        ),
        "exposure_source_row_fraction": (
            Decimal(sum(bool(row["holding"]) for row in equity_curve))
            / Decimal(len(equity_curve))
            if equity_curve
            else Decimal(0)
        ),
        "turnover_multiple": sum(
            (row["raw_price"] * Decimal(row["quantity"]) for row in account.fills),
            Decimal(0),
        )
        / initial,
        "completed_trade_return_pct": sum(
            (row["pnl_amount"] for row in trades), Decimal(0)
        )
        / initial
        * Decimal(100),
        "research_session_count": sum(
            research_start <= day <= research_end for day in known_days
        ),
    }


def _summary_for_group(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    wins = [row for row in trades if row["pnl_amount"] > 0]
    losses = [row for row in trades if row["pnl_amount"] < 0]
    gross_profit = sum((row["pnl_amount"] for row in wins), Decimal(0))
    gross_loss = -sum((row["pnl_amount"] for row in losses), Decimal(0))
    pnl = [row["pnl_pct"] for row in trades]
    return {
        "count": len(trades),
        "win_rate_pct": (
            Decimal(len(wins)) / Decimal(len(trades)) * Decimal(100) if trades else None
        ),
        "mean_pnl_pct": sum(pnl, Decimal(0)) / len(pnl) if pnl else None,
        "median_pnl_pct": median(pnl) if pnl else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
    }


def _coverage(source_bars: Sequence[MinuteSourceBar]) -> dict[str, Any]:
    rows_by_date: dict[date, list[MinuteSourceBar]] = defaultdict(list)
    for bar in source_bars:
        rows_by_date[bar.trading_date].append(bar)
    by_date = Counter({day: len(rows) for day, rows in rows_by_date.items()})
    first_times = Counter(rows[0].source_label[-6:] for rows in rows_by_date.values())
    last_times = Counter(rows[-1].source_label[-6:] for rows in rows_by_date.values())
    return {
        "rows": len(source_bars),
        "sessions": len(by_date),
        "first_source_label": source_bars[0].source_label,
        "last_source_label": source_bars[-1].source_label,
        "rows_per_session_distribution": dict(
            sorted(Counter(by_date.values()).items())
        ),
        "first_label_time_distribution": dict(sorted(first_times.items())),
        "last_label_time_distribution": dict(sorted(last_times.items())),
        "continuous_source_sequence": [bar.source_bar_sequence for bar in source_bars]
        == list(range(len(source_bars))),
    }


def load_offline_inputs(
    stock_code: str,
) -> tuple[tuple[DailyBar, ...], tuple[MinuteSourceBar, ...], dict[str, Any]]:
    daily = _load_existing_daily_bars(stock_code)
    raw = _load_cached_minute_series(
        MinuteCollectionRequest(
            stock_code, MINUTE_REQUIRED_START, RESEARCH_END, MinutePriceBasis.RAW
        )
    )
    adjusted = _load_cached_minute_series(
        MinuteCollectionRequest(
            stock_code,
            MINUTE_REQUIRED_START,
            RESEARCH_END,
            MinutePriceBasis.ADJUSTED,
        )
    )
    if raw is None or adjusted is None:
        raise FileNotFoundError(f"missing cached minute artifacts for {stock_code}")
    source_bars, excluded = align_source_bars(
        raw, adjusted, latest_label_time=time(15, 30)
    )
    provenance = {
        "raw_artifact_set_sha256": raw.artifact_set_sha256,
        "adjusted_artifact_set_sha256": adjusted.artifact_set_sha256,
        "raw_rows": len(raw.rows),
        "adjusted_rows": len(adjusted.rows),
        "aligned_included_rows": len(source_bars),
        "excluded_rows": excluded,
    }
    return daily, source_bars, provenance


def _generate_charts(
    results: Mapping[str, Mapping[str, Any]],
    daily_by_stock: Mapping[str, Sequence[DailyBar]],
    chart_root: Path,
) -> list[dict[str, Any]]:
    from src.strategy_review.chart import (
        ChartType,
        ReviewEvent,
        ReviewEventType,
        deterministic_chart_filename,
        prepare_review_chart,
        render_review_chart,
    )

    trades = [
        trade
        for stock_code in sorted(results)
        for trade in results[stock_code]["completed_trades"]
    ]
    best = sorted(trades, key=lambda row: (-row["pnl_pct"], row["stock_code"]))[:5]
    worst = sorted(trades, key=lambda row: (row["pnl_pct"], row["stock_code"]))[:5]
    half = sorted(
        (row for row in trades if row["half_exit"] is not None),
        key=lambda row: (row["stock_code"], row["entry_fill_date"]),
    )[:5]
    selected: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in best + worst + half:
        selected[(row["stock_code"], str(row["setup_id"]))] = row
    for field, values in (
        ("entry_type", ("MA5_TURN", "SMA10_REBREAK", "BOTH")),
        ("entry_location", ("LOWER", "MIDDLE")),
    ):
        for value in values:
            row = next(
                (
                    item
                    for item in sorted(
                        trades,
                        key=lambda item: (item["stock_code"], item["entry_fill_date"]),
                    )
                    if item.get(field) == value
                ),
                None,
            )
            if row is not None:
                selected[(row["stock_code"], str(row["setup_id"]))] = row
    artifacts = []
    for row in sorted(
        selected.values(),
        key=lambda item: (item["stock_code"], item["entry_fill_date"]),
    ):
        stock_code = row["stock_code"]
        events = [
            ReviewEvent(
                ReviewEventType.BOX_BUY_CANDIDATE,
                row["entry_daily_signal_date"],
                "BUY",
            ),
            ReviewEvent(
                ReviewEventType.ENTRY_FILL,
                row["entry_fill_date"],
                "ENTRY",
                raw_fill_price=row["entry_raw_price"],
                source_label=row["entry_fill_source_label"],
            ),
        ]
        if row["half_exit"] is not None:
            half_row = row["half_exit"]
            events.append(
                ReviewEvent(
                    ReviewEventType.EXIT_FILL,
                    half_row["fill_date"],
                    "HALF EXIT",
                    raw_fill_price=half_row["raw_price"],
                    source_label=half_row["fill_source_label"],
                )
            )
        events.append(
            ReviewEvent(
                ReviewEventType.EXIT_FILL,
                row["exit_fill_date"],
                "EXIT",
                raw_fill_price=row["exit_raw_price"],
                source_label=row["exit_fill_source_label"],
            )
        )
        daily = daily_by_stock[stock_code]
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in daily)
        prepared = prepare_review_chart(
            daily,
            chart_type=ChartType.EVENT_REVIEW,
            events=events,
            calendar=calendar,
            focus_date=row["entry_daily_signal_date"],
            event_end_date=row["exit_fill_date"],
            pre_sessions=60,
            post_sessions=20,
            show_sma5=True,
            horizontal_levels={
                "BOX_FLOOR": row["box_floor"],
                "BOX_UPPER": row["box_upper"],
                "UPPER_SELL_LEVEL": row["box_upper"] * Decimal("0.97"),
            },
        )
        filename = deterministic_chart_filename(
            stock_code,
            ChartType.EVENT_REVIEW,
            row["entry_daily_signal_date"],
            slug="down-box-execution",
        )
        artifact = render_review_chart(
            prepared,
            chart_root / stock_code / filename,
            strategy_policy=PROOF_VERSION,
            summary=dict(row),
        )
        artifacts.append(
            {
                "stock_code": stock_code,
                "setup_id": row["setup_id"],
                "pnl_pct": row["pnl_pct"],
                "png_path": artifact.png_path.as_posix(),
                "metadata_path": artifact.metadata_path.as_posix(),
            }
        )
    return artifacts


def run_ten_stock_offline_proof(
    *,
    output: Path = OUTPUT_PATH,
    chart_root: Path = CHART_ROOT,
    stocks: Sequence[str] = UNIVERSE,
) -> dict[str, Any]:
    per_stock: dict[str, Any] = {}
    daily_by_stock: dict[str, tuple[DailyBar, ...]] = {}
    all_trades: list[dict[str, Any]] = []
    for stock_code in sorted(stocks):
        daily, source, provenance = load_offline_inputs(stock_code)
        daily_by_stock[stock_code] = daily
        result = run_down_box_execution_proof(
            daily_bars=daily,
            source_bars=source,
            calendar=ExplicitTradingCalendar(bar.trade_date for bar in daily),
            research_start=RESEARCH_START,
            research_end=RESEARCH_END,
            stock_full_weight=STOCK_FULL_WEIGHT,
            initial_capital=INITIAL_CAPITAL,
        )
        result["minute_coverage"] = _coverage(source)
        result["artifact_provenance"] = provenance
        per_stock[stock_code] = result
        all_trades.extend(result["completed_trades"])
    entry_types = {
        key: _summary_for_group([row for row in all_trades if row["entry_type"] == key])
        for key in ("MA5_TURN", "SMA10_REBREAK", "BOTH")
    }
    locations = {
        key: _summary_for_group(
            [row for row in all_trades if row["entry_location"] == key]
        )
        for key in ("LOWER", "MIDDLE", "UPPER")
    }
    recency = [
        {
            "stock_code": row["stock_code"],
            "setup_id": row["setup_id"],
            "sessions_since_lower_zone_touch": row["sessions_since_lower_zone_touch"],
            "pnl_pct": row["pnl_pct"],
        }
        for row in all_trades
    ]
    half_exit_rows = [
        {
            "stock_code": row["stock_code"],
            "setup_id": row["setup_id"],
            "entry_type": row["entry_type"],
            "half_exit": row["half_exit"],
            "final_exit_raw_price": row["exit_raw_price"],
            "total_trade_pnl": row["pnl_amount"],
            "total_trade_return_pct": row["pnl_pct"],
        }
        for row in all_trades
        if row["half_exit"] is not None
    ]
    large = [
        {
            "stock_code": row["stock_code"],
            "setup_id": row["setup_id"],
            "pnl_pct": row["pnl_pct"],
            "entry_type": row["entry_type"],
            "entry_location": row["entry_location"],
            "half_exit": row["half_exit"] is not None,
            "exit_reason": row["exit_action"],
        }
        for row in all_trades
        if row["pnl_pct"] >= Decimal(20) or row["pnl_pct"] <= Decimal(-8)
    ]
    charts = _generate_charts(per_stock, daily_by_stock, chart_root)
    aggregate_counts = Counter()
    for stock in per_stock.values():
        aggregate_counts.update(stock["counts"])
    stock_returns = [
        stock["metrics"]["cumulative_return_pct"]
        for stock in per_stock.values()
        if stock["metrics"]["cumulative_return_pct"] is not None
    ]
    stock_mdds = [stock["metrics"]["mdd_pct"] for stock in per_stock.values()]
    result = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "universe": sorted(stocks),
        "research_period": {
            "minute_warmup_start": MINUTE_REQUIRED_START,
            "research_start": RESEARCH_START,
            "research_end": RESEARCH_END,
        },
        "execution_contract": {
            "daily_signal_basis": "ADJUSTED",
            "fill_and_accounting_basis": "RAW",
            "entry": "T+1 earliest included ka10080 source row RAW open",
            "included_labels": "09:00..15:30 inclusive; 15:35+ excluded",
            "entry_and_half_expiry": "T+1 only; no carry",
            "full_exit": "persistent until next tradable included source row",
            "cost_profile": "ZERO_COST",
            "timestamp_semantics": ASSUMPTION_ID,
        },
        "up_handoff": {
            "implemented_through": "HANDOFF_TO_UP_FILLED",
            "continuous_up_management": False,
            "reason": "existing UP runner has no externally-filled position injection contract",
        },
        "aggregate_counts": dict(sorted(aggregate_counts.items())),
        "aggregate_trade_metrics": _summary_for_group(all_trades),
        "aggregate_independent_stock_metrics": {
            "stock_count": len(per_stock),
            "mean_stock_return_pct": sum(stock_returns, Decimal(0))
            / len(stock_returns),
            "median_stock_return_pct": median(stock_returns),
            "mean_stock_mdd_pct": sum(stock_mdds, Decimal(0)) / len(stock_mdds),
            "median_stock_mdd_pct": median(stock_mdds),
            "completed_trade_pnl_amount": sum(
                (row["pnl_amount"] for row in all_trades), Decimal(0)
            ),
        },
        "exit_reason_distribution": dict(
            sorted(Counter(row["exit_action"] for row in all_trades).items())
        ),
        "entry_type_analysis": entry_types,
        "entry_location_analysis": locations,
        "entry_location_continuous": [
            {
                "stock_code": row["stock_code"],
                "setup_id": row["setup_id"],
                "box_position_close": row["box_position_close"],
                "box_position_low": row["box_position_low"],
                "pnl_pct": row["pnl_pct"],
            }
            for row in all_trades
        ],
        "lower_zone_recency_analysis": recency,
        "half_exit_analysis": half_exit_rows,
        "large_winners_losses": large,
        "breakout_reentry_actual_fills": [
            handoff for stock in per_stock.values() for handoff in stock["handoffs"]
        ],
        "representative_charts": charts,
        "per_stock": per_stock,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return result


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal, Enum)):
        if isinstance(value, date):
            return value.isoformat()
        return str(value.value if isinstance(value, Enum) else value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    args = parser.parse_args()
    result = run_ten_stock_offline_proof(
        output=args.output,
        chart_root=args.chart_root,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "network_calls": result["network_calls"],
                "completed_trades": result["aggregate_trade_metrics"]["count"],
                "charts": len(result["representative_charts"]),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
