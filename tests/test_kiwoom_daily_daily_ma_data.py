from __future__ import annotations

from datetime import date

import pytest

from src.backtest_engine.validation import MarketDataValidationError
from src.kiwoom_daily.daily_ma.data import load_local_daily_bars, slice_daily_bars
from src.kiwoom_minute.small_up_path_proof import _load_existing_daily_bars

STOCKS = (
    "000660",
    "005380",
    "005930",
    "012450",
    "034020",
    "035420",
    "035720",
    "066570",
    "068270",
    "105560",
    "207940",
)
START = date(2023, 6, 1)
BASE = date(2026, 8, 31)


@pytest.mark.parametrize("stock_code", STOCKS)
def test_new_local_boundary_matches_frozen_loader(stock_code: str) -> None:
    assert load_local_daily_bars(stock_code, START, BASE) == _load_existing_daily_bars(
        stock_code
    )


def test_slice_is_inclusive_canonical_and_has_no_network() -> None:
    bars = load_local_daily_bars("005930", START, BASE)
    sliced = slice_daily_bars(
        tuple(reversed(bars)), date(2024, 1, 2), date(2024, 1, 10)
    )
    assert sliced == tuple(sorted(sliced, key=lambda bar: bar.trade_date))
    assert all(
        date(2024, 1, 2) <= bar.trade_date <= date(2024, 1, 10) for bar in sliced
    )


def test_duplicate_dates_are_rejected_before_slice() -> None:
    bars = load_local_daily_bars("005930", START, BASE)
    with pytest.raises(MarketDataValidationError):
        slice_daily_bars((bars[0], bars[0]), START, BASE)
