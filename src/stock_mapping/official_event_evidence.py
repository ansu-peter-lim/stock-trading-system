"""Immutable official-event evidence and offline revision resolution.

"Authoritative" in this module means only the single currently usable ACTIVE
leaf in an official-source revision set.  It does not confirm a historical
state or authorize promotion into the historical master.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .normalization import normalize_stock_name


class OfficialEventType(str, Enum):
    NAME_CHANGE = "NAME_CHANGE"


class OfficialSourceSystem(str, Enum):
    KRX_KIND = "KRX_KIND"


class OfficialDocumentType(str, Enum):
    """Immutable kind of document asserted by the source adapter."""

    ORIGINAL = "ORIGINAL"
    CORRECTION = "CORRECTION"
    CANCELLATION = "CANCELLATION"


class ResolvedEvidenceDisposition(str, Enum):
    """Current node disposition derived from the complete revision graph."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"


class EvidenceResolutionStatus(str, Enum):
    RESOLVED = "RESOLVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CONFLICT = "CONFLICT"
    CANCELLED = "CANCELLED"


class EvidenceResolutionReason(str, Enum):
    INVALID_ARTIFACT_PROVENANCE = "INVALID_ARTIFACT_PROVENANCE"
    INVALID_EVIDENCE_FIELD = "INVALID_EVIDENCE_FIELD"
    UNSUPPORTED_EVENT_TYPE = "UNSUPPORTED_EVENT_TYPE"
    INVALID_CANONICAL_STOCK_CODE = "INVALID_CANONICAL_STOCK_CODE"
    UNCHANGED_OFFICIAL_NAME = "UNCHANGED_OFFICIAL_NAME"
    MIXED_REVISION_SETS = "MIXED_REVISION_SETS"
    DUPLICATE_DOCUMENT_ID = "DUPLICATE_DOCUMENT_ID"
    CONFLICTING_DOCUMENT_ID = "CONFLICTING_DOCUMENT_ID"
    MISSING_SUPERSEDED_DOCUMENT = "MISSING_SUPERSEDED_DOCUMENT"
    SELF_SUPERSESSION = "SELF_SUPERSESSION"
    REVISION_CYCLE = "REVISION_CYCLE"
    INVALID_REVISION_ORDER = "INVALID_REVISION_ORDER"
    INVALID_LIFECYCLE_CHAIN = "INVALID_LIFECYCLE_CHAIN"
    COMPETING_ACTIVE_LEAVES = "COMPETING_ACTIVE_LEAVES"
    CANCELLED_AUTHORITATIVE_LEAF = "CANCELLED_AUTHORITATIVE_LEAF"


@dataclass(frozen=True, slots=True)
class OfficialEffectiveDateEvidence:
    revision_set_id: str
    event_type: OfficialEventType
    canonical_stock_code: str | None
    raw_source_stock_code: str
    source_code_namespace: str
    identity_contract_version: str
    official_effective_date: date
    previous_full_name_raw: str
    current_full_name_raw: str
    previous_abbreviation_raw: str
    current_abbreviation_raw: str
    market: str
    source_system: OfficialSourceSystem
    source_document_id: str
    source_reference: str
    published_at: datetime | None
    retrieved_at: datetime
    artifact_path: str
    artifact_sha256: str
    artifact_byte_size: int
    parser_version: str
    schema_version: str
    revision_number: int
    document_type: OfficialDocumentType
    supersedes_document_id: str | None

    @property
    def evidence_id(self) -> str:
        """Return a deterministic digest of semantic content and source bytes.

        Local ``artifact_path`` and collection-specific ``retrieved_at`` are
        intentionally excluded.  JSON keys are sorted, enum values are strings,
        datetimes are UTC ISO-8601 with ``Z``, and ``None`` is JSON ``null``.
        """
        canonical = {
            "artifact_byte_size": self.artifact_byte_size,
            "artifact_sha256": self.artifact_sha256.lower(),
            "canonical_stock_code": self.canonical_stock_code,
            "current_abbreviation_raw": self.current_abbreviation_raw,
            "current_full_name_raw": self.current_full_name_raw,
            "event_type": self.event_type.value,
            "identity_contract_version": self.identity_contract_version,
            "market": self.market,
            "official_effective_date": self.official_effective_date.isoformat(),
            "parser_version": self.parser_version,
            "previous_abbreviation_raw": self.previous_abbreviation_raw,
            "previous_full_name_raw": self.previous_full_name_raw,
            "published_at": _datetime_text(self.published_at),
            "raw_source_stock_code": self.raw_source_stock_code,
            "revision_number": self.revision_number,
            "revision_set_id": self.revision_set_id,
            "schema_version": self.schema_version,
            "source_code_namespace": self.source_code_namespace,
            "source_document_id": self.source_document_id,
            "source_reference": self.source_reference,
            "source_system": self.source_system.value,
            "supersedes_document_id": self.supersedes_document_id,
            "document_type": self.document_type.value,
        }
        body = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceResolutionResult:
    status: EvidenceResolutionStatus
    reasons: tuple[EvidenceResolutionReason, ...]
    authoritative_evidence: OfficialEffectiveDateEvidence | None
    node_dispositions: tuple[tuple[str, ResolvedEvidenceDisposition], ...]


