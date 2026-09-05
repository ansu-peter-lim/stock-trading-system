from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.indicators import calculate_daily_indicators
from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.kiwoom_daily.market_clock_audit import (
    _clock_series,
    _efficiency,
    _ma_distances,
    _quartile,
    _true_range_pct,
)


def _bars(count: int = 35) -> tuple[DailyBar, ...]:
    result: list[DailyBar] = []
    for index in range(count):
        signal_close = Decimal(100 + index)
        raw_close = Decimal(200 + index)
        raw = Ohlcv(
            raw_close - 1,
            raw_close + 2,
            raw_close - 2,
            raw_close,
            100 + index,
        )
        signal = Ohlcv(
            signal_close - 1,
            signal_close + 2,
            signal_close - 2,
            signal_close,
            100 + index,
        )
        result.append(
            DailyBar("005930", date(2024, 1, 1) + timedelta(days=index), raw, signal)
        )
    return tuple(result)


def test_efficiency_uses_directional_net_move_without_clamping() -> None:
    closes = [Decimal(100), Decimal(102), Decimal(101), Decimal(104)]
    assert _efficiency(closes, 3, 3) == Decimal(4) / Decimal(6)
    assert _efficiency([Decimal(100)] * 4, 3, 3) is None
    assert _efficiency(closes, 2, 3) is None


def test_quartile_boundaries_are_deterministic_and_inclusive() -> None:
    values = [Decimal(index) for index in range(1, 5)]
    assert _quartile(Decimal(1), values) == "Q1"
    assert _quartile(Decimal("1.75"), values) == "Q1"
    assert _quartile(Decimal(2), values) == "Q2"
    assert _quartile(Decimal("2.5"), values) == "Q2"
    assert _quartile(Decimal(3), values) == "Q3"
    assert _quartile(Decimal(4), values) == "Q4"
    assert _quartile(None, values) is None


def test_ma_role_tie_break_prefers_shorter_ma() -> None:
    distances, nearest, minimum, tie_count = _ma_distances(
        Decimal(101),
        {
            "MA5": Decimal(100),
            "MA10": Decimal(102),
            "MA20": Decimal(105),
            "MA60": Decimal(110),
        },
        Decimal(1),
    )
    assert distances["MA5"] == distances["MA10"] == Decimal(1)
    assert nearest == "MA5"
    assert minimum == Decimal(1)
    assert tie_count == 2


def test_clock_uses_signal_for_direction_and_raw_for_flow() -> None:
    bars = _bars()
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
    points = calculate_daily_indicators(bars, calendar)
    rows = _clock_series(bars, points)
    row = rows[20]
    assert row["net_move_atr_10"] is not None
    assert row["flow20"] == sum(
        (bar.raw.close * Decimal(bar.raw.volume) for bar in bars[1:21]),
        Decimal(0),
    ) / Decimal(20)
    assert row["net_move_atr_10"] == Decimal(10) / row["atr20"]


def test_true_range_pct_is_signal_ohlc_and_first_value_is_unavailable() -> None:
    bars = _bars(3)
    values = _true_range_pct(bars)
    assert values[0] is None
    expected = Decimal(4) / Decimal(100)
    assert values[1] == expected
