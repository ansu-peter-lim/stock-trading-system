"""Deterministic Daily-data eligibility for one research subject and period.

The adapter consumes canonical, already-ingested inputs only.  It performs no
historical-identity, intraday, corporate-action, network, backfill, or source
file work.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.backtest_engine.validation import (
    MarketDataValidationError,
    validate_daily_bars,
    validate_ohlcv,
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


class DailySeriesRole(str, Enum):
    RAW = "RAW"
    SIGNAL_ADJUSTED = "SIGNAL_ADJUSTED"


class DailyEvidenceIssue(str, Enum):
    """Typed upstream failures whose messages must never be interpreted."""

    RAW_CONFLICT = "RAW_CONFLICT"
    ARTIFACT_DIGEST_MISMATCH = "ARTIFACT_DIGEST_MISMATCH"
    MISSING_PROVENANCE = "MISSING_PROVENANCE"
    DATA_VALIDATION_ERROR = "DATA_VALIDATION_ERROR"


_ISSUE_REASON = {
    DailyEvidenceIssue.RAW_CONFLICT: ResearchEligibilityReason.RAW_CONFLICT,
    DailyEvidenceIssue.ARTIFACT_DIGEST_MISMATCH: (
        ResearchEligibilityReason.ARTIFACT_DIGEST_MISMATCH
    ),
    DailyEvidenceIssue.MISSING_PROVENANCE: (
        ResearchEligibilityReason.MISSING_PROVENANCE
    ),
    DailyEvidenceIssue.DATA_VALIDATION_ERROR: (
        ResearchEligibilityReason.DATA_VALIDATION_ERROR
    ),
}

_EXCLUDED_REASONS = frozenset(
    {
        ResearchEligibilityReason.INSUFFICIENT_DAILY_DATA,
        ResearchEligibilityReason.DUPLICATE_DAILY_DATE,
        ResearchEligibilityReason.INVALID_DAILY_OHLCV,
        ResearchEligibilityReason.MISSING_REQUIRED_TRADING_DAYS,
        ResearchEligibilityReason.ADJUSTED_PRICE_UNAVAILABLE,
        ResearchEligibilityReason.ADJUSTED_RAW_ALIGNMENT_ERROR,
        ResearchEligibilityReason.RAW_CONFLICT,
        ResearchEligibilityReason.ARTIFACT_DIGEST_MISMATCH,
        ResearchEligibilityReason.DATA_VALIDATION_ERROR,
    }
)


@dataclass(frozen=True, slots=True)
class DailyEligibilityConfig:
    """Versioned policy identity; V1 has zero missing-session tolerance."""

    config_id: str
    config_version: str

    def __post_init__(self) -> None:
        _require_text(self.config_id, "config_id")
        _require_text(self.config_version, "config_version")


@dataclass(frozen=True, slots=True)
class DailyCalendarSnapshot:
    """Immutable, versioned, network-free trading-session evidence."""

    calendar_id: str
    calendar_version: str
    schema_version: str
    coverage_start: date
    coverage_end: date
    trading_sessions: tuple[date, ...]
    source_reference: str
    artifact_sha256: str | None
    session_set_digest: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "calendar_id",
            "calendar_version",
            "schema_version",
            "source_reference",
        ):
            _require_text(getattr(self, name), name)
        _require_date(self.coverage_start, "coverage_start")
        _require_date(self.coverage_end, "coverage_end")
        if self.coverage_start > self.coverage_end:
            raise ValueError("coverage_start must not be after coverage_end")
        sessions = _canonical_dates(self.trading_sessions, "trading_sessions")
        if len(set(sessions)) != len(sessions):
            raise ValueError("trading_sessions must not contain duplicates")
        if any(
            session < self.coverage_start or session > self.coverage_end
            for session in sessions
        ):
            raise ValueError("trading session must be within calendar coverage")
        object.__setattr__(self, "trading_sessions", sessions)
        object.__setattr__(self, "session_set_digest", _session_digest(sessions))
        object.__setattr__(
            self,
            "artifact_sha256",
            _validated_digest(self.artifact_sha256, "artifact_sha256"),
        )

    @property
    def evidence_reference(self) -> EligibilityEvidenceReference:
        return EligibilityEvidenceReference(
            evidence_type=EligibilityEvidenceType.TRADING_CALENDAR,
            source_id=f"{self.calendar_id}:{self.calendar_version}",
            source_reference=(
                f"{self.source_reference}|sessions_sha256={self.session_set_digest}"
            ),
            artifact_sha256=self.artifact_sha256,
            schema_version=self.schema_version,
            coverage_start=self.coverage_start,
            coverage_end=self.coverage_end,
        )

    def covers(self, period: ResearchPeriod) -> bool:
        return (
            self.coverage_start <= period.required_data_start
            and self.coverage_end >= period.required_data_end
        )

    def sessions_for(self, period: ResearchPeriod) -> tuple[date, ...]:
        """Return sessions only when the required window is authoritative."""
        if not self.covers(period):
            raise ValueError("calendar does not cover the required-data window")
        return tuple(
            session
            for session in self.trading_sessions
            if period.required_data_start <= session <= period.required_data_end
        )


@dataclass(frozen=True, slots=True)
class DailyDatasetEvidence:
    """Semantic evidence for one raw or adjusted Daily series."""

    series_role: DailySeriesRole
    stock_code: str
    session_dates: tuple[date, ...]
    evidence_reference: EligibilityEvidenceReference | None
    provider_id: str
    dataset_id: str
    dataset_version: str
    price_policy_id: str
    parser_id: str
    schema_version: str
    session_set_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.series_role, DailySeriesRole):
            raise TypeError("series_role must be DailySeriesRole")
        if not _is_canonical_stock_code(self.stock_code):
            raise ValueError("stock_code must be exactly six ASCII digits")
        for name in (
            "provider_id",
            "dataset_id",
            "dataset_version",
            "price_policy_id",
            "parser_id",
            "schema_version",
        ):
            _require_text(getattr(self, name), name)
        dates = _canonical_dates(self.session_dates, "session_dates")
        object.__setattr__(self, "session_dates", dates)
        object.__setattr__(self, "session_set_digest", _session_digest(dates))
        if self.evidence_reference is not None and not isinstance(
            self.evidence_reference, EligibilityEvidenceReference
        ):
            raise TypeError(
                "evidence_reference must be EligibilityEvidenceReference or None"
            )

    @property
    def coverage_start(self) -> date | None:
        return self.session_dates[0] if self.session_dates else None

    @property
    def coverage_end(self) -> date | None:
        return self.session_dates[-1] if self.session_dates else None

    @property
    def result_reference(self) -> EligibilityEvidenceReference | None:
        upstream = self.evidence_reference
        if upstream is None:
            return None
        return EligibilityEvidenceReference(
            evidence_type=EligibilityEvidenceType.DAILY_DATASET,
            source_id=(
                f"{self.series_role.value}:{self.provider_id}:{self.dataset_id}:"
                f"{self.dataset_version}"
            ),
            source_reference=(
                f"{upstream.source_reference}|role={self.series_role.value}"
                f"|price_policy={self.price_policy_id}|parser={self.parser_id}"
                f"|sessions_sha256={self.session_set_digest}"
            ),
            artifact_sha256=upstream.artifact_sha256,
            schema_version=self.schema_version,
            coverage_start=self.coverage_start,
            coverage_end=self.coverage_end,
        )

    def dates_within(self, period: ResearchPeriod) -> tuple[date, ...]:
        return tuple(
            day
            for day in self.session_dates
            if period.required_data_start <= day <= period.required_data_end
        )


@dataclass(frozen=True, slots=True)
class DailyEligibilityInput:
    source_stock_code: str
    canonical_stock_code: str
    scope: ResearchEligibilityScope
    research_period: ResearchPeriod
    canonical_daily_bars: tuple[DailyBar, ...]
    calendar_snapshot: DailyCalendarSnapshot
    raw_dataset: DailyDatasetEvidence | None
    signal_dataset: DailyDatasetEvidence | None
    config: DailyEligibilityConfig
    issues: tuple[DailyEvidenceIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_stock_code, str) or not self.source_stock_code:
            raise ValueError("source_stock_code is required")
        if not _is_canonical_stock_code(self.canonical_stock_code):
            raise ValueError("canonical_stock_code must be exactly six ASCII digits")
        if self.source_stock_code != self.canonical_stock_code:
            raise ValueError(
                "R3 source identity must equal the verified canonical stock code"
            )
        if not isinstance(self.scope, ResearchEligibilityScope):
            raise TypeError("scope must be ResearchEligibilityScope")
        if self.scope not in {
            ResearchEligibilityScope.DAILY_RESEARCH,
            ResearchEligibilityScope.INTRADAY_COMPARISON,
        }:
            raise ValueError("unsupported daily eligibility scope")
        if not isinstance(self.research_period, ResearchPeriod):
            raise TypeError("research_period must be ResearchPeriod")
        if not isinstance(self.canonical_daily_bars, tuple):
            raise TypeError("canonical_daily_bars must be a tuple")
        if not isinstance(self.calendar_snapshot, DailyCalendarSnapshot):
            raise TypeError("calendar_snapshot must be DailyCalendarSnapshot")
        for name in ("raw_dataset", "signal_dataset"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, DailyDatasetEvidence):
                raise TypeError(f"{name} must be DailyDatasetEvidence or None")
        if not isinstance(self.config, DailyEligibilityConfig):
            raise TypeError("config must be DailyEligibilityConfig")
        if not isinstance(self.issues, tuple) or any(
            not isinstance(issue, DailyEvidenceIssue) for issue in self.issues
        ):
            raise TypeError("issues must contain DailyEvidenceIssue values")


def assess_daily_eligibility(
    eligibility_input: DailyEligibilityInput,
) -> ResearchEligibilityResult:
    """Evaluate one stock and required-data window without source inference."""
    if not isinstance(eligibility_input, DailyEligibilityInput):
        raise TypeError("eligibility_input must be DailyEligibilityInput")

    reasons = [_ISSUE_REASON[issue] for issue in eligibility_input.issues]
    references: list[EligibilityEvidenceReference] = []
    period = eligibility_input.research_period
    calendar = eligibility_input.calendar_snapshot

    calendar_reference = calendar.evidence_reference
    references.append(calendar_reference)
    if calendar.artifact_sha256 is None:
        reasons.append(ResearchEligibilityReason.MISSING_PROVENANCE)

    expected_dates: set[date] | None = None
    if calendar.covers(period):
        expected_dates = set(calendar.sessions_for(period))
        if not expected_dates:
            reasons.append(ResearchEligibilityReason.INSUFFICIENT_DAILY_DATA)
    else:
        reasons.append(ResearchEligibilityReason.CALENDAR_COVERAGE_MISSING)

    raw_dates = _assess_dataset(
        eligibility_input.raw_dataset,
        DailySeriesRole.RAW,
        eligibility_input.canonical_stock_code,
        period,
        ResearchEligibilityReason.INSUFFICIENT_DAILY_DATA,
        reasons,
        references,
    )
    signal_dates = _assess_dataset(
        eligibility_input.signal_dataset,
        DailySeriesRole.SIGNAL_ADJUSTED,
        eligibility_input.canonical_stock_code,
        period,
        ResearchEligibilityReason.ADJUSTED_PRICE_UNAVAILABLE,
        reasons,
        references,
    )
    canonical_dates = _assess_canonical_bars(
        eligibility_input.canonical_daily_bars,
        eligibility_input.canonical_stock_code,
        period,
        expected_dates,
        reasons,
    )

    if raw_dates is not None and not raw_dates:
        reasons.append(ResearchEligibilityReason.INSUFFICIENT_DAILY_DATA)
    if signal_dates is not None and not signal_dates:
        reasons.append(ResearchEligibilityReason.ADJUSTED_PRICE_UNAVAILABLE)
    if expected_dates and not canonical_dates:
        reasons.append(ResearchEligibilityReason.INSUFFICIENT_DAILY_DATA)

    if raw_dates is not None and signal_dates is not None:
        if raw_dates != signal_dates:
            reasons.append(ResearchEligibilityReason.ADJUSTED_RAW_ALIGNMENT_ERROR)
        if canonical_dates != raw_dates or canonical_dates != signal_dates:
            reasons.append(ResearchEligibilityReason.ADJUSTED_RAW_ALIGNMENT_ERROR)

    if expected_dates is not None:
        if raw_dates is not None and expected_dates - raw_dates:
            reasons.append(ResearchEligibilityReason.MISSING_REQUIRED_TRADING_DAYS)
        if signal_dates is not None and expected_dates - signal_dates:
            reasons.append(ResearchEligibilityReason.ADJUSTED_PRICE_UNAVAILABLE)
        observed_sets = (canonical_dates, raw_dates, signal_dates)
        if any(
            observed is not None and observed - expected_dates
            for observed in observed_sets
        ):
            reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)

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
        stock_code=eligibility_input.source_stock_code,
        canonical_stock_code=eligibility_input.canonical_stock_code,
        scope=eligibility_input.scope,
        research_period=period,
        status=status,
        reason_codes=canonical_reasons,
        evidence_references=tuple(references),
        config_id=eligibility_input.config.config_id,
        config_version=eligibility_input.config.config_version,
    )


def _assess_dataset(
    dataset: DailyDatasetEvidence | None,
    expected_role: DailySeriesRole,
    stock_code: str,
    period: ResearchPeriod,
    missing_reason: ResearchEligibilityReason,
    reasons: list[ResearchEligibilityReason],
    references: list[EligibilityEvidenceReference],
) -> set[date] | None:
    if dataset is None:
        reasons.extend((missing_reason, ResearchEligibilityReason.MISSING_PROVENANCE))
        return None
    if dataset.series_role is not expected_role:
        reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)
    if dataset.stock_code != stock_code:
        reasons.append(ResearchEligibilityReason.ADJUSTED_RAW_ALIGNMENT_ERROR)
        return None
    if len(set(dataset.session_dates)) != len(dataset.session_dates):
        reasons.append(ResearchEligibilityReason.DUPLICATE_DAILY_DATE)
    reference = dataset.result_reference
    if reference is None:
        reasons.append(ResearchEligibilityReason.MISSING_PROVENANCE)
    else:
        references.append(reference)
        if (
            dataset.evidence_reference is not None
            and dataset.evidence_reference.evidence_type
            is not EligibilityEvidenceType.DAILY_DATASET
        ):
            reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)
        if reference.artifact_sha256 is None:
            reasons.append(ResearchEligibilityReason.MISSING_PROVENANCE)
    return set(dataset.dates_within(period))


def _assess_canonical_bars(
    bars: tuple[DailyBar, ...],
    stock_code: str,
    period: ResearchPeriod,
    expected_dates: set[date] | None,
    reasons: list[ResearchEligibilityReason],
) -> set[date]:
    candidates: list[DailyBar] = []
    keys: list[tuple[str, date]] = []
    preflight_failed = False
    for bar in bars:
        if not isinstance(bar, DailyBar):
            reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)
            preflight_failed = True
            continue
        if not isinstance(bar.trade_date, date) or isinstance(bar.trade_date, datetime):
            reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)
            preflight_failed = True
            continue
        if not period.required_data_start <= bar.trade_date <= period.required_data_end:
            continue
        if bar.stock_code != stock_code:
            reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)
            preflight_failed = True
            continue
        key = (bar.stock_code, bar.trade_date)
        keys.append(key)
        candidates.append(bar)
        for series_name, value in (("raw", bar.raw), ("signal", bar.signal)):
            try:
                validate_ohlcv(value, series_name, "daily eligibility")
            except MarketDataValidationError:
                reasons.append(ResearchEligibilityReason.INVALID_DAILY_OHLCV)
                preflight_failed = True

    if len(set(keys)) != len(keys):
        reasons.append(ResearchEligibilityReason.DUPLICATE_DAILY_DATE)
        preflight_failed = True

    ordered = tuple(sorted(candidates, key=lambda item: item.trade_date))
    if not preflight_failed:
        try:
            validation_calendar = (
                ExplicitTradingCalendar(expected_dates) if expected_dates else None
            )
            validate_daily_bars(ordered, validation_calendar)
        except MarketDataValidationError:
            reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)

    actual_dates = {bar.trade_date for bar in candidates}
    if expected_dates is not None and actual_dates - expected_dates:
        reasons.append(ResearchEligibilityReason.DATA_VALIDATION_ERROR)
    return actual_dates


def _canonical_dates(values: object, field_name: str) -> tuple[date, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    if any(
        not isinstance(value, date) or isinstance(value, datetime) for value in values
    ):
        raise TypeError(f"{field_name} must contain date values")
    return tuple(sorted(values))


def _session_digest(values: tuple[date, ...]) -> str:
    body = "\n".join(value.isoformat() for value in values).encode("ascii")
    return hashlib.sha256(body).hexdigest()


def _validated_digest(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    digest = value.lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"{field_name} must be a 64-character hex digest")
    return digest


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")


def _require_date(value: object, field_name: str) -> None:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a date")


def _is_canonical_stock_code(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 6
        and value.isascii()
        and value.isdigit()
    )
