from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .parser import DatasetResult, ParsedRow, ReportResult, build_dataset


ACTIONS = {
    "exclude_report",
    "use_report",
    "correct_report_date",
    "correct_stock_name",
    "correct_return_pct",
    "correct_stock",
}
CORRECTION_FIELDS = (
    "telegram_message_id",
    "rank",
    "action",
    "corrected_report_date",
    "corrected_stock_name",
    "corrected_return_pct",
    "reason",
    "note",
)
ANALYSIS_FIELDS = (
    "original_report_date",
    "report_date",
    "telegram_posted_at",
    "telegram_message_id",
    "rank",
    "original_stock_name",
    "stock_name",
    "original_return_pct",
    "return_pct",
    "manual_correction_applied",
    "detail_raw",
    "source_file",
)
LOG_FIELDS = (
    "telegram_message_id",
    "rank",
    "action",
    "old_value",
    "new_value",
    "reason",
    "status",
)


@dataclass(frozen=True)
class Correction:
    line_number: int
    telegram_message_id: int | None
    rank: int | None
    action: str
    corrected_report_date: str
    corrected_stock_name: str
    corrected_return_pct: str
    reason: str
    note: str
    raw_message_id: str = ""
    raw_rank: str = ""


@dataclass
class CorrectionLog:
    telegram_message_id: int | str | None
    rank: int | str | None
    action: str
    old_value: str
    new_value: str
    reason: str
    status: str


@dataclass
class AnalysisRow:
    original_report_date: str
    report_date: str
    telegram_posted_at: str
    telegram_message_id: int
    rank: int
    original_stock_name: str
    stock_name: str
    original_return_pct: Decimal | None
    return_pct: Decimal | None
    manual_correction_applied: bool
    detail_raw: str
    source_file: str


@dataclass
class CorrectionResult:
    rows: list[AnalysisRow]
    logs: list[CorrectionLog]


def ensure_correction_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerow(CORRECTION_FIELDS)


def load_corrections(path: Path) -> list[Correction]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return []
        missing = set(CORRECTION_FIELDS) - set(reader.fieldnames)
        if missing:
            raise ValueError("correction CSV missing fields: " + ",".join(sorted(missing)))
        output = []
        for line_number, row in enumerate(reader, start=2):
            if not any((row.get(name) or "").strip() for name in CORRECTION_FIELDS):
                continue
            raw_id = (row.get("telegram_message_id") or "").strip()
            raw_rank = (row.get("rank") or "").strip()
            try:
                message_id = int(raw_id)
            except ValueError:
                message_id = None
            try:
                rank = int(raw_rank) if raw_rank else None
            except ValueError:
                rank = None
            output.append(
                Correction(
                    line_number,
                    message_id,
                    rank,
                    (row.get("action") or "").strip(),
                    (row.get("corrected_report_date") or "").strip(),
                    (row.get("corrected_stock_name") or "").strip(),
                    (row.get("corrected_return_pct") or "").strip(),
                    (row.get("reason") or "").strip(),
                    (row.get("note") or "").strip(),
                    raw_id,
                    raw_rank,
                )
            )
    return output


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _valid_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _mutation_keys(correction: Correction) -> set[tuple[object, ...]]:
    mid, rank, action = correction.telegram_message_id, correction.rank, correction.action
    if action in {"exclude_report", "use_report"}:
        return {(mid, None, "selection")}
    if action == "correct_report_date":
        return {(mid, None, "report_date")}
    if action == "correct_stock_name":
        return {(mid, rank, "stock_name")}
    if action == "correct_return_pct":
        return {(mid, rank, "return_pct")}
    if action == "correct_stock":
        return {(mid, rank, "stock_name"), (mid, rank, "return_pct")}
    return set()


