"""Source-neutral market-data models for the backtest engine.

The models preserve raw and signal price series side by side.  They deliberately
do not coerce floats or repair invalid records; validation belongs to the input
validation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum

FIVE_MINUTES = timedelta(minutes=5)


class TimestampSemantics(str, Enum):
    """Meaning of a source-provided intraday timestamp."""

    START = "START"
    END = "END"


class TradingSession(str, Enum):
    """Supported market-data sessions.

    Strategy V1 accepts only ``REGULAR``.  The other value lets validation
    reject after-hours input explicitly instead of silently dropping it.
    """

    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"


@dataclass(frozen=True, slots=True)
class Ohlcv:
    """One OHLCV price series.

    Prices are Decimal by contract.  Volume is an integer and may be zero;
    whether a zero-volume bar is tradable is a later execution-engine concern.
    """

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True, slots=True)
class DailyBar:
    """A completed daily bar with aligned raw and adjusted signal OHLCV."""

    stock_code: str
    trade_date: date
    raw: Ohlcv
    signal: Ohlcv


@dataclass(frozen=True, slots=True)
class FiveMinuteBar:
    """A completed five-minute bar with explicit source timestamp meaning."""

    stock_code: str
    source_timestamp: datetime
    source_timestamp_semantics: TimestampSemantics
    bar_start_at: datetime
    bar_end_at: datetime
    signal_available_at: datetime
    raw: Ohlcv
    signal: Ohlcv
    session: TradingSession

    @classmethod
    def from_source_timestamp(
        cls,
        *,
        stock_code: str,
        source_timestamp: datetime,
        source_timestamp_semantics: TimestampSemantics,
        raw: Ohlcv,
        signal: Ohlcv,
        session: TradingSession = TradingSession.REGULAR,
        signal_available_at: datetime | None = None,
    ) -> FiveMinuteBar:
        """Derive bar bounds without guessing the source timestamp meaning.

        The strict validator remains responsible for timezone, session, OHLCV,
        and ordering checks.  This constructor only performs the declared
        START/END conversion.
        """

        if source_timestamp_semantics is TimestampSemantics.START:
            bar_start_at = source_timestamp
            bar_end_at = source_timestamp + FIVE_MINUTES
        elif source_timestamp_semantics is TimestampSemantics.END:
            bar_end_at = source_timestamp
            bar_start_at = source_timestamp - FIVE_MINUTES
        else:
            raise ValueError("source_timestamp_semantics must be START or END")
        return cls(
            stock_code=stock_code,
            source_timestamp=source_timestamp,
            source_timestamp_semantics=source_timestamp_semantics,
            bar_start_at=bar_start_at,
            bar_end_at=bar_end_at,
            signal_available_at=signal_available_at or bar_end_at,
            raw=raw,
            signal=signal,
            session=session,
        )
