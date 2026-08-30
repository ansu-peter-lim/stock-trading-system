"""Historical stock master validation and temporal TOP30 mapping MVP."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .normalization import normalize_stock_name


MASTER_FIELDS = (
    "stock_code", "market", "stock_name", "stock_name_normalized",
    "valid_from", "valid_to", "listing_date", "delisting_date",
    "security_type", "security_type_raw", "source", "source_as_of",
)
MAPPING_FIELDS = (
    "report_date", "observed_stock_name", "stock_name_normalized",
    "stock_code", "market", "mapping_status", "mapping_method",
    "confidence", "source", "review_note",
)
OVERRIDE_FIELDS = (
    "report_date", "observed_stock_name", "stock_code", "action", "reason", "note",
)
MAPPING_STATUSES = {
    "AUTO_EXACT_TEMPORAL", "AUTO_NORMALIZED_TEMPORAL", "REVIEW_REQUIRED",
    "UNMAPPED", "MANUAL_CONFIRMED",
}
OVERRIDE_ACTIONS = {"confirm_mapping"}
REVIEW_SECURITY_MARKERS = ("SPAC", "스팩", "합병", "MERGER")
SECURITY_TYPES = {"COMMON", "PREFERRED", "SPAC", "REIT", "OTHER"}


class ValidationError(ValueError):
    """One or more input records violate the mapping contract."""


def parse_iso_date(value: str, field: str, *, required: bool = True) -> date | None:
    text = value.strip()
    if not text:
        if required:
            raise ValidationError(f"{field} is required")
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError(f"{field} must use YYYY-MM-DD") from exc


def validate_stock_code(value: str) -> str:
    code = value.strip()
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        raise ValidationError("stock_code must be exactly six ASCII digits")
    return code


@dataclass(frozen=True)
class HistoricalStock:
    stock_code: str
    market: str
    stock_name: str
    stock_name_normalized: str
    valid_from: date
    valid_to: date | None
    listing_date: date | None
    delisting_date: date | None
    security_type: str
    security_type_raw: str
    source: str
    source_as_of: date

    def active_on(self, observed_date: date) -> bool:
        """Name bounds are inclusive; delisting_date is an exclusive bound."""
        return (
            self.valid_from <= observed_date
            and (self.valid_to is None or observed_date <= self.valid_to)
            and (self.listing_date is None or self.listing_date <= observed_date)
            and (self.delisting_date is None or observed_date < self.delisting_date)
        )

    @property
    def requires_review(self) -> bool:
        upper = self.security_type.upper()
        return any(marker in upper for marker in REVIEW_SECURITY_MARKERS)


@dataclass(frozen=True)
class Observation:
    report_date: date
    observed_stock_name: str

    @property
    def normalized_name(self) -> str:
        return normalize_stock_name(self.observed_stock_name)


@dataclass(frozen=True)
class ManualOverride:
    report_date: date
    observed_stock_name: str
    stock_code: str
    action: str
    reason: str
    note: str

    @property
    def target(self) -> tuple[date, str]:
        return self.report_date, normalize_stock_name(self.observed_stock_name)


@dataclass(frozen=True)
class MappingResult:
    report_date: str
    observed_stock_name: str
    stock_name_normalized: str
    stock_code: str
    market: str
    mapping_status: str
    mapping_method: str
    confidence: str
    source: str
    review_note: str


def historical_stock_from_row(row: Mapping[str, str], row_number: int = 0) -> HistoricalStock:
    prefix = f"row {row_number}: " if row_number else ""
    missing = [field for field in MASTER_FIELDS if field not in row]
    if missing:
        raise ValidationError(prefix + "missing master field(s): " + ", ".join(missing))
    try:
        stock_name = row["stock_name"]
        normalized = normalize_stock_name(stock_name)
        supplied_normalized = row["stock_name_normalized"].strip()
        if supplied_normalized != normalized:
            raise ValidationError("stock_name_normalized does not match normalization rule")
        valid_from = parse_iso_date(row["valid_from"], "valid_from")
        valid_to = parse_iso_date(row["valid_to"], "valid_to", required=False)
        listing = parse_iso_date(row["listing_date"], "listing_date", required=False)
        delisting = parse_iso_date(row["delisting_date"], "delisting_date", required=False)
        source_as_of = parse_iso_date(row["source_as_of"], "source_as_of")
        if valid_to is not None and valid_from > valid_to:
            raise ValidationError("valid_from must not be after valid_to")
        if listing is not None and delisting is not None and listing >= delisting:
            raise ValidationError("listing_date must be before delisting_date")
        if listing is not None and valid_from < listing:
            raise ValidationError("valid_from must not be before listing_date")
        if delisting is not None and valid_from >= delisting:
            raise ValidationError("valid_from must be before exclusive delisting_date")
        if delisting is not None and valid_to is not None and valid_to >= delisting:
            raise ValidationError("valid_to must be before exclusive delisting_date")
        security_type = row["security_type"].strip()
        if security_type not in SECURITY_TYPES:
            raise ValidationError("security_type is not a supported standard value")
        return HistoricalStock(
            validate_stock_code(row["stock_code"]), row["market"].strip(), stock_name,
            supplied_normalized, valid_from, valid_to, listing, delisting,
            security_type, row["security_type_raw"], row["source"].strip(), source_as_of,
        )
    except ValidationError as exc:
        raise ValidationError(prefix + str(exc)) from exc


def load_historical_master(path: Path) -> list[HistoricalStock]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    records = [historical_stock_from_row(row, index) for index, row in enumerate(rows, 2)]
    validate_master_intervals(records)
    return records


def validate_master_intervals(records: Sequence[HistoricalStock]) -> None:
    """Reject overlapping name intervals for one code/market."""
    grouped: dict[tuple[str, str], list[HistoricalStock]] = {}
    for record in records:
        grouped.setdefault((record.stock_code, record.market), []).append(record)
    for key, items in grouped.items():
        ordered = sorted(items, key=lambda item: item.valid_from)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.valid_to is None or current.valid_from <= previous.valid_to:
                raise ValidationError(f"overlapping name intervals for {key[0]} {key[1]}")


def load_observations(path: Path) -> list[Observation]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {"report_date", "stock_name"}.issubset(reader.fieldnames):
            raise ValidationError("TOP30 input requires report_date and stock_name")
        return [
            Observation(parse_iso_date(row["report_date"], "report_date"), row["stock_name"])
            for row in reader
        ]


def load_overrides(
    path: Path,
    observations: Sequence[Observation],
    master: Sequence[HistoricalStock],
) -> list[ManualOverride]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != OVERRIDE_FIELDS:
            raise ValidationError("manual override header does not match required schema")
        raw_rows = list(reader)
    valid_dates = {item.report_date for item in observations}
    master_codes = {item.stock_code for item in master}
    overrides: list[ManualOverride] = []
    targets: set[tuple[date, str]] = set()
    for index, row in enumerate(raw_rows, 2):
        try:
            report_date = parse_iso_date(row["report_date"], "report_date")
            if report_date not in valid_dates:
                raise ValidationError("report_date does not exist in TOP30 observations")
            action = row["action"].strip()
            if action not in OVERRIDE_ACTIONS:
                raise ValidationError("unsupported override action")
            if not row["reason"].strip():
                raise ValidationError("reason is required")
            override = ManualOverride(
                report_date, row["observed_stock_name"], validate_stock_code(row["stock_code"]),
                action, row["reason"].strip(), row["note"].strip(),
            )
            if override.stock_code not in master_codes:
                raise ValidationError("stock_code does not exist in historical master")
            if override.target in targets:
                raise ValidationError("duplicate or conflicting override target")
            if not any(
                item.report_date == report_date and item.normalized_name == override.target[1]
                for item in observations
            ):
                raise ValidationError("observed_stock_name does not exist on report_date")
            targets.add(override.target)
            overrides.append(override)
        except ValidationError as exc:
            raise ValidationError(f"override row {index}: {exc}") from exc
    return overrides


def _auto_map(observation: Observation, master: Sequence[HistoricalStock]) -> MappingResult:
    candidates = [
        record for record in master
        if record.stock_name_normalized == observation.normalized_name
        and record.active_on(observation.report_date)
    ]
    identities = {(item.stock_code, item.market) for item in candidates}
    base = {
        "report_date": observation.report_date.isoformat(),
        "observed_stock_name": observation.observed_stock_name,
        "stock_name_normalized": observation.normalized_name,
    }
    if not candidates:
        return MappingResult(**base, stock_code="", market="", mapping_status="UNMAPPED",
                             mapping_method="none", confidence="0.00", source="",
                             review_note="No temporally valid normalized-name candidate")
    if len(identities) > 1:
        codes = ";".join(sorted(code for code, _ in identities))
        return MappingResult(**base, stock_code="", market="", mapping_status="REVIEW_REQUIRED",
                             mapping_method="ambiguous_normalized_temporal", confidence="0.00",
                             source=";".join(sorted({item.source for item in candidates})),
                             review_note=f"Multiple candidate stock codes: {codes}")
    candidate = candidates[0]
    if any(item.requires_review for item in candidates):
        return MappingResult(**base, stock_code="", market=candidate.market,
                             mapping_status="REVIEW_REQUIRED", mapping_method="security_type_guard",
                             confidence="0.00", source=candidate.source,
                             review_note="SPAC/merger security type requires manual review")
    exact = any(item.stock_name == observation.observed_stock_name for item in candidates)
    return MappingResult(
        **base,
        stock_code=candidate.stock_code,
        market=candidate.market,
        mapping_status="AUTO_EXACT_TEMPORAL" if exact else "AUTO_NORMALIZED_TEMPORAL",
        mapping_method="exact_name+temporal_interval" if exact else "nfkc_name+temporal_interval",
        confidence="1.00" if exact else "0.95",
        source=";".join(sorted({item.source for item in candidates})),
        review_note="",
    )


def map_observations(
    observations: Sequence[Observation],
    master: Sequence[HistoricalStock],
    overrides: Sequence[ManualOverride] = (),
) -> list[MappingResult]:
    override_index = {item.target: item for item in overrides}
    results: list[MappingResult] = []
    markets_by_code: dict[str, set[str]] = {}
    for record in master:
        markets_by_code.setdefault(record.stock_code, set()).add(record.market)
    for observation in observations:
        override = override_index.get((observation.report_date, observation.normalized_name))
        if override is None:
            results.append(_auto_map(observation, master))
            continue
        markets = markets_by_code.get(override.stock_code, set())
        results.append(MappingResult(
            report_date=observation.report_date.isoformat(),
            observed_stock_name=observation.observed_stock_name,
            stock_name_normalized=observation.normalized_name,
            stock_code=override.stock_code,
            market=next(iter(markets)) if len(markets) == 1 else "",
            mapping_status="MANUAL_CONFIRMED",
            mapping_method="manual_override",
            confidence="1.00",
            source="manual_override",
            review_note=f"{override.reason}" + (f" | {override.note}" if override.note else ""),
        ))
    return results


def write_mapping_results(path: Path, results: Iterable[MappingResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=MAPPING_FIELDS)
        writer.writeheader()
        writer.writerows(asdict(item) for item in results)


def main() -> None:
    parser = argparse.ArgumentParser(description="Map TOP30 names through a historical stock master")
    parser.add_argument("--top30", type=Path,
                        default=Path("data/processed/telegram/top30_analysis_ready.csv"))
    parser.add_argument("--master", type=Path, required=True)
    parser.add_argument("--overrides", type=Path,
                        default=Path("data/manual/stock_mapping_overrides.csv"))
    parser.add_argument("--output", type=Path,
                        default=Path("data/processed/stock_mapping/top30_stock_mapping.csv"))
    args = parser.parse_args()
    observations = load_observations(args.top30)
    master = load_historical_master(args.master)
    overrides = load_overrides(args.overrides, observations, master)
    write_mapping_results(args.output, map_observations(observations, master, overrides))


if __name__ == "__main__":
    main()
