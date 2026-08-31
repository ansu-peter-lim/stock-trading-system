"""ZERO_COST, integer-share accounting for one Core position only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from enum import Enum

from .core_actions import CoreActionType, PendingCoreAction
from .events import stable_id
from .ledgers import FillLedgerEntry


class AccountingValidationError(ValueError):
    """A single-stock accounting operation violates the MVP contract."""


class PositionStatus(str, Enum):
    FLAT = "FLAT"
    CORE = "CORE"


@dataclass(frozen=True, slots=True)
class CorePosition:
    stock_code: str
    stock_full_weight: Decimal
    core_target_weight: Decimal
    actual_quantity: int
    average_cost: Decimal
    position_status: PositionStatus


@dataclass(frozen=True, slots=True)
class PositionTransition:
    ledger_sequence: int
    transition_id: str
    action_id: str
    execution_signal_id: str
    order_id: str
    fill_id: str
    transitioned_at: datetime
    previous_status: PositionStatus
    status: PositionStatus
    quantity_before: int
    quantity_after: int
    average_cost_before: Decimal
    average_cost_after: Decimal
    cash_before: Decimal
    cash_after: Decimal
    raw_fill_price: Decimal
    commission: Decimal
    tax: Decimal
    slippage: Decimal


class ZeroCostSingleStockAccount:
    """Minimal accounting projection backed by immutable position transitions."""

    def __init__(
        self,
        *,
        stock_code: str,
        stock_full_weight: Decimal,
        initial_capital: Decimal,
    ) -> None:
        if len(stock_code) != 6 or not stock_code.isascii() or not stock_code.isdigit():
            raise AccountingValidationError(
                "stock_code must be exactly six ASCII digits"
            )
        if not isinstance(stock_full_weight, Decimal) or not Decimal(
            0
        ) < stock_full_weight <= Decimal(1):
            raise AccountingValidationError(
                "stock_full_weight must be Decimal in (0, 1]"
            )
        if (
            not isinstance(initial_capital, Decimal)
            or not initial_capital.is_finite()
            or initial_capital <= 0
        ):
            raise AccountingValidationError(
                "initial_capital must be a finite positive Decimal"
            )
        self.cash = initial_capital
        self.position = CorePosition(
            stock_code,
            stock_full_weight,
            Decimal(0),
            0,
            Decimal(0),
            PositionStatus.FLAT,
        )
        self._entries: list[PositionTransition] = []

    @property
    def entries(self) -> tuple[PositionTransition, ...]:
        return tuple(self._entries)

    @property
    def holding_core(self) -> bool:
        return self.position.position_status is PositionStatus.CORE

    def entry_quantity(
        self,
        *,
        portfolio_equity_at_decision: Decimal,
        core_fraction_of_full: Decimal,
        raw_fill_price: Decimal,
    ) -> int:
        for value, name in (
            (portfolio_equity_at_decision, "portfolio_equity_at_decision"),
            (core_fraction_of_full, "core_fraction_of_full"),
            (raw_fill_price, "raw_fill_price"),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise AccountingValidationError(
                    f"{name} must be a finite positive Decimal"
                )
        stock_full_amount = (
            portfolio_equity_at_decision * self.position.stock_full_weight
        )
        core_target_amount = stock_full_amount * core_fraction_of_full
        return int(
            (core_target_amount / raw_fill_price).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )

    def apply_fill(
        self,
        *,
        action: PendingCoreAction,
        fill: FillLedgerEntry,
        execution_signal_id: str,
    ) -> PositionTransition:
        if action.stock_code != self.position.stock_code:
            raise AccountingValidationError("action stock does not match account")
        before = self.position
        cash_before = self.cash
        if action.action is CoreActionType.ENTER:
            if before.position_status is not PositionStatus.FLAT:
                raise AccountingValidationError("Core ENTER requires a flat position")
            cost = fill.raw_price * Decimal(fill.quantity)
            if cost > self.cash:
                raise AccountingValidationError("Core ENTER would make cash negative")
            self.cash -= cost
            after = CorePosition(
                before.stock_code,
                before.stock_full_weight,
                action.target_core_weight,
                fill.quantity,
                fill.raw_price,
                PositionStatus.CORE,
            )
        elif action.action is CoreActionType.FULL_EXIT:
            if before.position_status is not PositionStatus.CORE:
                raise AccountingValidationError("FULL_EXIT requires a Core position")
            if fill.quantity != before.actual_quantity:
                raise AccountingValidationError(
                    "FULL_EXIT quantity must equal the complete holding"
                )
            self.cash += fill.raw_price * Decimal(fill.quantity)
            after = CorePosition(
                before.stock_code,
                before.stock_full_weight,
                Decimal(0),
                0,
                Decimal(0),
                PositionStatus.FLAT,
            )
        else:
            raise AccountingValidationError("unsupported Core action")

        if self.cash < 0 or after.actual_quantity < 0:
            raise AccountingValidationError("cash and quantity must not be negative")
        self.position = after
        transition_id = stable_id(
            "position_transition",
            action.action_id,
            fill.fill_id,
            before.position_status.value,
            after.position_status.value,
            before.actual_quantity,
            after.actual_quantity,
            cash_before,
            self.cash,
        )
        row = PositionTransition(
            len(self._entries),
            transition_id,
            action.action_id,
            execution_signal_id,
            fill.order_id,
            fill.fill_id,
            fill.filled_at,
            before.position_status,
            after.position_status,
            before.actual_quantity,
            after.actual_quantity,
            before.average_cost,
            after.average_cost,
            cash_before,
            self.cash,
            fill.raw_price,
            Decimal(0),
            Decimal(0),
            Decimal(0),
        )
        if any(entry.transition_id == transition_id for entry in self._entries):
            raise AccountingValidationError("duplicate position transition")
        self._entries.append(row)
        return row
