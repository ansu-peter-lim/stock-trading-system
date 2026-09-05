from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.kiwoom_daily.market_clock_compression_audit import (
    _band_exit_flags,
    _cross_flags,
    _event_at,
    _metric_summary,
    _ordering_flip_count,
)


def test_close_cross_uses_inclusive_previous_boundary() -> None:
    assert _cross_flags(Decimal(100), Decimal(100), Decimal(101), Decimal(100)) == (
        True,
        False,
    )
    assert _cross_flags(Decimal(100), Decimal(100), Decimal(99), Decimal(100)) == (
        False,
        True,
    )
    assert _cross_flags(Decimal(101), Decimal(100), Decimal(101), Decimal(100)) == (
        False,
        False,
    )


def test_ordering_flips_ignore_ties_and_use_trailing_twenty_sessions() -> None:
    first = [Decimal(value) for value in (1, 3, 2, 1, 3)]
    second = [Decimal(2)] * len(first)
    assert _ordering_flip_count(first, second, 4) == 3
    long_first = [Decimal(1)] * 20 + [Decimal(3), Decimal(1)]
    long_second = [Decimal(2)] * len(long_first)
    assert _ordering_flip_count(long_first, long_second, 21) == 2


def test_band_exit_uses_previous_band_and_current_band() -> None:
    assert _band_exit_flags(
        Decimal(100),
        Decimal(100),
        Decimal(90),
        Decimal(111),
        Decimal(110),
        Decimal(90),
    ) == (True, False)
    assert _band_exit_flags(
        Decimal(90),
        Decimal(110),
        Decimal(80),
        Decimal(89),
        Decimal(110),
        Decimal(90),
    ) == (False, True)


def test_event_summary_exposes_follow_through_and_persistence() -> None:
    closes = [Decimal(value) for value in (100, 101, 102, 105, 104, 103)]
    reference = [Decimal(value) for value in (100, 100, 100, 100, 100, 100)]
    event = _event_at(
        event_type="CLOSE_MA10_CROSS",
        stock_code="005930",
        event_date=date(2024, 1, 2),
        index=1,
        direction=1,
        closes=closes,
        reference=reference,
        opposite_indexes={"CLOSE_MA10_CROSS": [(4, -1)]},
        source_pivot_kinds="LOW",
    )
    summary = _metric_summary([event], event_type="CLOSE_MA10_CROSS")
    assert summary["count"] == 1
    assert summary["same_side_d1"]["true_count"] == 1
    assert summary["opposite_recross_within_3"]["true_count"] == 1
    assert summary["aligned_return_3_pct"]["median"] == (
        Decimal(104) / Decimal(101) - Decimal(1)
    ) * Decimal(100)
