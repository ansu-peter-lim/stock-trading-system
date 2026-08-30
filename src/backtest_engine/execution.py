"""Minimal strategy intent and next-bar scheduling contracts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from .events import (
    BarIdentity,
    DeterministicTieBreak,
    EntityKindRank,
    EventKey,
    EventModelError,
    EventPhase,
    canonical_timestamp,
    require_aware,
    stable_id,
)


class ExecutionModelError(ValueError):
    """An intent or scheduling result violates the execution contract."""


class IntentType(str, Enum):
    TARGET_WEIGHT = "TARGET_WEIGHT"
    UNIT_DELTA = "UNIT_DELTA"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ScheduleStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    NO_NEXT_BAR = "NO_NEXT_BAR"


@dataclass(frozen=True, slots=True)
class StrategyIntent:
    """Small, strategy-neutral contract handed to the execution layer."""

    intent_id: str
    stock_code: str
    intent_type: IntentType
    side: OrderSide
    signal_generated_at: datetime
    signal_available_at: datetime
    execution_signal_at: datetime
    reason: str
    strategy_state: str
    signal_source_bar_id: str
    signal_source_bar_sequence: int
    target_weight: Decimal | None = None
    unit_delta: int | None = None

    def __post_init__(self) -> None:
        if not self.intent_id:
            raise ExecutionModelError("intent_id must not be empty")
        if (
            len(self.stock_code) != 6
            or not self.stock_code.isascii()
            or not self.stock_code.isdigit()
        ):
            raise ExecutionModelError("stock_code must be exactly six ASCII digits")
        for name in (
            "signal_generated_at",
            "signal_available_at",
            "execution_signal_at",
        ):
            try:
                require_aware(getattr(self, name), name)
            except EventModelError as exc:
                raise ExecutionModelError(str(exc)) from exc
        if not (
            self.signal_generated_at
            <= self.signal_available_at
            <= self.execution_signal_at
        ):
            raise ExecutionModelError(
                "require signal_generated_at <= signal_available_at "
                "<= execution_signal_at"
            )
        if not self.reason:
            raise ExecutionModelError("reason must not be empty")
        if not self.strategy_state:
            raise ExecutionModelError("strategy_state must not be empty")
        if not self.signal_source_bar_id:
            raise ExecutionModelError("signal_source_bar_id must not be empty")
        if (
            not isinstance(self.signal_source_bar_sequence, int)
            or self.signal_source_bar_sequence < 0
        ):
            raise ExecutionModelError(
                "signal_source_bar_sequence must be a non-negative integer"
            )
        if self.intent_type is IntentType.TARGET_WEIGHT:
            if not isinstance(self.target_weight, Decimal):
                raise ExecutionModelError(
                    "TARGET_WEIGHT requires Decimal target_weight"
                )
            if not Decimal(0) <= self.target_weight <= Decimal(1):
                raise ExecutionModelError("target_weight must be between 0 and 1")
            if self.unit_delta is not None:
                raise ExecutionModelError("TARGET_WEIGHT must not set unit_delta")
        elif self.intent_type is IntentType.UNIT_DELTA:
            if (
                not isinstance(self.unit_delta, int)
                or isinstance(self.unit_delta, bool)
                or self.unit_delta == 0
            ):
                raise ExecutionModelError(
                    "UNIT_DELTA requires non-zero integer unit_delta"
                )
            if self.target_weight is not None:
                raise ExecutionModelError("UNIT_DELTA must not set target_weight")
        else:
            raise ExecutionModelError("intent_type must be a supported IntentType")


@dataclass(frozen=True, slots=True)
class IntentSubmission:
    """An intent plus a synthetic quantity used by the phase-2 fixture."""

    intent: StrategyIntent
    requested_quantity: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.requested_quantity, int)
            or isinstance(self.requested_quantity, bool)
            or self.requested_quantity <= 0
        ):
            raise ExecutionModelError("requested_quantity must be a positive integer")


def intent_stable_id(intent: StrategyIntent) -> str:
    """Canonical identity used for ordering; it intentionally excludes input order."""

    return stable_id(
        "intent",
        intent.stock_code,
        intent.intent_type.value,
        intent.side.value,
        canonical_timestamp(intent.signal_generated_at),
        canonical_timestamp(intent.signal_available_at),
        canonical_timestamp(intent.execution_signal_at),
        intent.signal_source_bar_id,
        intent.signal_source_bar_sequence,
        intent.target_weight,
        intent.unit_delta,
        intent.reason,
        intent.strategy_state,
    )


def signal_event_key(intent: StrategyIntent) -> EventKey:
    return EventKey(
        intent.execution_signal_at,
        EventPhase.SIGNAL_EVALUATION,
        DeterministicTieBreak(
            intent.stock_code,
            intent.signal_source_bar_sequence,
            EntityKindRank.SIGNAL,
            intent_stable_id(intent),
        ),
    )


@dataclass(frozen=True, slots=True)
class ScheduleResult:
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
    status: ScheduleStatus
    reason: str


class NextBarScheduler:
    """Find the next regular-session bar without deciding expire/carry policy."""

    def schedule(
        self,
        submission: IntentSubmission,
        bars: Iterable[BarIdentity],
    ) -> ScheduleResult:
        intent = submission.intent
        candidates = sorted(
            (
                identity
                for identity in bars
                if identity.stock_code == intent.stock_code
                and identity.bar_sequence > intent.signal_source_bar_sequence
                and identity.bar.bar_start_at >= intent.execution_signal_at
            ),
            key=lambda identity: identity.bar_sequence,
        )
        target = candidates[0] if candidates else None
        target_marker = target.bar_id if target else "NO_NEXT_BAR"
        order_id = stable_id(
            "order",
            intent_stable_id(intent),
            submission.requested_quantity,
            target_marker,
        )
        created_key = EventKey(
            intent.execution_signal_at,
            EventPhase.ORDER_CREATED_OR_SCHEDULED,
            DeterministicTieBreak(
                intent.stock_code,
                intent.signal_source_bar_sequence,
                EntityKindRank.ORDER,
                order_id,
            ),
        )
        if target is None:
            return ScheduleResult(
                order_id,
                intent.intent_id,
                intent.stock_code,
                intent.execution_signal_at,
                created_key,
                None,
                None,
                None,
                intent.side,
                submission.requested_quantity,
                ScheduleStatus.NO_NEXT_BAR,
                "NO_NEXT_BAR",
            )
        if (
            target.bar_sequence <= intent.signal_source_bar_sequence
            or target.bar_id == intent.signal_source_bar_id
        ):
            raise ExecutionModelError("eligible bar must be later than source bar")
        return ScheduleResult(
            order_id,
            intent.intent_id,
            intent.stock_code,
            intent.execution_signal_at,
            created_key,
            target.bar.bar_start_at,
            target.bar_id,
            target.bar_sequence,
            intent.side,
            submission.requested_quantity,
            ScheduleStatus.SCHEDULED,
            "NEXT_REGULAR_BAR_RAW_OPEN",
        )