class OfficialEvidenceValidationError(ValueError):
    """Typed, deterministic validation failure for one evidence object."""

    def __init__(self, reasons: tuple[EvidenceResolutionReason, ...]) -> None:
        self.reasons = _ordered_reasons(reasons)
        super().__init__(", ".join(reason.value for reason in self.reasons))


def validate_official_evidence(evidence: OfficialEffectiveDateEvidence) -> None:
    """Validate fields and byte-exact local artifact provenance."""
    reasons: list[EvidenceResolutionReason] = []
    required_text = (
        evidence.revision_set_id,
        evidence.raw_source_stock_code,
        evidence.source_code_namespace,
        evidence.identity_contract_version,
        evidence.previous_full_name_raw,
        evidence.current_full_name_raw,
        evidence.previous_abbreviation_raw,
        evidence.current_abbreviation_raw,
        evidence.market,
        evidence.source_document_id,
        evidence.source_reference,
        evidence.artifact_path,
        evidence.parser_version,
        evidence.schema_version,
    )
    if any(not isinstance(value, str) or not value.strip() for value in required_text):
        reasons.append(EvidenceResolutionReason.INVALID_EVIDENCE_FIELD)
    if evidence.event_type is not OfficialEventType.NAME_CHANGE:
        reasons.append(EvidenceResolutionReason.UNSUPPORTED_EVENT_TYPE)
    if evidence.canonical_stock_code is not None and not _is_canonical_code(
        evidence.canonical_stock_code
    ):
        reasons.append(EvidenceResolutionReason.INVALID_CANONICAL_STOCK_CODE)
    if evidence.market.strip().upper() not in {"KOSPI", "KOSDAQ"}:
        reasons.append(EvidenceResolutionReason.INVALID_EVIDENCE_FIELD)
    if evidence.revision_number < 1:
        reasons.append(EvidenceResolutionReason.INVALID_EVIDENCE_FIELD)
    if not _is_aware(evidence.retrieved_at) or (
        evidence.published_at is not None and not _is_aware(evidence.published_at)
    ):
        reasons.append(EvidenceResolutionReason.INVALID_EVIDENCE_FIELD)
    if evidence.supersedes_document_id == "":
        reasons.append(EvidenceResolutionReason.INVALID_EVIDENCE_FIELD)
    if (
        evidence.document_type is OfficialDocumentType.ORIGINAL
        and evidence.supersedes_document_id is not None
    ) or (
        evidence.document_type
        in {OfficialDocumentType.CORRECTION, OfficialDocumentType.CANCELLATION}
        and evidence.supersedes_document_id is None
    ):
        reasons.append(EvidenceResolutionReason.INVALID_LIFECYCLE_CHAIN)
    if _reference_contains_credentials(evidence.source_reference):
        reasons.append(EvidenceResolutionReason.INVALID_EVIDENCE_FIELD)
    if normalize_stock_name(evidence.previous_full_name_raw) == normalize_stock_name(
        evidence.current_full_name_raw
    ) or normalize_stock_name(
        evidence.previous_abbreviation_raw
    ) == normalize_stock_name(evidence.current_abbreviation_raw):
        reasons.append(EvidenceResolutionReason.UNCHANGED_OFFICIAL_NAME)
    if not _valid_artifact(evidence):
        reasons.append(EvidenceResolutionReason.INVALID_ARTIFACT_PROVENANCE)
    if reasons:
        raise OfficialEvidenceValidationError(tuple(reasons))


