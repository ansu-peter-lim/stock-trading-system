from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from src.backtest_engine.events import stable_id
from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.backtest_engine.validation import KOREA_TZ
from src.kiwoom_minute import ASSUMPTION_ID, MinuteSourceBar
from src.kiwoom_minute.down_path_proof import run_down_path_sequence_proof

D = Decimal


def daily_fixture() -> tuple[list[DailyBar], list[date]]:
    start = date(2024, 1, 1)
    days = [start + timedelta(days=index) for index in range(74)]
    bars = []
    for index in range(70):
        close = D(200 - index)
        value = Ohlcv(close + 1, close + 2, close - 1, close, 100)
        bars.append(DailyBar("005930", days[index], value, value))
    for index, values in enumerate(
        (
            ("135", "180", "130", "139"),
            ("110", "115", "95", "100"),
            ("100", "101", "99", "100"),
        ),
        70,
    ):
        open_, high, low, close = (D(value) for value in values)
        value = Ohlcv(open_, high, low, close, 100)
        bars.append(DailyBar("005930", days[index], value, value))
    return bars, days


def source_bar(sequence: int, at: datetime, close: str, raw_open: str = "100"):
    signal_close = D(close)
    signal = Ohlcv(signal_close, signal_close + 1, signal_close - 1, signal_close, 100)
    raw_value = D(raw_open)
    raw = Ohlcv(raw_value, raw_value + 1, raw_value - 1, raw_value, 100)
    label = at.strftime("%Y%m%d%H%M%S")
    return MinuteSourceBar(
        "005930",
        label,
        at,
        at.date(),
        sequence,
        stable_id(ASSUMPTION_ID, "005930", label),
        raw,
        signal,
    )


def source_fixture(days: list[date], *, executable: bool = True):
    bars = []
    warmup = datetime.combine(days[69], time(9), tzinfo=KOREA_TZ)
    for index in range(60):
        close = str(100 + index) if executable else "100"
        bars.append(source_bar(index, warmup + timedelta(minutes=5 * index), close))
    entry = datetime.combine(days[71], time(9), tzinfo=KOREA_TZ)
    if executable:
        bars.extend(
            (
                source_bar(60, entry, "160"),
                source_bar(61, entry + timedelta(minutes=5), "161", "100"),
            )
        )
        exit_at = datetime.combine(days[72], time(9), tzinfo=KOREA_TZ)
        bars.extend(
            (
                source_bar(62, exit_at, "50"),
                source_bar(63, exit_at + timedelta(minutes=5), "51", "90"),
            )
        )
    else:
        bars.append(source_bar(60, entry, "90"))
    return bars


def run_fixture(*, executable: bool = True, reverse: bool = False):
    daily, days = daily_fixture()
    source = source_fixture(days, executable=executable)
    if reverse:
        daily.reverse()
        source.reverse()
    return run_down_path_sequence_proof(
        daily_bars=daily,
        source_bars=source,
        calendar=ExplicitTradingCalendar(days),
        research_start=days[70],
        research_end=days[72],
        stock_full_weight=D("0.10"),
        initial_capital=D("100000"),
    )


class DownSequenceProofTests(unittest.TestCase):
    def test_candidate_uses_t_plus_one_execution_next_row_fill_and_exit_c(self) -> None:
        result = run_fixture()
        self.assertEqual(1, result["counts"]["daily_buy_candidates"])
        self.assertEqual(1, result["counts"]["entry_fills"])
        self.assertEqual(1, result["counts"]["daily_full_exit_signals"])
        self.assertEqual(1, result["counts"]["exit_c_triggers"])
        self.assertEqual(1, result["counts"]["exit_fills"])
        trade = result["completed_trades"][0]
        self.assertEqual("REVERSAL_FIVE_TO_TEN", trade["entry_branch"])
        self.assertEqual(days := date(2024, 3, 11), trade["entry_daily_signal_date"])
        self.assertEqual(days + timedelta(days=1), trade["entry_fill_date"])
        self.assertGreater(trade["entry_fill_sequence"], 60)
        self.assertEqual("20240312090000", trade["entry_execution_source_label"])
        self.assertEqual("20240312090500", trade["entry_fill_source_label"])
        self.assertEqual("20240313090000", trade["exit_c_source_label"])
        self.assertEqual("20240313090500", trade["exit_fill_source_label"])

    def test_enter_expires_without_intraday_condition_or_synthetic_fill(self) -> None:
        result = run_fixture(executable=False)
        self.assertEqual(1, result["counts"]["pending_entries"])
        self.assertEqual(1, result["counts"]["entry_expirations"])
        self.assertEqual(0, result["counts"]["entry_fills"])
        self.assertEqual([], result["fills"])

    def test_input_permutation_does_not_change_stateful_result(self) -> None:
        self.assertEqual(run_fixture(), run_fixture(reverse=True))


if __name__ == "__main__":
    unittest.main()
