"""Adapt stored KRX stock-basic snapshots into non-production observations.

Observation dates are evidence dates, not confirmed historical interval bounds.
This module deliberately has no network or historical-master write path.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from itertools import pairwise
from pathlib import Path

from src.krx_openapi.parser import KrxParsedResponse
from src.krx_openapi.services import KRX_SERVICES
from src.krx_openapi.store import ManifestEvent, ManifestStatus

from .historical_master import ValidationError
from .normalization import normalize_stock_name


class CanonicalCodeEligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE_NON_NUMERIC_CODE = "INELIGIBLE_NON_NUMERIC_CODE"


class SnapshotStatus(str, Enum):
    SUCCESS_WITH_ROWS = "SUCCESS_WITH_ROWS"
    SUCCESS_EMPTY = "SUCCESS_EMPTY"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    HTTP_ERROR = "HTTP_ERROR"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    RAW_CONFLICT = "RAW_CONFLICT"


class MappingNameSource(str, Enum):
    ISU_ABBRV = "ISU_ABBRV"


class ListingDateBasis(str, Enum):
    KRX_LIST_DD_CANDIDATE = "KRX_LIST_DD_CANDIDATE"


class EffectiveDateBasis(str, Enum):
    OBSERVED_WINDOW = "OBSERVED_WINDOW"
    CONFIRMED_EFFECTIVE_FROM = "CONFIRMED_EFFECTIVE_FROM"


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    raw_source_code: str
    eligibility: CanonicalCodeEligibility
    canonical_stock_code: str | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class SnapshotProvenance:
    service_id: str
    bas_dd: str
    raw_artifact_path: str
    raw_sha256: str
    retrieved_at: str
    byte_size: int | None
    collector_version: str
    schema_version: str
    manifest_status: str


@dataclass(frozen=True, slots=True)
class SnapshotInput:
    status: SnapshotStatus
    expected_market: str
    provenance: SnapshotProvenance | None

    @property
    def observed_on(self) -> date | None:
        if self.provenance is None:
            return None
        return _parse_yyyymmdd(self.provenance.bas_dd, "basDd")


@dataclass(frozen=True, slots=True)
class SnapshotObservation:
    raw_source_code: str
    canonical_stock_code: str | None
    eligibility: CanonicalCodeEligibility
    eligibility_reason_code: str
    observed_on: date
    raw_isu_nm: str
    raw_isu_abbrv: str
    mapping_name_source: MappingNameSource
    normalized_mapping_name: str
    observed_market: str
    list_dd_raw: str
    list_dd_candidate: date
    listing_date_basis: ListingDateBasis
    security_type_raw: str
    provenance: SnapshotProvenance = field(compare=False)
    source_row_index: int = field(compare=False)

    @property
    def state_key(self) -> tuple[str, str, date, str]:
        return (
            self.normalized_mapping_name,
            self.observed_market,
            self.list_dd_candidate,
            self.security_type_raw,
        )


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    status: SnapshotStatus
    observed_on: date | None
    expected_market: str
    observations: tuple[SnapshotObservation, ...]
    exclusion_counts: tuple[tuple[str, int], ...]
    provenance: SnapshotProvenance | None


@dataclass(frozen=True, slots=True)
class ObservationRun:
    canonical_stock_code: str
    first_observed_on: date
    last_observed_on: date
    normalized_mapping_name: str
    observed_market: str
    list_dd_candidate: date
    security_type_raw: str
    supporting_observations: tuple[SnapshotObservation, ...]
    provenance_references: tuple[SnapshotProvenance, ...]


@dataclass(frozen=True, slots=True)
class TransitionCandidate:
    canonical_stock_code: str
    previous_observed_on: date
    current_observed_on: date
    changed_fields: tuple[str, ...]
    effective_date_basis: EffectiveDateBasis
    confirmed_effective_from: date | None
    previous_observation: SnapshotObservation
    current_observation: SnapshotObservation
    provenance_references: tuple[SnapshotProvenance, ...]


@dataclass(frozen=True, slots=True)
class AbsenceObservation:
    canonical_stock_code: str
    previously_observed_on: date
    absent_on: date
    market: str
    provenance_reference: SnapshotProvenance


@dataclass(frozen=True, slots=True)
class ConfirmedEffectiveDateEvidence:
    effective_from: date
    source_name: str
    reference: str


def classify_canonical_code(raw_source_code: str) -> EligibilityResult:
    if (
        not isinstance(raw_source_code, str)
        or len(raw_source_code) != 6
        or not raw_source_code.isascii()
        or not raw_source_code.isalnum()
    ):
        raise ValidationError(
            "raw_source_code must be six ASCII alphanumeric characters"
        )
    if raw_source_code.isdigit():
        return EligibilityResult(
            raw_source_code,
            CanonicalCodeEligibility.ELIGIBLE,
            raw_source_code,
            "ELIGIBLE_NUMERIC_CODE",
        )
    return EligibilityResult(
        raw_source_code,
        CanonicalCodeEligibility.INELIGIBLE_NON_NUMERIC_CODE,
        None,
        "NON_NUMERIC_KRX_SOURCE_CODE",
    )


def snapshot_input_from_manifest(
    event: ManifestEvent, *, expected_market: str
) -> SnapshotInput:
    status = {
        ManifestStatus.SUCCESS: SnapshotStatus.SUCCESS_WITH_ROWS,
        ManifestStatus.EMPTY_RESPONSE: SnapshotStatus.SUCCESS_EMPTY,
        ManifestStatus.HTTP_ERROR: SnapshotStatus.HTTP_ERROR,
        ManifestStatus.SCHEMA_ERROR: SnapshotStatus.SCHEMA_ERROR,
        ManifestStatus.CONFLICT: SnapshotStatus.RAW_CONFLICT,
    }[event.status]
    provenance = None
    if event.raw_file_path:
        provenance = SnapshotProvenance(
            service_id=event.service_id,
            bas_dd=event.bas_dd,
            raw_artifact_path=event.raw_file_path,
            raw_sha256=event.raw_sha256,
            retrieved_at=event.retrieved_at,
            byte_size=event.byte_size,
            collector_version=event.collector_version,
            schema_version=event.schema_version,
            manifest_status=event.status.value,
        )
    return SnapshotInput(status, _normalize_market(expected_market), provenance)


def adapt_stock_basic_snapshot(
    parsed: KrxParsedResponse | None, snapshot: SnapshotInput
) -> SnapshotResult:
    if snapshot.status in {
        SnapshotStatus.NOT_ATTEMPTED,
        SnapshotStatus.HTTP_ERROR,
        SnapshotStatus.SCHEMA_ERROR,
        SnapshotStatus.RAW_CONFLICT,
    }:
        raise ValidationError(
            f"snapshot status blocks processing: {snapshot.status.value}"
        )
    if snapshot.provenance is None:
        raise ValidationError("successful snapshot requires raw provenance")
    _validate_snapshot_contract(snapshot)
    validate_snapshot_artifact(snapshot.provenance)
    observed_on = snapshot.observed_on
    if observed_on is None:
        raise ValidationError("successful snapshot requires observed_on")
    if parsed is None:
        raise ValidationError("successful snapshot requires parsed rows")
    if snapshot.status is SnapshotStatus.SUCCESS_EMPTY:
        if parsed.row_count != 0:
            raise ValidationError("SUCCESS_EMPTY snapshot must not contain rows")
        return SnapshotResult(
            snapshot.status,
            observed_on,
            snapshot.expected_market,
            (),
            (),
            snapshot.provenance,
        )
    if parsed.row_count == 0:
        raise ValidationError("SUCCESS_WITH_ROWS snapshot must contain rows")

    observations = tuple(
        sorted(
            (
                _observation_from_row(
                    row.raw_fields,
                    observed_on=observed_on,
                    expected_market=snapshot.expected_market,
                    provenance=snapshot.provenance,
                    source_row_index=index,
                )
                for index, row in enumerate(parsed.rows, start=1)
            ),
            key=_observation_sort_key,
        )
    )
    _validate_same_date_market_conflicts(observations)
    counts: dict[str, int] = {}
    for observation in observations:
        if observation.eligibility is not CanonicalCodeEligibility.ELIGIBLE:
            reason = observation.eligibility_reason_code
            counts[reason] = counts.get(reason, 0) + 1
    return SnapshotResult(
        snapshot.status,
        observed_on,
        snapshot.expected_market,
        observations,
        tuple(sorted(counts.items())),
        snapshot.provenance,
    )


def build_observation_runs(
    observations: Sequence[SnapshotObservation],
) -> tuple[ObservationRun, ...]:
    eligible = [
        item
        for item in observations
        if item.eligibility is CanonicalCodeEligibility.ELIGIBLE
        and item.canonical_stock_code is not None
    ]
    _validate_same_date_market_conflicts(eligible)
    validate_list_dd_consistency(eligible)
    ordered = sorted(eligible, key=_observation_sort_key)
    runs: list[ObservationRun] = []
    for observation in ordered:
        previous = runs[-1] if runs else None
        if (
            previous is not None
            and previous.canonical_stock_code == observation.canonical_stock_code
            and (
                previous.normalized_mapping_name,
                previous.observed_market,
                previous.list_dd_candidate,
                previous.security_type_raw,
            )
            == observation.state_key
        ):
            supporting = previous.supporting_observations + (observation,)
            references = _unique_provenance(supporting)
            runs[-1] = ObservationRun(
                previous.canonical_stock_code,
                previous.first_observed_on,
                observation.observed_on,
                previous.normalized_mapping_name,
                previous.observed_market,
                previous.list_dd_candidate,
                previous.security_type_raw,
                supporting,
                references,
            )
            continue
        runs.append(
            ObservationRun(
                observation.canonical_stock_code,
                observation.observed_on,
                observation.observed_on,
                observation.normalized_mapping_name,
                observation.observed_market,
                observation.list_dd_candidate,
                observation.security_type_raw,
                (observation,),
                (observation.provenance,),
            )
        )
    return tuple(runs)


def build_transition_candidates(
    runs: Sequence[ObservationRun],
) -> tuple[TransitionCandidate, ...]:
    by_code: dict[str, list[ObservationRun]] = {}
    for run in runs:
        by_code.setdefault(run.canonical_stock_code, []).append(run)
    candidates: list[TransitionCandidate] = []
    fields = (
        ("normalized_mapping_name", "normalized_mapping_name"),
        ("observed_market", "observed_market"),
        ("list_dd_candidate", "list_dd_candidate"),
        ("security_type_raw", "security_type_raw"),
    )
    for code in sorted(by_code):
        ordered = sorted(by_code[code], key=lambda item: item.first_observed_on)
        for previous, current in pairwise(ordered):
            changed = tuple(
                label
                for label, attribute in fields
                if getattr(previous, attribute) != getattr(current, attribute)
            )
            if not changed:
                continue
            candidates.append(
                TransitionCandidate(
                    code,
                    previous.last_observed_on,
                    current.first_observed_on,
                    changed,
                    EffectiveDateBasis.OBSERVED_WINDOW,
                    None,
                    previous.supporting_observations[-1],
                    current.supporting_observations[0],
                    _unique_provenance(
                        (
                            previous.supporting_observations[-1],
                            current.supporting_observations[0],
                        )
                    ),
                )
            )
    return tuple(candidates)


def build_absence_observations(
    previous: SnapshotResult, current: SnapshotResult
) -> tuple[AbsenceObservation, ...]:
    allowed = {SnapshotStatus.SUCCESS_WITH_ROWS, SnapshotStatus.SUCCESS_EMPTY}
    if previous.status not in allowed or current.status not in allowed:
        return ()
    if previous.expected_market != current.expected_market:
        return ()
    if (
        previous.observed_on is None
        or current.observed_on is None
        or current.provenance is None
    ):
        return ()
    current_codes = {
        item.canonical_stock_code
        for item in current.observations
        if item.canonical_stock_code is not None
    }
    absent = {
        item.canonical_stock_code: item
        for item in previous.observations
        if item.canonical_stock_code is not None
        and item.canonical_stock_code not in current_codes
    }
    return tuple(
        AbsenceObservation(
            code,
            observation.observed_on,
            current.observed_on,
            observation.observed_market,
            current.provenance,
        )
        for code, observation in sorted(absent.items())
    )


def require_confirmed_effective_date(
    candidate: TransitionCandidate,
    evidence: ConfirmedEffectiveDateEvidence | None = None,
) -> date:
    if (
        candidate.effective_date_basis
        is not EffectiveDateBasis.CONFIRMED_EFFECTIVE_FROM
        or candidate.confirmed_effective_from is None
        or evidence is None
        or evidence.effective_from != candidate.confirmed_effective_from
    ):
        raise ValidationError(
            "transition candidate lacks confirmed effective-date evidence; promotion blocked"
        )
    if not evidence.source_name.strip() or not evidence.reference.strip():
        raise ValidationError("confirmed effective-date evidence is incomplete")
    return evidence.effective_from


def validate_snapshot_artifact(provenance: SnapshotProvenance) -> None:
    digest = provenance.raw_sha256.lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValidationError("snapshot raw SHA-256 must be a 64-character hex digest")
    path = Path(provenance.raw_artifact_path)
    if not path.is_file():
        raise ValidationError("snapshot raw artifact does not exist")
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValidationError(
            f"snapshot raw artifact SHA-256 mismatch: {provenance.raw_artifact_path}"
        )
    if provenance.byte_size is not None and path.stat().st_size != provenance.byte_size:
        raise ValidationError("snapshot raw artifact byte size mismatch")


def validate_list_dd_consistency(
    observations: Sequence[SnapshotObservation],
) -> None:
    candidates: dict[str, set[date]] = {}
    for item in observations:
        if item.canonical_stock_code is not None:
            candidates.setdefault(item.canonical_stock_code, set()).add(
                item.list_dd_candidate
            )
    if any(len(values) > 1 for values in candidates.values()):
        raise ValidationError(
            "eligible canonical code has conflicting LIST_DD candidates"
        )


def _observation_from_row(
    raw: Mapping[str, str],
    *,
    observed_on: date,
    expected_market: str,
    provenance: SnapshotProvenance,
    source_row_index: int,
) -> SnapshotObservation:
    eligibility = classify_canonical_code(raw["ISU_SRT_CD"])
    payload_market = _normalize_market(raw["MKT_TP_NM"])
    if payload_market != expected_market:
        raise ValidationError(
            "stock-basic endpoint market does not match payload market"
        )
    list_dd_raw = raw["LIST_DD"]
    return SnapshotObservation(
        raw_source_code=eligibility.raw_source_code,
        canonical_stock_code=eligibility.canonical_stock_code,
        eligibility=eligibility.eligibility,
        eligibility_reason_code=eligibility.reason_code,
        observed_on=observed_on,
        raw_isu_nm=raw["ISU_NM"],
        raw_isu_abbrv=raw["ISU_ABBRV"],
        mapping_name_source=MappingNameSource.ISU_ABBRV,
        normalized_mapping_name=normalize_stock_name(raw["ISU_ABBRV"]),
        observed_market=payload_market,
        list_dd_raw=list_dd_raw,
        list_dd_candidate=_parse_yyyymmdd(list_dd_raw, "LIST_DD"),
        listing_date_basis=ListingDateBasis.KRX_LIST_DD_CANDIDATE,
        security_type_raw=raw["KIND_STKCERT_TP_NM"],
        provenance=provenance,
        source_row_index=source_row_index,
    )


def _normalize_market(value: str) -> str:
    market = value.strip().upper()
    if market not in {"KOSPI", "KOSDAQ"}:
        raise ValidationError("market must be KOSPI or KOSDAQ")
    return market


def _validate_snapshot_contract(snapshot: SnapshotInput) -> None:
    provenance = snapshot.provenance
    if provenance is None:
        raise ValidationError("successful snapshot requires raw provenance")
    service = KRX_SERVICES.get(provenance.service_id)
    if service is None or service.artifact_group != "stock_basic":
        raise ValidationError(
            "snapshot provenance must reference a stock-basic service"
        )
    if service.market != snapshot.expected_market:
        raise ValidationError("snapshot service market does not match expected market")
    expected_manifest_status = {
        SnapshotStatus.SUCCESS_WITH_ROWS: ManifestStatus.SUCCESS.value,
        SnapshotStatus.SUCCESS_EMPTY: ManifestStatus.EMPTY_RESPONSE.value,
    }[snapshot.status]
    if provenance.manifest_status != expected_manifest_status:
        raise ValidationError("snapshot status does not match manifest status")


def _parse_yyyymmdd(value: str, field_name: str) -> date:
    if len(value) != 8 or not value.isascii() or not value.isdigit():
        raise ValidationError(f"{field_name} must use YYYYMMDD")
    try:
        return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
    except ValueError as exc:
        raise ValidationError(f"{field_name} must use YYYYMMDD") from exc


def _observation_sort_key(
    item: SnapshotObservation,
) -> tuple[str, date, str, str, str]:
    return (
        item.canonical_stock_code or f"~{item.raw_source_code}",
        item.observed_on,
        item.observed_market,
        item.normalized_mapping_name,
        item.raw_source_code,
    )


def _validate_same_date_market_conflicts(
    observations: Sequence[SnapshotObservation],
) -> None:
    markets: dict[tuple[str, date], set[str]] = {}
    for item in observations:
        if item.canonical_stock_code is None:
            continue
        key = (item.canonical_stock_code, item.observed_on)
        markets.setdefault(key, set()).add(item.observed_market)
    if any(len(values) > 1 for values in markets.values()):
        raise ValidationError(
            "eligible canonical code appears in multiple markets on the same date"
        )


def _unique_provenance(
    observations: Sequence[SnapshotObservation],
) -> tuple[SnapshotProvenance, ...]:
    values = {item.provenance for item in observations}
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item.bas_dd,
                item.service_id,
                item.raw_artifact_path,
                item.raw_sha256,
            ),
        )
    )
