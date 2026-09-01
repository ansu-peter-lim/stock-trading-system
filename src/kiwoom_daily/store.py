"""Immutable byte store and secret-free manifest for Kiwoom Daily pages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from .models import (
    API_ID,
    PARSER_ID,
    PROVIDER_ID,
    SCHEMA_VERSION,
    DailyCollectionRequest,
    DailyPipelineIssue,
    KiwoomDailyValidationError,
)


class ManifestStatus(str, Enum):
    SUCCESS = "SUCCESS"
    HTTP_ERROR = "HTTP_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"


@dataclass(frozen=True, slots=True)
class StoredPage:
    raw_file_path: str
    raw_file_sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class PageManifestEvent:
    provider: str
    api_id: str
    stock_code: str
    price_basis: str
    base_date: str
    pagination_sequence: int
    request_continuation_identity: str
    response_continuation_identity: str
    retrieved_at: str
    raw_file_path: str
    raw_file_sha256: str
    row_count: int | None
    status: ManifestStatus
    error_code: str = ""
    parser_id: str = PARSER_ID
    schema_version: str = SCHEMA_VERSION


class ImmutableKiwoomDailyStore:
    """Store pages by content digest so no successful response is overwritten."""

    def __init__(self, root: Path = Path("data/raw/kiwoom/daily")) -> None:
        self.root = root

    def store_page(
        self,
        request: DailyCollectionRequest,
        *,
        pagination_sequence: int,
        raw_bytes: bytes,
    ) -> StoredPage:
        if not isinstance(raw_bytes, bytes):
            raise TypeError("raw_bytes must be bytes")
        if pagination_sequence < 1:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.PROVENANCE_ERROR,
                "pagination_sequence must be one-based",
            )
        digest = hashlib.sha256(raw_bytes).hexdigest()
        path = (
            self.root
            / request.stock_code
            / request.price_basis.value.lower()
            / request.base_date
            / f"page-{pagination_sequence:03d}-{digest}.json"
        )
        try:
            if path.exists():
                if path.read_bytes() != raw_bytes:
                    raise KiwoomDailyValidationError(
                        DailyPipelineIssue.PROVENANCE_ERROR,
                        "immutable Kiwoom artifact identity collision",
                    )
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw_bytes)
        except OSError as exc:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.PROVENANCE_ERROR,
                "Kiwoom raw artifact could not be stored",
            ) from exc
        return StoredPage(path.as_posix(), digest, len(raw_bytes))

    def append_manifest(self, event: PageManifestEvent) -> Path:
        path = self.root / "manifest" / "requests.jsonl"
        record = asdict(event)
        record["status"] = event.status.value
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
                )
        except OSError as exc:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.PROVENANCE_ERROR,
                "Kiwoom manifest event could not be stored",
            ) from exc
        return path


def continuation_identity(value: str) -> str:
    """Return a non-reversible identity without persisting the opaque key."""

    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def manifest_event(
    request: DailyCollectionRequest,
    *,
    pagination_sequence: int,
    request_continuation_key: str,
    response_continuation_key: str,
    retrieved_at: str,
    stored: StoredPage,
    row_count: int | None,
    status: ManifestStatus,
    error_code: str = "",
) -> PageManifestEvent:
    return PageManifestEvent(
        provider=PROVIDER_ID,
        api_id=API_ID,
        stock_code=request.stock_code,
        price_basis=request.price_basis.value,
        base_date=request.base_date,
        pagination_sequence=pagination_sequence,
        request_continuation_identity=continuation_identity(request_continuation_key),
        response_continuation_identity=continuation_identity(response_continuation_key),
        retrieved_at=retrieved_at,
        raw_file_path=stored.raw_file_path,
        raw_file_sha256=stored.raw_file_sha256,
        row_count=row_count,
        status=status,
        error_code=error_code,
    )
