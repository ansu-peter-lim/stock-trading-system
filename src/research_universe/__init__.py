"""Period-aware, source-neutral research-universe eligibility models."""

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
    "assess_historical_eligibility",
]
