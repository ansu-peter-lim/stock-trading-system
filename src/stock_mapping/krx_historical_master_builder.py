"""Build an auditable historical stock master from adapted KRX source rows.

This module does not download data.  Network use is gated separately and no
network implementation is invoked by the builder CLI.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .historical_master import (
    MASTER_FIELDS,
    HistoricalStock,
    MappingResult,
    ValidationError,
    parse_iso_date,
    validate_master_intervals,
    validate_stock_code,
    write_mapping_results,
)
from .normalization import normalize_stock_name


DEFAULT_MASTER_OUTPUT = Path("data/processed/market/historical_stock_master.csv")
DEFAULT_PROVENANCE_OUTPUT = Path("data/processed/market/historical_stock_master_provenance.csv")
TOP30_MAPPING_OUTPUT = Path("data/processed/telegram/top30_stock_mapping.csv")
TOP30_REVIEW_OUTPUT = Path("data/processed/telegram/top30_stock_mapping_review.csv")
TOP30_SUMMARY_OUTPUT = Path("data/processed/telegram/top30_stock_mapping_summary.json")

SOURCE_STATE_FIELDS = (
    "stock_code", "market", "stock_name", "effective_date", "listing_date",
    "delisting_date_raw", "delisting_date_meaning", "security_type_raw",
    "source_name", "source_role", "service_name", "source_as_of", "raw_file_path",
)
PROVENANCE_FIELDS = (
    "source_name", "service_name", "requested_base_date", "retrieved_at",
    "request_parameters", "raw_file_path", "raw_file_sha256", "row_count",
)
ISSUE_FIELDS = ("severity", "issue_code", "stock_code", "source_row", "message")


class DateMeaning(str, Enum):
    EFFECTIVE_DATE = "EFFECTIVE_DATE"
    LAST_TRADING_DATE = "LAST_TRADING_DATE"
    OTHER = "OTHER"
    UNCONFIRMED = "UNCONFIRMED"


class SourceRole(str, Enum):
    PRIMARY_KRX = "PRIMARY_KRX"
    EXCEPTION_KIND = "EXCEPTION_KIND"
    CURRENT_CROSSCHECK_KIWOOM = "CURRENT_CROSSCHECK_KIWOOM"


@dataclass(frozen=True)
class SourceSecurityState:
    stock_code: str
    market: str
    stock_name: str
    effective_date: date
    listing_date: date
    delisting_date_raw: date | None
    delisting_date_meaning: DateMeaning
    security_type_raw: str
    source_name: str
    source_role: SourceRole
    service_name: str
    source_as_of: date
    raw_file_path: str
    source_row: int


@dataclass(frozen=True)
class RawProvenance:
    source_name: str
    service_name: str
    requested_base_date: str
    retrieved_at: str
    request_parameters: str
    raw_file_path: str
    raw_file_sha256: str
    row_count: int


@dataclass(frozen=True)
class BuildIssue:
    severity: str
    issue_code: str
    stock_code: str
    source_row: int
    message: str


@dataclass(frozen=True)
class BuildResult:
    records: list[HistoricalStock]
    issues: list[BuildIssue]

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "ERROR" for issue in self.issues)


def standardize_security_type(raw_value: str) -> str:
    """Classify only explicit raw labels; retain raw_value on the master row."""
    upper = raw_value.strip().upper()
    if "SPAC" in upper or "스팩" in raw_value or "기업인수목적" in raw_value:
        return "SPAC"
    if "PREFERRED" in upper or "우선" in raw_value:
        return "PREFERRED"
    if "REIT" in upper or "리츠" in raw_value:
        return "REIT"
    if "COMMON" in upper or "보통" in raw_value:
        return "COMMON"
    return "OTHER"


def source_state_from_row(row: Mapping[str, str], row_number: int) -> SourceSecurityState:
    missing = [field for field in SOURCE_STATE_FIELDS if field not in row]
    if missing:
        raise ValidationError(f"row {row_number}: missing source field(s): {', '.join(missing)}")
    try:
        market = row["market"].strip().upper()
        if market not in {"KOSPI", "KOSDAQ"}:
            raise ValidationError("market must be KOSPI or KOSDAQ")
        meaning_text = row["delisting_date_meaning"].strip() or "UNCONFIRMED"
        try:
            meaning = DateMeaning(meaning_text)
        except ValueError as exc:
            raise ValidationError("unsupported delisting_date_meaning") from exc
        try:
            source_role = SourceRole(row["source_role"].strip())
        except ValueError as exc:
            raise ValidationError("unsupported source_role") from exc
        return SourceSecurityState(
            stock_code=validate_stock_code(row["stock_code"]),
            market=market,
            stock_name=row["stock_name"],
            effective_date=parse_iso_date(row["effective_date"], "effective_date"),
            listing_date=parse_iso_date(row["listing_date"], "listing_date"),
            delisting_date_raw=parse_iso_date(
                row["delisting_date_raw"], "delisting_date_raw", required=False
            ),
            delisting_date_meaning=meaning,
            security_type_raw=row["security_type_raw"],
            source_name=row["source_name"].strip(),
            source_role=source_role,
            service_name=row["service_name"].strip(),
            source_as_of=parse_iso_date(row["source_as_of"], "source_as_of"),
            raw_file_path=row["raw_file_path"].strip(),
            source_row=row_number,
        )
    except ValidationError as exc:
        raise ValidationError(f"row {row_number}: {exc}") from exc


def load_source_states(path: Path) -> list[SourceSecurityState]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return [source_state_from_row(row, number) for number, row in enumerate(reader, 2)]


def provenance_for_file(
    path: Path,
    *,
    source_name: str,
    service_name: str,
    requested_base_date: str,
    retrieved_at: str,
    request_parameters: Mapping[str, object],
    row_count: int,
) -> RawProvenance:
    body = path.read_bytes()
    parameters = dict(request_parameters)
    for key in parameters:
        if "KEY" in key.upper() or "TOKEN" in key.upper() or "AUTH" in key.upper():
            raise ValidationError("request_parameters must not contain credentials")
    return RawProvenance(
        source_name=source_name,
        service_name=service_name,
        requested_base_date=requested_base_date,
        retrieved_at=retrieved_at,
        request_parameters=json.dumps(parameters, ensure_ascii=False, sort_keys=True),
        raw_file_path=path.as_posix(),
        raw_file_sha256=hashlib.sha256(body).hexdigest(),
        row_count=row_count,
    )


def load_provenance(path: Path) -> list[RawProvenance]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != PROVENANCE_FIELDS:
            raise ValidationError("provenance header does not match required schema")
        records: list[RawProvenance] = []
        for number, row in enumerate(reader, 2):
            try:
                parameters = json.loads(row["request_parameters"])
                if not isinstance(parameters, dict):
                    raise ValidationError("request_parameters must be a JSON object")
                for key in parameters:
                    if "KEY" in key.upper() or "TOKEN" in key.upper() or "AUTH" in key.upper():
                        raise ValidationError("request_parameters must not contain credentials")
                digest = row["raw_file_sha256"].strip().lower()
                if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise ValidationError("raw_file_sha256 must be a 64-character hex digest")
                records.append(RawProvenance(
                    source_name=row["source_name"].strip(),
                    service_name=row["service_name"].strip(),
                    requested_base_date=row["requested_base_date"].strip(),
                    retrieved_at=row["retrieved_at"].strip(),
                    request_parameters=json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                    raw_file_path=row["raw_file_path"].strip(),
                    raw_file_sha256=digest,
                    row_count=int(row["row_count"]),
                ))
            except (ValueError, json.JSONDecodeError, ValidationError) as exc:
                raise ValidationError(f"provenance row {number}: {exc}") from exc
    return records


def validate_provenance(
    states: Sequence[SourceSecurityState], records: Sequence[RawProvenance]
) -> None:
    canonical = lambda value: Path(value).resolve().as_posix()
    by_path: dict[str, RawProvenance] = {}
    for record in records:
        key = canonical(record.raw_file_path)
        if key in by_path:
            raise ValidationError("duplicate provenance raw_file_path")
        by_path[key] = record
    missing = sorted(
        {canonical(state.raw_file_path) for state in states} - set(by_path)
    )
    if missing:
        raise ValidationError("missing provenance for source state raw_file_path")
    for canonical_path, record in by_path.items():
        path = Path(canonical_path)
        if path.exists():
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != record.raw_file_sha256:
                raise ValidationError(
                    f"raw provenance SHA-256 mismatch: {record.raw_file_path}"
                )
        if record.row_count < 0:
            raise ValidationError("provenance row_count must not be negative")


def _resolved_delisting_date(
    states: Sequence[SourceSecurityState], issues: list[BuildIssue]
) -> date | None:
    values = {(item.delisting_date_raw, item.delisting_date_meaning) for item in states
              if item.delisting_date_raw is not None}
    if not values:
        return None
    if len(values) > 1:
        item = states[0]
        issues.append(BuildIssue("ERROR", "CONFLICTING_DELISTING_SOURCE", item.stock_code,
                                 item.source_row, "Conflicting raw delisting dates or meanings"))
        return None
    raw_date, meaning = next(iter(values))
    if meaning is not DateMeaning.EFFECTIVE_DATE:
        item = next(state for state in states if state.delisting_date_raw is not None)
        issues.append(BuildIssue(
            "ERROR", "UNRESOLVED_DELISTING_DATE_SEMANTICS", item.stock_code,
            item.source_row,
            f"Raw delisting date meaning is {meaning.value}; exclusive boundary not inferred",
        ))
        return None
    return raw_date


def _compact_states(states: Sequence[SourceSecurityState]) -> list[SourceSecurityState]:
    compacted: list[SourceSecurityState] = []
    for state in sorted(states, key=lambda item: item.effective_date):
        if compacted and (
            compacted[-1].market == state.market
            and compacted[-1].stock_name == state.stock_name
            and compacted[-1].security_type_raw == state.security_type_raw
        ):
            continue
        compacted.append(state)
    return compacted


def build_historical_master(states: Sequence[SourceSecurityState]) -> BuildResult:
    issues: list[BuildIssue] = []
    records: list[HistoricalStock] = []
    grouped: dict[str, list[SourceSecurityState]] = {}
    for state in states:
        if state.source_role is SourceRole.CURRENT_CROSSCHECK_KIWOOM:
            issues.append(BuildIssue(
                "ERROR", "KIWOOM_CANNOT_DEFINE_HISTORICAL_INTERVAL", state.stock_code,
                state.source_row, "Kiwoom current master may only cross-check current state",
            ))
            continue
        grouped.setdefault(state.stock_code, []).append(state)

    for stock_code, code_states in grouped.items():
        listing_dates = {item.listing_date for item in code_states}
        if len(listing_dates) != 1:
            first = code_states[0]
            issues.append(BuildIssue("ERROR", "CONFLICTING_LISTING_DATE", stock_code,
                                     first.source_row, "Multiple listing dates for one code"))
            continue
        listing_date = next(iter(listing_dates))
        delisting_date = _resolved_delisting_date(code_states, issues)
        ordered = _compact_states(code_states)
        effective_dates = [item.effective_date for item in ordered]
        if len(effective_dates) != len(set(effective_dates)):
            first = ordered[0]
            issues.append(BuildIssue("ERROR", "DUPLICATE_EFFECTIVE_DATE", stock_code,
                                     first.source_row, "Multiple states begin on the same date"))
            continue
        for index, state in enumerate(ordered):
            if state.effective_date < listing_date:
                issues.append(BuildIssue("ERROR", "INTERVAL_BEFORE_LISTING", stock_code,
                                         state.source_row, "effective_date precedes listing_date"))
                continue
            if delisting_date is not None and state.effective_date >= delisting_date:
                issues.append(BuildIssue("ERROR", "INTERVAL_AT_OR_AFTER_DELISTING", stock_code,
                                         state.source_row, "state begins at/after exclusive delisting_date"))
                continue
            next_start = ordered[index + 1].effective_date if index + 1 < len(ordered) else None
            valid_to = next_start - timedelta(days=1) if next_start else None
            if delisting_date is not None:
                delisting_last_valid = delisting_date - timedelta(days=1)
                valid_to = min(valid_to, delisting_last_valid) if valid_to else delisting_last_valid
            if valid_to is not None and state.effective_date > valid_to:
                issues.append(BuildIssue("ERROR", "INVALID_INTERVAL", stock_code,
                                         state.source_row, "valid_from is after valid_to"))
                continue
            records.append(HistoricalStock(
                stock_code=stock_code,
                market=state.market,
                stock_name=state.stock_name,
                stock_name_normalized=normalize_stock_name(state.stock_name),
                valid_from=state.effective_date,
                valid_to=valid_to,
                listing_date=listing_date,
                delisting_date=delisting_date,
                security_type=standardize_security_type(state.security_type_raw),
                security_type_raw=state.security_type_raw,
                source=f"{state.source_name}:{state.service_name}",
                source_as_of=state.source_as_of,
            ))
    try:
        validate_master_intervals(records)
    except ValidationError as exc:
        issues.append(BuildIssue("ERROR", "OVERLAPPING_INTERVAL", "", 0, str(exc)))
    return BuildResult(records, issues)


def _date_text(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


def write_master(path: Path, result: BuildResult) -> None:
    if not result.valid:
        raise ValidationError("historical master has validation errors; output blocked")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MASTER_FIELDS)
        writer.writeheader()
        for record in result.records:
            row = asdict(record)
            for field in ("valid_from", "valid_to", "listing_date", "delisting_date", "source_as_of"):
                row[field] = _date_text(row[field])
            writer.writerow(row)


def write_provenance(path: Path, records: Iterable[RawProvenance]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=PROVENANCE_FIELDS)
        writer.writeheader()
        writer.writerows(asdict(item) for item in records)


def write_issues(path: Path, issues: Iterable[BuildIssue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ISSUE_FIELDS)
        writer.writeheader()
        writer.writerows(asdict(item) for item in issues)


def write_top30_mapping_bundle(
    results: Sequence[MappingResult],
    mapping_path: Path = TOP30_MAPPING_OUTPUT,
    review_path: Path = TOP30_REVIEW_OUTPUT,
    summary_path: Path = TOP30_SUMMARY_OUTPUT,
) -> None:
    """Prepared output contract; callers must supply a completed real master."""
    write_mapping_results(mapping_path, results)
    review = [item for item in results if item.mapping_status in {"REVIEW_REQUIRED", "UNMAPPED"}]
    write_mapping_results(review_path, review)
    counts: dict[str, int] = {}
    for item in results:
        counts[item.mapping_status] = counts.get(item.mapping_status, 0) + 1
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total_rows": len(results), "status_counts": dict(sorted(counts.items())),
        "review_rows": len(review),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require_explicit_network_access(explicit_network: bool, environ: Mapping[str, str] = os.environ) -> None:
    if not explicit_network:
        raise ValidationError("network access requires an explicit --network flag")
    if not environ.get("KRX_AUTH_KEY", "").strip():
        raise ValidationError("KRX_AUTH_KEY is required for network access")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build historical master from adapted KRX rows")
    parser.add_argument("--states", type=Path, required=True,
                        help="Adapted KRX state CSV; this command performs no download")
    parser.add_argument("--output", type=Path, default=DEFAULT_MASTER_OUTPUT)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--provenance-output", type=Path, default=DEFAULT_PROVENANCE_OUTPUT)
    parser.add_argument("--issues", type=Path,
                        default=Path("data/processed/market/historical_stock_master_quality.csv"))
    parser.add_argument("--network", action="store_true",
                        help="Reserved gate for a future explicit network adapter")
    args = parser.parse_args()
    if args.network:
        require_explicit_network_access(True)
        raise ValidationError("network adapter is not implemented in this MVP")
    states = load_source_states(args.states)
    provenance = load_provenance(args.provenance)
    validate_provenance(states, provenance)
    result = build_historical_master(states)
    write_issues(args.issues, result.issues)
    write_master(args.output, result)
    write_provenance(args.provenance_output, provenance)


if __name__ == "__main__":
    main()