def validate_corrections(dataset: DatasetResult, corrections: list[Correction]) -> tuple[list[Correction], list[CorrectionLog]]:
    reports = {report.telegram_message_id: report for report in dataset.reports if report.telegram_message_id is not None}
    errors: dict[int, list[str]] = defaultdict(list)
    keys: dict[tuple[object, ...], list[int]] = defaultdict(list)
    report_actions = {"exclude_report", "use_report", "correct_report_date"}
    stock_actions = {"correct_stock_name", "correct_return_pct", "correct_stock"}

    for index, correction in enumerate(corrections):
        if correction.telegram_message_id is None:
            errors[index].append("telegram_message_id must be an integer")
        elif correction.telegram_message_id not in reports:
            errors[index].append("telegram_message_id does not exist")
        if correction.action not in ACTIONS:
            errors[index].append("invalid action")
        if not correction.reason:
            errors[index].append("reason is required")
        if correction.action in report_actions and correction.raw_rank:
            errors[index].append("rank must be empty for report-level action")
        if correction.action in stock_actions:
            if correction.rank is None or not 1 <= correction.rank <= 30:
                errors[index].append("rank must be between 1 and 30")
            elif correction.telegram_message_id in reports and correction.rank not in {row.rank for row in reports[correction.telegram_message_id].rows}:
                errors[index].append("rank does not exist in parsed report")
        if correction.action == "correct_report_date" and not _valid_date(correction.corrected_report_date):
            errors[index].append("corrected_report_date must be YYYY-MM-DD")
        if correction.action in {"correct_stock_name", "correct_stock"} and not correction.corrected_stock_name:
            errors[index].append("corrected_stock_name is required")
        if correction.action in {"correct_return_pct", "correct_stock"} and _decimal(correction.corrected_return_pct) is None:
            errors[index].append("corrected_return_pct must be numeric")
        for key in _mutation_keys(correction):
            keys[key].append(index)

    for indices in keys.values():
        if len(indices) > 1:
            for index in indices:
                errors[index].append("conflicting corrections for the same target")

    valid: list[Correction] = []
    logs: list[CorrectionLog] = []
    for index, correction in enumerate(corrections):
        if errors[index]:
            logs.append(
                CorrectionLog(
                    correction.telegram_message_id if correction.telegram_message_id is not None else correction.raw_message_id,
                    correction.rank if correction.rank is not None else correction.raw_rank,
                    correction.action,
                    "",
                    "",
                    correction.reason,
                    "VALIDATION_ERROR: " + "; ".join(dict.fromkeys(errors[index])),
                )
            )
        else:
            valid.append(correction)
    return valid, logs


def _complete(rows: list[AnalysisRow]) -> bool:
    ranks = [row.rank for row in rows]
    return (
        len(ranks) == 30
        and len(set(ranks)) == 30
        and set(ranks) == set(range(1, 31))
        and all(row.stock_name.strip() and row.return_pct is not None and row.detail_raw.strip() for row in rows)
    )


