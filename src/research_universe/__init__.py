"""Period-aware, source-neutral research-universe eligibility models."""

from .daily_eligibility import (
    DailyCalendarSnapshot,
    DailyDatasetEvidence,
    DailyEligibilityConfig,
    DailyEligibilityInput,
    DailyEvidenceIssue,
    DailySeriesRole,
    assess_daily_eligibility,
)
from .historical_eligibility import (
    HistoricalEligibilityConfig,
    HistoricalEligibilityInput,
    HistoricalEvidenceIssue,
    ManualHistoricalMapping,
    assess_historical_eligibility,
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

__all__ = [
    "DailyCalendarSnapshot",
    "DailyDatasetEvidence",
    "DailyEligibilityConfig",
    "DailyEligibilityInput",
    "DailyEvidenceIssue",
    "DailySeriesRole",
    "EligibilityEvidenceReference",
    "EligibilityEvidenceType",
    "HistoricalEligibilityConfig",
    "HistoricalEligibilityInput",
    "HistoricalEvidenceIssue",
    "ManualHistoricalMapping",
    "ResearchEligibilityReason",
    "ResearchEligibilityResult",
    "ResearchEligibilityScope",
    "ResearchEligibilityStatus",
    "ResearchPeriod",
    "assess_daily_eligibility",
    "assess_historical_eligibility",
]
