"""Period-aware historical identity eligibility without reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from itertools import pairwise

from src.stock_mapping.historical_master import HistoricalStock, MappingResult
from src.stock_mapping.krx_stock_basic_adapter import (
    EffectiveDateBasis,
    TransitionCandidate,
)

from .models import (
    EligibilityEvidenceReference,
    EligibilityEvidenceType,
    ResearchEligibilityReason,
    ResearchEligibilityResult,
    ResearchEligibilityScope,
    ResearchEligibilityStatus,
    ResearchPeriod,
)


class HistoricalEvidenceIssue(str, Enum):
    """Typed upstream conditions that cannot be derived safely from records."""

    DUPLICATE_SECURITY_IDENTITY = "DUPLICATE_SECURITY_IDENTITY"
    NORMALIZED_NAME_AMBIGUITY = "NORMALIZED_NAME_AMBIGUITY"
    CROSS_MARKET_CONFLICT = "CROSS_MARKET_CONFLICT"
    MARKET_TRANSFER_UNRESOLVED = "MARKET_TRANSFER_UNRESOLVED"
    LISTING_BOUNDARY_UNRESOLVED = "LISTING_BOUNDARY_UNRESOLVED"
    RELISTING_BOUNDARY_UNRESOLVED = "RELISTING_BOUNDARY_UNRESOLVED"
    DELISTING_BOUNDARY_UNRESOLVED = "DELISTING_BOUNDARY_UNRESOLVED"
    RAW_CONFLICT = "RAW_CONFLICT"
    ARTIFACT_DIGEST_MISMATCH = "ARTIFACT_DIGEST_MISMATCH"
    DATA_VALIDATION_ERROR = "DATA_VALIDATION_ERROR"


@dataclass(frozen=True, slots=True)
class HistoricalEligibilityConfig:
    config_id: str
    config_version: str
    supported_security_types: frozenset[str]
    allow_manual_mapping: bool

    def __post_init__(self) -> None:
        if not self.config_id.strip() or not self.config_version.strip():
            raise ValueError("config_id and config_version are required")
        if not self.supported_security_types or any(
            not isinstance(item, str) or not item.strip()
            for item in self.supported_security_types
        ):
            raise ValueError("supported_security_types must be explicit and non-empty")


@dataclass(frozen=True, slots=True)
class ManualHistoricalMapping:
    stock_code: str
    effective_start: date | None
    effective_end: date | None
    source_id: str
    source_reference: str
    source_note: str
    artifact_sha256: str | None
    schema_version: str

    @property
    def complete(self) -> bool:
        return (
            _is_canonical_code(self.stock_code)
            and type(self.effective_start) is date
            and type(self.effective_end) is date
            and self.effective_start <= self.effective_end
            and bool(self.source_id.strip())
            and bool(self.source_reference.strip())
            and bool(self.source_note.strip())
            and bool(self.schema_version.strip())
        )


@dataclass(frozen=True, slots=True)
class HistoricalEligibilityInput:
    records: tuple[HistoricalStock, ...] = ()
    mapping_results: tuple[MappingResult, ...] = ()
    transition_candidates: tuple[TransitionCandidate, ...] = ()
    evidence_references: tuple[EligibilityEvidenceReference, ...] = ()
    issues: tuple[HistoricalEvidenceIssue, ...] = ()
    manual_mappings: tuple[ManualHistoricalMapping, ...] = ()


_ISSUE_REASON = {
    HistoricalEvidenceIssue.DUPLICATE_SECURITY_IDENTITY: (
        ResearchEligibilityReason.DUPLICATE_SECURITY_IDENTITY
    ),
    HistoricalEvidenceIssue.NORMALIZED_NAME_AMBIGUITY: (
        ResearchEligibilityReason.NAME_MAPPING_CONFLICT
    ),
    HistoricalEvidenceIssue.CROSS_MARKET_CONFLICT: (
        ResearchEligibilityReason.CROSS_MARKET_CONFLICT
    ),
    HistoricalEvidenceIssue.MARKET_TRANSFER_UNRESOLVED: (
        ResearchEligibilityReason.MARKET_TRANSFER_UNRESOLVED
    ),
    HistoricalEvidenceIssue.LISTING_BOUNDARY_UNRESOLVED: (
        ResearchEligibilityReason.LISTING_HISTORY_UNRESOLVED
    ),
    HistoricalEvidenceIssue.RELISTING_BOUNDARY_UNRESOLVED: (
        ResearchEligibilityReason.RELISTING_HISTORY_UNRESOLVED
    ),
    HistoricalEvidenceIssue.DELISTING_BOUNDARY_UNRESOLVED: (
        ResearchEligibilityReason.DELISTING_HISTORY_UNRESOLVED
    ),
    HistoricalEvidenceIssue.RAW_CONFLICT: ResearchEligibilityReason.RAW_CONFLICT,
    HistoricalEvidenceIssue.ARTIFACT_DIGEST_MISMATCH: (
        ResearchEligibilityReason.ARTIFACT_DIGEST_MISMATCH
    ),
    HistoricalEvidenceIssue.DATA_VALIDATION_ERROR: (
        ResearchEligibilityReason.DATA_VALIDATION_ERROR
    ),
}

_EXCLUDED_REASONS = frozenset(
    {
        ResearchEligibilityReason.NON_NUMERIC_CANONICAL_CODE,
        ResearchEligibilityReason.DUPLICATE_SECURITY_IDENTITY,
        ResearchEligibilityReason.UNSUPPORTED_SECURITY_TYPE,
        ResearchEligibilityReason.CODE_MAPPING_CONFLICT,
        ResearchEligibilityReason.NAME_MAPPING_CONFLICT,
        ResearchEligibilityReason.CROSS_MARKET_CONFLICT,
        ResearchEligibilityReason.RAW_CONFLICT,
        ResearchEligibilityReason.ARTIFACT_DIGEST_MISMATCH,
        ResearchEligibilityReason.DATA_VALIDATION_ERROR,
    }
)


def assess_historical_eligibility(
    stock_code: str,
    scope: ResearchEligibilityScope,
    period: ResearchPeriod,
    evidence: HistoricalEligibilityInput,
    config: HistoricalEligibilityConfig,
) -> ResearchEligibilityResult:
    """Assess identity safety over the required-data window.

    The source code is preserved exactly.  A noncanonical value produces an
    excluded typed result and is never normalized or used to query canonical
    historical records.
    """
    reasons: list[ResearchEligibilityReason] = [
        _ISSUE_REASON[item] for item in evidence.issues
    ]
    references = list(evidence.evidence_references)
    canonical_stock_code = stock_code if _is_canonical_code(stock_code) else None

    if canonical_stock_code is None:
        reasons.append(ResearchEligibilityReason.NON_NUMERIC_CANONICAL_CODE)
        record_coverage = False
        manual_coverage = False
    else:
        records = sorted(
            (
                item
                for item in evidence.records
                if item.stock_code == canonical_stock_code
            ),
            key=lambda item: (item.valid_from, item.market, item.stock_name),
        )
        other_codes = {item.stock_code for item in evidence.records} - {
            canonical_stock_code
        }
        if other_codes:
            reasons.append(ResearchEligibilityReason.CODE_MAPPING_CONFLICT)

        manual_coverage = _assess_manual_mappings(
            canonical_stock_code,
            period,
            evidence.manual_mappings,
            config,
            reasons,
            references,
        )
        record_coverage = _assess_records(records, period, config, reasons)
        _assess_mapping_results(
            canonical_stock_code,
            period,
            evidence.mapping_results,
            evidence.manual_mappings,
            reasons,
        )
        _assess_transitions(
            canonical_stock_code,
            period,
            evidence.transition_candidates,
            reasons,
            references,
        )

    if not references:
        reasons.append(ResearchEligibilityReason.MISSING_PROVENANCE)
    if canonical_stock_code is not None and not record_coverage and not manual_coverage:
        reasons.append(ResearchEligibilityReason.HISTORICAL_COVERAGE_UNRESOLVED)

    canonical_reasons = tuple(
        reason for reason in ResearchEligibilityReason if reason in set(reasons)
    )
    if canonical_reasons:
        status = (
            ResearchEligibilityStatus.EXCLUDED
            if any(reason in _EXCLUDED_REASONS for reason in canonical_reasons)
            else ResearchEligibilityStatus.REVIEW_REQUIRED
        )
    else:
        status = ResearchEligibilityStatus.ELIGIBLE
    return ResearchEligibilityResult(
        stock_code=stock_code,
        scope=scope,
        research_period=period,
        status=status,
        reason_codes=canonical_reasons,
        evidence_references=tuple(references),
        config_id=config.config_id,
        config_version=config.config_version,
        canonical_stock_code=canonical_stock_code,
    )


def _assess_records(
    records: list[HistoricalStock],
    period: ResearchPeriod,
    config: HistoricalEligibilityConfig,
    reasons: list[ResearchEligibilityReason],
) -> bool:
    if not records:
        return False
    for record in records:
        if record.security_type not in config.supported_security_types:
            reasons.append(ResearchEligibilityReason.UNSUPPORTED_SECURITY_TYPE)

    intervals = sorted(
        (_effective_interval(item) for item in records), key=lambda item: item[0]
    )
    relevant = [
        item
        for item in intervals
        if _intervals_overlap(
            item[0], item[1], period.required_data_start, period.required_data_end
        )
    ]
    for previous, current in pairwise(relevant):
        if current[0] <= previous[1]:
            reasons.append(ResearchEligibilityReason.DUPLICATE_SECURITY_IDENTITY)
        if current[2].market != previous[2].market:
            if current[0] <= previous[1]:
                reasons.append(ResearchEligibilityReason.CROSS_MARKET_CONFLICT)
            else:
                reasons.append(ResearchEligibilityReason.MARKET_TRANSFER_UNRESOLVED)

    if not relevant:
        return False
    cursor = period.required_data_start
    for start, end, _ in relevant:
        if end < cursor:
            continue
        if start > cursor:
            return False
        if end >= period.required_data_end:
            return True
        cursor = max(cursor, end + timedelta(days=1))
    return cursor > period.required_data_end


def _effective_interval(record: HistoricalStock) -> tuple[date, date, HistoricalStock]:
    start = max(
        record.valid_from,
        record.listing_date if record.listing_date is not None else record.valid_from,
    )
    ends = [record.valid_to or date.max]
    if record.delisting_date is not None:
        ends.append(record.delisting_date - timedelta(days=1))
    return start, min(ends), record


def _assess_mapping_results(
    stock_code: str,
    period: ResearchPeriod,
    mapping_results: tuple[MappingResult, ...],
    manual_mappings: tuple[ManualHistoricalMapping, ...],
    reasons: list[ResearchEligibilityReason],
) -> None:
    for result in sorted(
        mapping_results,
        key=lambda item: (
            item.report_date,
            item.observed_stock_name,
            item.mapping_status,
        ),
    ):
        try:
            report_date = date.fromisoformat(result.report_date)
        except ValueError:
            reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)
            continue
        if not period.required_data_start <= report_date <= period.required_data_end:
            continue
        if result.mapping_status == "UNMAPPED":
            reasons.append(ResearchEligibilityReason.HISTORICAL_COVERAGE_UNRESOLVED)
        elif result.mapping_status == "REVIEW_REQUIRED":
            if result.mapping_method == "security_type_guard":
                reasons.append(ResearchEligibilityReason.UNSUPPORTED_SECURITY_TYPE)
            else:
                reasons.append(ResearchEligibilityReason.NAME_MAPPING_CONFLICT)
        elif result.mapping_status == "MANUAL_CONFIRMED":
            if not any(
                item.complete
                and item.stock_code == stock_code
                and item.effective_start <= report_date <= item.effective_end
                for item in manual_mappings
            ):
                reasons.append(ResearchEligibilityReason.HISTORICAL_COVERAGE_UNRESOLVED)
        elif result.mapping_status in {
            "AUTO_EXACT_TEMPORAL",
            "AUTO_NORMALIZED_TEMPORAL",
        }:
            if result.stock_code != stock_code:
                reasons.append(ResearchEligibilityReason.CODE_MAPPING_CONFLICT)
        else:
            reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)


def _assess_transitions(
    stock_code: str,
    period: ResearchPeriod,
    candidates: tuple[TransitionCandidate, ...],
    reasons: list[ResearchEligibilityReason],
    references: list[EligibilityEvidenceReference],
) -> None:
    for candidate in sorted(
        candidates,
        key=lambda item: (
            item.canonical_stock_code,
            item.previous_observed_on,
            item.current_observed_on,
        ),
    ):
        if candidate.canonical_stock_code != stock_code:
            reasons.append(ResearchEligibilityReason.CODE_MAPPING_CONFLICT)
            continue
        if (
            candidate.effective_date_basis is not EffectiveDateBasis.OBSERVED_WINDOW
            or candidate.confirmed_effective_from is not None
        ):
            reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)
        if _intervals_overlap(
            candidate.previous_observed_on,
            candidate.current_observed_on,
            period.required_data_start,
            period.required_data_end,
        ):
            reasons.append(ResearchEligibilityReason.AMBIGUOUS_NAME_HISTORY)
        for provenance in candidate.provenance_references:
            references.append(
                EligibilityEvidenceReference(
                    evidence_type=EligibilityEvidenceType.KRX_SNAPSHOT,
                    source_id=f"{provenance.service_id}:{provenance.bas_dd}",
                    source_reference=(
                        f"krx:{provenance.service_id}:basDd={provenance.bas_dd}"
                    ),
                    artifact_sha256=provenance.raw_sha256,
                    schema_version=provenance.schema_version,
                    coverage_start=candidate.previous_observed_on,
                    coverage_end=candidate.current_observed_on,
                )
            )


def _assess_manual_mappings(
    stock_code: str,
    period: ResearchPeriod,
    mappings: tuple[ManualHistoricalMapping, ...],
    config: HistoricalEligibilityConfig,
    reasons: list[ResearchEligibilityReason],
    references: list[EligibilityEvidenceReference],
) -> bool:
    if not mappings:
        return False
    if not config.allow_manual_mapping:
        reasons.append(ResearchEligibilityReason.HISTORICAL_COVERAGE_UNRESOLVED)
        return False
    coverage = False
    for item in sorted(
        mappings,
        key=lambda value: (
            value.stock_code,
            value.effective_start or date.min,
            value.source_id,
        ),
    ):
        if not item.complete or item.stock_code != stock_code:
            reasons.append(ResearchEligibilityReason.HISTORICAL_COVERAGE_UNRESOLVED)
            continue
        assert item.effective_start is not None and item.effective_end is not None
        references.append(
            EligibilityEvidenceReference(
                EligibilityEvidenceType.HISTORICAL_MAPPING,
                item.source_id,
                item.source_reference,
                item.artifact_sha256,
                item.schema_version,
                item.effective_start,
                item.effective_end,
            )
        )
        if (
            item.effective_start <= period.required_data_start
            and item.effective_end >= period.required_data_end
        ):
            coverage = True
    return coverage


def _intervals_overlap(
    first_start: date, first_end: date, second_start: date, second_end: date
) -> bool:
    """Return overlap for inclusive intervals, matching HistoricalStock bounds."""
    return first_start <= second_end and second_start <= first_end


def _is_canonical_code(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 6
        and value.isascii()
        and value.isdigit()
    )
