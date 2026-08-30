"""Deterministic phase-2 event runner for synthetic next-bar execution."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from heapq import heappop, heappush

from .events import (
    CANONICAL_TIMEZONE_ID,
    BarIdentity,
    DeterministicTieBreak,
    EntityKindRank,
    EventKey,
    EventPhase,
    assign_bar_identities,
    stable_id,
)
from .execution import (
    IntentSubmission,
    NextBarScheduler,
    ScheduleResult,
    ScheduleStatus,
    signal_event_key,
)
from .ledgers import (
    FillLedger,
    FillLedgerEntry,
    OrderLedger,
    OrderLedgerEntry,
    SignalLedger,
    SignalLedgerEntry,
    SignalStatus,
)
from .models import FiveMinuteBar


class EventRunnerError(ValueError):
    """The event stream violates availability or deterministic ordering."""


class EventType(str, Enum):
    BAR_AVAILABLE = "BAR_AVAILABLE"
    SIGNAL = "SIGNAL"
    ORDER = "ORDER"
    FILL = "FILL"


@dataclass(frozen=True, slots=True)
class EventTraceEntry:
    event_sequence: int
    event_key: EventKey
    event_type: EventType
    reference_id: str


@dataclass(frozen=True, slots=True)
class EventRunResult:
    timezone_identifier: str
    bars: tuple[BarIdentity, ...]
    event_trace: tuple[EventTraceEntry, ...]
    signal_ledger: tuple[SignalLedgerEntry, ...]
    order_ledger: tuple[OrderLedgerEntry, ...]
    fill_ledger: tuple[FillLedgerEntry, ...]


@dataclass(order=True, slots=True)
class _QueuedEvent:
    key: EventKey
    event_type: EventType = field(compare=False)
    payload: object = field(compare=False)
    reference_id: str = field(compare=False)


@dataclass(frozen=True, slots=True)
class _OrderPayload:
    submission: IntentSubmission
    schedule: ScheduleResult


@dataclass(frozen=True, slots=True)
class _FillPayload:
    submission: IntentSubmission
    schedule: ScheduleResult
    target: BarIdentity
    fill_id: str


class DeterministicEventRunner:
    """Run bar availability, signal, order, and fill events by ``EventKey``."""

    def __init__(self, scheduler: NextBarScheduler | None = None) -> None:
        self._scheduler = scheduler or NextBarScheduler()

    def run(
        self,
        bars: Iterable[FiveMinuteBar],
        submissions: Iterable[IntentSubmission],
    ) -> EventRunResult:
        identities = assign_bar_identities(bars)
        submissions_tuple = tuple(submissions)
        identity_by_id = {identity.bar_id: identity for identity in identities}

        queue: list[_QueuedEvent] = []
        queued_keys: set[EventKey] = set()

        def enqueue(event: _QueuedEvent) -> None:
            if event.key in queued_keys:
                raise EventRunnerError(
                    "duplicate EventKey; canonical entities must be distinguishable"
                )
            queued_keys.add(event.key)
            heappush(queue, event)

        for identity in identities:
            key = EventKey(
                identity.bar.signal_available_at,
                EventPhase.PREVIOUS_BAR_CLOSE_AVAILABLE,
                DeterministicTieBreak(
                    identity.stock_code,
                    identity.bar_sequence,
                    EntityKindRank.BAR,
                    identity.bar_id,
                ),
            )
            enqueue(
                _QueuedEvent(
                    key,
                    EventType.BAR_AVAILABLE,
                    identity,
                    identity.bar_id,
                )
            )

        for submission in submissions_tuple:
            intent = submission.intent
            enqueue(
                _QueuedEvent(
                    signal_event_key(intent),
                    EventType.SIGNAL,
                    submission,
                    intent.intent_id,
                )
            )

        signals = SignalLedger()
        orders = OrderLedger(signals)
        fills = FillLedger(orders, signals)
        available_bars: set[str] = set()
        trace: list[EventTraceEntry] = []

        while queue:
            event = heappop(queue)
            queued_keys.remove(event.key)
            trace.append(
                EventTraceEntry(
                    len(trace), event.key, event.event_type, event.reference_id
                )
            )
            if event.event_type is EventType.BAR_AVAILABLE:
                identity = event.payload
                if not isinstance(identity, BarIdentity):
                    raise EventRunnerError("invalid bar availability payload")
                available_bars.add(identity.bar_id)
                continue

            if event.event_type is EventType.SIGNAL:
                submission = event.payload
                if not isinstance(submission, IntentSubmission):
                    raise EventRunnerError("invalid signal payload")
                intent = submission.intent
                source = identity_by_id.get(intent.signal_source_bar_id)
                if source is None:
                    raise EventRunnerError("signal references an unknown source bar")
                if (
                    source.stock_code != intent.stock_code
                    or source.bar_sequence != intent.signal_source_bar_sequence
                ):
                    raise EventRunnerError("signal source bar identity is inconsistent")
                if source.bar.signal_available_at > intent.signal_available_at:
                    raise EventRunnerError(
                        "signal_available_at precedes source bar availability"
                    )
                if source.bar_id not in available_bars:
                    raise EventRunnerError(
                        "source bar is not available at signal evaluation"
                    )
                signals.append_transition(
                    intent,
                    status=SignalStatus.CREATED,
                    event_key=event.key,
                    root_signal_event_key=event.key,
                )
                schedule = self._scheduler.schedule(submission, identities)
                enqueue(
                    _QueuedEvent(
                        schedule.created_event_key,
                        EventType.ORDER,
                        _OrderPayload(submission, schedule),
                        schedule.order_id,
                    )
                )
                continue

            if event.event_type is EventType.ORDER:
                payload = event.payload
                if not isinstance(payload, _OrderPayload):
                    raise EventRunnerError("invalid order payload")
                schedule = payload.schedule
                orders.append_schedule(schedule)
                if schedule.status is ScheduleStatus.NO_NEXT_BAR:
                    signals.append_transition(
                        payload.submission.intent,
                        status=SignalStatus.NO_NEXT_BAR,
                        event_key=event.key,
                    )
                    continue
                signals.append_transition(
                    payload.submission.intent,
                    status=SignalStatus.ORDER_SCHEDULED,
                    event_key=event.key,
                )
                target = identity_by_id.get(schedule.eligible_bar_id or "")
                if target is None:
                    raise EventRunnerError("scheduled eligible bar does not exist")
                fill_id = stable_id("fill", schedule.order_id, target.bar_id)
                fill_key = EventKey(
                    target.bar.bar_start_at,
                    EventPhase.NEXT_BAR_OPEN_FILL,
                    DeterministicTieBreak(
                        target.stock_code,
                        payload.submission.intent.signal_source_bar_sequence,
                        EntityKindRank.FILL,
                        fill_id,
                    ),
                )
                enqueue(
                    _QueuedEvent(
                        fill_key,
                        EventType.FILL,
                        _FillPayload(payload.submission, schedule, target, fill_id),
                        fill_id,
                    )
                )
                continue

            if event.event_type is EventType.FILL:
                payload = event.payload
                if not isinstance(payload, _FillPayload):
                    raise EventRunnerError("invalid fill payload")
                target = payload.target
                intent = payload.submission.intent
                fills.append(
                    fill_id=payload.fill_id,
                    order_id=payload.schedule.order_id,
                    filled_at=target.bar.bar_start_at,
                    fill_event_key=event.key,
                    fill_bar_id=target.bar_id,
                    fill_bar_sequence=target.bar_sequence,
                    raw_price=target.bar.raw.open,
                    quantity=payload.submission.requested_quantity,
                )
                orders.append_filled(payload.schedule.order_id, event_key=event.key)
                signals.append_transition(
                    intent,
                    status=SignalStatus.FILLED,
                    event_key=event.key,
                    executed_at=target.bar.bar_start_at,
                )
                continue

            raise EventRunnerError(f"unsupported event type: {event.event_type}")

        return EventRunResult(
            CANONICAL_TIMEZONE_ID,
            identities,
            tuple(trace),
            signals.entries,
            orders.entries,
            fills.entries,
        )