def resolve_authoritative_evidence(
    revision_set_id: str,
    evidence_set: list[OfficialEffectiveDateEvidence]
    | tuple[OfficialEffectiveDateEvidence, ...],
) -> EvidenceResolutionResult:
    """Resolve one explicitly grouped revision set, never a historical state.

    The caller supplies the stable revision-set identity.  This function never
    groups documents by stock code, because one security can have multiple
    unrelated name-change events.
    """
    ordered = sorted(
        evidence_set, key=lambda item: (item.source_document_id, item.evidence_id)
    )
    reasons: list[EvidenceResolutionReason] = []
    if not revision_set_id.strip() or any(
        evidence.revision_set_id != revision_set_id for evidence in ordered
    ):
        reasons.append(EvidenceResolutionReason.MIXED_REVISION_SETS)
    for evidence in ordered:
        try:
            validate_official_evidence(evidence)
        except OfficialEvidenceValidationError as exc:
            reasons.extend(exc.reasons)

    if EvidenceResolutionReason.MIXED_REVISION_SETS in reasons:
        return EvidenceResolutionResult(
            EvidenceResolutionStatus.REVIEW_REQUIRED,
            _ordered_reasons(tuple(reasons)),
            None,
            (),
        )

    by_document: dict[str, OfficialEffectiveDateEvidence] = {}
    duplicates: set[str] = set()
    conflicting_duplicates: set[str] = set()
    for evidence in ordered:
        if evidence.source_document_id in by_document:
            duplicates.add(evidence.source_document_id)
            if (
                by_document[evidence.source_document_id].evidence_id
                != evidence.evidence_id
            ):
                conflicting_duplicates.add(evidence.source_document_id)
        else:
            by_document[evidence.source_document_id] = evidence
    if duplicates:
        reasons.append(EvidenceResolutionReason.DUPLICATE_DOCUMENT_ID)
    if conflicting_duplicates:
        reasons.append(EvidenceResolutionReason.CONFLICTING_DOCUMENT_ID)

    superseded_targets: set[str] = set()
    for evidence in ordered:
        target_id = evidence.supersedes_document_id
        if target_id is None:
            continue
        if target_id == evidence.source_document_id:
            reasons.append(EvidenceResolutionReason.SELF_SUPERSESSION)
            continue
        target = by_document.get(target_id)
        if target is None:
            reasons.append(EvidenceResolutionReason.MISSING_SUPERSEDED_DOCUMENT)
            continue
        superseded_targets.add(target_id)
        if evidence.revision_number <= target.revision_number:
            reasons.append(EvidenceResolutionReason.INVALID_REVISION_ORDER)
    if _has_revision_cycle(by_document):
        reasons.append(EvidenceResolutionReason.REVISION_CYCLE)

    leaves = [
        evidence
        for evidence in ordered
        if evidence.source_document_id not in superseded_targets
    ]
    active_leaves = [
        evidence
        for evidence in leaves
        if evidence.document_type is not OfficialDocumentType.CANCELLATION
    ]
    cancelled_leaves = [
        evidence
        for evidence in leaves
        if evidence.document_type is OfficialDocumentType.CANCELLATION
    ]
    if len(leaves) > 1:
        reasons.append(EvidenceResolutionReason.COMPETING_ACTIVE_LEAVES)
    if len(leaves) == 1 and cancelled_leaves:
        reasons.append(EvidenceResolutionReason.CANCELLED_AUTHORITATIVE_LEAF)

    dispositions = _node_dispositions(ordered, superseded_targets, leaves)

    canonical_reasons = _ordered_reasons(tuple(reasons))
    if canonical_reasons:
        status = _resolution_status(canonical_reasons)
        return EvidenceResolutionResult(status, canonical_reasons, None, dispositions)
    if len(active_leaves) != 1:
        return EvidenceResolutionResult(
            EvidenceResolutionStatus.REVIEW_REQUIRED,
            (EvidenceResolutionReason.INVALID_LIFECYCLE_CHAIN,),
            None,
            dispositions,
        )
    return EvidenceResolutionResult(
        EvidenceResolutionStatus.RESOLVED,
        (),
        active_leaves[0],
        dispositions,
    )


