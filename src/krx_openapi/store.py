"""Immutable raw response store and append-only KRX request manifest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from .services import KrxServiceDefinition
from .transport import TransportResult

COLLECTOR_VERSION = "K1"
SCHEMA_VERSION = "1"


class ArtifactDisposition(str, Enum):
    CREATED = "CREATED"
    IDEMPOTENT = "IDEMPOTENT"
    CONFLICT = "CONFLICT"


class ManifestStatus(str, Enum):
    SUCCESS = "SUCCESS"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    HTTP_ERROR = "HTTP_ERROR"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    disposition: ArtifactDisposition
    raw_file_path: str
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class ManifestEvent:
    service_id: str
    market: str
    bas_dd: str
    status: ManifestStatus
    retrieved_at: str
    http_status: int | None
    row_count: int | None
    raw_file_path: str
    raw_sha256: str
    byte_size: int | None
    collector_version: str = COLLECTOR_VERSION
    schema_version: str = SCHEMA_VERSION
    error_code: str = ""


class ImmutableRawStore:
    def __init__(self, root: Path = Path("data/raw/krx")) -> None:
        self.root = root

    def artifact_path(self, service: KrxServiceDefinition, bas_dd: str) -> Path:
        return (
            self.root
            / service.artifact_group
            / service.market.lower()
            / bas_dd[:4]
            / bas_dd[4:6]
            / f"{bas_dd[6:8]}.json"
        )

    def store(
        self, service: KrxServiceDefinition, result: TransportResult
    ) -> StoredArtifact:
        body = result.raw_bytes
        digest = hashlib.sha256(body).hexdigest()
        primary = self.artifact_path(service, result.identity.bas_dd)
        if primary.exists():
            existing_digest = hashlib.sha256(primary.read_bytes()).hexdigest()
            if existing_digest == digest:
                return StoredArtifact(
                    ArtifactDisposition.IDEMPOTENT,
                    primary.as_posix(),
                    digest,
                    len(body),
                )
            revision = primary.parent / "revisions" / f"{primary.stem}.{digest}.json"
            if not revision.exists():
                revision.parent.mkdir(parents=True, exist_ok=True)
                revision.write_bytes(body)
            elif hashlib.sha256(revision.read_bytes()).hexdigest() != digest:
                raise RuntimeError("deterministic KRX revision identity collision")
            return StoredArtifact(
                ArtifactDisposition.CONFLICT,
                revision.as_posix(),
                digest,
                len(body),
            )
        primary.parent.mkdir(parents=True, exist_ok=True)
        primary.write_bytes(body)
        return StoredArtifact(
            ArtifactDisposition.CREATED,
            primary.as_posix(),
            digest,
            len(body),
        )

    def append_manifest(self, event: ManifestEvent) -> Path:
        path = self.root / "manifest" / "requests.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = asdict(event)
        record["status"] = event.status.value
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return path
