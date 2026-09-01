from __future__ import annotations

import inspect
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal

from src.backtest_engine.models import DailyBar, Ohlcv
from src.research_universe import daily_eligibility
from src.research_universe.daily_eligibility import (
    DailyCalendarSnapshot,
    DailyDatasetEvidence,
    DailyEligibilityConfig,
    DailyEligibilityInput,
    DailyEvidenceIssue,
    DailySeriesRole,
    assess_daily_eligibility,
)
from src.research_universe.models import (
    EligibilityEvidenceReference,
    EligibilityEvidenceType,
    ResearchEligibilityReason,
    ResearchEligibilityScope,
    ResearchEligibilityStatus,
    ResearchPeriod,
)

D = Decimal
JAN_1 = date(2024, 1, 1)
JAN_2 = date(2024, 1, 2)
JAN_3 = date(2024, 1, 3)
JAN_4 = date(2024, 1, 4)
JAN_5 = date(2024, 1, 5)
JAN_6 = date(2024, 1, 6)
JAN_7 = date(2024, 1, 7)
DEFAULT_DATES = (JAN_2, JAN_3, JAN_4)
DEFAULT_REFERENCE = object()


class DailyResearchEligibilityTests(unittest.TestCase):
    def period(self, start: date = JAN_2, end: date = JAN_4) -> ResearchPeriod:
        return ResearchPeriod(start, end, start, end)

    def ohlcv(self, *, volume: int = 100, zero: bool = False) -> Ohlcv:
        if zero:
            return Ohlcv(D("0"), D("0"), D("0"), D("0"), volume)
        return Ohlcv(D("100"), D("102"), D("98"), D("101"), volume)

    def bar(
        self,
        day: date,
        *,
        stock_code: str = "005930",
        volume: int = 100,
        zero: bool = False,
    ) -> DailyBar:
        raw = self.ohlcv(volume=volume, zero=zero)
        signal = self.ohlcv(volume=volume, zero=zero)
        return DailyBar(stock_code, day, raw, signal)

    def reference(
        self,
        name: str,
        *,
        digest: str | None = "a" * 64,
        evidence_type: EligibilityEvidenceType = EligibilityEvidenceType.DAILY_DATASET,
    ) -> EligibilityEvidenceReference:
        return EligibilityEvidenceReference(
            evidence_type=evidence_type,
            source_id=f"fixture:{name}",
            source_reference=f"dataset:fixture:{name}",
            artifact_sha256=digest,
            schema_version="source-v1",
            coverage_start=JAN_1,
            coverage_end=JAN_5,
        )

    def dataset(
        self,
        role: DailySeriesRole,
        *,
        dates: tuple[date, ...] = DEFAULT_DATES,
        stock_code: str = "005930",
        evidence: EligibilityEvidenceReference | None | object = DEFAULT_REFERENCE,
        dataset_version: str = "v1",
    ) -> DailyDatasetEvidence:
        reference = (
            self.reference(role.value) if evidence is DEFAULT_REFERENCE else evidence
        )
        return DailyDatasetEvidence(
            series_role=role,
            stock_code=stock_code,
            session_dates=dates,
            evidence_reference=reference,  # type: ignore[arg-type]
            provider_id="fixture-provider",
            dataset_id=f"fixture-{role.value.lower()}",
            dataset_version=dataset_version,
            price_policy_id=(
                "RAW_TRADE_PRICE"
                if role is DailySeriesRole.RAW
                else "ADJUSTED_SIGNAL_V1"
            ),
            parser_id="fixture-parser-v1",
            schema_version="canonical-daily-v1",
        )

    def calendar(
        self,
        *,
        sessions: tuple[date, ...] = DEFAULT_DATES,
        coverage_start: date = JAN_1,
        coverage_end: date = JAN_5,
        digest: str | None = "c" * 64,
        version: str = "v1",
    ) -> DailyCalendarSnapshot:
        return DailyCalendarSnapshot(
            calendar_id="fixture-krx-calendar",
            calendar_version=version,
            schema_version="calendar-v1",
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            trading_sessions=sessions,
            source_reference="calendar:fixture:krx",
            artifact_sha256=digest,
        )

    def eligibility_input(self, **changes: object) -> DailyEligibilityInput:
        values: dict[str, object] = {
            "source_stock_code": "005930",
            "canonical_stock_code": "005930",
            "scope": ResearchEligibilityScope.DAILY_RESEARCH,
            "research_period": self.period(),
            "canonical_daily_bars": tuple(self.bar(day) for day in DEFAULT_DATES),
            "calendar_snapshot": self.calendar(),
            "raw_dataset": self.dataset(DailySeriesRole.RAW),
            "signal_dataset": self.dataset(DailySeriesRole.SIGNAL_ADJUSTED),
            "config": DailyEligibilityConfig("daily-eligibility", "1"),
            "issues": (),
        }
        values.update(changes)
        return DailyEligibilityInput(**values)  # type: ignore[arg-type]

    def assess(self, **changes: object):
        return assess_daily_eligibility(self.eligibility_input(**changes))

    def assert_reason(
        self,
        reason: ResearchEligibilityReason,
        expected_status: ResearchEligibilityStatus,
        **changes: object,
    ) -> None:
        result = self.assess(**changes)
        self.assertEqual(expected_status, result.status)
        self.assertIn(reason, result.reason_codes)

    def test_complete_daily_and_intraday_prerequisite_are_eligible(self) -> None:
        daily = self.assess()
        intraday = self.assess(scope=ResearchEligibilityScope.INTRADAY_COMPARISON)
        for result in (daily, intraday):
            self.assertEqual(ResearchEligibilityStatus.ELIGIBLE, result.status)
            self.assertEqual(3, len(result.evidence_references))
            self.assertEqual("005930", result.source_stock_code)
            self.assertEqual("005930", result.canonical_stock_code)
        self.assertNotEqual(daily.result_id, intraday.result_id)

    def test_identity_is_explicit_and_never_canonicalized(self) -> None:
        with self.assertRaisesRegex(ValueError, "six ASCII digits"):
            self.eligibility_input(canonical_stock_code="A12345")
        with self.assertRaisesRegex(ValueError, "must equal"):
            self.eligibility_input(source_stock_code="A005930")

    def test_required_start_and_end_boundaries_are_inclusive(self) -> None:
        for name, required_period, dates in (
            ("start", self.period(JAN_2, JAN_2), (JAN_2,)),
            ("end", self.period(JAN_4, JAN_4), (JAN_4,)),
        ):
            with self.subTest(name=name):
                result = self.assess(
                    research_period=required_period,
                    canonical_daily_bars=tuple(self.bar(day) for day in dates),
                    raw_dataset=self.dataset(DailySeriesRole.RAW, dates=dates),
                    signal_dataset=self.dataset(
                        DailySeriesRole.SIGNAL_ADJUSTED, dates=dates
                    ),
                )
                self.assertEqual(ResearchEligibilityStatus.ELIGIBLE, result.status)

    def test_nontrading_required_boundary_uses_actual_session_set(self) -> None:
        result = self.assess(
            research_period=self.period(JAN_1, JAN_4),
        )
        self.assertEqual(ResearchEligibilityStatus.ELIGIBLE, result.status)

    def test_missing_middle_raw_session_is_excluded(self) -> None:
        self.assert_reason(
            ResearchEligibilityReason.MISSING_REQUIRED_TRADING_DAYS,
            ResearchEligibilityStatus.EXCLUDED,
            raw_dataset=self.dataset(DailySeriesRole.RAW, dates=(JAN_2, JAN_4)),
        )

    def test_missing_middle_signal_session_is_excluded(self) -> None:
        self.assert_reason(
            ResearchEligibilityReason.ADJUSTED_PRICE_UNAVAILABLE,
            ResearchEligibilityStatus.EXCLUDED,
            signal_dataset=self.dataset(
                DailySeriesRole.SIGNAL_ADJUSTED, dates=(JAN_2, JAN_4)
            ),
        )

    def test_duplicate_canonical_or_dataset_date_is_typed(self) -> None:
        duplicate_bar = self.bar(JAN_2)
        cases = (
            {
                "canonical_daily_bars": (
                    duplicate_bar,
                    duplicate_bar,
                    self.bar(JAN_3),
                    self.bar(JAN_4),
                )
            },
            {
                "raw_dataset": self.dataset(
                    DailySeriesRole.RAW,
                    dates=(JAN_2, JAN_2, JAN_3, JAN_4),
                )
            },
        )
        for changes in cases:
            with self.subTest(changes=tuple(changes)):
                self.assert_reason(
                    ResearchEligibilityReason.DUPLICATE_DAILY_DATE,
                    ResearchEligibilityStatus.EXCLUDED,
                    **changes,
                )

    def test_invalid_ohlc_is_excluded_but_zero_volume_is_valid(self) -> None:
        invalid = (self.bar(JAN_2, zero=True), self.bar(JAN_3), self.bar(JAN_4))
        self.assert_reason(
            ResearchEligibilityReason.INVALID_DAILY_OHLCV,
            ResearchEligibilityStatus.EXCLUDED,
            canonical_daily_bars=invalid,
        )
        zero_volume = tuple(self.bar(day, volume=0) for day in DEFAULT_DATES)
        result = self.assess(canonical_daily_bars=zero_volume)
        self.assertEqual(ResearchEligibilityStatus.ELIGIBLE, result.status)

    def test_zero_ohlc_is_not_interpreted_as_halt(self) -> None:
        result = self.assess(
            canonical_daily_bars=(
                self.bar(JAN_2),
                self.bar(JAN_3, zero=True, volume=0),
                self.bar(JAN_4),
            )
        )
        self.assertIn(
            ResearchEligibilityReason.INVALID_DAILY_OHLCV, result.reason_codes
        )
        self.assertFalse(hasattr(result, "halt_status"))

    def test_nontrading_day_canonical_row_is_data_validation_error(self) -> None:
        dates = (JAN_2, JAN_3, JAN_4)
        self.assert_reason(
            ResearchEligibilityReason.DATA_VALIDATION_ERROR,
            ResearchEligibilityStatus.EXCLUDED,
            calendar_snapshot=self.calendar(sessions=(JAN_2, JAN_4)),
            canonical_daily_bars=tuple(self.bar(day) for day in dates),
            raw_dataset=self.dataset(DailySeriesRole.RAW, dates=dates),
            signal_dataset=self.dataset(DailySeriesRole.SIGNAL_ADJUSTED, dates=dates),
        )

    def test_incomplete_calendar_is_review_and_does_not_infer_missing(self) -> None:
        result = self.assess(
            calendar_snapshot=self.calendar(
                sessions=(JAN_2, JAN_3),
                coverage_start=JAN_2,
                coverage_end=JAN_3,
            )
        )
        self.assertEqual(ResearchEligibilityStatus.REVIEW_REQUIRED, result.status)
        self.assertIn(
            ResearchEligibilityReason.CALENDAR_COVERAGE_MISSING,
            result.reason_codes,
        )
        self.assertNotIn(
            ResearchEligibilityReason.MISSING_REQUIRED_TRADING_DAYS,
            result.reason_codes,
        )

    def test_empty_expected_session_window_fails_closed(self) -> None:
        empty_calendar = self.calendar(
            sessions=(), coverage_start=JAN_6, coverage_end=JAN_7
        )
        result = self.assess(
            research_period=self.period(JAN_6, JAN_7),
            calendar_snapshot=empty_calendar,
            canonical_daily_bars=(),
            raw_dataset=self.dataset(DailySeriesRole.RAW, dates=()),
            signal_dataset=self.dataset(DailySeriesRole.SIGNAL_ADJUSTED, dates=()),
        )
        self.assertEqual(ResearchEligibilityStatus.EXCLUDED, result.status)
        self.assertIn(
            ResearchEligibilityReason.INSUFFICIENT_DAILY_DATA,
            result.reason_codes,
        )

    def test_missing_raw_or_signal_evidence_fails_closed(self) -> None:
        raw_missing = self.assess(raw_dataset=None)
        signal_missing = self.assess(signal_dataset=None)
        self.assertIn(
            ResearchEligibilityReason.INSUFFICIENT_DAILY_DATA,
            raw_missing.reason_codes,
        )
        self.assertIn(
            ResearchEligibilityReason.ADJUSTED_PRICE_UNAVAILABLE,
            signal_missing.reason_codes,
        )
        for result in (raw_missing, signal_missing):
            self.assertEqual(ResearchEligibilityStatus.EXCLUDED, result.status)
            self.assertIn(
                ResearchEligibilityReason.MISSING_PROVENANCE,
                result.reason_codes,
            )

    def test_raw_signal_and_canonical_date_mismatches_are_alignment_errors(
        self,
    ) -> None:
        cases = (
            {"raw_dataset": self.dataset(DailySeriesRole.RAW, dates=(JAN_2, JAN_4))},
            {"canonical_daily_bars": (self.bar(JAN_2), self.bar(JAN_4))},
            {
                "signal_dataset": self.dataset(
                    DailySeriesRole.SIGNAL_ADJUSTED, dates=(JAN_2, JAN_4)
                )
            },
        )
        for changes in cases:
            with self.subTest(changes=tuple(changes)):
                self.assert_reason(
                    ResearchEligibilityReason.ADJUSTED_RAW_ALIGNMENT_ERROR,
                    ResearchEligibilityStatus.EXCLUDED,
                    **changes,
                )

    def test_raw_or_signal_stock_mismatch_is_alignment_error(self) -> None:
        cases = (
            {"raw_dataset": self.dataset(DailySeriesRole.RAW, stock_code="000660")},
            {
                "signal_dataset": self.dataset(
                    DailySeriesRole.SIGNAL_ADJUSTED, stock_code="000660"
                )
            },
        )
        for changes in cases:
            with self.subTest(changes=tuple(changes)):
                self.assert_reason(
                    ResearchEligibilityReason.ADJUSTED_RAW_ALIGNMENT_ERROR,
                    ResearchEligibilityStatus.EXCLUDED,
                    **changes,
                )

    def test_typed_upstream_issues_map_without_message_parsing(self) -> None:
        cases = (
            (
                DailyEvidenceIssue.RAW_CONFLICT,
                ResearchEligibilityReason.RAW_CONFLICT,
                ResearchEligibilityStatus.EXCLUDED,
            ),
            (
                DailyEvidenceIssue.ARTIFACT_DIGEST_MISMATCH,
                ResearchEligibilityReason.ARTIFACT_DIGEST_MISMATCH,
                ResearchEligibilityStatus.EXCLUDED,
            ),
            (
                DailyEvidenceIssue.MISSING_PROVENANCE,
                ResearchEligibilityReason.MISSING_PROVENANCE,
                ResearchEligibilityStatus.REVIEW_REQUIRED,
            ),
            (
                DailyEvidenceIssue.DATA_VALIDATION_ERROR,
                ResearchEligibilityReason.DATA_VALIDATION_ERROR,
                ResearchEligibilityStatus.EXCLUDED,
            ),
        )
        for issue, reason, status in cases:
            with self.subTest(issue=issue):
                self.assert_reason(reason, status, issues=(issue,))

    def test_missing_dataset_or_calendar_digest_is_missing_provenance(self) -> None:
        cases = (
            {
                "raw_dataset": self.dataset(
                    DailySeriesRole.RAW,
                    evidence=self.reference("raw", digest=None),
                )
            },
            {"calendar_snapshot": self.calendar(digest=None)},
        )
        for changes in cases:
            with self.subTest(changes=tuple(changes)):
                self.assert_reason(
                    ResearchEligibilityReason.MISSING_PROVENANCE,
                    ResearchEligibilityStatus.REVIEW_REQUIRED,
                    **changes,
                )

    def test_reason_severity_and_order_are_deterministic(self) -> None:
        first = self.assess(
            issues=(
                DailyEvidenceIssue.MISSING_PROVENANCE,
                DailyEvidenceIssue.ARTIFACT_DIGEST_MISMATCH,
                DailyEvidenceIssue.RAW_CONFLICT,
            )
        )
        second = self.assess(
            issues=(
                DailyEvidenceIssue.RAW_CONFLICT,
                DailyEvidenceIssue.ARTIFACT_DIGEST_MISMATCH,
                DailyEvidenceIssue.MISSING_PROVENANCE,
            )
        )
        self.assertEqual(first, second)
        self.assertEqual(ResearchEligibilityStatus.EXCLUDED, first.status)
        self.assertEqual(
            tuple(
                reason
                for reason in ResearchEligibilityReason
                if reason in set(first.reason_codes)
            ),
            first.reason_codes,
        )

    def test_review_and_excluded_reason_resolves_to_excluded(self) -> None:
        result = self.assess(
            calendar_snapshot=self.calendar(
                sessions=(JAN_2, JAN_3),
                coverage_start=JAN_2,
                coverage_end=JAN_3,
            ),
            canonical_daily_bars=(
                self.bar(JAN_2, zero=True),
                self.bar(JAN_3),
                self.bar(JAN_4),
            ),
        )
        self.assertEqual(ResearchEligibilityStatus.EXCLUDED, result.status)
        self.assertIn(
            ResearchEligibilityReason.CALENDAR_COVERAGE_MISSING,
            result.reason_codes,
        )
        self.assertIn(
            ResearchEligibilityReason.INVALID_DAILY_OHLCV,
            result.reason_codes,
        )

    def test_bar_session_issue_and_evidence_order_are_deterministic(self) -> None:
        baseline_input = self.eligibility_input()
        reordered = replace(
            baseline_input,
            canonical_daily_bars=tuple(reversed(baseline_input.canonical_daily_bars)),
            raw_dataset=replace(
                baseline_input.raw_dataset,
                session_dates=tuple(reversed(baseline_input.raw_dataset.session_dates)),
            ),
            signal_dataset=replace(
                baseline_input.signal_dataset,
                session_dates=tuple(
                    reversed(baseline_input.signal_dataset.session_dates)
                ),
            ),
        )
        first = assess_daily_eligibility(baseline_input)
        second = assess_daily_eligibility(reordered)
        self.assertEqual(first, second)
        evidence_reordered = replace(
            first,
            evidence_references=tuple(reversed(first.evidence_references)),
        )
        self.assertEqual(first, evidence_reordered)
        self.assertEqual(
            tuple(
                sorted(
                    first.evidence_references,
                    key=lambda item: item.canonical_key,
                )
            ),
            first.evidence_references,
        )

    def test_wider_artifact_outside_required_window_is_allowed(self) -> None:
        period = self.period(JAN_3, JAN_4)
        dates = (JAN_2, JAN_3, JAN_4)
        result = self.assess(
            research_period=period,
            canonical_daily_bars=tuple(self.bar(day) for day in dates),
            raw_dataset=self.dataset(DailySeriesRole.RAW, dates=dates),
            signal_dataset=self.dataset(DailySeriesRole.SIGNAL_ADJUSTED, dates=dates),
        )
        self.assertEqual(ResearchEligibilityStatus.ELIGIBLE, result.status)

    def test_config_scope_period_and_dataset_digest_affect_result_id(self) -> None:
        baseline = self.assess()
        config_changed = self.assess(
            config=DailyEligibilityConfig("daily-eligibility", "2")
        )
        scope_changed = self.assess(scope=ResearchEligibilityScope.INTRADAY_COMPARISON)
        period_changed = self.assess(research_period=self.period(JAN_2, JAN_3))
        wider_dates = (JAN_1, JAN_2, JAN_3, JAN_4)
        digest_changed = self.assess(
            raw_dataset=self.dataset(DailySeriesRole.RAW, dates=wider_dates),
            signal_dataset=self.dataset(
                DailySeriesRole.SIGNAL_ADJUSTED, dates=wider_dates
            ),
            canonical_daily_bars=tuple(self.bar(day) for day in wider_dates),
        )
        for changed in (
            config_changed,
            scope_changed,
            period_changed,
            digest_changed,
        ):
            self.assertNotEqual(baseline.result_id, changed.result_id)

    def test_same_semantic_session_set_has_same_digest(self) -> None:
        first = self.dataset(DailySeriesRole.RAW, dates=DEFAULT_DATES)
        second = self.dataset(DailySeriesRole.RAW, dates=tuple(reversed(DEFAULT_DATES)))
        self.assertEqual(first.session_set_digest, second.session_set_digest)
        self.assertEqual(first, second)

    def test_calendar_snapshot_invariants_and_ordering(self) -> None:
        first = self.calendar(sessions=DEFAULT_DATES)
        second = self.calendar(sessions=tuple(reversed(DEFAULT_DATES)))
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "duplicates"):
            self.calendar(sessions=(JAN_2, JAN_2))
        with self.assertRaisesRegex(ValueError, "within calendar coverage"):
            self.calendar(sessions=(JAN_1,), coverage_start=JAN_2, coverage_end=JAN_5)
        with self.assertRaisesRegex(ValueError, "64-character"):
            self.calendar(digest="bad")

    def test_r3_has_no_forbidden_domain_or_io_dependency(self) -> None:
        source = inspect.getsource(daily_eligibility)
        for forbidden in (
            "historical_eligibility",
            "FiveMinuteBar",
            "CorporateAction",
            "requests",
            "httpx",
            "urlopen",
            "Path(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
