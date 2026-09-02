"""Minimal Kiwoom ``ka10080`` source-sequence research pipeline."""

from .pipeline import (
    ASSUMPTION_ID,
    CollectedMinuteSeries,
    KiwoomMinuteStore,
    MinuteCollectionRequest,
    MinutePipelineIssue,
    MinutePriceBasis,
    MinuteSourceBar,
    MinuteValidationError,
    align_source_bars,
    collect_minute_series,
    parse_minute_page,
)
from .proof import run_up_path_sequence_proof

__all__ = [
    "ASSUMPTION_ID",
    "CollectedMinuteSeries",
    "KiwoomMinuteStore",
    "MinuteCollectionRequest",
    "MinutePipelineIssue",
    "MinutePriceBasis",
    "MinuteSourceBar",
    "MinuteValidationError",
    "align_source_bars",
    "collect_minute_series",
    "parse_minute_page",
    "run_up_path_sequence_proof",
]
