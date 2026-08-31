"""Append-only transition ledgers for signals, orders, and fills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import ClassVar

from .events import EventKey, require_aware
from .execution import (
    IntentType,
    OrderSide,
    ScheduleResult,
    ScheduleStatus,
    StrategyIntent,
)


class LedgerValidationError(ValueError):
    """A ledger reference or transition violates the audit contract."""


class SignalStatus(str, Enum):
    CREATED = "CREATED"
    ORDER_SCHEDULED = "ORDER_SCHEDULED"
    NO_NEXT_BAR = "NO_NEXT_BAR"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class OrderStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    NO_NEXT_BAR = "NO_NEXT_BAR"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class SignalLedgerEntry:
    ledger_sequence: int
    event_key: EventKey
    previous_status: SignalStatus | None
    status: SignalStatus
    signal_id: str
    stock_code: str
    signal_type: IntentType
    side: OrderSide
    signal_generated_at: datetime
    signal_available_at: datetime
    execution_signal_at: datetime
    executed_at: datetime | None
    reason: str
    strategy_state: str
    rejection_reason: str | None
    signal_event_key: EventKey
    signal_source_bar_id: str
    signal_source_bar_sequence: int


@dataclass(frozen=True, slots=True)
class OrderLedgerEntry:
    ledger_sequence: int
    event_key: EventKey
    previous_status: OrderStatus | None
    status: OrderStatus
    order_id: str
    signal_id: str
    stock_code: str
    created_at: datetime
    created_event_key: EventKey
    eligible_at: datetime | None
    eligible_bar_id: str | None
    eligible_bar_sequence: int | None
    side: OrderSide
    requested_quantity: int
    reason: str


@dataclass(frozen=True, slots=True)
class FillLedgerEntry:
    ledger_sequence: int
    event_key: EventKey
    fill_id: str
    order_id: str
    filled_at: datetime
    fill_event_key: EventKey
    fill_bar_id: str
    fill_bar_sequence: int
    raw_price: Decimal
    quantity: int


def _require_chronological(entries: list[object], event_key: EventKey) -> None:
    if entries and event_key < entries[-1].event_key:  # type: ignore[attr-defined]
        raise LedgerValidationError(
            "ledger transitions must be appended in event order"
        )


class SignalLedger:
    """Append-only signal transition log."""

    _ALLOWED: ClassVar[dict[SignalStatus | None, set[SignalStatus]]] = {
        None: {SignalStatus.CREATED},
        SignalStatus.CREATED: {
            SignalStatus.ORDER_SCHEDULED,
            SignalStatus.NO_NEXT_BAR,
            SignalStatus.REJECTED,
        },
        SignalStatus.ORDER_SCHEDULED: {
            SignalStatus.FILLED,
            SignalStatus.REJECTED,
        },
    }

    def __init__(self) -> None:
        self._entries: list[SignalLedgerEntry] = []

    @property
    def entries(self) -> tuple[SignalLedgerEntry, ...]:
        return tuple(self._entries)

    def latest(self, signal_id: str) -> SignalLedgerEntry | None:
        return next(
            (row for row in reversed(self._entries) if row.signal_id == signal_id),
            None,
        )

    def append_transition(
        self,
        intent: StrategyIntent,
        *,
        status: SignalStatus,
        event_key: EventKey,
        executed_at: datetime | None = None,
        rejection_reason: str | None = None,
        root_signal_event_key: EventKey | None = None,
    ) -> SignalLedgerEntry:
        _require_chronological(self._entries, event_key)
        previous = self.latest(intent.intent_id)
        previous_status = previous.status if previous else None
        if status not in self._ALLOWED.get(previous_status, set()):
            raise LedgerValidationError(
                f"invalid signal transition {previous_status!s} -> {status.value}"
            )
        signal_key = (
            previous.signal_event_key
            if previous is not None
            else root_signal_event_key or event_key
        )
        if status is SignalStatus.FILLED:
            if executed_at is None:
                raise LedgerValidationError("FILLED signal requires executed_at")
            require_aware(executed_at, "executed_at")
            if executed_at < intent.signal_available_at:
                raise LedgerValidationError(
                    "executed_at must be >= signal_available_at"
                )
        elif executed_at is not None:
            raise LedgerValidationError("executed_at is valid only for FILLED")
        if status is SignalStatus.REJECTED and not rejection_reason:
            raise LedgerValidationError("REJECTED signal requires rejection_reason")
        row = SignalLedgerEntry(
            len(self._entries),
            event_key,
            previous_status,
            status,
            intent.intent_id,
            intent.stock_code,
            intent.intent_type,
            intent.side,
            intent.signal_generated_at,
            intent.signal_available_at,
            intent.execution_signal_at,
            executed_at,
            intent.reason,
            intent.strategy_state,
            rejection_reason,
            signal_key,
            intent.signal_source_bar_id,
            intent.signal_source_bar_sequence,
        )
        self._entries.append(row)
        return row


class OrderLedger:
    """Append-only order transition log with signal referential integrity."""

    _ALLOWED: ClassVar[dict[OrderStatus | None, set[OrderStatus]]] = {
        None: {OrderStatus.SCHEDULED, OrderStatus.NO_NEXT_BAR},
        OrderStatus.SCHEDULED: {OrderStatus.FILLED, OrderStatus.REJECTED},
    }

    def __init__(self, signals: SignalLedger) -> None:
        self._signals = signals
        self._entries: list[OrderLedgerEntry] = []

    @property
    def entries(self) -> tuple[OrderLedgerEntry, ...]:
        return tuple(self._entries)

    def latest(self, order_id: str) -> OrderLedgerEntry | None:
        return next(
            (row for row in reversed(self._entries) if row.order_id == order_id),
            None,
        )

    def append_schedule(self, result: ScheduleResult) -> OrderLedgerEntry:
        signal = self._signals.latest(result.signal_id)
        if signal is None:
            raise LedgerValidationError("cannot create order without a signal")
        status = (
            OrderStatus.SCHEDULED
            if result.status is ScheduleStatus.SCHEDULED
            else OrderStatus.NO_NEXT_BAR
        )
        if status is OrderStatus.SCHEDULED:
            if (
                result.eligible_at is None
                or result.eligible_bar_id is None
                or result.eligible_bar_sequence is None
            ):
                raise LedgerValidationError(
                    "scheduled order requires eligible bar identity"
                )
            if (
                result.eligible_bar_sequence <= signal.signal_source_bar_sequence
                or result.eligible_bar_id == signal.signal_source_bar_id
            ):
                raise LedgerValidationError(
                    "eligible bar must be later than and differ from source bar"
                )
        elif any(
            value is not None
            for value in (
                result.eligible_at,
                result.eligible_bar_id,
                result.eligible_bar_sequence,
            )
        ):
            raise LedgerValidationError("NO_NEXT_BAR must not invent eligible fields")
        return self._append(
            result=result,
            status=status,
            event_key=result.created_event_key,
        )

    def append_filled(
        self,
        order_id: str,
        *,
        event_key: EventKey,
    ) -> OrderLedgerEntry:
        current = self.latest(order_id)
        if current is None:
            raise LedgerValidationError("cannot fill unknown order")
        return self._append_from_current(current, OrderStatus.FILLED, event_key)

    def append_rejected(
        self,
        order_id: str,
        *,
        event_key: EventKey,
    ) -> OrderLedgerEntry:
        current = self.latest(order_id)
        if current is None:
            raise LedgerValidationError("cannot reject unknown order")
        return self._append_from_current(current, OrderStatus.REJECTED, event_key)

    def _append(
        self,
        *,
        result: ScheduleResult,
        status: OrderStatus,
        event_key: EventKey,
    ) -> OrderLedgerEntry:
        _require_chronological(self._entries, event_key)
        previous = self.latest(result.order_id)
        previous_status = previous.status if previous else None
        if status not in self._ALLOWED.get(previous_status, set()):
            raise LedgerValidationError(
                f"invalid order transition {previous_status!s} -> {status.value}"
            )
        row = OrderLedgerEntry(
            len(self._entries),
            event_key,
            previous_status,
            status,
            result.order_id,
            result.signal_id,
            result.stock_code,
            result.created_at,
            result.created_event_key,
            result.eligible_at,
            result.eligible_bar_id,
            result.eligible_bar_sequence,
            result.side,
            result.requested_quantity,
            result.reason,
        )
        self._entries.append(row)
        return row

    def _append_from_current(
        self,
        current: OrderLedgerEntry,
        status: OrderStatus,
        event_key: EventKey,
    ) -> OrderLedgerEntry:
        _require_chronological(self._entries, event_key)
        if status not in self._ALLOWED.get(current.status, set()):
            raise LedgerValidationError(
                f"invalid order transition {current.status.value} -> {status.value}"
            )
        row = OrderLedgerEntry(
            len(self._entries),
            event_key,
            current.status,
            status,
            current.order_id,
            current.signal_id,
            current.stock_code,
            current.created_at,
            current.created_event_key,
            current.eligible_at,
            current.eligible_bar_id,
            current.eligible_bar_sequence,
            current.side,
            current.requested_quantity,
            current.reason,
        )
        self._entries.append(row)
        return row


class FillLedger:
    """Immutable fill facts with complete order and signal traceability."""

    def __init__(self, orders: OrderLedger, signals: SignalLedger) -> None:
        self._orders = orders
        self._signals = signals
        self._entries: list[FillLedgerEntry] = []

    @property
    def entries(self) -> tuple[FillLedgerEntry, ...]:
        return tuple(self._entries)

    def append(
        self,
        *,
        fill_id: str,
        order_id: str,
        filled_at: datetime,
        fill_event_key: EventKey,
        fill_bar_id: str,
        fill_bar_sequence: int,
        raw_price: Decimal,
        quantity: int,
    ) -> FillLedgerEntry:
        _require_chronological(self._entries, fill_event_key)
        order = self._orders.latest(order_id)
        if order is None:
            raise LedgerValidationError("cannot create fill without an order")
        if order.status is not OrderStatus.SCHEDULED:
            raise LedgerValidationError("only a scheduled order may be filled")
        signal = self._signals.latest(order.signal_id)
        if signal is None:
            raise LedgerValidationError("order does not reference an existing signal")
        if signal.status is not SignalStatus.ORDER_SCHEDULED:
            raise LedgerValidationError(
                "fill requires an ORDER_SCHEDULED signal transition"
            )
        require_aware(filled_at, "filled_at")
        if not (
            signal.signal_generated_at
            <= signal.signal_available_at
            <= signal.execution_signal_at
            <= filled_at
        ):
            raise LedgerValidationError("fill violates the wall-clock causal chain")
        if fill_event_key <= signal.signal_event_key:
            raise LedgerValidationError("fill_event_key must be > signal_event_key")
        if fill_event_key <= order.created_event_key:
            raise LedgerValidationError("created_event_key must be < fill_event_key")
        if (
            fill_bar_sequence <= signal.signal_source_bar_sequence
            or fill_bar_id == signal.signal_source_bar_id
        ):
            raise LedgerValidationError("fill must occur on a later, different bar")
        if (
            fill_bar_id != order.eligible_bar_id
            or fill_bar_sequence != order.eligible_bar_sequence
            or filled_at != order.eligible_at
        ):
            raise LedgerValidationError(
                "fill bar must match authoritative eligible bar"
            )
        if (
            not isinstance(raw_price, Decimal)
            or not raw_price.is_finite()
            or raw_price <= 0
        ):
            raise LedgerValidationError("raw_price must be a finite positive Decimal")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise LedgerValidationError("quantity must be a positive integer")
        if any(row.fill_id == fill_id for row in self._entries):
            raise LedgerValidationError("duplicate fill_id")
        row = FillLedgerEntry(
            len(self._entries),
            fill_event_key,
            fill_id,
            order_id,
            filled_at,
            fill_event_key,
            fill_bar_id,
            fill_bar_sequence,
            raw_price,
            quantity,
        )
        self._entries.append(row)
        return row
