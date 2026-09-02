from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.core_strategy import DailyCoreSignalType
from src.backtest_engine.down_strategy import (
    DownBlockReason,
    DownDailySignalGenerator,
    DownEntryBranch,
    DownRiseBranch,
    SurgeSetupEventType,
    analyze_down_entry,
    down_block_reasons,
)
from src.backtest_engine.indicators import DailyIndicatorPoint
from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar

D = Decimal


def value(open_: str, close: str, high: str | None = None, low: str | None = None):
    open_value = D(open_)
    close_value = D(close)
    return Ohlcv(
        open_value,
        D(high) if high is not None else max(open_value, close_value) + 1,
        D(low) if low is not None else min(open_value, close_value) - 1,
        close_value,
        100,
    )


def fixture(
    *,
    t_close: str = "105",
    t_open: str = "103",
    t_high: str = "160",
    t_low: str = "102",
    t_sma10: str = "104",
    slope20: str = "-1",
    slope60: str = "-1",
    sma20: str = "150",
    sma60: str = "180",
):
    start = date(2024, 1, 1)
    bars = [
        DailyBar(
            "005930", start + timedelta(days=i), value("99", "100"), value("99", "100")
        )
        for i in range(10)
    ]
    bars.append(
        DailyBar(
            "005930",
            start + timedelta(days=10),
            value(t_open, t_close, t_high, t_low),
            value(t_open, t_close, t_high, t_low),
        )
    )
    points = [
        DailyIndicatorPoint(
            "005930",
            bar.trade_date,
            D(0),
            D("101"),
            D("150"),
            D("180"),
            D("-1"),
            D("-1"),
        )
        for bar in bars[:-1]
    ]
    points.append(
        DailyIndicatorPoint(
            "005930",
            bars[-1].trade_date,
            D(0),
            D(t_sma10),
            D(sma20),
            D(sma60),
            D(slope20),
            D(slope60),
        )
    )
    return bars, points


def append_day(
    bars: list[DailyBar],
    points: list[DailyIndicatorPoint],
    *,
    open_: str = "89",
    close: str = "90",
    high: str = "120",
    low: str = "80",
    sma10: str = "100",
    slope20: str = "-1",
    slope60: str = "-1",
):
    day = bars[-1].trade_date + timedelta(days=1)
    ohlcv = value(open_, close, high, low)
    bars.append(DailyBar("005930", day, ohlcv, ohlcv))
    points.append(
        DailyIndicatorPoint(
            "005930", day, D(0), D(sma10), D("150"), D("180"), D(slope20), D(slope60)
        )
    )


