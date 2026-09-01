from __future__ import annotations

import inspect
import unittest
from dataclasses import replace
from datetime import date

from src.research_universe import historical_eligibility
from src.research_universe.historical_eligibility import (
    HistoricalEligibilityConfig,
    HistoricalEligibilityInput,
    HistoricalEvidenceIssue,
    ManualHistoricalMapping,
    assess_historical_eligibility,
)
from src.research_universe.models import (
    EligibilityEvidenceReference,
    EligibilityEvidenceType,
    ResearchEligibilityReason,
    ResearchEligibilityScope,
    ResearchEligibilityStatus,
    ResearchPeriod,
)
from src.stock_mapping.historical_master import HistoricalStock, MappingResult
from src.stock_mapping.krx_stock_basic_adapter import (
    CanonicalCodeEligibility,
    EffectiveDateBasis,
    ListingDateBasis,
    MappingNameSource,
    SnapshotObservation,
    SnapshotProvenance,
    TransitionCandidate,
)


class HistoricalResearchEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.period = ResearchPeriod(
            research_start=date(2024, 2, 1),
            research_end=date(2024, 11, 30),
            required_data_start=date(2024, 1, 2),
            required_data_end=date(2024, 12, 31),
        )
        self.config = HistoricalEligibilityConfig(
            "historical-eligibility",
            "1",
            frozenset({"COMMON"}),
            True,
        )

    def record(self, **changes: object) -> HistoricalStock:
        values: dict[str, object] = {
            "stock_code": "005930",
            "market": "KOSPI",
            "stock_name": "삼성전자",
            "stock_name_normalized": "삼성전자",
            "valid_from": date(2000, 1, 1),
            "valid_to": None,
            "listing_date": date(1975, 6, 11),
            "delisting_date": None,
            "security_type": "COMMON",
            "security_type_raw": "보통주",
            "source": "KRX:stock-basic",
            "source_as_of": date(2026, 8, 31),
        }
        values.update(changes)
        return HistoricalStock(**values)  # type: ignore[arg-type]

    def reference(
        self,
        source_id: str = "historical-master-v1",
        digest: str = "a" * 64,
    ) -> EligibilityEvidenceReference:
        return EligibilityEvidenceReference(
            EligibilityEvidenceType.HISTORICAL_MAPPING,
            source_id,
            f"master:{source_id}",
            digest,
            "1",
            date(2000, 1, 1),
            None,
        )

    def evidence(self, **changes: object) -> HistoricalEligibilityInput:
        values: dict[str, object] = {
            "records": (self.record(),),
            "mapping_results": (),
            "transition_candidates": (),
            "evidence_references": (self.reference(),),
            "issues": (),
            "manual_mappings": (),
        }
        values.update(changes)
        return HistoricalEligibilityInput(**values)  # type: ignore[arg-type]

    def assess(
        self,
        evidence: HistoricalEligibilityInput | None = None,
        *,
        stock_code: str = "005930",
        scope: ResearchEligibilityScope = ResearchEligibilityScope.DAILY_RESEARCH,
        period: ResearchPeriod | None = None,
        config: HistoricalEligibilityConfig | None = None,
    ):
        return assess_historical_eligibility(
            stock_code,
            scope,
            period or self.period,
            evidence or self.evidence(),
            config or self.config,
        )

    def transition(
        self,
        previous_on: date,
        current_on: date,
    ) -> TransitionCandidate:
        def observation(
            observed_on: date, name: str, bas_dd: str
        ) -> SnapshotObservation:
            provenance = SnapshotProvenance(
                "stk_isu_base_info",
                bas_dd,
                f"ignored/{bas_dd}.json",
                ("b" if name == "old" else "c") * 64,
                "2026-08-31T00:00:00Z",
                100,
                "K1",
                "1",
                "SUCCESS",
            )
            return SnapshotObservation(
                "005930",
                "005930",
                CanonicalCodeEligibility.ELIGIBLE,
                "ELIGIBLE_NUMERIC_CODE",
                observed_on,
                f"{name}보통주",
                name,
                MappingNameSource.ISU_ABBRV,
                name,
                "KOSPI",
                "19750611",
                date(1975, 6, 11),
                ListingDateBasis.KRX_LIST_DD_CANDIDATE,
                "보통주",
                provenance,
                1,
            )

        previous = observation(previous_on, "old", previous_on.strftime("%Y%m%d"))
        current = observation(current_on, "new", current_on.strftime("%Y%m%d"))
        return TransitionCandidate(
            "005930",
            previous_on,
            current_on,
            ("normalized_mapping_name",),
            EffectiveDateBasis.OBSERVED_WINDOW,
            None,
            previous,
            current,
            (previous.provenance, current.provenance),
        )

    def test_full_required_period_exact_mapping_is_eligible(self) -> None:
        result = self.assess()
        self.assertEqual(ResearchEligibilityStatus.ELIGIBLE, result.status)
        self.assertTrue(result.admitted)
        self.assertEqual((), result.reason_codes)

    def test_missing_mapping_and_gap_are_review_required(self) -> None:
        missing = self.assess(
            HistoricalEligibilityInput(evidence_references=(self.reference(),))
        )
        gap_records = (
            self.record(valid_to=date(2024, 5, 31)),
            self.record(valid_from=date(2024, 7, 1), stock_name="new"),
        )
        gap = self.assess(self.evidence(records=gap_records))
        for result in (missing, gap):
            self.assertEqual(ResearchEligibilityStatus.REVIEW_REQUIRED, result.status)
            self.assertIn(
                ResearchEligibilityReason.HISTORICAL_COVERAGE_UNRESOLVED,
                result.reason_codes,
            )

    def test_overlap_duplicate_and_cross_market_conflicts_are_excluded(self) -> None:
        overlap = self.record(valid_from=date(2024, 6, 1), stock_name="new")
        duplicate = self.assess(self.evidence(records=(self.record(), overlap)))
        cross_market = self.assess(
            self.evidence(records=(self.record(), replace(overlap, market="KOSDAQ")))
        )
        self.assertIn(
            ResearchEligibilityReason.DUPLICATE_SECURITY_IDENTITY,
            duplicate.reason_codes,
        )
        self.assertEqual(ResearchEligibilityStatus.EXCLUDED, duplicate.status)
        self.assertIn(
            ResearchEligibilityReason.CROSS_MARKET_CONFLICT,
            cross_market.reason_codes,
        )

    def test_typed_identity_market_and_security_issues_map_to_reasons(self) -> None:
        cases = (
            (
                HistoricalEvidenceIssue.NORMALIZED_NAME_AMBIGUITY,
                ResearchEligibilityReason.NAME_MAPPING_CONFLICT,
            ),
            (
                HistoricalEvidenceIssue.CROSS_MARKET_CONFLICT,
                ResearchEligibilityReason.CROSS_MARKET_CONFLICT,
            ),
            (
                HistoricalEvidenceIssue.DUPLICATE_SECURITY_IDENTITY,
                ResearchEligibilityReason.DUPLICATE_SECURITY_IDENTITY,
            ),
        )
        for issue, reason in cases:
            with self.subTest(issue=issue):
                result = self.assess(self.evidence(issues=(issue,)))
                self.assertEqual(ResearchEligibilityStatus.EXCLUDED, result.status)
                self.assertIn(reason, result.reason_codes)
        unsupported = self.assess(
            self.evidence(records=(self.record(security_type="PREFERRED"),))
        )
        self.assertIn(
            ResearchEligibilityReason.UNSUPPORTED_SECURITY_TYPE,
            unsupported.reason_codes,
        )

    def test_nonnumeric_code_is_excluded_without_normalization(self) -> None:
        results = []
        for value in ("A12345", "12AB34"):
            with self.subTest(value=value):
                result = self.assess(stock_code=value)
                results.append(result)
                self.assertEqual(value, result.source_stock_code)
                self.assertIsNone(result.canonical_stock_code)
                self.assertEqual(ResearchEligibilityStatus.EXCLUDED, result.status)
                self.assertIn(
                    ResearchEligibilityReason.NON_NUMERIC_CANONICAL_CODE,
                    result.reason_codes,
                )
                self.assertNotIn(
                    ResearchEligibilityReason.CODE_MAPPING_CONFLICT,
                    result.reason_codes,
                )
        self.assertNotEqual(results[0].result_id, results[1].result_id)

    def test_historical_interval_boundaries_reuse_master_semantics(self) -> None:
        def period(start: date, end: date) -> ResearchPeriod:
            return ResearchPeriod(start, end, start, end)

        cases = (
            (
                "required start equals inclusive valid_from",
                self.record(valid_from=date(2024, 1, 2)),
                period(date(2024, 1, 2), date(2024, 1, 3)),
                ResearchEligibilityStatus.ELIGIBLE,
            ),
            (
                "required end equals inclusive valid_to",
                self.record(valid_from=date(2024, 1, 1), valid_to=date(2024, 1, 3)),
                period(date(2024, 1, 2), date(2024, 1, 3)),
                ResearchEligibilityStatus.ELIGIBLE,
            ),
            (
                "required start immediately after valid_to",
                self.record(valid_to=date(2024, 1, 1)),
                period(date(2024, 1, 2), date(2024, 1, 2)),
                ResearchEligibilityStatus.REVIEW_REQUIRED,
            ),
            (
                "delisting date equals required start",
                self.record(delisting_date=date(2024, 1, 2)),
                period(date(2024, 1, 2), date(2024, 1, 2)),
                ResearchEligibilityStatus.REVIEW_REQUIRED,
            ),
            (
                "delisting date equals required end",
                self.record(delisting_date=date(2024, 1, 3)),
                period(date(2024, 1, 2), date(2024, 1, 3)),
                ResearchEligibilityStatus.REVIEW_REQUIRED,
            ),
            (
                "open-ended valid_to",
                self.record(valid_to=None),
                period(date(2024, 1, 2), date(2024, 1, 3)),
                ResearchEligibilityStatus.ELIGIBLE,
            ),
        )
        for name, record, required_period, expected_status in cases:
            with self.subTest(name=name):
                result = self.assess(
                    self.evidence(records=(record,)), period=required_period
                )
                self.assertEqual(expected_status, result.status)
                self.assertEqual(
                    expected_status is ResearchEligibilityStatus.ELIGIBLE,
                    ResearchEligibilityReason.HISTORICAL_COVERAGE_UNRESOLVED
                    not in result.reason_codes,
                )

    def test_provenance_conditions_are_typed_and_fail_safe(self) -> None:
        missing = self.assess(self.evidence(evidence_references=()))
        self.assertEqual(ResearchEligibilityStatus.REVIEW_REQUIRED, missing.status)
        self.assertIn(
            ResearchEligibilityReason.MISSING_PROVENANCE, missing.reason_codes
        )
        for issue, reason in (
            (
                HistoricalEvidenceIssue.ARTIFACT_DIGEST_MISMATCH,
                ResearchEligibilityReason.ARTIFACT_DIGEST_MISMATCH,
            ),
            (
                HistoricalEvidenceIssue.RAW_CONFLICT,
                ResearchEligibilityReason.RAW_CONFLICT,
            ),
        ):
            result = self.assess(self.evidence(issues=(issue,)))
            self.assertEqual(ResearchEligibilityStatus.EXCLUDED, result.status)
            self.assertIn(reason, result.reason_codes)

    def test_transition_inside_window_is_ambiguous_and_never_promoted(self) -> None:
        candidate = self.transition(date(2024, 5, 1), date(2024, 5, 2))
        result = self.assess(self.evidence(transition_candidates=(candidate,)))
        self.assertEqual(ResearchEligibilityStatus.REVIEW_REQUIRED, result.status)
        self.assertIn(
            ResearchEligibilityReason.AMBIGUOUS_NAME_HISTORY,
            result.reason_codes,
        )
        self.assertEqual(
            EffectiveDateBasis.OBSERVED_WINDOW, candidate.effective_date_basis
        )
        self.assertIsNone(candidate.confirmed_effective_from)
        self.assertNotEqual(
            candidate.current_observed_on,
            getattr(result, "confirmed_effective_from", None),
        )

    def test_transition_outside_required_window_does_not_exclude(self) -> None:
        candidate = self.transition(date(2023, 1, 2), date(2023, 1, 3))
        result = self.assess(self.evidence(transition_candidates=(candidate,)))
        self.assertEqual(ResearchEligibilityStatus.ELIGIBLE, result.status)

    def test_unresolved_listing_relisting_and_delisting_are_review(self) -> None:
        cases = (
            (
                HistoricalEvidenceIssue.LISTING_BOUNDARY_UNRESOLVED,
                ResearchEligibilityReason.LISTING_HISTORY_UNRESOLVED,
            ),
            (
                HistoricalEvidenceIssue.RELISTING_BOUNDARY_UNRESOLVED,
                ResearchEligibilityReason.RELISTING_HISTORY_UNRESOLVED,
            ),
            (
                HistoricalEvidenceIssue.DELISTING_BOUNDARY_UNRESOLVED,
                ResearchEligibilityReason.DELISTING_HISTORY_UNRESOLVED,
            ),
        )
        for issue, reason in cases:
            result = self.assess(self.evidence(issues=(issue,)))
            self.assertEqual(ResearchEligibilityStatus.REVIEW_REQUIRED, result.status)
            self.assertIn(reason, result.reason_codes)

    def manual_mapping(self, **changes: object) -> ManualHistoricalMapping:
        values: dict[str, object] = {
            "stock_code": "005930",
            "effective_start": date(2024, 1, 2),
            "effective_end": date(2024, 12, 31),
            "source_id": "manual-005930-v1",
            "source_reference": "manual:approved-mapping",
            "source_note": "official identity reviewed",
            "artifact_sha256": "d" * 64,
            "schema_version": "1",
        }
        values.update(changes)
        return ManualHistoricalMapping(**values)  # type: ignore[arg-type]

    def test_complete_manual_mapping_can_cover_window_but_incomplete_cannot(
        self,
    ) -> None:
        valid = self.assess(
            HistoricalEligibilityInput(manual_mappings=(self.manual_mapping(),))
        )
        self.assertEqual(ResearchEligibilityStatus.ELIGIBLE, valid.status)
        invalid = self.assess(
            HistoricalEligibilityInput(
                manual_mappings=(self.manual_mapping(source_note=""),)
            )
        )
        self.assertEqual(ResearchEligibilityStatus.REVIEW_REQUIRED, invalid.status)
        self.assertIn(
            ResearchEligibilityReason.HISTORICAL_COVERAGE_UNRESOLVED,
            invalid.reason_codes,
        )

    def test_mapping_statuses_are_converted_without_message_parsing(self) -> None:
        base = {
            "report_date": "2024-06-03",
            "observed_stock_name": "name",
            "stock_name_normalized": "name",
            "stock_code": "",
            "market": "",
            "confidence": "0.00",
            "source": "fixture",
            "review_note": "arbitrary text must not be parsed",
        }
        ambiguous = MappingResult(
            **base,
            mapping_status="REVIEW_REQUIRED",
            mapping_method="ambiguous_normalized_temporal",
        )
        result = self.assess(self.evidence(mapping_results=(ambiguous,)))
        self.assertIn(
            ResearchEligibilityReason.NAME_MAPPING_CONFLICT, result.reason_codes
        )

    def test_multiple_reasons_and_input_order_are_deterministic(self) -> None:
        issues = (
            HistoricalEvidenceIssue.RAW_CONFLICT,
            HistoricalEvidenceIssue.NORMALIZED_NAME_AMBIGUITY,
            HistoricalEvidenceIssue.ARTIFACT_DIGEST_MISMATCH,
        )
        first = self.assess(
            self.evidence(
                issues=issues,
                evidence_references=(self.reference("z"), self.reference("a")),
            )
        )
        second = self.assess(
            self.evidence(
                issues=tuple(reversed(issues)),
                evidence_references=(self.reference("a"), self.reference("z")),
            )
        )
        self.assertEqual(first, second)
        self.assertEqual(
            tuple(
                reason
                for reason in ResearchEligibilityReason
                if reason in set(first.reason_codes)
            ),
            first.reason_codes,
        )

    def test_scope_and_required_period_change_result_identity(self) -> None:
        baseline = self.assess()
        intraday = self.assess(scope=ResearchEligibilityScope.INTRADAY_COMPARISON)
        changed_period = ResearchPeriod(
            date(2024, 2, 1),
            date(2024, 11, 30),
            date(2024, 1, 3),
            date(2024, 12, 31),
        )
        shifted = self.assess(period=changed_period)
        self.assertNotEqual(baseline.result_id, intraday.result_id)
        self.assertNotEqual(baseline.result_id, shifted.result_id)

    def test_adapter_has_no_reconstruction_data_or_network_dependency(self) -> None:
        source = inspect.getsource(historical_eligibility)
        for forbidden in (
            "name_change_confirmation",
            "requests",
            "httpx",
            "backfill",
            "DailyBar",
            "FiveMinuteBar",
            "SourceSecurityState(",
        ):
            self.assertNotIn(forbidden, source)
        result = self.assess()
        self.assertFalse(hasattr(result, "historical_state"))
        self.assertFalse(hasattr(result, "daily_quality"))


if __name__ == "__main__":
    unittest.main()
