from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import src.kiwoom_daily.adapter as adapter_module
import src.kiwoom_daily.collector as collector_module
import src.kiwoom_daily.models as models_module
import src.kiwoom_daily.parser as parser_module
from src.kiwoom_daily import (
    ADJUSTED_PRICE_POLICY_ID,
    RAW_PRICE_POLICY_ID,
    CollectedDailySeries,
    DailyCollectionRequest,
    DailyPipelineIssue,
    ImmutableKiwoomDailyStore,
    KiwoomDailyValidationError,
    PageProvenance,
    ParsedDailyRow,
    PriceBasis,
    VolumeBasis,
    align_and_build_daily_bars,
    build_dataset_evidence,
    collect_daily_series,
    parse_daily_page,
)
from src.kiwoom_rest.auth import DemoConfig, TokenInfo
from src.kiwoom_rest.market_data_pilot import ChartHttpResult
from src.research_universe.daily_eligibility import (
    DailyCalendarSnapshot,
    DailyEligibilityConfig,
    DailyEligibilityInput,
    DailySeriesRole,
    assess_daily_eligibility,
)
from src.research_universe.models import (
    ResearchEligibilityScope,
    ResearchEligibilityStatus,
    ResearchPeriod,
)

D = Decimal
AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
TOKEN_SECRET = "synthetic-access-token-secret"
NEXT_KEY_SECRET = "synthetic-next-key-secret"


def source_row(
    day: str,
    *,
    open_price: object = "100",
    high_price: object = "110",
    low_price: object = "90",
    close_price: object = "105",
    volume: object = "1000",
) -> dict[str, object]:
    return {
        "dt": day,
        "open_pric": open_price,
        "high_pric": high_price,
        "low_pric": low_price,
        "cur_prc": close_price,
        "trde_qty": volume,
    }


def response_bytes(
    rows: list[object],
    *,
    stock_code: str = "005930",
    return_code: int = 0,
) -> bytes:
    return json.dumps(
        {
            "stk_cd": stock_code,
            "stk_dt_pole_chart_qry": rows,
            "return_code": return_code,
            "return_msg": "fixture",
        },
        separators=(",", ":"),
    ).encode()


def request(
    basis: PriceBasis,
    *,
    stock_code: str = "005930",
    start: date = date(2024, 1, 2),
    end: date = date(2024, 1, 5),
) -> DailyCollectionRequest:
    return DailyCollectionRequest(stock_code, start, end, basis)


def parse(
    basis: PriceBasis,
    rows: list[object],
    *,
    stock_code: str = "005930",
    page: int = 1,
):
    raw = response_bytes(rows, stock_code=stock_code)
    return parse_daily_page(
        raw,
        request(basis, stock_code=stock_code),
        source_page=page,
        artifact_sha256=hashlib.sha256(raw).hexdigest(),
    )


def parsed_row(
    basis: PriceBasis,
    day: date,
    *,
    stock_code: str = "005930",
    open_price: str = "100",
    high_price: str = "110",
    low_price: str = "90",
    close_price: str = "105",
    volume: int = 1000,
    digest: str = "a" * 64,
    source_row_index: int = 0,
) -> ParsedDailyRow:
    return ParsedDailyRow(
        stock_code,
        day,
        D(open_price),
        D(high_price),
        D(low_price),
        D(close_price),
        volume,
        basis,
        1,
        source_row_index,
        digest,
    )


def provenance(
    basis: PriceBasis,
    *,
    stock_code: str = "005930",
    digest: str = "a" * 64,
) -> PageProvenance:
    return PageProvenance(
        provider="KIWOOM",
        api_id="ka10081",
        stock_code=stock_code,
        price_basis=basis,
        base_date="20240105",
        pagination_sequence=1,
        request_continuation_identity="",
        response_continuation_identity="",
        retrieved_at="2026-09-01T12:00:00Z",
        raw_file_path=f"fixture/{basis.value}.json",
        raw_file_sha256=digest,
        row_count=2,
    )


