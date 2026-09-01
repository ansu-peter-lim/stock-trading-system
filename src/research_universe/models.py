"""Immutable domain models for period-aware research eligibility.

This module defines vocabulary and deterministic identities only.  It does not
inspect historical mappings, market data, corporate actions, or backtests.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class ResearchEligibilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    EXCLUDED = "EXCLUDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ResearchEligibilityScope(str, Enum):
    DAILY_RESEARCH = "DAILY_RESEARCH"
    INTRADAY_COMPARISON = "INTRADAY_COMPARISON"


class ResearchEligibilityReason(str, Enum):
    """Canonical reason vocabulary; declaration order is result order."""

    # Identity and security eligibility.
    NON_NUMERIC_CANONICAL_CODE = "NON_NUMERIC_CANONICAL_CODE"
    DUPLICATE_SECURITY_IDENTITY = "DUPLICATE_SECURITY_IDENTITY"
    UNSUPPORTED_SECURITY_TYPE = "UNSUPPORTED_SECURITY_TYPE"
    CODE_MAPPING_CONFLICT = "CODE_MAPPING_CONFLICT"
    NAME_MAPPING_CONFLICT = "NAME_MAPPING_CONFLICT"
    CROSS_MARKET_CONFLICT = "CROSS_MARKET_CONFLICT"

    # Historical lifecycle.
    AMBIGUOUS_NAME_HISTORY = "AMBIGUOUS_NAME_HISTORY"
    MARKET_TRANSFER_UNRESOLVED = "MARKET_TRANSFER_UNRESOLVED"
    LISTING_HISTORY_UNRESOLVED = "LISTING_HISTORY_UNRESOLVED"
    RELISTING_HISTORY_UNRESOLVED = "RELISTING_HISTORY_UNRESOLVED"
    DELISTING_HISTORY_UNRESOLVED = "DELISTING_HISTORY_UNRESOLVED"
    HISTORICAL_COVERAGE_UNRESOLVED = "HISTORICAL_COVERAGE_UNRESOLVED"

    # Corporate actions.
    CORPORATE_ACTION_COVERAGE_MISSING = "CORPORATE_ACTION_COVERAGE_MISSING"
    CORPORATE_ACTION_UNRESOLVED = "CORPORATE_ACTION_UNRESOLVED"
    CORPORATE_ACTION_CONFLICT = "CORPORATE_ACTION_CONFLICT"
    CORPORATE_ACTION_PROVENANCE_INVALID = "CORPORATE_ACTION_PROVENANCE_INVALID"

    # Raw artifacts and provenance.
    RAW_CONFLICT = "RAW_CONFLICT"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    ARTIFACT_DIGEST_MISMATCH = "ARTIFACT_DIGEST_MISMATCH"
    DATA_VALIDATION_ERROR = "DATA_VALIDATION_ERROR"

    # Daily data.
    INSUFFICIENT_DAILY_DATA = "INSUFFICIENT_DAILY_DATA"
    DUPLICATE_DAILY_DATE = "DUPLICATE_DAILY_DATE"
    INVALID_DAILY_OHLCV = "INVALID_DAILY_OHLCV"
    MISSING_REQUIRED_TRADING_DAYS = "MISSING_REQUIRED_TRADING_DAYS"
    ADJUSTED_PRICE_UNAVAILABLE = "ADJUSTED_PRICE_UNAVAILABLE"
    ADJUSTED_RAW_ALIGNMENT_ERROR = "ADJUSTED_RAW_ALIGNMENT_ERROR"

    # Intraday data.
    INSUFFICIENT_INTRADAY_DATA = "INSUFFICIENT_INTRADAY_DATA"
    DUPLICATE_INTRADAY_BAR = "DUPLICATE_INTRADAY_BAR"
    INVALID_INTRADAY_TIMESTAMP = "INVALID_INTRADAY_TIMESTAMP"
    INVALID_REGULAR_SESSION_BAR = "INVALID_REGULAR_SESSION_BAR"
    UNEXPECTED_INTRADAY_GAP = "UNEXPECTED_INTRADAY_GAP"
    DAILY_INTRADAY_ALIGNMENT_ERROR = "DAILY_INTRADAY_ALIGNMENT_ERROR"

    # Configuration and calendar coverage.
    CALENDAR_COVERAGE_MISSING = "CALENDAR_COVERAGE_MISSING"
    REQUIRED_CONFIG_MISSING = "REQUIRED_CONFIG_MISSING"
    UNSUPPORTED_ELIGIBILITY_SCOPE = "UNSUPPORTED_ELIGIBILITY_SCOPE"


class EligibilityEvidenceType(str, Enum):
    HISTORICAL_MAPPING = "HISTORICAL_MAPPING"
    KRX_SNAPSHOT = "KRX_SNAPSHOT"
    OFFICIAL_EVENT = "OFFICIAL_EVENT"
    DAILY_DATASET = "DAILY_DATASET"
    INTRADAY_DATASET = "INTRADAY_DATASET"
    CORPORATE_ACTION_DATASET = "CORPORATE_ACTION_DATASET"
    TRADING_CALENDAR = "TRADING_CALENDAR"
    CONFIGURATION = "CONFIGURATION"


@dataclass(frozen=True, slots=True)
class ResearchPeriod:
    research_start: date
    research_end: date
    required_data_start: date
    required_data_end: date

    def __post_init__(self) -> None:
        values = (
            self.research_start,
            self.research_end,
            self.required_data_start,
            self.required_data_end,
        )
        if any(type(value) is not date for value in values):
            raise TypeError("research period fields must be date values")
        if not (
            self.required_data_start
            <= self.research_start
            <= self.research_end
            <= self.required_data_end
        ):
            raise ValueError(
                "require required_data_start <= research_start <= "
                "research_end <= required_data_end"
            )


@dataclass(frozen=True, slots=True)
class EligibilityEvidenceReference:
    evidence_type: EligibilityEvidenceType
    source_id: str
    source_reference: str
    artifact_sha256: str | None
    schema_version: str
    coverage_start: date | None = None
    coverage_end: date | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_type, EligibilityEvidenceType):
            raise TypeError("evidence_type must be EligibilityEvidenceType")
        for name in ("source_id", "source_reference", "schema_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if self.artifact_sha256 is not None:
            digest = self.artifact_sha256.lower()
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError("artifact_sha256 must be a 64-character hex digest")
            object.__setattr__(self, "artifact_sha256", digest)
        for name in ("coverage_start", "coverage_end"):
            value = getattr(self, name)
            if value is not None and type(value) is not date:
                raise TypeError(f"{name} must be a date or None")
        if (
            self.coverage_start is not None
            and self.coverage_end is not None
            and self.coverage_start > self.coverage_end
        ):
            raise ValueError("coverage_start must not be after coverage_end")

    @property
    def canonical_key(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.evidence_type.value,
            self.source_id,
            self.source_reference,
            self.artifact_sha256 or "",
            self.schema_version,
            _date_text(self.coverage_start) or "",
            _date_text(self.coverage_end) or "",
        )


@dataclass(frozen=True, slots=True)
class ResearchEligibilityResult:
    """Eligibility decision preserving source and canonical subject identities.

    ``stock_code`` is the source/raw code exactly as supplied.  The separate
    ``canonical_stock_code`` is populated only when that source value already
    is exactly six ASCII digits; this model never derives a canonical code.
    """

    stock_code: str
    scope: ResearchEligibilityScope
    research_period: ResearchPeriod
    status: ResearchEligibilityStatus
    reason_codes: tuple[ResearchEligibilityReason, ...]
    evidence_references: tuple[EligibilityEvidenceReference, ...]
    config_id: str
    config_version: str
    canonical_stock_code: str | None = None
    result_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.stock_code, str) or not self.stock_code:
            raise ValueError("stock_code source identity is required")
        source_is_canonical = _is_canonical_stock_code(self.stock_code)
        canonical_stock_code = self.canonical_stock_code
        if canonical_stock_code is None and source_is_canonical:
            canonical_stock_code = self.stock_code
            object.__setattr__(self, "canonical_stock_code", canonical_stock_code)
        elif canonical_stock_code is not None and (
            not _is_canonical_stock_code(canonical_stock_code)
            or canonical_stock_code != self.stock_code
        ):
            raise ValueError(
                "canonical_stock_code must be the unchanged six-digit source code"
            )
        if not isinstance(self.scope, ResearchEligibilityScope):
            raise TypeError("scope must be ResearchEligibilityScope")
        if not isinstance(self.research_period, ResearchPeriod):
            raise TypeError("research_period must be ResearchPeriod")
        if not isinstance(self.status, ResearchEligibilityStatus):
            raise TypeError("status must be ResearchEligibilityStatus")
        if not self.config_id.strip() or not self.config_version.strip():
            raise ValueError("config_id and config_version are required")
        if any(
            not isinstance(reason, ResearchEligibilityReason)
            for reason in self.reason_codes
        ):
            raise TypeError("reason_codes must contain ResearchEligibilityReason")
        if any(
            not isinstance(reference, EligibilityEvidenceReference)
            for reference in self.evidence_references
        ):
            raise TypeError(
                "evidence_references must contain EligibilityEvidenceReference"
            )

        canonical_reasons = tuple(
            reason
            for reason in ResearchEligibilityReason
            if reason in self.reason_codes
        )
        canonical_evidence = tuple(
            sorted(set(self.evidence_references), key=lambda item: item.canonical_key)
        )
        object.__setattr__(self, "reason_codes", canonical_reasons)
        object.__setattr__(self, "evidence_references", canonical_evidence)

        if self.status is ResearchEligibilityStatus.ELIGIBLE:
            if canonical_stock_code is None:
                raise ValueError("ELIGIBLE result requires canonical_stock_code")
            if canonical_reasons:
                raise ValueError("ELIGIBLE result must not contain reason codes")
            if not canonical_evidence:
                raise ValueError("ELIGIBLE result requires evidence references")
        elif not canonical_reasons:
            raise ValueError("non-ELIGIBLE result requires at least one reason code")
        elif (
            canonical_stock_code is None
            and ResearchEligibilityReason.NON_NUMERIC_CANONICAL_CODE
            not in canonical_reasons
        ):
            raise ValueError(
                "missing canonical_stock_code requires "
                "NON_NUMERIC_CANONICAL_CODE reason"
            )

        object.__setattr__(self, "result_id", self._build_result_id())

    @property
    def admitted(self) -> bool:
        """Return true only for automatic research-universe admission."""
        return self.status is ResearchEligibilityStatus.ELIGIBLE

    @property
    def source_stock_code(self) -> str:
        """Return the unmodified source/raw subject identity."""
        return self.stock_code

    def _build_result_id(self) -> str:
        period = self.research_period
        canonical = {
            "config_id": self.config_id,
            "config_version": self.config_version,
            "evidence_references": [
                {
                    "artifact_sha256": item.artifact_sha256,
                    "coverage_end": _date_text(item.coverage_end),
                    "coverage_start": _date_text(item.coverage_start),
                    "evidence_type": item.evidence_type.value,
                    "schema_version": item.schema_version,
                    "source_id": item.source_id,
                    "source_reference": item.source_reference,
                }
                for item in self.evidence_references
            ],
            "reason_codes": [item.value for item in self.reason_codes],
            "required_data_end": period.required_data_end.isoformat(),
            "required_data_start": period.required_data_start.isoformat(),
            "research_end": period.research_end.isoformat(),
            "research_start": period.research_start.isoformat(),
            "scope": self.scope.value,
            "status": self.status.value,
            "source_stock_code": self.source_stock_code,
            "canonical_stock_code": self.canonical_stock_code,
        }
        body = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()


def _is_canonical_stock_code(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 6
        and value.isascii()
        and value.isdigit()
    )


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
