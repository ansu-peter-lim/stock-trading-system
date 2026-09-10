from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.kiwoom_daily.daily_ma_unified_buy_visual_proof_v0_2 import (
    BuyType,
    DailyMaPoint,
    accepted_signals,
    assert_no_future_outcome_fields,
    detect_buy_candidates,
)


def d(value: str | int) -> Decimal:
    return Decimal(str(value))


def _point(
    index: int,
    *,
    run10: int = 0,
    run20: int = 0,
    ma5_event: bool = False,
    ma10_event: bool = False,
    ma10_breakout: bool = False,
    ma20_breakout: bool = False,
    ma10_breakdown: bool = False,
    slope: str = "0",
) -> DailyMaPoint:
    return DailyMaPoint(
        trade_date=date(2024, 1, 1) + timedelta(days=index),
        close=d(102),
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
        ma10_breakout=ma10_breakout,
        ma20_breakout=ma20_breakout,
        ma10_breakdown=ma10_breakdown,
    )


def _a_points(*, below_run: int = 10, slope: str = "0") -> list[DailyMaPoint]:
    points = [_point(index, run10=index + 1) for index in range(below_run)]
    points[-1] = _point(below_run - 1, run10=below_run, ma5_event=True)
    points.append(_point(below_run, ma10_breakout=True, slope=slope))
    return points


def test_buy_a_requires_ten_below_bars_and_inflection_after_completion() -> None:
    assert not detect_buy_candidates("005930", _a_points(below_run=9))
    accepted = accepted_signals(detect_buy_candidates("005930", _a_points()))
    assert len(accepted) == 1
    assert accepted[0].buy_type is BuyType.A
    early = [_point(index, run10=index + 1) for index in range(10)]
    early[8] = _point(8, run10=9, ma5_event=True)
    early.append(_point(10, ma10_breakout=True))
    assert not detect_buy_candidates("005930", early)


def test_buy_a_slope_guard_is_inclusive_and_no_future_signal_is_needed() -> None:
    rejected = detect_buy_candidates("005930", _a_points(slope="-20.01"))
    assert len(rejected) == 1 and not rejected[0].slope_guard_pass
    accepted = detect_buy_candidates("005930", _a_points(slope="-20"))
    assert accepted[0].slope_guard_pass
    prefix = _a_points()[:-1]
    assert not detect_buy_candidates("005930", prefix)


def test_buy_b_requires_accepted_parent_failure_within_three_then_reclaim() -> None:
    points = _a_points()
    points.append(_point(11, ma10_breakdown=True))
    points.append(_point(12, ma10_breakout=True))
    candidates = detect_buy_candidates("005930", points)
    b = next(candidate for candidate in candidates if candidate.buy_type is BuyType.B)
    assert b.parent_buy_a_id is not None
    assert b.parent_buy_a_date == date(2024, 1, 11)
    assert b.bars_since_breakdown == 1
    assert len(accepted_signals(candidates)) == 2


def test_buy_b_three_bar_boundary_and_no_parent_or_late_failure_reject() -> None:
    no_parent = [_point(0, ma10_breakdown=True), _point(1, ma10_breakout=True)]
    assert not detect_buy_candidates("005930", no_parent)
    points = _a_points()
    points.extend((_point(11), _point(12), _point(13, ma10_breakdown=True)))
    points.append(_point(14, ma10_breakout=True))
    assert any(
        candidate.buy_type is BuyType.B
        for candidate in detect_buy_candidates("005930", points)
    )
    late = _a_points()
    late.extend((_point(11), _point(12), _point(13), _point(14, ma10_breakdown=True)))
    late.append(_point(15, ma10_breakout=True))
    assert all(
        candidate.buy_type is not BuyType.B
        for candidate in detect_buy_candidates("005930", late)
    )


def test_buy_c_requires_fifteen_below_ma20_and_ma10_inflection() -> None:
    insufficient = [_point(index, run20=index + 1) for index in range(14)]
    insufficient[-1] = _point(13, run20=14, ma10_event=True)
    insufficient.append(_point(14, ma20_breakout=True))
    assert not detect_buy_candidates("005930", insufficient)
    points = [_point(index, run20=index + 1) for index in range(15)]
    points[-1] = _point(14, run20=15, ma10_event=True)
    points.append(_point(15, ma20_breakout=True))
    signal = accepted_signals(detect_buy_candidates("005930", points))[0]
    assert signal.buy_type is BuyType.C
    assert signal.ma10_inflection_date == date(2024, 1, 15)


def test_outcome_fields_are_forbidden() -> None:
    assert_no_future_outcome_fields({"signals": []})
    try:
        assert_no_future_outcome_fields({"pnl": 1})
    except ValueError as error:
        assert "future-outcome" in str(error)
    else:
        raise AssertionError("outcome field must be rejected")