def _node_dispositions(
    evidence_set: list[OfficialEffectiveDateEvidence],
    superseded_targets: set[str],
    leaves: list[OfficialEffectiveDateEvidence],
) -> tuple[tuple[str, ResolvedEvidenceDisposition], ...]:
    leaf_ids = {item.source_document_id for item in leaves}
    values: list[tuple[str, ResolvedEvidenceDisposition]] = []
    for evidence in evidence_set:
        if evidence.source_document_id in superseded_targets:
            disposition = ResolvedEvidenceDisposition.SUPERSEDED
        elif (
            evidence.source_document_id in leaf_ids
            and evidence.document_type is OfficialDocumentType.CANCELLATION
        ):
            disposition = ResolvedEvidenceDisposition.CANCELLED
        else:
            disposition = ResolvedEvidenceDisposition.ACTIVE
        values.append((evidence.source_document_id, disposition))
    return tuple(sorted(set(values)))


def _valid_artifact(evidence: OfficialEffectiveDateEvidence) -> bool:
    digest = evidence.artifact_sha256.lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return False
    if evidence.artifact_byte_size < 0:
        return False
    path = Path(evidence.artifact_path)
    if not path.is_file():
        return False
    body = path.read_bytes()
    return (
        len(body) == evidence.artifact_byte_size
        and hashlib.sha256(body).hexdigest() == digest
    )


def _has_revision_cycle(
    by_document: dict[str, OfficialEffectiveDateEvidence],
) -> bool:
    for start in sorted(by_document):
        seen: set[str] = set()
        current: str | None = start
        while current is not None and current in by_document:
            if current in seen:
                return True
            seen.add(current)
            current = by_document[current].supersedes_document_id
    return False


def _ordered_reasons(
    reasons: tuple[EvidenceResolutionReason, ...],
) -> tuple[EvidenceResolutionReason, ...]:
    present = set(reasons)
    return tuple(reason for reason in EvidenceResolutionReason if reason in present)


def _resolution_status(
    reasons: tuple[EvidenceResolutionReason, ...],
) -> EvidenceResolutionStatus:
    if EvidenceResolutionReason.CANCELLED_AUTHORITATIVE_LEAF in reasons:
        return EvidenceResolutionStatus.CANCELLED
    conflicts = {
        EvidenceResolutionReason.DUPLICATE_DOCUMENT_ID,
        EvidenceResolutionReason.CONFLICTING_DOCUMENT_ID,
        EvidenceResolutionReason.REVISION_CYCLE,
        EvidenceResolutionReason.COMPETING_ACTIVE_LEAVES,
    }
    if any(reason in conflicts for reason in reasons):
        return EvidenceResolutionStatus.CONFLICT
    return EvidenceResolutionStatus.REVIEW_REQUIRED


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _is_canonical_code(value: str) -> bool:
    return len(value) == 6 and value.isascii() and value.isdigit()


def _reference_contains_credentials(reference: str) -> bool:
    secret_markers = ("AUTH", "KEY", "TOKEN", "SECRET", "PASSWORD")
    try:
        keys = (key.upper() for key, _ in parse_qsl(urlsplit(reference).query))
        return any(any(marker in key for marker in secret_markers) for key in keys)
    except ValueError:
        return True