class DownEntryFactsTests(unittest.TestCase):
    def test_down_context_breakout_and_t_is_not_part_of_prior_ten(self) -> None:
        bars, points = fixture()
        facts = analyze_down_entry(tuple(bars), tuple(points), 10)
        self.assertTrue(facts.down_trend)
        self.assertTrue(facts.prior_ten_below_sma10)
        self.assertTrue(facts.sma10_breakout)
        self.assertEqual(DownRiseBranch.FIVE_TO_TEN, facts.rise_branch)

    def test_context_uses_exactly_previous_ten_sessions(self) -> None:
        bars, points = fixture()
        old_bar = DailyBar(
            "005930",
            bars[0].trade_date - timedelta(days=1),
            value("199", "200"),
            value("199", "200"),
        )
        old_point = DailyIndicatorPoint(
            "005930",
            old_bar.trade_date,
            D(0),
            D("100"),
            D("150"),
            D("180"),
            D("-1"),
            D("-1"),
        )
        facts = analyze_down_entry((old_bar, *bars), (old_point, *points), 11)
        self.assertTrue(facts.prior_ten_below_sma10)

    def test_one_prior_close_at_sma10_rejects_context(self) -> None:
        bars, points = fixture()
        points[4] = DailyIndicatorPoint(
            "005930",
            bars[4].trade_date,
            D(0),
            D("100"),
            D("150"),
            D("180"),
            D("-1"),
            D("-1"),
        )
        self.assertFalse(
            analyze_down_entry(tuple(bars), tuple(points), 10).prior_ten_below_sma10
        )

    def test_breakout_is_strictly_close_above_sma10(self) -> None:
        bars, points = fixture(t_close="104", t_sma10="104")
        self.assertFalse(
            analyze_down_entry(tuple(bars), tuple(points), 10).sma10_breakout
        )

    def test_exact_five_and_ten_percent_are_basic_branch(self) -> None:
        for close in ("105", "110"):
            with self.subTest(close=close):
                bars, points = fixture(t_close=close)
                facts = analyze_down_entry(tuple(bars), tuple(points), 10)
                self.assertEqual(DownRiseBranch.FIVE_TO_TEN, facts.rise_branch)
                calendar = ExplicitTradingCalendar(
                    [bar.trade_date for bar in bars]
                    + [bars[-1].trade_date + timedelta(days=1)]
                )
                signal = (
                    DownDailySignalGenerator(calendar)
                    .evaluate(
                        tuple(bars),
                        tuple(points),
                        10,
                        holding_core=False,
                        entry_allowed=True,
                        stock_full_weight=D("0.10"),
                    )
                    .signal
                )
                self.assertIsNotNone(signal)
                assert signal is not None
                self.assertEqual(DownEntryBranch.REVERSAL, signal.entry_branch)

    def test_below_five_without_soldiers_has_no_entry(self) -> None:
        bars, points = fixture(t_close="104", t_sma10="103")
        calendar = ExplicitTradingCalendar(
            [bar.trade_date for bar in bars] + [bars[-1].trade_date + timedelta(days=1)]
        )
        decision = DownDailySignalGenerator(calendar).evaluate(
            tuple(bars),
            tuple(points),
            10,
            holding_core=False,
            entry_allowed=True,
            stock_full_weight=D("0.10"),
        )
        self.assertIsNone(decision.signal)

    def test_below_five_requires_red_three_soldiers(self) -> None:
        bars, points = fixture(t_close="104", t_sma10="103")
        for position, (open_, close) in zip(
            (8, 9), (("95", "96"), ("99", "100")), strict=True
        ):
            ohlcv = value(open_, close)
            bars[position] = DailyBar("005930", bars[position].trade_date, ohlcv, ohlcv)
        facts = analyze_down_entry(tuple(bars), tuple(points), 10)
        self.assertEqual(DownRiseBranch.BELOW_FIVE, facts.rise_branch)
        self.assertTrue(facts.red_three_soldiers)

        calendar = ExplicitTradingCalendar(
            [bar.trade_date for bar in bars] + [bars[-1].trade_date + timedelta(days=1)]
        )
        signal = (
            DownDailySignalGenerator(calendar)
            .evaluate(
                tuple(bars),
                tuple(points),
                10,
                holding_core=False,
                entry_allowed=True,
                stock_full_weight=D("0.10"),
            )
            .signal
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(DownEntryBranch.RED_THREE_SOLDIERS, signal.entry_branch)

    def test_bearish_or_non_increasing_three_bars_reject_soldiers(self) -> None:
        cases = (("101", "100", "bearish"), ("95", "96", "non-increasing"))
        for open_, close, label in cases:
            with self.subTest(label=label):
                bars, points = fixture(t_close="104", t_sma10="103")
                for position, pair in zip(
                    (8, 9), (("95", "96"), ("99", "100")), strict=True
                ):
                    ohlcv = value(*pair)
                    bars[position] = DailyBar(
                        "005930", bars[position].trade_date, ohlcv, ohlcv
                    )
                ohlcv = value(open_, close)
                bars[9] = DailyBar("005930", bars[9].trade_date, ohlcv, ohlcv)
                self.assertFalse(
                    analyze_down_entry(
                        tuple(bars), tuple(points), 10
                    ).red_three_soldiers
                )


class DownBlockingFilterTests(unittest.TestCase):
    def test_steep_slope_boundary_and_below_block(self) -> None:
        for slope in ("-5", "-5.01"):
            bars, points = fixture(slope20=slope)
            self.assertIn(
                DownBlockReason.STEEP_MA20,
                analyze_down_entry(tuple(bars), tuple(points), 10).block_reasons,
            )

    def test_ma20_and_ma60_resistance_are_independent(self) -> None:
        cases = (
            ("110", "180", DownBlockReason.MA20_RESISTANCE),
            ("150", "110", DownBlockReason.MA60_RESISTANCE),
        )
        for sma20, sma60, expected in cases:
            with self.subTest(expected=expected):
                bars, points = fixture(t_high="108", sma20=sma20, sma60=sma60)
                self.assertIn(expected, down_block_reasons(bars[-1], points[-1]))

    def test_resistance_high_band_boundaries_are_inclusive(self) -> None:
        for high in ("106.7", "113.3"):
            bars, points = fixture(t_high=high, sma20="110")
            self.assertIn(
                DownBlockReason.MA20_RESISTANCE,
                down_block_reasons(bars[-1], points[-1]),
            )

    def test_close_at_or_above_ma_does_not_block(self) -> None:
        bars, points = fixture(t_close="110", t_high="110", sma20="110")
        self.assertNotIn(
            DownBlockReason.MA20_RESISTANCE, down_block_reasons(bars[-1], points[-1])
        )

    def test_each_filter_prevents_basic_buy_signal(self) -> None:
        cases = (
            {"slope20": "-5"},
            {"t_high": "108", "sma20": "110"},
            {"t_high": "108", "sma60": "110"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                bars, points = fixture(**changes)
                calendar = ExplicitTradingCalendar(
                    [bar.trade_date for bar in bars]
                    + [bars[-1].trade_date + timedelta(days=1)]
                )
                decision = DownDailySignalGenerator(calendar).evaluate(
                    tuple(bars),
                    tuple(points),
                    10,
                    holding_core=False,
                    entry_allowed=True,
                    stock_full_weight=D("0.10"),
                )
                self.assertIsNone(decision.signal)


class SurgePullbackStateTests(unittest.TestCase):
    def origin(self):
        bars, points = fixture(t_close="111", t_sma10="105")
        calendar_days = [bars[0].trade_date + timedelta(days=i) for i in range(30)]
        generator = DownDailySignalGenerator(ExplicitTradingCalendar(calendar_days))
        decision = generator.evaluate(
            tuple(bars),
            tuple(points),
            10,
            holding_core=False,
            entry_allowed=True,
            stock_full_weight=D("0.10"),
        )
        self.assertIsNone(decision.signal)
        self.assertEqual(
            [SurgeSetupEventType.CREATED],
            [event.event_type for event in decision.setup_events],
        )
        self.assertEqual(0, decision.active_setup.sessions_elapsed)
        return bars, points, generator

    def test_d1_low_touch_activates_but_origin_day_never_does(self) -> None:
        bars, points, generator = self.origin()
        append_day(bars, points, low="100", close="105")
        decision = generator.evaluate(
            tuple(bars),
            tuple(points),
            11,
            holding_core=False,
            entry_allowed=True,
            stock_full_weight=D("0.10"),
        )
        self.assertEqual(DownEntryBranch.SURGE_PULLBACK, decision.signal.entry_branch)
        self.assertIn(
            SurgeSetupEventType.TOUCHED,
            [event.event_type for event in decision.setup_events],
        )

    def test_close_touch_is_accepted(self) -> None:
        bars, points, generator = self.origin()
        append_day(bars, points, low="80", close="100")
        decision = generator.evaluate(
            tuple(bars),
            tuple(points),
            11,
            holding_core=False,
            entry_allowed=True,
            stock_full_weight=D("0.10"),
        )
        self.assertIsNotNone(decision.signal)

    def test_d10_touch_is_valid_and_d11_has_expired(self) -> None:
        bars, points, generator = self.origin()
        for _ in range(9):
            append_day(bars, points, open_="95", close="96")
            decision = generator.evaluate(
                tuple(bars),
                tuple(points),
                len(bars) - 1,
                holding_core=False,
                entry_allowed=True,
                stock_full_weight=D("0.10"),
            )
            self.assertIsNone(decision.signal)
        append_day(bars, points, open_="99", low="98", close="100")
        decision = generator.evaluate(
            tuple(bars),
            tuple(points),
            len(bars) - 1,
            holding_core=False,
            entry_allowed=True,
            stock_full_weight=D("0.10"),
        )
        self.assertIsNotNone(decision.signal)

        bars, points, generator = self.origin()
        for _ in range(10):
            append_day(bars, points)
            decision = generator.evaluate(
                tuple(bars),
                tuple(points),
                len(bars) - 1,
                holding_core=False,
                entry_allowed=True,
                stock_full_weight=D("0.10"),
            )
        self.assertIn(
            SurgeSetupEventType.EXPIRED,
            [event.event_type for event in decision.setup_events],
        )
        append_day(bars, points, low="100", close="105")
        decision = generator.evaluate(
            tuple(bars),
            tuple(points),
            len(bars) - 1,
            holding_core=False,
            entry_allowed=True,
            stock_full_weight=D("0.10"),
        )
        self.assertIsNone(decision.signal)

    def test_touch_with_current_filter_failure_does_not_enter(self) -> None:
        bars, points, generator = self.origin()
        append_day(bars, points, low="100", close="105", slope20="-5")
        decision = generator.evaluate(
            tuple(bars),
            tuple(points),
            11,
            holding_core=False,
            entry_allowed=True,
            stock_full_weight=D("0.10"),
        )
        self.assertIsNone(decision.signal)
        touched = next(
            event
            for event in decision.setup_events
            if event.event_type is SurgeSetupEventType.TOUCHED
        )
        self.assertIn(DownBlockReason.STEEP_MA20, touched.block_reasons)

    def test_newer_active_surge_supersedes_old_setup(self) -> None:
        bars, points, generator = self.origin()
        append_day(
            bars, points, open_="112", close="130", high="160", low="110", sma10="120"
        )
        decision = generator.evaluate(
            tuple(bars),
            tuple(points),
            11,
            holding_core=False,
            entry_allowed=True,
            stock_full_weight=D("0.10"),
        )
        self.assertEqual(
            [SurgeSetupEventType.SUPERSEDED, SurgeSetupEventType.CREATED],
            [event.event_type for event in decision.setup_events],
        )
        self.assertEqual(bars[-1].trade_date, decision.active_setup.origin_trade_date)

    def test_future_suffix_does_not_change_origin_decision(self) -> None:
        bars, points = fixture(t_close="111", t_sma10="105")
        days = [bars[0].trade_date + timedelta(days=i) for i in range(30)]
        prefix = DownDailySignalGenerator(ExplicitTradingCalendar(days)).evaluate(
            tuple(bars),
            tuple(points),
            10,
            holding_core=False,
            entry_allowed=True,
            stock_full_weight=D("0.10"),
        )
        append_day(bars, points, low="100", close="105")
        full = DownDailySignalGenerator(ExplicitTradingCalendar(days)).evaluate(
            tuple(bars),
            tuple(points),
            10,
            holding_core=False,
            entry_allowed=True,
            stock_full_weight=D("0.10"),
        )
        self.assertEqual(prefix, full)

    def test_holding_close_below_sma10_creates_full_exit_not_entry(self) -> None:
        bars, points = fixture(t_close="99", t_sma10="100")
        days = [bar.trade_date for bar in bars] + [
            bars[-1].trade_date + timedelta(days=1)
        ]
        decision = DownDailySignalGenerator(ExplicitTradingCalendar(days)).evaluate(
            tuple(bars),
            tuple(points),
            10,
            holding_core=True,
            entry_allowed=False,
            stock_full_weight=D("0.10"),
        )
        self.assertEqual(DailyCoreSignalType.FULL_EXIT, decision.signal.signal_type)
        self.assertIsNone(decision.signal.entry_branch)

    def test_holding_position_never_creates_duplicate_down_entry(self) -> None:
        bars, points = fixture()
        days = [bar.trade_date for bar in bars] + [
            bars[-1].trade_date + timedelta(days=1)
        ]
        decision = DownDailySignalGenerator(ExplicitTradingCalendar(days)).evaluate(
            tuple(bars),
            tuple(points),
            10,
            holding_core=True,
            entry_allowed=False,
            stock_full_weight=D("0.10"),
        )
        self.assertIsNone(decision.signal)


if __name__ == "__main__":
    unittest.main()
