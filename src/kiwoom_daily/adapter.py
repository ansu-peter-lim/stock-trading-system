"""RAW/ADJUSTED alignment, canonical DailyBar mapping, and R3 evidence."""

from __future__ import annotations

from dataclasses import dataclass

from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.validation import (
    MarketDataValidationError,
    validate_daily_bars,
)
from src.research_universe.daily_eligibility import (
    DailyDatasetEvidence,
    DailyEvidenceIssue,
    DailySeriesRole,
)
from src.research_universe.models import (
    EligibilityEvidenceReference,
    EligibilityEvidenceType,
)

from .models import (
    DATASET_ID,
    DATASET_VERSION,
    PARSER_ID,
    PROVIDER_ID,
    SCHEMA_VERSION,
    CollectedDailySeries,
    DailyPipelineIssue,
    KiwoomDailyValidationError,
    ParsedDailyRow,
    PriceBasis,
)


@dataclass(frozen=True, slots=True)
class CanonicalDailyOutput:
    stock_code: str
    bars: tuple[DailyBar, ...]
    raw_evidence: DailyDatasetEvidence
    signal_evidence: DailyDatasetEvidence


def align_and_build_daily_bars(
    raw_series: CollectedDailySeries,
    adjusted_series: CollectedDailySeries,
) -> CanonicalDailyOutput:
    """Align exact dates without interpolation and preserve both price bases."""

    if raw_series.request.price_basis is not PriceBasis.RAW:
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.ADJUSTED_RAW_ALIGNMENT_ERROR,
            "raw_series must use RAW price basis",
        )
    if adjusted_series.request.price_basis is not PriceBasis.ADJUSTED:
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.ADJUSTED_RAW_ALIGNMENT_ERROR,
            "adjusted_series must use ADJUSTED price basis",
        )
    raw_request = raw_series.request
    adjusted_request = adjusted_series.request
    if (
        raw_request.stock_code != adjusted_request.stock_code
        or raw_request.start_date != adjusted_request.start_date
        or raw_request.end_date != adjusted_request.end_date
    ):
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.STOCK_MISMATCH,
            "RAW and ADJUSTED request identities must match",
        )

    raw_by_date = {row.trade_date: row for row in raw_series.rows}
    signal_by_date = {row.trade_date: row for row in adjusted_series.rows}
    if set(raw_by_date) != set(signal_by_date):
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.ADJUSTED_RAW_ALIGNMENT_ERROR,
            "RAW and ADJUSTED date sets must match exactly",
        )
    bars = tuple(
        DailyBar(
            stock_code=raw_request.stock_code,
            trade_date=trade_date,
            raw=_to_ohlcv(raw_by_date[trade_date]),
            signal=_to_ohlcv(signal_by_date[trade_date]),
        )
        for trade_date in sorted(raw_by_date)
    )
    try:
        validate_daily_bars(bars)
    except MarketDataValidationError as exc:
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.INVALID_DAILY_OHLCV,
            "canonical DailyBar validation failed",
        ) from exc
    return CanonicalDailyOutput(
        stock_code=raw_request.stock_code,
        bars=bars,
        raw_evidence=build_dataset_evidence(raw_series),
        signal_evidence=build_dataset_evidence(adjusted_series),
    )


def build_dataset_evidence(series: CollectedDailySeries) -> DailyDatasetEvidence:
    role = (
        DailySeriesRole.RAW
        if series.request.price_basis is PriceBasis.RAW
        else DailySeriesRole.SIGNAL_ADJUSTED
    )
    reference = EligibilityEvidenceReference(
        evidence_type=EligibilityEvidenceType.DAILY_DATASET,
        source_id=f"{PROVIDER_ID}:{DATASET_ID}:{series.request.price_basis.value}",
        source_reference=(
            f"provider={PROVIDER_ID}|api_id=ka10081"
            f"|stock_code={series.request.stock_code}"
            f"|price_basis={series.request.price_basis.value}"
            f"|volume_basis={series.volume_basis.value}"
            f"|base_date={series.request.base_date}"
            f"|pages={len(series.pages)}|artifact_set={series.artifact_set_sha256}"
        ),
        artifact_sha256=series.artifact_set_sha256,
        schema_version=SCHEMA_VERSION,
        coverage_start=series.session_dates[0] if series.session_dates else None,
        coverage_end=series.session_dates[-1] if series.session_dates else None,
    )
    return DailyDatasetEvidence(
        series_role=role,
        stock_code=series.request.stock_code,
        session_dates=series.session_dates,
        evidence_reference=reference,
        provider_id=PROVIDER_ID,
        dataset_id=DATASET_ID,
        dataset_version=DATASET_VERSION,
        price_policy_id=series.request.price_basis.price_policy_id,
        parser_id=PARSER_ID,
        schema_version=SCHEMA_VERSION,
    )


def to_r3_issue(error: KiwoomDailyValidationError) -> DailyEvidenceIssue:
    if error.issue is DailyPipelineIssue.PROVENANCE_ERROR:
        return DailyEvidenceIssue.MISSING_PROVENANCE
    return DailyEvidenceIssue.DATA_VALIDATION_ERROR


def _to_ohlcv(row: ParsedDailyRow) -> Ohlcv:
    return Ohlcv(row.open, row.high, row.low, row.close, row.volume)
