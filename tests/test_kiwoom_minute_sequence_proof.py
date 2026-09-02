from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from src.backtest_engine.events import stable_id
from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.backtest_engine.validation import KOREA_TZ
from src.kiwoom_minute import (
    ASSUMPTION_ID,
    CollectedMinuteSeries,
    KiwoomMinuteStore,
    MinuteCollectionRequest,
    MinutePipelineIssue,
    MinutePriceBasis,
    MinuteSourceBar,
    MinuteValidationError,
    align_source_bars,
    collect_minute_series,
    parse_minute_page,
    run_up_path_sequence_proof,
)
from src.kiwoom_minute.pipeline import ParsedMinuteRow
from src.kiwoom_rest.auth import DemoConfig, TokenInfo
from src.kiwoom_rest.market_data_pilot import ChartHttpResult

D = Decimal


def response(rows: list[dict[str, str]], stock_code: str = "005930") -> bytes:
    return json.dumps(
        {
            "stk_cd": stock_code,
            "stk_min_pole_chart_qry": rows,
            "return_code": 0,
            "return_msg": "ok",
        }
    ).encode()


def row(label: str, price: str = "+100", volume: str = "10") -> dict[str, str]:
    return {
        "cntr_tm": label,
        "open_pric": price,
        "high_pric": price,
        "low_pric": price,
        "cur_prc": price,
        "trde_qty": volume,
    }


class Ka10080ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = MinuteCollectionRequest(
            "005930", date(2026, 8, 28), date(2026, 8, 28), MinutePriceBasis.ADJUSTED
        )

    def parse(self, rows: list[dict[str, str]]):
        raw = response(rows)
        return parse_minute_page(
            raw,
            self.request,
            source_page=1,
            artifact_sha256=hashlib.sha256(raw).hexdigest(),
        )

    def test_source_specific_sign_normalization_preserves_raw_text(self) -> None:
        rows = [
            row("20260828090000", "+71200"),
            row("20260828090500", "-71200"),
            row("20260828091000", "71200"),
        ]
        parsed = self.parse(rows)
        self.assertEqual([D("71200")] * 3, [item.raw.close for item in parsed.rows])
        self.assertEqual(
            ["+71200", "-71200", "71200"],
            [item.source_price_text[3] for item in parsed.rows],
        )

    def test_malformed_sign_and_signed_volume_are_rejected(self) -> None:
        for bad in ("++100", "--100", "10-0", ""):
            with (
                self.subTest(price=bad),
                self.assertRaises(MinuteValidationError) as caught,
            ):
                self.parse([row("20260828090000", bad)])
            self.assertEqual(
                MinutePipelineIssue.MALFORMED_PRICE, caught.exception.issue
            )
        with self.assertRaises(MinuteValidationError) as caught:
            self.parse([row("20260828090000", "+100", "+10")])
        self.assertEqual(MinutePipelineIssue.MALFORMED_VOLUME, caught.exception.issue)

        non_string = row("20260828090000")
        non_string["cur_prc"] = 100  # type: ignore[assignment]
        with self.assertRaises(MinuteValidationError) as caught:
            self.parse([non_string])
        self.assertEqual(MinutePipelineIssue.MALFORMED_PRICE, caught.exception.issue)

    def test_duplicate_source_label_is_rejected(self) -> None:
        with self.assertRaises(MinuteValidationError) as caught:
            self.parse([row("20260828090000"), row("20260828090000")])
        self.assertEqual(
            MinutePipelineIssue.DUPLICATE_SOURCE_LABEL, caught.exception.issue
        )


def parsed_row(label: str, basis: MinutePriceBasis, price: str) -> ParsedMinuteRow:
    at = datetime.strptime(label, "%Y%m%d%H%M%S").replace(tzinfo=KOREA_TZ)
    value = Ohlcv(D(price), D(price), D(price), D(price), 10)
    return ParsedMinuteRow(
        "005930",
        label,
        at,
        at.date(),
        value,
        (price, price, price, price),
        basis,
        1,
        0,
        "a" * 64,
    )


class SourceBarAdapterTests(unittest.TestCase):
    def series(
        self, basis: MinutePriceBasis, labels: list[str], price: str
    ) -> CollectedMinuteSeries:
        request = MinuteCollectionRequest(
            "005930", date(2026, 8, 28), date(2026, 8, 28), basis
        )
        return CollectedMinuteSeries(
            request,
            tuple(parsed_row(label, basis, price) for label in labels),
            (),
        )

    def test_primary_and_sensitivity_filter_without_synthetic_rows(self) -> None:
        labels = [
            "20260828151500",
            "20260828153000",
            "20260828153500",
        ]
        raw = self.series(MinutePriceBasis.RAW, labels, "100")
        adjusted = self.series(MinutePriceBasis.ADJUSTED, labels, "200")
        primary, primary_excluded = align_source_bars(
            raw, adjusted, latest_label_time=time(15, 30)
        )
        sensitivity, sensitivity_excluded = align_source_bars(
            raw, adjusted, latest_label_time=time(15, 15)
        )
        self.assertEqual(labels[:2], [bar.source_label for bar in primary])
        self.assertEqual(labels[:1], [bar.source_label for bar in sensitivity])
        self.assertEqual((1, 2), (primary_excluded, sensitivity_excluded))
        self.assertEqual(D("100"), primary[0].raw.close)
        self.assertEqual(D("200"), primary[0].signal.close)
        self.assertEqual([0, 1], [bar.source_bar_sequence for bar in primary])
        self.assertTrue(all(bar.assumption_id == ASSUMPTION_ID for bar in primary))


