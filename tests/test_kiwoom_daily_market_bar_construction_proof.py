from datetime import date, datetime, time
from decimal import Decimal

from src.backtest_engine.validation import KOREA_TZ
from src.kiwoom_daily.market_bar_construction_proof import (
    ActivitySegment,
    MarketBarConstructionError,
    build_market_bars,
)


def _segment(
    stock: str,
    start: str,
    end: str,
    day: date,
    source: str,
    value: str,
) -> ActivitySegment:
    session_start = datetime.combine(day, time(9), tzinfo=KOREA_TZ)
    session_end = datetime.combine(day, time(15, 30), tzinfo=KOREA_TZ)
    return ActivitySegment(
        stock,
        Decimal(start),
        Decimal(end),
        session_start,
        session_end,
        Decimal(value),
        Decimal(value) + 1,
        Decimal(value) - 1,
        Decimal(value) + Decimal("0.5"),
        Decimal(10),
        source,
        source,
    )


def test_fast_day_can_produce_multiple_unit_market_bars() -> None:
    bars = build_market_bars(
        (
            _segment("005930", "0", "0.5", date(2024, 1, 2), "5M", "100"),
            _segment("005930", "0.5", "1", date(2024, 1, 2), "5M", "101"),
            _segment("005930", "1", "2", date(2024, 1, 2), "5M", "102"),
        )
    )
    assert len(bars) == 2
    assert all(bar.tau_end - bar.tau_start == Decimal(1) for bar in bars)
    assert bars[0].source_resolution == "5M"
    assert bars[0].open == Decimal(100)
    assert bars[0].close == Decimal("101.5")


def test_slow_segments_cross_calendar_day_without_resetting_tau() -> None:
    bars = build_market_bars(
        (
            _segment("005930", "0", "0.4", date(2024, 1, 2), "DAILY", "100"),
            _segment("005930", "0.4", "0.8", date(2024, 1, 3), "DAILY", "101"),
            _segment("005930", "0.8", "1.2", date(2024, 1, 4), "DAILY", "102"),
        )
    )
    assert len(bars) == 1
    assert bars[0].calendar_start_datetime.date() == date(2024, 1, 2)
    assert bars[0].calendar_end_datetime.date() == date(2024, 1, 4)
    assert bars[0].tau_start == Decimal(0)
    assert bars[0].tau_end == Decimal(1)
    assert any(item["boundary_split"] for item in bars[0].source_segments)


def test_tail_below_one_tau_is_not_materialized_as_a_partial_bar() -> None:
    bars = build_market_bars(
        (_segment("005930", "0", "0.7", date(2024, 1, 2), "DAILY", "100"),)
    )
    assert bars == ()


def test_noncontiguous_tau_is_rejected() -> None:
    first = _segment("005930", "0", "0.5", date(2024, 1, 2), "DAILY", "100")
    second = _segment("005930", "0.6", "1", date(2024, 1, 2), "DAILY", "101")
    try:
        build_market_bars((first, second))
    except MarketBarConstructionError as exc:
        assert "contiguous" in str(exc)
    else:
        raise AssertionError("expected noncontiguous input to be rejected")


def test_identity_prefix_makes_rebased_runs_distinct() -> None:
    segment = _segment("005930", "0", "1", date(2024, 1, 2), "DAILY", "100")
    first = build_market_bars((segment,), identity_prefix="RUN:1")
    second = build_market_bars((segment,), identity_prefix="RUN:2")
    assert first[0].market_bar_id != second[0].market_bar_id
