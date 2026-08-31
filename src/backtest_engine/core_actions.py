"""Append-only PendingCoreAction state for the phase-3 Core MVP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import ClassVar

from .core_strategy import DailyCoreSignal, DailyCoreSignalType
from .events import require_aware, stable_id
from .validation import KOREA_TZ


class CoreActionValidationError(ValueError):
    """A pending Core action transition violates Strategy V1."""


class CoreActionType(str, Enum):
    ENTER = "ENTER"
    FULL_EXIT = "FULL_EXIT"


class CoreActionStatus(str, Enum):
    PENDING = "PENDING"
    ARMED = "ARMED"
    ORDER_SCHEDULED = "ORDER_SCHEDULED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


ACTIVE_CORE_ACTION_STATUSES = frozenset(
    {
        CoreActionStatus.PENDING,
        CoreActionStatus.ARMED,
        CoreActionStatus.ORDER_SCHEDULED,
    }
)


@dataclass(frozen=True, slots=True)
class PendingCoreAction:
    ledger_sequence: int
    transition_id: str
    action_id: str
    daily_signal_id: str
    stock_code: str
    action: CoreActionType
    stock_full_weight: Decimal
    target_core_weight: Decimal
    portfolio_equity_at_decision: Decimal
    generated_trade_date: date
    activation_trade_date: date
    reason: str
    previous_status: CoreActionStatus | None
    status: CoreActionStatus
    transition_at: datetime
    transition_reason: str
    superseded_by: str | None
    execution_signal_id: str | None
    order_id: str | None
    fill_id: str | None


class PendingCoreActionLedger:
    """Immutable transition rows with one active action per stock."""

    _ALLOWED: ClassVar[dict[CoreActionStatus, set[CoreActionStatus]]] = {
        CoreActionStatus.PENDING: {
            CoreActionStatus.ARMED,
            CoreActionStatus.CANCELLED,
            CoreActionStatus.EXPIRED,
            CoreActionStatus.REJECTED,
        },
        CoreActionStatus.ARMED: {
            CoreActionStatus.ORDER_SCHEDULED,
            CoreActionStatus.CANCELLED,
            CoreActionStatus.EXPIRED,
            CoreActionStatus.REJECTED,
        },
        CoreActionStatus.ORDER_SCHEDULED: {
            CoreActionStatus.ARMED,
            CoreActionStatus.FILLED,
            CoreActionStatus.CANCELLED,
            CoreActionStatus.EXPIRED,
            CoreActionStatus.REJECTED,
        },
    }

    def __init__(self) -> None:
        self._entries: list[PendingCoreAction] = []
        self._transition_ids: set[str] = set()

    @property
    def entries(self) -> tuple[PendingCoreAction, ...]:
        return tuple(self._entries)

    def latest(self, action_id: str) -> PendingCoreAction | None:
        return next(
            (row for row in reversed(self._entries) if row.action_id == action_id),
            None,
        )

    def active_for_stock(self, stock_code: str) -> PendingCoreAction | None:
        latest_by_action: dict[str, PendingCoreAction] = {}
        for row in self._entries:
            if row.stock_code == stock_code:
                latest_by_action[row.action_id] = row
        active = [
            row
            for row in latest_by_action.values()
            if row.status in ACTIVE_CORE_ACTION_STATUSES
        ]
        if len(active) > 1:
            raise CoreActionValidationError("multiple active Core actions for stock")
        return active[0] if active else None

    @staticmethod
    def action_id_for(signal: DailyCoreSignal) -> str:
        action = (
            CoreActionType.ENTER
            if signal.signal_type is DailyCoreSignalType.ENTER
            else CoreActionType.FULL_EXIT
        )
        return stable_id(
            "core_action",
            signal.signal_id,
            signal.stock_code,
            action.value,
            signal.generated_trade_date.isoformat(),
            signal.activation_trade_date.isoformat(),
        )

    def create_from_signal(
        self,
        signal: DailyCoreSignal,
        *,
        portfolio_equity_at_decision: Decimal,
    ) -> PendingCoreAction:
        if (
            not isinstance(portfolio_equity_at_decision, Decimal)
            or not portfolio_equity_at_decision.is_finite()
            or portfolio_equity_at_decision <= 0
        ):
            raise CoreActionValidationError(
                "portfolio_equity_at_decision must be a finite positive Decimal"
            )
        action = (
            CoreActionType.ENTER
            if signal.signal_type is DailyCoreSignalType.ENTER
            else CoreActionType.FULL_EXIT
        )
        action_id = self.action_id_for(signal)
        if self.latest(action_id) is not None:
            raise CoreActionValidationError("duplicate canonical Core action")
        active = self.active_for_stock(signal.stock_code)
        if active is not None:
            if (
                action is CoreActionType.FULL_EXIT
                and active.action is CoreActionType.ENTER
            ):
                self.transition(
                    active.action_id,
                    CoreActionStatus.CANCELLED,
                    transition_at=signal.signal_available_at,
                    transition_reason="SUPERSEDED_BY_FULL_EXIT",
                    superseded_by=action_id,
                )
            else:
                raise CoreActionValidationError(
                    "a stock may have only one active Core action"
                )
        return self._append_initial(
            signal,
            action,
            action_id,
            portfolio_equity_at_decision,
        )

    def _append_initial(
        self,
        signal: DailyCoreSignal,
        action: CoreActionType,
        action_id: str,
        portfolio_equity_at_decision: Decimal,
    ) -> PendingCoreAction:
        transition_id = stable_id("core_transition", action_id, "PENDING")
        row = PendingCoreAction(
            len(self._entries),
            transition_id,
            action_id,
            signal.signal_id,
            signal.stock_code,
            action,
            signal.stock_full_weight,
            signal.target_core_weight,
            portfolio_equity_at_decision,
            signal.generated_trade_date,
            signal.activation_trade_date,
            signal.reason,
            None,
            CoreActionStatus.PENDING,
            signal.signal_available_at,
            "DAILY_SIGNAL_CREATED_PENDING_ACTION",
            None,
            None,
            None,
            None,
        )
        self._append(row)
        return row

    def transition(
        self,
        action_id: str,
        status: CoreActionStatus,
        *,
        transition_at: datetime,
        transition_reason: str,
        superseded_by: str | None = None,
        execution_signal_id: str | None = None,
        order_id: str | None = None,
        fill_id: str | None = None,
    ) -> PendingCoreAction:
        current = self.latest(action_id)
        if current is None:
            raise CoreActionValidationError("unknown Core action")
        try:
            require_aware(transition_at, "transition_at")
        except ValueError as exc:
            raise CoreActionValidationError(str(exc)) from exc
        if transition_at < current.transition_at:
            raise CoreActionValidationError(
                "Core transition time must not go backwards"
            )
        if status not in self._ALLOWED.get(current.status, set()):
            raise CoreActionValidationError(
                f"invalid Core transition {current.status.value} -> {status.value}"
            )
        if (
            current.status is CoreActionStatus.PENDING
            and status is CoreActionStatus.ARMED
            and transition_at.astimezone(KOREA_TZ).date()
            != current.activation_trade_date
        ):
            raise CoreActionValidationError(
                "Core action may be ARMED only on activation_trade_date"
            )
        if status is CoreActionStatus.CANCELLED and not superseded_by:
            raise CoreActionValidationError("CANCELLED requires superseded_by")
        if status is CoreActionStatus.ORDER_SCHEDULED and (
            not execution_signal_id or not order_id
        ):
            raise CoreActionValidationError(
                "ORDER_SCHEDULED requires execution signal and order IDs"
            )
        if status is CoreActionStatus.FILLED and (
            not execution_signal_id or not order_id or not fill_id
        ):
            raise CoreActionValidationError(
                "FILLED requires execution signal, order, and fill IDs"
            )
        transition_id = stable_id(
            "core_transition",
            action_id,
            current.status.value,
            status.value,
            transition_at.isoformat(),
            transition_reason,
            superseded_by,
            execution_signal_id,
            order_id,
            fill_id,
        )
        row = PendingCoreAction(
            len(self._entries),
            transition_id,
            current.action_id,
            current.daily_signal_id,
            current.stock_code,
            current.action,
            current.stock_full_weight,
            current.target_core_weight,
            current.portfolio_equity_at_decision,
            current.generated_trade_date,
            current.activation_trade_date,
            current.reason,
            current.status,
            status,
            transition_at,
            transition_reason,
            superseded_by,
            execution_signal_id,
            order_id,
            fill_id,
        )
        self._append(row)
        return row

    def _append(self, row: PendingCoreAction) -> None:
        if row.transition_id in self._transition_ids:
            raise CoreActionValidationError("duplicate canonical Core transition")
        self._entries.append(row)
        self._transition_ids.add(row.transition_id)