class MinuteCollectorTests(unittest.TestCase):
    def test_pagination_is_deterministic_and_manifest_excludes_token(self) -> None:
        request = MinuteCollectionRequest(
            "005930", date(2026, 8, 27), date(2026, 8, 28), MinutePriceBasis.RAW
        )
        responses = [
            ChartHttpResult(
                200,
                response([row("20260828090000")]),
                {"cont-yn": "Y", "next-key": "opaque-next"},
            ),
            ChartHttpResult(
                200,
                response([row("20260827090000")]),
                {"cont-yn": "N", "next-key": ""},
            ),
        ]
        calls = []

        def transport(url, headers, body):
            calls.append((url, dict(headers), dict(body)))
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as temporary:
            store = KiwoomMinuteStore(Path(temporary))
            series = collect_minute_series(
                request,
                config=DemoConfig("demo", "app-secret", "key-secret"),
                token=TokenInfo("token-secret", "bearer", "20260902120000", 200),
                store=store,
                transport=transport,
                page_delay=0,
                clock=lambda: datetime(2026, 9, 2, tzinfo=KOREA_TZ),
            )
            manifest = (Path(temporary) / "manifest" / "requests.jsonl").read_text()
        self.assertEqual(2, len(series.pages))
        self.assertEqual(
            [date(2026, 8, 27), date(2026, 8, 28)],
            [item.trading_date for item in series.rows],
        )
        self.assertEqual("0", calls[0][2]["upd_stkpc_tp"])
        self.assertEqual("opaque-next", calls[1][1]["next-key"])
        self.assertNotIn("token-secret", manifest)
        self.assertNotIn("opaque-next", manifest)


def daily_fixture() -> tuple[list[DailyBar], list[date]]:
    start = date(2024, 1, 1)
    days = [start + timedelta(days=index) for index in range(69)]
    bars: list[DailyBar] = []
    for index in range(69):
        close = D(100 + index) if index <= 65 else D("100")
        low = D("155.5") if index == 65 else close - D(1)
        value = Ohlcv(close, close + D(1), low, close, 100)
        bars.append(DailyBar("005930", days[index], value, value))
    return bars, days


def source_bar(
    sequence: int, at: datetime, close: str, raw_open: str = "100"
) -> MinuteSourceBar:
    label = at.strftime("%Y%m%d%H%M%S")
    signal_close = D(close)
    signal = Ohlcv(
        signal_close, signal_close + D(1), signal_close - D(1), signal_close, 10
    )
    raw = Ohlcv(
        D(raw_open), max(D(raw_open), D(raw_open) + 1), D(raw_open) - 1, D(raw_open), 10
    )
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


class SequenceProofTests(unittest.TestCase):
    def test_stateful_entry_and_exit_use_next_distinct_source_rows(self) -> None:
        daily, days = daily_fixture()
        bars: list[MinuteSourceBar] = []
        start = datetime.combine(days[64], time(9), tzinfo=KOREA_TZ)
        for index in range(60):
            bars.append(
                source_bar(
                    index, start + timedelta(minutes=5 * index), str(100 + index)
                )
            )
        entry = datetime.combine(days[66], time(9), tzinfo=KOREA_TZ)
        bars.extend(
            [
                source_bar(60, entry, "160"),
                source_bar(61, entry + timedelta(minutes=5), "161", "100"),
            ]
        )
        exit_at = datetime.combine(days[67], time(9), tzinfo=KOREA_TZ)
        bars.extend(
            [
                source_bar(62, exit_at, "50"),
                source_bar(63, exit_at + timedelta(minutes=5), "51", "90"),
            ]
        )
        calendar = ExplicitTradingCalendar(days)
        result = run_up_path_sequence_proof(
            daily_bars=daily,
            source_bars=bars,
            calendar=calendar,
            research_start=days[65],
            research_end=days[67],
            stock_full_weight=D("0.10"),
            initial_capital=D("100000"),
        )
        self.assertEqual(1, result["counts"]["entry_fills"])
        self.assertEqual(1, result["counts"]["exit_fills"])
        self.assertEqual(1, len(result["completed_trades"]))
        trade = result["completed_trades"][0]
        self.assertEqual("20240307090000", trade["entry_execution_source_label"])
        self.assertEqual("20240307090500", trade["entry_fill_source_label"])
        self.assertEqual("20240308090000", trade["exit_c_source_label"])
        self.assertEqual("20240308090500", trade["exit_fill_source_label"])
        self.assertLess(trade["entry_fill_sequence"], trade["exit_fill_sequence"])
        self.assertEqual(D("-10"), trade["pnl_pct"])

    def test_input_permutation_is_canonicalized(self) -> None:
        daily, days = daily_fixture()
        start = datetime.combine(days[64], time(9), tzinfo=KOREA_TZ)
        bars = [
            source_bar(index, start + timedelta(minutes=5 * index), str(100 + index))
            for index in range(60)
        ]
        kwargs = {
            "calendar": ExplicitTradingCalendar(days),
            "research_start": days[65],
            "research_end": days[65],
            "stock_full_weight": D("0.10"),
            "initial_capital": D("100000"),
        }
        first = run_up_path_sequence_proof(daily_bars=daily, source_bars=bars, **kwargs)
        second = run_up_path_sequence_proof(
            daily_bars=list(reversed(daily)), source_bars=list(reversed(bars)), **kwargs
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
