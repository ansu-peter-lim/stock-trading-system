from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.kiwoom_daily.daily_ma.core import confirmed_breakdown, confirmed_breakout
from src.kiwoom_daily.daily_ma_unified_buy_visual_proof_v0_2 import DailyMaPoint
from src.kiwoom_daily.daily_ma_unified_buy_visual_proof_v0_3 import (
    BuyType,
    MarketCapRecord,
    SizeBucket,
    accepted_signals,
    assign_market_cap_terciles,
    detect_buy_candidates,
    select_balanced_review_universe,
)


def d(value: str | int) -> Decimal:
    return Decimal(str(value))


def _point(
    index: int,
    *,
    close: str = "100",
    run10: int = 0,
    run20: int = 0,
    ma5_event: bool = False,
    ma10_event: bool = False,
    slope: str = "0",
) -> DailyMaPoint:
    return DailyMaPoint(
        trade_date=date(2024, 1, 1) + timedelta(days=index),
        close=d(close),
        ma5=d(100),
        ma10=d(100),
        ma20=d(100),
        ma60=d(100),
        ma120=d(100),
        ma10_slope10_pct=d(slope),
        ma20_slope10_pct=d(0),
        ma60_slope10_pct=d(0),
        ma10_below_run=run10,
        ma20_below_run=run20,
        ma5_inflection_state=ma5_event,
        ma5_inflection_event=ma5_event,
        ma10_inflection_state=ma10_event,
        ma10_inflection_event=ma10_event,
        ma10_breakout=False,
        ma20_breakout=False,
        ma10_breakdown=False,
    )


def _a_points(*, slope: str = "0") -> list[DailyMaPoint]:
    points = [_point(index, close="98", run10=index + 1) for index in range(10)]
    points[-1] = _point(9, close="98", run10=10, ma5_event=True)
    points.append(_point(10, close="101", slope=slope))
    return points


def _b_episode(breakdown_offset: int, reclaim_offset: int | None) -> list[DailyMaPoint]:
    points = _a_points()
    final_offset = max(breakdown_offset, reclaim_offset or breakdown_offset)
    for offset in range(1, final_offset + 1):
        close = "100"
        if offset == breakdown_offset:
            close = "99"
        if reclaim_offset is not None and offset == reclaim_offset:
            close = "101"
        points.append(_point(10 + offset, close=close))
    return points


def _b_signals(points: list[DailyMaPoint]):
    return tuple(
        candidate
        for candidate in detect_buy_candidates("005930", points)
        if candidate.buy_type is BuyType.B
    )


def test_one_percent_cross_boundaries_and_earlier_detection() -> None:
    assert confirmed_breakout(d(101), d(100), d("100.99"), d(100), ratio=d("1.01"))
    assert not confirmed_breakout(
        d("100.999"), d(100), d("100.99"), d(100), ratio=d("1.01")
    )
    assert confirmed_breakdown(d(99), d(100), d("99.01"), d(100), ratio=d("0.99"))
    assert not confirmed_breakdown(
        d("99.001"), d(100), d("99.01"), d(100), ratio=d("0.99")
    )
    assert confirmed_breakout(d("101.5"), d(100), d(100), d(100), ratio=d("1.01"))
    assert not confirmed_breakout(d("101.5"), d(100), d(100), d(100))


def test_buy_a_and_c_use_one_percent_with_unchanged_guard() -> None:
    assert not detect_buy_candidates("005930", _a_points()[:-1])
    accepted_a = accepted_signals(detect_buy_candidates("005930", _a_points()))
    assert [signal.buy_type for signal in accepted_a] == [BuyType.A]
    rejected = detect_buy_candidates("005930", _a_points(slope="-20.01"))
    assert len(rejected) == 1 and not rejected[0].slope_guard_pass
    boundary = detect_buy_candidates("005930", _a_points(slope="-20"))
    assert boundary[0].slope_guard_pass

    points = [_point(index, close="98", run20=index + 1) for index in range(15)]
    points[-1] = _point(14, close="98", run20=15, ma10_event=True)
    points.append(_point(15, close="101"))
    assert (
        accepted_signals(detect_buy_candidates("005930", points))[0].buy_type
        is BuyType.C
    )
    original = detect_buy_candidates("005930", points)
    assert (
        detect_buy_candidates("005930", (*points, _point(16, close="100")))[:1]
        == original
    )


