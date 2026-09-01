"""Minimal Kiwoom ``ka10081`` RAW/ADJUSTED Daily pipeline."""

from .adapter import (
    CanonicalDailyOutput,
    align_and_build_daily_bars,
    build_dataset_evidence,
    to_r3_issue,
)
from .collector import collect_daily_series
from .models import (
    ADJUSTED_PRICE_POLICY_ID,
    API_ID,
    PARSER_ID,
    RAW_PRICE_POLICY_ID,
    SCHEMA_VERSION,
    CollectedDailySeries,
    DailyCollectionRequest,
    DailyPipelineIssue,
    KiwoomDailyValidationError,
    PageProvenance,
    ParsedDailyRow,
    PriceBasis,
    VolumeBasis,
)
from .parser import ParsedDailyPage, parse_daily_page
from .store import ImmutableKiwoomDailyStore, PageManifestEvent

__all__ = [
    "ADJUSTED_PRICE_POLICY_ID",
    "API_ID",
    "PARSER_ID",
    "RAW_PRICE_POLICY_ID",
    "SCHEMA_VERSION",
    "CanonicalDailyOutput",
    "CollectedDailySeries",
    "DailyCollectionRequest",
    "DailyPipelineIssue",
    "ImmutableKiwoomDailyStore",
    "KiwoomDailyValidationError",
    "PageManifestEvent",
    "PageProvenance",
    "ParsedDailyPage",
    "ParsedDailyRow",
    "PriceBasis",
    "VolumeBasis",
    "align_and_build_daily_bars",
    "build_dataset_evidence",
    "collect_daily_series",
    "parse_daily_page",
    "to_r3_issue",
]