def series(
    basis: PriceBasis,
    rows: tuple[ParsedDailyRow, ...],
    *,
    stock_code: str = "005930",
    digest: str = "a" * 64,
) -> CollectedDailySeries:
    return CollectedDailySeries(
        request=request(basis, stock_code=stock_code),
        rows=rows,
        pages=(provenance(basis, stock_code=stock_code, digest=digest),),
        volume_basis=(
            VolumeBasis.RAW
            if basis is PriceBasis.RAW
            else VolumeBasis.PROVIDER_ADJUSTED_UNKNOWN_POLICY
        ),
    )


class FakeTransport:
    def __init__(self, results: list[ChartHttpResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, str],
    ) -> ChartHttpResult:
        self.calls.append((url, dict(headers), dict(body)))
        if not self.results:
            raise AssertionError("unexpected transport call")
        return self.results.pop(0)


class KiwoomDailyParserModelTests(unittest.TestCase):
    def test_raw_page_parse_preserves_six_digit_code_and_decimal_price(self) -> None:
        page = parse(PriceBasis.RAW, [source_row("20240102")])
        row = page.rows[0]
        self.assertEqual("005930", row.stock_code)
        self.assertEqual(D("100"), row.open)
        self.assertIsInstance(row.open, Decimal)
        self.assertEqual(1000, row.volume)
        self.assertIsInstance(row.volume, int)
        self.assertIs(PriceBasis.RAW, row.price_basis)

    def test_adjusted_page_parse_uses_distinct_basis(self) -> None:
        row = parse(
            PriceBasis.ADJUSTED,
            [
                source_row(
                    "20240102",
                    open_price="20",
                    high_price="22",
                    low_price="18",
                    close_price="21",
                )
            ],
        ).rows[0]
        self.assertIs(PriceBasis.ADJUSTED, row.price_basis)
        self.assertEqual(D("20"), row.open)

    def test_invalid_stock_code_is_rejected_without_canonicalization(self) -> None:
        for code in ("5930", "0059300", "A05930", "００５９３０", 5930):
            with (
                self.subTest(code=code),
                self.assertRaises(KiwoomDailyValidationError) as caught,
            ):
                DailyCollectionRequest(
                    code,  # type: ignore[arg-type]
                    date(2024, 1, 2),
                    date(2024, 1, 5),
                    PriceBasis.RAW,
                )
            self.assertIs(DailyPipelineIssue.INVALID_STOCK_CODE, caught.exception.issue)

    def test_response_stock_mismatch_is_typed(self) -> None:
        raw = response_bytes([source_row("20240102")], stock_code="000660")
        with self.assertRaises(KiwoomDailyValidationError) as caught:
            parse_daily_page(
                raw,
                request(PriceBasis.RAW),
                source_page=1,
                artifact_sha256=hashlib.sha256(raw).hexdigest(),
            )
        self.assertIs(DailyPipelineIssue.STOCK_MISMATCH, caught.exception.issue)

    def test_zero_volume_with_positive_ohlc_is_valid(self) -> None:
        row = parse(PriceBasis.RAW, [source_row("20240102", volume="0")]).rows[0]
        self.assertEqual(0, row.volume)
        self.assertFalse(hasattr(row, "halt_status"))

    def test_zero_ohlc_is_invalid(self) -> None:
        with self.assertRaises(KiwoomDailyValidationError) as caught:
            parse(
                PriceBasis.RAW,
                [
                    source_row(
                        "20240102",
                        open_price="0",
                        high_price="0",
                        low_price="0",
                        close_price="0",
                        volume="0",
                    )
                ],
            )
        self.assertIs(DailyPipelineIssue.INVALID_DAILY_OHLCV, caught.exception.issue)

    def test_malformed_numeric_is_rejected_without_absolute_value_or_repair(
        self,
    ) -> None:
        for value in ("+100", "-100", " 100", "1,000", "100.0", 100):
            with (
                self.subTest(value=value),
                self.assertRaises(KiwoomDailyValidationError) as caught,
            ):
                parse(PriceBasis.RAW, [source_row("20240102", open_price=value)])
            self.assertIs(DailyPipelineIssue.MALFORMED_NUMERIC, caught.exception.issue)

    def test_invalid_ohlc_relationship_is_typed(self) -> None:
        with self.assertRaises(KiwoomDailyValidationError) as caught:
            parse(
                PriceBasis.RAW,
                [source_row("20240102", open_price="120", high_price="110")],
            )
        self.assertIs(DailyPipelineIssue.INVALID_DAILY_OHLCV, caught.exception.issue)

    def test_duplicate_date_within_page_is_rejected(self) -> None:
        with self.assertRaises(KiwoomDailyValidationError) as caught:
            parse(
                PriceBasis.RAW,
                [source_row("20240102"), source_row("20240102")],
            )
        self.assertIs(DailyPipelineIssue.DUPLICATE_DAILY_DATE, caught.exception.issue)