def apply_corrections(dataset: DatasetResult, corrections: list[Correction]) -> CorrectionResult:
    valid, logs = validate_corrections(dataset, corrections)
    by_report: dict[int, list[Correction]] = defaultdict(list)
    for correction in valid:
        assert correction.telegram_message_id is not None
        by_report[correction.telegram_message_id].append(correction)

    candidates: dict[int, list[AnalysisRow]] = {}
    excluded: set[int] = set()
    requested_use: set[int] = set()
    for report in dataset.reports:
        if report.report_type != "daily" or report.telegram_message_id is None or not report.report_date:
            continue
        mid = report.telegram_message_id
        analysis_rows = [
            AnalysisRow(report.report_date, report.report_date, row.telegram_posted_at, mid, row.rank, row.stock_name, row.stock_name, row.return_pct, row.return_pct, False, row.detail_raw, row.source_file)
            for row in report.rows
        ]
        rank_rows = {row.rank: row for row in analysis_rows}
        for correction in by_report.get(mid, []):
            if correction.action == "exclude_report":
                excluded.add(mid)
                logs.append(CorrectionLog(mid, "", correction.action, "included", "excluded", correction.reason, "APPLIED"))
            elif correction.action == "use_report":
                requested_use.add(mid)
                logs.append(CorrectionLog(mid, "", correction.action, "unselected", "preferred", correction.reason, "APPLIED"))
            elif correction.action == "correct_report_date":
                old = analysis_rows[0].report_date if analysis_rows else report.report_date
                for row in analysis_rows:
                    row.report_date = correction.corrected_report_date
                    row.manual_correction_applied = True
                logs.append(CorrectionLog(mid, "", correction.action, old, correction.corrected_report_date, correction.reason, "APPLIED"))
            else:
                assert correction.rank is not None
                row = rank_rows[correction.rank]
                if correction.action == "correct_stock":
                    old = json.dumps({"stock_name": row.stock_name, "return_pct": "" if row.return_pct is None else str(row.return_pct)}, ensure_ascii=False)
                    row.stock_name = correction.corrected_stock_name
                    row.return_pct = _decimal(correction.corrected_return_pct)
                    row.manual_correction_applied = True
                    new = json.dumps({"stock_name": row.stock_name, "return_pct": str(row.return_pct)}, ensure_ascii=False)
                    logs.append(CorrectionLog(mid, correction.rank, correction.action, old, new, correction.reason, "APPLIED"))
                elif correction.action == "correct_stock_name":
                    old = row.stock_name
                    row.stock_name = correction.corrected_stock_name
                    row.manual_correction_applied = True
                    logs.append(CorrectionLog(mid, correction.rank, correction.action, old, row.stock_name, correction.reason, "APPLIED"))
                elif correction.action == "correct_return_pct":
                    old = "" if row.return_pct is None else str(row.return_pct)
                    row.return_pct = _decimal(correction.corrected_return_pct)
                    row.manual_correction_applied = True
                    logs.append(CorrectionLog(mid, correction.rank, correction.action, old, str(row.return_pct), correction.reason, "APPLIED"))
        if mid not in excluded and _complete(analysis_rows):
            candidates[mid] = analysis_rows

    date_groups: dict[str, list[int]] = defaultdict(list)
    for mid, rows in candidates.items():
        date_groups[rows[0].report_date].append(mid)

    selected: set[int] = set()
    for report_date, ids in date_groups.items():
        if len(ids) == 1:
            selected.add(ids[0])
            continue
        preferred = [mid for mid in ids if mid in requested_use]
        if len(preferred) == 1:
            selected.add(preferred[0])
        elif len(preferred) > 1:
            for mid in preferred:
                logs.append(CorrectionLog(mid, "", "use_report", "preferred", "", "", f"VALIDATION_ERROR: multiple use_report corrections for {report_date}"))

    rows = [row for mid in sorted(selected) for row in candidates[mid]]
    rows.sort(key=lambda row: (row.report_date, row.rank, row.telegram_message_id))
    return CorrectionResult(rows, logs)


def _analysis_dict(row: AnalysisRow) -> dict[str, object]:
    return {
        "original_report_date": row.original_report_date,
        "report_date": row.report_date,
        "telegram_posted_at": row.telegram_posted_at,
        "telegram_message_id": row.telegram_message_id,
        "rank": row.rank,
        "original_stock_name": row.original_stock_name,
        "stock_name": row.stock_name,
        "original_return_pct": "" if row.original_return_pct is None else str(row.original_return_pct),
        "return_pct": "" if row.return_pct is None else str(row.return_pct),
        "manual_correction_applied": str(row.manual_correction_applied).lower(),
        "detail_raw": row.detail_raw,
        "source_file": row.source_file,
    }


def write_outputs(result: CorrectionResult, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "top30_analysis_ready.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANALYSIS_FIELDS)
        writer.writeheader()
        writer.writerows(_analysis_dict(row) for row in result.rows)
    with (output_dir / "top30_manual_correction_log.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(vars(log) for log in result.logs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply manual TOP30 corrections")
    parser.add_argument("--input-dir", type=Path, default=Path("data/raw/telegram/daily"))
    parser.add_argument("--corrections", type=Path, default=Path("data/manual/telegram/top30_corrections.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/telegram"))
    args = parser.parse_args(argv)
    ensure_correction_file(args.corrections)
    corrections = load_corrections(args.corrections)
    result = apply_corrections(build_dataset(args.input_dir), corrections)
    write_outputs(result, args.output_dir)
    print(json.dumps({"correction_count": len(corrections), "analysis_ready_row_count": len(result.rows), "log_count": len(result.logs)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
