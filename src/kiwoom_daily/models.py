"""Strict, source-specific models for Kiwoom ``ka10081`` Daily data."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

PROVIDER_ID = "KIWOOM"
API_ID = "ka10081"
DATASET_ID = "KIWOOM_KA10081_DAILY"
DATASET_VERSION = "R5"
PARSER_ID = "KIWOOM_KA10081_DAILY_PARSER_V1"
SCHEMA_VERSION = "kiwoom-ka10081-daily-v1"
RAW_PRICE_POLICY_ID = "KIWOOM_KA10081_UPD_STKPC_TP_0"
ADJUSTED_PRICE_POLICY_ID = "KIWOOM_KA10081_UPD_STKPC_TP_1"


class PriceBasis(str, Enum):
    RAW = "RAW"
    ADJUSTED = "ADJUSTED"

    @property
    def request_value(self) -> str:
        return "0" if self is PriceBasis.RAW else "1"

    @property
    def price_policy_id(self) -> str:
        return (
            RAW_PRICE_POLICY_ID if self is PriceBasis.RAW else ADJUSTED_PRICE_POLICY_ID
        )


class VolumeBasis(str, Enum):
    RAW = "RAW"
    PROVIDER_ADJUSTED_UNKNOWN_POLICY = "PROVIDER_ADJUSTED_UNKNOWN_POLICY"


class DailyPipelineIssue(str, Enum):
    INVALID_STOCK_CODE = "INVALID_STOCK_CODE"
    INVALID_DATE_RANGE = "INVALID_DATE_RANGE"
    HTTP_ERROR = "HTTP_ERROR"
    API_ERROR = "API_ERROR"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    STOCK_MISMATCH = "STOCK_MISMATCH"
    MALFORMED_ROW = "MALFORMED_ROW"
    MALFORMED_NUMERIC = "MALFORMED_NUMERIC"
    INVALID_DAILY_OHLCV = "INVALID_DAILY_OHLCV"
    DUPLICATE_DAILY_DATE = "DUPLICATE_DAILY_DATE"
    PAGINATION_ERROR = "PAGINATION_ERROR"
    REQUIRED_START_NOT_REACHED = "REQUIRED_START_NOT_REACHED"
    PROVENANCE_ERROR = "PROVENANCE_ERROR"
    ADJUSTED_RAW_ALIGNMENT_ERROR = "ADJUSTED_RAW_ALIGNMENT_ERROR"


class KiwoomDailyValidationError(ValueError):
    """Typed, safe failure suitable for per-stock fail-safe exclusion."""

    def __init__(self, issue: DailyPipelineIssue, message: str) -> None:
        super().__init__(message)
        self.issue = issue


@dataclass(frozen=True, slots=True)
class DailyCollectionRequest:
    stock_code: str
    start_date: date
    end_date: date
    price_basis: PriceBasis

    def __post_init__(self) -> None:
        require_stock_code(self.stock_code)
        if type(self.start_date) is not date or type(self.end_date) is not date:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.INVALID_DATE_RANGE,
                "start_date and end_date must be date values",
            )
        if self.start_date > self.end_date:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.INVALID_DATE_RANGE,
                "start_date must not be after end_date",
            )
        if not isinstance(self.price_basis, PriceBasis):
            raise TypeError("price_basis must be PriceBasis")

    @property
    def base_date(self) -> str:
        return self.end_date.strftime("%Y%m%d")


@dataclass(frozen=True, slots=True)
class ParsedDailyRow:
    stock_code: str
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    price_basis: PriceBasis
    source_page: int
    source_row_index: int
    artifact_sha256: str

    def __post_init__(self) -> None:
        require_stock_code(self.stock_code)
        if type(self.trade_date) is not date:
            raise TypeError("trade_date must be a date")
        if not isinstance(self.price_basis, PriceBasis):
            raise TypeError("price_basis must be PriceBasis")
        if self.source_page < 1 or self.source_row_index < 0:
            raise ValueError("source page/index must be non-negative canonical values")
        require_sha256(self.artifact_sha256)


@dataclass(frozen=True, slots=True)
class PageProvenance:
    provider: str
    api_id: str
    stock_code: str
    price_basis: PriceBasis
    base_date: str
    pagination_sequence: int
    request_continuation_identity: str
    response_continuation_identity: str
    retrieved_at: str
    raw_file_path: str
    raw_file_sha256: str
    row_count: int
    parser_id: str = PARSER_ID
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.provider != PROVIDER_ID or self.api_id != API_ID:
            raise ValueError("unexpected provider/API identity")
        require_stock_code(self.stock_code)
        if not isinstance(self.price_basis, PriceBasis):
            raise TypeError("price_basis must be PriceBasis")
        if self.pagination_sequence < 1 or self.row_count < 0:
            raise ValueError("invalid pagination sequence or row count")
        if not self.raw_file_path or not self.retrieved_at:
            raise ValueError("raw path and retrieved_at are required")
        require_sha256(self.raw_file_sha256)


@dataclass(frozen=True, slots=True)
class CollectedDailySeries:
    request: DailyCollectionRequest
    rows: tuple[ParsedDailyRow, ...]
    pages: tuple[PageProvenance, ...]
    volume_basis: VolumeBasis
    artifact_set_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, DailyCollectionRequest):
            raise TypeError("request must be DailyCollectionRequest")
        if not isinstance(self.rows, tuple) or not isinstance(self.pages, tuple):
            raise TypeError("rows and pages must be tuples")
        if not isinstance(self.volume_basis, VolumeBasis):
            raise TypeError("volume_basis must be VolumeBasis")
        expected_volume_basis = (
            VolumeBasis.RAW
            if self.request.price_basis is PriceBasis.RAW
            else VolumeBasis.PROVIDER_ADJUSTED_UNKNOWN_POLICY
        )
        if self.volume_basis is not expected_volume_basis:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.PROVENANCE_ERROR,
                "volume basis does not match the requested price basis",
            )

        rows = tuple(sorted(self.rows, key=lambda row: row.trade_date))
        pages = tuple(sorted(self.pages, key=lambda page: page.pagination_sequence))
        dates: set[date] = set()
        source_locations: set[tuple[int, int]] = set()
        for row in rows:
            if not isinstance(row, ParsedDailyRow):
                raise TypeError("rows must contain ParsedDailyRow values")
            if (
                row.stock_code != self.request.stock_code
                or row.price_basis is not self.request.price_basis
            ):
                raise KiwoomDailyValidationError(
                    DailyPipelineIssue.STOCK_MISMATCH,
                    "parsed row does not match collection request identity",
                )
            if not self.request.start_date <= row.trade_date <= self.request.end_date:
                raise KiwoomDailyValidationError(
                    DailyPipelineIssue.INVALID_DATE_RANGE,
                    "canonical row is outside the requested date window",
                )
            if row.trade_date in dates:
                raise KiwoomDailyValidationError(
                    DailyPipelineIssue.DUPLICATE_DAILY_DATE,
                    "duplicate trade date in collected series",
                )
            source_location = (row.source_page, row.source_row_index)
            if source_location in source_locations:
                raise KiwoomDailyValidationError(
                    DailyPipelineIssue.PROVENANCE_ERROR,
                    "multiple parsed rows claim the same source page location",
                )
            dates.add(row.trade_date)
            source_locations.add(source_location)

        expected_sequences = tuple(range(1, len(pages) + 1))
        actual_sequences = tuple(page.pagination_sequence for page in pages)
        if not pages:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.PROVENANCE_ERROR,
                "collected series requires at least one page artifact",
            )
        if actual_sequences != expected_sequences:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.PAGINATION_ERROR,
                "page provenance must use contiguous one-based sequences",
            )
        for page in pages:
            if (
                page.stock_code != self.request.stock_code
                or page.price_basis is not self.request.price_basis
            ):
                raise KiwoomDailyValidationError(
                    DailyPipelineIssue.STOCK_MISMATCH,
                    "page provenance does not match collection request identity",
                )
            if (
                page.base_date != self.request.base_date
                or page.parser_id != PARSER_ID
                or page.schema_version != SCHEMA_VERSION
            ):
                raise KiwoomDailyValidationError(
                    DailyPipelineIssue.PROVENANCE_ERROR,
                    "page provenance parser/schema/request identity is inconsistent",
                )
        page_digests = {
            page.pagination_sequence: page.raw_file_sha256 for page in pages
        }
        if any(
            page_digests.get(row.source_page) != row.artifact_sha256 for row in rows
        ):
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.PROVENANCE_ERROR,
                "parsed row is not linked to its source page artifact",
            )

        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "pages", pages)
        semantic_artifacts = [
            {
                "sequence": page.pagination_sequence,
                "sha256": page.raw_file_sha256,
            }
            for page in pages
        ]
        payload = json.dumps(
            {
                "provider": PROVIDER_ID,
                "api_id": API_ID,
                "stock_code": self.request.stock_code,
                "price_basis": self.request.price_basis.value,
                "start_date": self.request.start_date.isoformat(),
                "end_date": self.request.end_date.isoformat(),
                "parser_id": PARSER_ID,
                "schema_version": SCHEMA_VERSION,
                "artifacts": semantic_artifacts,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(
            self, "artifact_set_sha256", hashlib.sha256(payload).hexdigest()
        )

    @property
    def session_dates(self) -> tuple[date, ...]:
        return tuple(row.trade_date for row in self.rows)


def require_stock_code(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 6
        or not value.isascii()
        or not value.isdigit()
    ):
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.INVALID_STOCK_CODE,
            "stock_code must be exactly six ASCII digits",
        )
    return value


def require_sha256(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.lower())
    ):
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.PROVENANCE_ERROR,
            "artifact digest must be a 64-character hexadecimal SHA-256",
        )
    return value.lower()
