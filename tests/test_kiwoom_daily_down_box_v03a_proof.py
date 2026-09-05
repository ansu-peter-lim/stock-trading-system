from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.down_box_strategy import BoxSetup
from src.backtest_engine.indicators import DailyIndicatorPoint
from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.kiwoom_daily.down_box_daily_execution_proof import _load_stock
from src.kiwoom_daily.down_box_v03a_proof import (
    RescueWait,
    _campaign_classification,
    _candidate_conditions,
    _exact_turn_indexes,
    _run_variant_b,
)


def _synthetic_bars() -> tuple[DailyBar, ...]:
    bars: list[DailyBar] = []
    for offset in range(8):
        day = date(2026, 1, 2) + timedelta(days=offset)
        close = Decimal(110)
        ohlcv = Ohlcv(close, close, close, close, 1)
        bars.append(DailyBar("005930", day, ohlcv, ohlcv))
    return tuple(bars)


def _synthetic_points(bars: tuple[DailyBar, ...]) -> tuple[DailyIndicatorPoint, ...]:
    return tuple(
        DailyIndicatorPoint(
            stock_code=bar.stock_code,
            trade_date=bar.trade_date,
            daily_return=None,
            sma10=Decimal(110),
            sma20=Decimal(120),
            sma60=Decimal(130),
            ma20_slope_5=None,
            ma60_slope_5=None,
        )
        for bar in bars
    )


def _synthetic_wait(entry_type: str = "MA5_TURN") -> RescueWait:
    bars = _synthetic_bars()
    return RescueWait(
        setup=BoxSetup(
            "synthetic-setup",
            "005930",
            bars[0].trade_date,
            Decimal(100),
            Decimal(200),
            bars[0].trade_date,
            bars[0].trade_date,
        ),
        original_trade={"entry_type": entry_type},
        original_entry_type=entry_type,
        floor_exit_fill_date=bars[1].trade_date,
        floor_exit_fill_index=1,
        turn_dates=(bars[2].trade_date, bars[4].trade_date, bars[6].trade_date),
        turn_indexes=(2, 4, 6),
    )


def test_exact_turn_uses_non_strict_previous_comparison() -> None:
    bars = _synthetic_bars()
    sma5 = [
        Decimal(100),
        Decimal(101),
        Decimal(100),
        Decimal(100),
        Decimal(101),
        Decimal(101),
        Decimal(100),
        Decimal(102),
    ]
    # Index 4 is valid because SMA5[2] == SMA5[3] is allowed in the
    # preceding non-increasing step; index 5 is not a turn.
    assert _exact_turn_indexes(bars, 0, sma5) == (4, 7)


def test_candidate_requires_ma5_entry_component() -> None:
    bars = _synthetic_bars()
    points = _synthetic_points(bars)
    sma5 = [
        Decimal(100),
        Decimal(100),
        Decimal(101),
        Decimal(100),
        Decimal(101),
        Decimal(100),
        Decimal(101),
        Decimal(100),
    ]
    wait = _synthetic_wait("SMA10_REBREAK")
    valid, metadata = _candidate_conditions(
        wait=wait, index=4, bars=bars, points=points, sma5=sma5
    )
    assert valid is False
    assert metadata["rejection_reason"] == "ORIGINAL_ENTRY_HAS_NO_MA5_COMPONENT"


def test_candidate_is_only_second_turn_and_turn3_is_rejected() -> None:
    bars = _synthetic_bars()
    points = _synthetic_points(bars)
    sma5 = [Decimal(100)] * len(bars)
    wait = _synthetic_wait()
    valid, metadata = _candidate_conditions(
        wait=wait, index=4, bars=bars, points=points, sma5=sma5
    )
    assert valid is True
    assert metadata["turn_ordinal"] == 2
    valid, metadata = _candidate_conditions(
        wait=wait, index=6, bars=bars, points=points, sma5=sma5
    )
    assert valid is False
    assert metadata["reason"] == "NOT_SECOND_TURN"


def test_candidate_rejects_pre_exit_and_invalid_floor_or_upper_zone() -> None:
    bars = _synthetic_bars()
    points = _synthetic_points(bars)
    sma5 = [Decimal(100)] * len(bars)
    wait = _synthetic_wait()
    before_exit, metadata = _candidate_conditions(
        wait=replace(wait, floor_exit_fill_index=5),
        index=4,
        bars=bars,
        points=points,
        sma5=sma5,
    )
    assert before_exit is False
    assert metadata["reason"] == "TURN2_BEFORE_FLOOR_EXIT_FILL"

    below_floor = list(bars)
    below_floor[4] = DailyBar(
        "005930",
        below_floor[4].trade_date,
        below_floor[4].raw,
        Ohlcv(Decimal(90), Decimal(90), Decimal(90), Decimal(90), 1),
    )
    valid, metadata = _candidate_conditions(
        wait=wait, index=4, bars=below_floor, points=points, sma5=sma5
    )
    assert valid is False
    assert metadata["rejection_reason"] == "TURN2_CLOSE_BELOW_FLOOR"

    upper_zone_wait = replace(
        wait,
        setup=replace(wait.setup, box_upper=Decimal(110)),
    )
    valid, metadata = _candidate_conditions(
        wait=upper_zone_wait, index=4, bars=bars, points=points, sma5=sma5
    )
    assert valid is False
    assert metadata["rejection_reason"] == "TURN2_CLOSE_IN_UPPER_SELL_ZONE"