@pytest.mark.parametrize(
    ("breakdown_offset", "reclaim_offset"),
    ((1, 2), (4, 5), (9, 10)),
)
def test_buy_b_complete_sequence_anywhere_inside_ten_bar_episode(
    breakdown_offset: int, reclaim_offset: int
) -> None:
    signals = _b_signals(_b_episode(breakdown_offset, reclaim_offset))
    assert len(signals) == 1
    assert signals[0].bars_since_parent_a == reclaim_offset
    assert signals[0].parent_buy_a_date == date(2024, 1, 11)
    assert signals[0].parent_buy_a_signal_id is not None


def test_buy_b_episode_expiry_and_chronology() -> None:
    assert not _b_signals(_b_episode(10, None))
    assert not _b_signals(_b_episode(11, 12))
    assert not _b_signals(_b_episode(9, 11))
    assert not _b_signals(_a_points())

    reclaim_before_breakdown = _a_points()
    reclaim_before_breakdown.extend(
        (_point(11, close="101"), _point(12, close="99"), _point(13, close="100"))
    )
    assert not _b_signals(reclaim_before_breakdown)

    same_bar_impossible = _a_points()
    same_bar_impossible.append(_point(11, close="99"))
    assert not _b_signals(same_bar_impossible)

    no_parent = (
        _point(0, close="100"),
        _point(1, close="99"),
        _point(2, close="101"),
    )
    assert not _b_signals(list(no_parent))


def test_buy_b_reclaim_applies_slope_guard() -> None:
    points = _b_episode(1, 2)
    points[-1] = _point(12, close="101", slope="-20.01")
    candidate = _b_signals(points)[0]
    assert not candidate.slope_guard_pass
    assert candidate not in accepted_signals((candidate,))


def test_multiple_parents_choose_most_recent_and_do_not_duplicate_b() -> None:
    points = _a_points()
    points.append(_point(11, close="100", run10=10, ma5_event=True))
    points.append(_point(12, close="101"))
    points.append(_point(13, close="99"))
    points.append(_point(14, close="101"))
    candidates = detect_buy_candidates("005930", points)
    parents = tuple(item for item in candidates if item.buy_type is BuyType.A)
    signals_b = tuple(item for item in candidates if item.buy_type is BuyType.B)
    assert len(parents) == 2
    assert len(signals_b) == 1
    assert signals_b[0].parent_buy_a_signal_id == parents[-1].signal_id
    assert signals_b[0].other_qualifying_parent_ids == (parents[0].signal_id,)


def test_market_cap_terciles_and_selection_are_deterministic() -> None:
    records = tuple(
        MarketCapRecord(f"{index:06d}", 1200 - index * 100, date(2026, 8, 28))
        for index in range(9)
    )
    bucketed = assign_market_cap_terciles(tuple(reversed(records)))
    assert [row.bucket for row in bucketed] == [
        *(SizeBucket.LARGE for _ in range(3)),
        *(SizeBucket.MID for _ in range(3)),
        *(SizeBucket.SMALL for _ in range(3)),
    ]
    selected = select_balanced_review_universe(tuple(reversed(records)), per_bucket=2)
    assert [(row.bucket, row.stock_code) for row in selected] == [
        (SizeBucket.LARGE, "000000"),
        (SizeBucket.LARGE, "000002"),
        (SizeBucket.MID, "000003"),
        (SizeBucket.MID, "000005"),
        (SizeBucket.SMALL, "000006"),
        (SizeBucket.SMALL, "000008"),
    ]
