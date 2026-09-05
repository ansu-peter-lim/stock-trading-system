from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.kiwoom_daily.market_clock_compression_audit_v0_2 import (
    _band_series,
    _frequency,
    _regime_event_summary,
)


def test_band_series_uses_ma5_ma10_ma20_only() -> None:
    highs, lows = _band_series(
        [Decimal(10), Decimal(11), None],
        [Decimal(9), Decimal(12), Decimal(8)],
        [Decimal(8), Decimal(10), Decimal(7)],
    )
    assert highs == [Decimal(10), Decimal(12), None]
    assert lows == [Decimal(8), Decimal(10), None]


def test_frequency_is_events_per_hundred_sessions() -> None:
    assert _frequency(5, 20) == Decimal(25)
    assert _frequency(0, 20) == Decimal(0)
    assert _frequency(1, 0) is None


def test_full_daily_regime_summary_uses_unique_stock_date_denominator() -> None:
    rows = [
        {"stock_code": "005930", "trade_date": date(2024, 1, 1)},
        {"stock_code": "005930", "trade_date": date(2024, 1, 1)},
        {"stock_code": "000660", "trade_date": date(2024, 1, 1)},
    ]
    events = [
        {
            "event_type": "CLOSE_MA10_CROSS",
            "stock_code": "005930",
            "event_date": date(2024, 1, 1),
            "direction": 1,
            "same_side_d1": True,
            "same_side_d3": True,
            "same_side_d5": False,
            "opposite_recross_within_1": False,
            "opposite_recross_within_3": False,
            "opposite_recross_within_5": False,
            "aligned_return_3_pct": Decimal(1),
            "aligned_return_5_pct": Decimal(2),
            "aligned_return_10_pct": Decimal(3),
        }
    ]
    summary = _regime_event_summary(rows, events, lambda _: True)
    assert summary["daily_session_count"] == 2
    assert summary["ma10_cross"]["count"] == 1
    assert summary["ma10_cross"]["events_per_100_sessions"] == Decimal(50)