def test_turn_on_d11_is_outside_original_setup_window() -> None:
    bars = tuple(
        DailyBar(
            "005930",
            date(2026, 1, 2) + timedelta(days=index),
            Ohlcv(Decimal(100), Decimal(100), Decimal(100), Decimal(100), 1),
            Ohlcv(Decimal(100), Decimal(100), Decimal(100), Decimal(100), 1),
        )
        for index in range(12)
    )
    sma5 = [Decimal(100)] * 12
    sma5[9] = Decimal(99)
    sma5[10] = Decimal(99)
    sma5[11] = Decimal(101)
    assert _exact_turn_indexes(bars, 0, sma5) == ()


def test_campaign_classification_handles_incomplete_rescue() -> None:
    assert _campaign_classification(None, Decimal(-100)) == "INCOMPLETE"
    assert _campaign_classification(Decimal(100), Decimal(-100)) == "FULL_RECOVERY"
    assert _campaign_classification(Decimal(-50), Decimal(-100)) == "PARTIAL_RECOVERY"
    assert _campaign_classification(Decimal(-100), Decimal(-100)) == "NO_IMPROVEMENT"


def test_cached_expected_candidates_and_d10_t_plus_one_fill() -> None:
    expected = {
        "005380": ("2026-08-04", "MA5_TURN"),
        "012450": ("2023-10-25", "MA5_TURN"),
        "035420": ("2024-07-05", "BOTH"),
    }
    for stock_code, (signal_date, entry_type) in expected.items():
        bars, _ = _load_stock(stock_code)
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        result = _run_variant_b(daily_bars=bars, calendar=calendar)
        assert len(result["rescue_candidates"]) == 1
        candidate = result["rescue_candidates"][0]
        assert str(candidate["signal_date"]) == signal_date
        assert candidate["original_entry_type"] == entry_type
        assert candidate["setup_relative_day"] <= 10
        assert candidate["floor_reclaim"] is True
        assert candidate["close"] < candidate["upper_sell_level"]
        assert len(result["rescue_fills"]) == 1
        assert str(result["rescue_fills"][0]["signal_date"]) == signal_date
        assert (
            sum(fill["action"] == "HALF_EXIT" for fill in result["rescue_fills"]) <= 1
        )
        # A candidate is signalled once; no retry or TURN3 rescue is created.
        assert len(result["rescue_candidates"]) == 1


def test_variant_b_replay_is_deterministic_and_rescue_is_after_floor_fill() -> None:
    bars, _ = _load_stock("012450")
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
    first = _run_variant_b(daily_bars=bars, calendar=calendar)
    second = _run_variant_b(daily_bars=tuple(reversed(bars)), calendar=calendar)
    assert first == second
    candidate = first["rescue_candidates"][0]
    original_floor_fill = next(
        fill
        for fill in first["fills"]
        if fill["trade_leg"] == "ORIGINAL" and fill["action"] == "FULL_EXIT_FLOOR_BREAK"
    )
    rescue_fill = first["rescue_fills"][0]
    assert rescue_fill["filled_at"] > original_floor_fill["filled_at"]
    assert rescue_fill["filled_at"] > candidate["signal_date"]


def test_rescue_uses_frozen_box_and_only_floor_or_upper_management() -> None:
    bars, _ = _load_stock("035420")
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
    result = _run_variant_b(daily_bars=bars, calendar=calendar)
    candidate = result["rescue_candidates"][0]
    campaign_setup_id = candidate["setup_id"]
    original = next(
        trade
        for trade in result["completed_trades"]
        if trade["setup_id"] == campaign_setup_id and trade["trade_leg"] == "ORIGINAL"
    )
    assert candidate["box_floor"] == original["box_floor"]
    assert candidate["box_upper"] == original["box_upper"]
    assert original["box_floor"] < original["box_upper"]
    assert result["rescue_fills"][0]["source"] == "KIWOOM_KA10081_RAW"


def test_candidate_metadata_keeps_source_price_basis_distinct() -> None:
    bars, _ = _load_stock("012450")
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
    result = _run_variant_b(daily_bars=bars, calendar=calendar)
    assert result["accounting"]["signal_price_basis"] == "DAILY_ADJUSTED"
    assert result["accounting"]["execution_price_basis"] == "KA10081_RAW_DAILY_OPEN"
    assert result["accounting"]["completed_trade_pnl_basis"] == "REALIZED_ONLY"
    assert (
        result["accounting"]["cumulative_return_basis"]
        == "RESEARCH_END_MARK_TO_MARKET_RAW_CLOSE"
    )
    assert result["accounting"]["open_position_included_in_metrics"] is True
    assert all(fill["price_basis"] == "RAW" for fill in result["rescue_fills"])
