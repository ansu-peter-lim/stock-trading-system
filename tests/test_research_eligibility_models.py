from __future__ import annotations

import inspect
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

from src.research_universe import models
from src.research_universe.models import (
    EligibilityEvidenceReference,
    EligibilityEvidenceType,
    ResearchEligibilityReason,
    ResearchEligibilityResult,
    ResearchEligibilityScope,
    ResearchEligibilityStatus,
    ResearchPeriod,
)


class ResearchEligibilityModelTests(unittest.TestCase):
    def period(self, **changes: date) -> ResearchPeriod:
        values = {
            "research_start": date(2024, 1, 2),
            "research_end": date(2024, 12, 30),
            "required_data_start": date(2023, 10, 2),
            "required_data_end": date(2024, 12, 31),
        }
        values.update(changes)
        return ResearchPeriod(**values)

    def evidence(
        self,
        source_id: str = "daily-005930",
        *,
        digest: str | None = "a" * 64,
        evidence_type: EligibilityEvidenceType = EligibilityEvidenceType.DAILY_DATASET,
    ) -> EligibilityEvidenceReference:
        return EligibilityEvidenceReference(
            evidence_type=evidence_type,
            source_id=source_id,
            source_reference=f"dataset:{source_id}",
            artifact_sha256=digest,
            schema_version="1",
            coverage_start=date(2023, 10, 2),
            coverage_end=date(2024, 12, 31),
        )

    def result(
        self,
        *,
        scope: ResearchEligibilityScope = ResearchEligibilityScope.DAILY_RESEARCH,
        status: ResearchEligibilityStatus = ResearchEligibilityStatus.ELIGIBLE,
        reasons: tuple[ResearchEligibilityReason, ...] = (),
        evidence: tuple[EligibilityEvidenceReference, ...] | None = None,
        period: ResearchPeriod | None = None,
        config_id: str = "research-eligibility",
        config_version: str = "1",
    ) -> ResearchEligibilityResult:
        return ResearchEligibilityResult(
            stock_code="005930",
            scope=scope,
            research_period=period or self.period(),
            status=status,
            reason_codes=reasons,
            evidence_references=(self.evidence(),) if evidence is None else evidence,
            config_id=config_id,
            config_version=config_version,
        )

    def test_valid_daily_and_intraday_eligible_are_independent(self) -> None:
        daily = self.result()
        intraday = self.result(scope=ResearchEligibilityScope.INTRADAY_COMPARISON)
        self.assertTrue(daily.admitted)
        self.assertTrue(intraday.admitted)
        self.assertNotEqual(daily.result_id, intraday.result_id)
        self.assertFalse(hasattr(intraday, "daily_eligibility"))

    def test_invalid_stock_code_is_rejected(self) -> None:
        valid = self.result()
        for value in ("5930", "A12345", "12AB34", "１２３４５６"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(valid, stock_code=value)

    def test_period_order_and_non_date_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.period(required_data_start=date(2024, 2, 1))
        with self.assertRaises(ValueError):
            self.period(research_end=date(2023, 12, 31))
        with self.assertRaises(TypeError):
            self.period(  # type: ignore[arg-type]
                research_start=datetime(2024, 1, 2, tzinfo=timezone.utc)
            )

    def test_nonnumeric_source_identity_has_typed_noneligible_result(self) -> None:
        result = ResearchEligibilityResult(
            stock_code="A12345",
            scope=ResearchEligibilityScope.DAILY_RESEARCH,
            research_period=self.period(),
            status=ResearchEligibilityStatus.EXCLUDED,
            reason_codes=(ResearchEligibilityReason.NON_NUMERIC_CANONICAL_CODE,),
            evidence_references=(self.evidence(),),
            config_id="research-eligibility",
            config_version="1",
        )
        self.assertEqual("A12345", result.source_stock_code)
        self.assertIsNone(result.canonical_stock_code)
        self.assertFalse(result.admitted)

    def test_missing_canonical_identity_requires_typed_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "NON_NUMERIC_CANONICAL_CODE"):
            ResearchEligibilityResult(
                stock_code="A12345",
                scope=ResearchEligibilityScope.DAILY_RESEARCH,
                research_period=self.period(),
                status=ResearchEligibilityStatus.REVIEW_REQUIRED,
                reason_codes=(ResearchEligibilityReason.MISSING_PROVENANCE,),
                evidence_references=(),
                config_id="research-eligibility",
                config_version="1",
            )

    def test_result_id_includes_source_and_canonical_subject_identity(self) -> None:
        def invalid_result(source_code: str) -> ResearchEligibilityResult:
            return ResearchEligibilityResult(
                stock_code=source_code,
                scope=ResearchEligibilityScope.DAILY_RESEARCH,
                research_period=self.period(),
                status=ResearchEligibilityStatus.EXCLUDED,
                reason_codes=(ResearchEligibilityReason.NON_NUMERIC_CANONICAL_CODE,),
                evidence_references=(self.evidence(),),
                config_id="research-eligibility",
                config_version="1",
            )

        first = invalid_result("A12345")
        repeated = invalid_result("A12345")
        other = invalid_result("B12345")
        self.assertEqual(first.result_id, repeated.result_id)
        self.assertNotEqual(first.result_id, other.result_id)
        self.assertIsNone(first.canonical_stock_code)

    def test_status_reason_invariants_are_fail_safe(self) -> None:
        reason = (ResearchEligibilityReason.RAW_CONFLICT,)
        with self.assertRaisesRegex(ValueError, "ELIGIBLE"):
            self.result(reasons=reason)
        for status in (
            ResearchEligibilityStatus.EXCLUDED,
            ResearchEligibilityStatus.REVIEW_REQUIRED,
        ):
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(ValueError, "requires at least one reason"),
            ):
                self.result(status=status)

    def test_non_eligible_statuses_are_never_admitted(self) -> None:
        for status in (
            ResearchEligibilityStatus.EXCLUDED,
            ResearchEligibilityStatus.REVIEW_REQUIRED,
        ):
            result = self.result(
                status=status,
                reasons=(ResearchEligibilityReason.MISSING_PROVENANCE,),
            )
            self.assertFalse(result.admitted)

    def test_reasons_are_deduplicated_in_enum_order(self) -> None:
        result = self.result(
            status=ResearchEligibilityStatus.EXCLUDED,
            reasons=(
                ResearchEligibilityReason.RAW_CONFLICT,
                ResearchEligibilityReason.CODE_MAPPING_CONFLICT,
                ResearchEligibilityReason.RAW_CONFLICT,
            ),
        )
        self.assertEqual(
            (
                ResearchEligibilityReason.CODE_MAPPING_CONFLICT,
                ResearchEligibilityReason.RAW_CONFLICT,
            ),
            result.reason_codes,
        )

    def test_evidence_is_deduplicated_and_canonically_ordered(self) -> None:
        first = self.evidence("first")
        second = self.evidence(
            "second", evidence_type=EligibilityEvidenceType.HISTORICAL_MAPPING
        )
        result = self.result(evidence=(second, first, second))
        self.assertEqual((first, second), result.evidence_references)

    def test_evidence_invariants_and_digest_normalization(self) -> None:
        uppercase = self.evidence(digest="A" * 64)
        self.assertEqual("a" * 64, uppercase.artifact_sha256)
        with self.assertRaisesRegex(ValueError, "64-character"):
            self.evidence(digest="bad")
        with self.assertRaisesRegex(ValueError, "coverage_start"):
            replace(
                uppercase,
                coverage_start=date(2025, 1, 1),
                coverage_end=date(2024, 1, 1),
            )

    def test_eligible_requires_evidence_but_review_can_report_missing_evidence(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "requires evidence"):
            self.result(evidence=())
        review = ResearchEligibilityResult(
            "005930",
            ResearchEligibilityScope.DAILY_RESEARCH,
            self.period(),
            ResearchEligibilityStatus.REVIEW_REQUIRED,
            (ResearchEligibilityReason.MISSING_PROVENANCE,),
            (),
            "research-eligibility",
            "1",
        )
        self.assertFalse(review.admitted)

    def test_result_id_is_order_independent_and_has_no_time_or_random_input(
        self,
    ) -> None:
        first_evidence = self.evidence("first")
        second_evidence = self.evidence("second")
        reasons = (
            ResearchEligibilityReason.RAW_CONFLICT,
            ResearchEligibilityReason.CODE_MAPPING_CONFLICT,
        )
        first = self.result(
            status=ResearchEligibilityStatus.EXCLUDED,
            reasons=reasons,
            evidence=(first_evidence, second_evidence),
        )
        second = self.result(
            status=ResearchEligibilityStatus.EXCLUDED,
            reasons=tuple(reversed(reasons)),
            evidence=(second_evidence, first_evidence),
        )
        self.assertEqual(first, second)
        self.assertEqual(64, len(first.result_id))
        source = inspect.getsource(models)
        self.assertNotIn("datetime.now", source)
        self.assertNotIn("uuid", source.lower())
        self.assertNotIn("random", source.lower())

    def test_semantic_changes_change_result_id(self) -> None:
        baseline = self.result()
        changes = (
            self.result(config_version="2"),
            self.result(scope=ResearchEligibilityScope.INTRADAY_COMPARISON),
            self.result(period=self.period(research_start=date(2024, 1, 3))),
            self.result(evidence=(self.evidence(digest="b" * 64),)),
        )
        for changed in changes:
            self.assertNotEqual(baseline.result_id, changed.result_id)

    def test_domain_layer_has_no_adapter_or_backtest_dependency(self) -> None:
        source = inspect.getsource(models)
        for forbidden in (
            "backtest_engine",
            "stock_mapping",
            "telegram_top30_parser",
            "krx_openapi",
        ):
            self.assertNotIn(forbidden, source)
        result = self.result()
        self.assertFalse(hasattr(result, "historical_state"))
        self.assertFalse(hasattr(result, "daily_bars"))
        self.assertFalse(hasattr(result, "backtest"))


if __name__ == "__main__":
    unittest.main()
