"""Deterministic event keys and stable intraday bar identities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from hashlib import sha256

from .models import FiveMinuteBar
from .validation import MarketDataValidationError, validate_five_minute_bars

CANONICAL_TIMEZONE_ID = "Asia/Seoul"


class EventModelError(ValueError):
    """An event or bar identity violates the engine time contract."""


class EventPhase(IntEnum):
    """Causal phases within one wall-clock timestamp."""

    CORPORATE_ACTION = 0
    PREVIOUS_BAR_CLOSE_AVAILABLE = 10
    SIGNAL_EVALUATION = 20
    ORDER_CREATED_OR_SCHEDULED = 30
    NEXT_BAR_OPEN_FILL = 40


class EntityKindRank(IntEnum):
    """Documented deterministic ordering for equal-time peer entities."""

    BAR = 0
    SIGNAL = 10
    ORDER = 20
    FILL = 30


def require_aware(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise EventModelError(f"{field} must be timezone-aware")
    return value


def canonical_timestamp(value: datetime) -> str:
    """Return a stable UTC representation without inferring a timezone ID."""

    return require_aware(value, "timestamp").astimezone(timezone.utc).isoformat()


def stable_id(prefix: str, *parts: object) -> str:
    """Build a deterministic ID from canonical, caller-selected fields."""

    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{sha256(material.encode('utf-8')).hexdigest()[:24]}"


@dataclass(frozen=True, order=True, slots=True)
class DeterministicTieBreak:
    """The document-defined equal-timestamp ordering tuple."""

    stock_code: str
    source_bar_sequence: int
    entity_kind_rank: EntityKindRank
    stable_entity_id: str

    def __post_init__(self) -> None:
        if (
            len(self.stock_code) != 6
            or not self.stock_code.isascii()
            or not self.stock_code.isdigit()
        ):
            raise EventModelError("stock_code must be exactly six ASCII digits")
        if not isinstance(self.source_bar_sequence, int):
            raise EventModelError("source_bar_sequence must be an integer")
        if not isinstance(self.entity_kind_rank, EntityKindRank):
            raise EventModelError("entity_kind_rank must be an EntityKindRank")
        if not self.stable_entity_id:
            raise EventModelError("stable_entity_id must not be empty")


@dataclass(frozen=True, order=True, slots=True)
class EventKey:
    """Causal ordering key: timestamp, phase, then deterministic tie-break."""

    timestamp: datetime
    event_phase: EventPhase
    deterministic_tie_break: DeterministicTieBreak

    def __post_init__(self) -> None:
        require_aware(self.timestamp, "EventKey.timestamp")
        if not isinstance(self.event_phase, EventPhase):
            raise EventModelError("event_phase must be an EventPhase")


@dataclass(frozen=True, slots=True)
class BarIdentity:
    """A stable identity for one validated regular-session intraday bar."""

    bar_id: str
    stock_code: str
    bar_sequence: int
    bar: FiveMinuteBar


def _bar_sort_key(bar: FiveMinuteBar) -> tuple[str, datetime, datetime, datetime]:
    start = require_aware(bar.bar_start_at, "bar_start_at")
    end = require_aware(bar.bar_end_at, "bar_end_at")
    source = require_aware(bar.source_timestamp, "source_timestamp")
    return (
        bar.stock_code,
        start.astimezone(timezone.utc),
        end.astimezone(timezone.utc),
        source.astimezone(timezone.utc),
    )


def assign_bar_identities(bars: Iterable[FiveMinuteBar]) -> tuple[BarIdentity, ...]:
    """Validate, canonically sort, and number bars continuously per stock."""

    canonical = tuple(sorted(bars, key=_bar_sort_key))
    try:
        validate_five_minute_bars(canonical)
    except MarketDataValidationError as exc:
        raise EventModelError(str(exc)) from exc

    next_sequence: dict[str, int] = {}
    identities: list[BarIdentity] = []
    for bar in canonical:
        sequence = next_sequence.get(bar.stock_code, 0)
        bar_id = stable_id(
            "bar",
            bar.stock_code,
            canonical_timestamp(bar.bar_start_at),
            canonical_timestamp(bar.bar_end_at),
        )
        identities.append(BarIdentity(bar_id, bar.stock_code, sequence, bar))
        next_sequence[bar.stock_code] = sequence + 1
    return tuple(identities)