class KiwoomDailyCollectorStoreTests(unittest.TestCase):
    config = DemoConfig("demo", "synthetic-app-key", "synthetic-secret-key")
    token = TokenInfo(TOKEN_SECRET, "bearer", "20260902120000", 200)

    def result(
        self,
        rows: list[object],
        *,
        cont_yn: str = "N",
        next_key: str = "",
    ) -> ChartHttpResult:
        headers = {"cont-yn": cont_yn}
        if next_key:
            headers["next-key"] = next_key
        return ChartHttpResult(200, response_bytes(rows), headers)

    def collect(
        self,
        basis: PriceBasis,
        transport: FakeTransport,
        root: Path,
        **kwargs: object,
    ) -> CollectedDailySeries:
        return collect_daily_series(
            request(basis),
            config=self.config,
            token=self.token,
            store=ImmutableKiwoomDailyStore(root),
            transport=transport,
            page_delay=0,
            clock=lambda: AT,
            **kwargs,  # type: ignore[arg-type]
        )

    def test_pagination_merge_and_descending_source_becomes_ascending(self) -> None:
        fake = FakeTransport(
            [
                self.result(
                    [source_row("20240105"), source_row("20240104")],
                    cont_yn="Y",
                    next_key=NEXT_KEY_SECRET,
                ),
                self.result([source_row("20240103"), source_row("20240102")]),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            output = self.collect(PriceBasis.ADJUSTED, fake, Path(temp))
        self.assertEqual(
            [date(2024, 1, day) for day in (2, 3, 4, 5)],
            [row.trade_date for row in output.rows],
        )
        self.assertEqual(
            (1, 2), tuple(page.pagination_sequence for page in output.pages)
        )
        self.assertEqual("1", fake.calls[0][2]["upd_stkpc_tp"])
        self.assertEqual("Y", fake.calls[1][1]["cont-yn"])
        self.assertEqual(NEXT_KEY_SECRET, fake.calls[1][1]["next-key"])

    def test_raw_and_adjusted_request_values_are_always_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            observed = []
            for basis in (PriceBasis.RAW, PriceBasis.ADJUSTED):
                fake = FakeTransport([self.result([source_row("20240102")])])
                self.collect(basis, fake, Path(temp) / basis.value)
                observed.append(fake.calls[0][2]["upd_stkpc_tp"])
        self.assertEqual(["0", "1"], observed)

    def test_page_boundary_duplicate_is_rejected(self) -> None:
        fake = FakeTransport(
            [
                self.result(
                    [source_row("20240105"), source_row("20240104")],
                    cont_yn="Y",
                    next_key=NEXT_KEY_SECRET,
                ),
                self.result([source_row("20240104"), source_row("20240102")]),
            ]
        )
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaises(KiwoomDailyValidationError) as caught,
        ):
            self.collect(PriceBasis.RAW, fake, Path(temp))
        self.assertIs(DailyPipelineIssue.DUPLICATE_DAILY_DATE, caught.exception.issue)

    def test_required_start_reached_stops_without_extra_page(self) -> None:
        fake = FakeTransport(
            [
                self.result(
                    [source_row("20240105"), source_row("20240102")],
                    cont_yn="Y",
                    next_key=NEXT_KEY_SECRET,
                )
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            output = self.collect(PriceBasis.RAW, fake, Path(temp))
        self.assertEqual(1, len(fake.calls))
        self.assertEqual(date(2024, 1, 2), output.rows[0].trade_date)

    def test_canonical_window_is_inclusive_and_preserves_newer_raw_row(self) -> None:
        fake = FakeTransport(
            [
                self.result(
                    [
                        source_row("20240106"),
                        source_row("20240105"),
                        source_row("20240102"),
                    ]
                )
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            output = self.collect(PriceBasis.RAW, fake, Path(temp))
            stored_payload = json.loads(
                Path(output.pages[0].raw_file_path).read_text(encoding="utf-8")
            )
        self.assertEqual(
            (date(2024, 1, 2), date(2024, 1, 5)),
            output.session_dates,
        )
        self.assertEqual("20240106", stored_payload["stk_dt_pole_chart_qry"][0]["dt"])

    def test_required_start_not_reached_is_typed(self) -> None:
        fake = FakeTransport(
            [self.result([source_row("20240105"), source_row("20240104")])]
        )
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaises(KiwoomDailyValidationError) as caught,
        ):
            self.collect(PriceBasis.RAW, fake, Path(temp))
        self.assertIs(
            DailyPipelineIssue.REQUIRED_START_NOT_REACHED,
            caught.exception.issue,
        )

    def test_raw_artifact_is_byte_preserved_and_immutable(self) -> None:
        raw = response_bytes([source_row("20240102")])
        store_request = request(PriceBasis.RAW)
        with tempfile.TemporaryDirectory() as temp:
            store = ImmutableKiwoomDailyStore(Path(temp))
            first = store.store_page(
                store_request, pagination_sequence=1, raw_bytes=raw
            )
            second = store.store_page(
                store_request, pagination_sequence=1, raw_bytes=raw
            )
            self.assertEqual(first, second)
            self.assertEqual(raw, Path(first.raw_file_path).read_bytes())

    def test_manifest_contains_provenance_but_no_secret_material(self) -> None:
        fake = FakeTransport(
            [
                self.result(
                    [source_row("20240102")],
                    cont_yn="Y",
                    next_key=NEXT_KEY_SECRET,
                )
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.collect(PriceBasis.RAW, fake, root)
            manifest = (root / "manifest" / "requests.jsonl").read_text()
        for forbidden in (
            TOKEN_SECRET,
            NEXT_KEY_SECRET,
            self.config.app_key,
            self.config.secret_key,
            "authorization",
        ):
            self.assertNotIn(forbidden, manifest)
        record = json.loads(manifest)
        self.assertEqual("KIWOOM", record["provider"])
        self.assertEqual("ka10081", record["api_id"])
        self.assertEqual("SUCCESS", record["status"])
        self.assertTrue(record["raw_file_sha256"])
        self.assertTrue(record["response_continuation_identity"])

    def test_normal_collector_test_uses_only_injected_transport(self) -> None:
        fake = FakeTransport([self.result([source_row("20240102")])])
        with tempfile.TemporaryDirectory() as temp:
            self.collect(PriceBasis.RAW, fake, Path(temp))
        self.assertEqual(1, len(fake.calls))


class KiwoomDailyAdapterEvidenceTests(unittest.TestCase):
    days = (date(2024, 1, 2), date(2024, 1, 3))

    def paired_series(self, *, reversed_rows: bool = False):
        raw_rows = tuple(
            parsed_row(PriceBasis.RAW, day, source_row_index=index)
            for index, day in enumerate(self.days)
        )
        adjusted_rows = tuple(
            parsed_row(
                PriceBasis.ADJUSTED,
                day,
                open_price="20",
                high_price="22",
                low_price="18",
                close_price="21",
                volume=5000,
                digest="b" * 64,
                source_row_index=index,
            )
            for index, day in enumerate(self.days)
        )
        if reversed_rows:
            raw_rows = tuple(reversed(raw_rows))
            adjusted_rows = tuple(reversed(adjusted_rows))
        return (
            series(PriceBasis.RAW, raw_rows, digest="a" * 64),
            series(PriceBasis.ADJUSTED, adjusted_rows, digest="b" * 64),
        )

    def test_identical_dates_map_raw_and_adjusted_without_copying(self) -> None:
        output = align_and_build_daily_bars(*self.paired_series())
        first = output.bars[0]
        self.assertEqual(D("100"), first.raw.open)
        self.assertEqual(D("20"), first.signal.open)
        self.assertEqual(1000, first.raw.volume)
        self.assertEqual(5000, first.signal.volume)
        self.assertNotEqual(first.raw, first.signal)

    def test_mock_collection_pair_reaches_dailybar_and_r3_evidence(self) -> None:
        config = DemoConfig("demo", "synthetic-app-key", "synthetic-secret-key")
        token = TokenInfo(TOKEN_SECRET, "bearer", "20260902120000", 200)
        raw_response = ChartHttpResult(
            200,
            response_bytes([source_row("20240102")]),
            {"cont-yn": "N"},
        )
        adjusted_response = ChartHttpResult(
            200,
            response_bytes(
                [
                    source_row(
                        "20240102",
                        open_price="20",
                        high_price="22",
                        low_price="18",
                        close_price="21",
                        volume="5000",
                    )
                ]
            ),
            {"cont-yn": "N"},
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = collect_daily_series(
                request(PriceBasis.RAW, end=date(2024, 1, 2)),
                config=config,
                token=token,
                store=ImmutableKiwoomDailyStore(root / "raw"),
                transport=FakeTransport([raw_response]),
                page_delay=0,
                clock=lambda: AT,
            )
            adjusted = collect_daily_series(
                request(PriceBasis.ADJUSTED, end=date(2024, 1, 2)),
                config=config,
                token=token,
                store=ImmutableKiwoomDailyStore(root / "adjusted"),
                transport=FakeTransport([adjusted_response]),
                page_delay=0,
                clock=lambda: AT,
            )
        output = align_and_build_daily_bars(raw, adjusted)
        self.assertEqual(D("100"), output.bars[0].raw.open)
        self.assertEqual(D("20"), output.bars[0].signal.open)
        self.assertIs(DailySeriesRole.RAW, output.raw_evidence.series_role)
        self.assertIs(
            DailySeriesRole.SIGNAL_ADJUSTED, output.signal_evidence.series_role
        )

    def test_split_fixture_preserves_each_source_price(self) -> None:
        raw = series(
            PriceBasis.RAW,
            (
                parsed_row(
                    PriceBasis.RAW,
                    self.days[0],
                    open_price="514000",
                    high_price="529000",
                    low_price="503000",
                    close_price="517000",
                    volume=1075042,
                ),
            ),
        )
        adjusted = series(
            PriceBasis.ADJUSTED,
            (
                parsed_row(
                    PriceBasis.ADJUSTED,
                    self.days[0],
                    open_price="102800",
                    high_price="105800",
                    low_price="100600",
                    close_price="103400",
                    volume=5375210,
                    digest="b" * 64,
                ),
            ),
            digest="b" * 64,
        )
        bar = align_and_build_daily_bars(raw, adjusted).bars[0]
        self.assertEqual(D("517000"), bar.raw.close)
        self.assertEqual(D("103400"), bar.signal.close)
        self.assertEqual(1075042, bar.raw.volume)
        self.assertEqual(5375210, bar.signal.volume)

    def test_date_mismatch_is_rejected_without_interpolation(self) -> None:
        raw, adjusted = self.paired_series()
        adjusted = series(
            PriceBasis.ADJUSTED,
            (adjusted.rows[0],),
            digest="b" * 64,
        )
        with self.assertRaises(KiwoomDailyValidationError) as caught:
            align_and_build_daily_bars(raw, adjusted)
        self.assertIs(
            DailyPipelineIssue.ADJUSTED_RAW_ALIGNMENT_ERROR,
            caught.exception.issue,
        )

    def test_stock_mismatch_is_rejected(self) -> None:
        raw, _ = self.paired_series()
        adjusted = series(
            PriceBasis.ADJUSTED,
            tuple(
                parsed_row(
                    PriceBasis.ADJUSTED,
                    day,
                    stock_code="000660",
                    digest="b" * 64,
                    source_row_index=index,
                )
                for index, day in enumerate(self.days)
            ),
            stock_code="000660",
            digest="b" * 64,
        )
        with self.assertRaises(KiwoomDailyValidationError) as caught:
            align_and_build_daily_bars(raw, adjusted)
        self.assertIs(DailyPipelineIssue.STOCK_MISMATCH, caught.exception.issue)

    def test_evidence_roles_policy_and_volume_basis_are_explicit(self) -> None:
        raw, adjusted = self.paired_series()
        raw_evidence = build_dataset_evidence(raw)
        adjusted_evidence = build_dataset_evidence(adjusted)
        self.assertIs(DailySeriesRole.RAW, raw_evidence.series_role)
        self.assertEqual(RAW_PRICE_POLICY_ID, raw_evidence.price_policy_id)
        self.assertIs(DailySeriesRole.SIGNAL_ADJUSTED, adjusted_evidence.series_role)
        self.assertEqual(ADJUSTED_PRICE_POLICY_ID, adjusted_evidence.price_policy_id)
        self.assertIs(VolumeBasis.RAW, raw.volume_basis)
        self.assertIs(
            VolumeBasis.PROVIDER_ADJUSTED_UNKNOWN_POLICY,
            adjusted.volume_basis,
        )

    def test_row_without_matching_page_artifact_is_rejected(self) -> None:
        with self.assertRaises(KiwoomDailyValidationError) as caught:
            series(
                PriceBasis.RAW,
                (
                    parsed_row(
                        PriceBasis.RAW,
                        self.days[0],
                        digest="d" * 64,
                    ),
                ),
                digest="a" * 64,
            )
        self.assertIs(DailyPipelineIssue.PROVENANCE_ERROR, caught.exception.issue)

    def test_session_digest_and_r3_result_are_input_order_independent(self) -> None:
        outputs = [
            align_and_build_daily_bars(*self.paired_series(reversed_rows=reverse))
            for reverse in (False, True)
        ]
        results = []
        for output in outputs:
            calendar = DailyCalendarSnapshot(
                calendar_id="fixture-calendar",
                calendar_version="1",
                schema_version="fixture-calendar-v1",
                coverage_start=self.days[0],
                coverage_end=self.days[-1],
                trading_sessions=tuple(reversed(self.days)),
                source_reference="fixture:calendar",
                artifact_sha256="c" * 64,
            )
            result = assess_daily_eligibility(
                DailyEligibilityInput(
                    source_stock_code="005930",
                    canonical_stock_code="005930",
                    scope=ResearchEligibilityScope.DAILY_RESEARCH,
                    research_period=ResearchPeriod(
                        self.days[0],
                        self.days[-1],
                        self.days[0],
                        self.days[-1],
                    ),
                    canonical_daily_bars=output.bars,
                    calendar_snapshot=calendar,
                    raw_dataset=output.raw_evidence,
                    signal_dataset=output.signal_evidence,
                    config=DailyEligibilityConfig("r5-fixture", "1"),
                )
            )
            results.append(result)
        self.assertTrue(
            all(
                result.status is ResearchEligibilityStatus.ELIGIBLE
                for result in results
            )
        )
        self.assertEqual(
            outputs[0].raw_evidence.session_set_digest,
            outputs[1].raw_evidence.session_set_digest,
        )
        self.assertEqual(results[0].result_id, results[1].result_id)

    def test_modules_have_no_top30_or_corporate_action_dependency(self) -> None:
        source = "\n".join(
            inspect.getsource(module).lower()
            for module in (
                models_module,
                parser_module,
                collector_module,
                adapter_module,
            )
        )
        self.assertNotIn("top30", source)
        self.assertNotIn("telegram", source)
        self.assertNotIn("corporateaction", source)
        self.assertNotIn("corporate_action", source)


if __name__ == "__main__":
    unittest.main()
