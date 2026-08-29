from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .parser import DatasetResult, ParsedRow, ReportResult, build_dataset


MANIFEST_FIELDS = (
    "report_date",
    "telegram_posted_at",
    "telegram_message_id",
    "source_file",
    "report_type",
    "parse_status",
    "parsed_rank_count",
    "missing_rank_count",
    "return_pct_missing_count",
    "recovery_used",
    "content_sha256",
    "analysis_grade",
    "duplicate_status",
)

CONFLICT_FIELDS = (
    "report_date",
    "telegram_posted_at",
    "telegram_message_id",
    "source_file",
    "analysis_grade",
    "parsed_rank_count",
    "content_sha256",
    "compared_source_file",
    "stock_name_different_rank_count",
    "stock_name_different_ranks",
    "return_pct_different_rank_count",
    "return_pct_different_ranks",
    "rank_only_in_this_version_count",
    "ranks_only_in_this_version",
    "rank_only_in_other_version_count",
    "ranks_only_in_other_version",
    "detail_raw_different_rank_count",
    "detail_raw_different_ranks",
)

EXACT_FIELDS = (
    "report_date",
    "telegram_posted_at",
    "telegram_message_id",
    "source_file",
    "analysis_grade",
    "parsed_rank_count",
    "content_sha256",
    "duplicate_group_size",
)


@dataclass(frozen=True)
class PairDifference:
    stock_name_different_ranks: tuple[int, ...]
    return_pct_different_ranks: tuple[int, ...]
    ranks_only_left: tuple[int, ...]
    ranks_only_right: tuple[int, ...]
    detail_raw_different_ranks: tuple[int, ...]


def _rank_map(report: ReportResult) -> dict[int, ParsedRow]:
    result: dict[int, ParsedRow] = {}
    for row in report.rows:
        result.setdefault(row.rank, row)
    return result


def report_analysis_grade(report: ReportResult) -> str:
    if report.report_type != "daily":
        return "N"
    ranks = [row.rank for row in report.rows]
    complete_ranks = len(ranks) == 30 and len(set(ranks)) == 30 and set(ranks) == set(range(1, 31))
    complete_values = all(
        row.stock_name.strip() and row.return_pct is not None and row.detail_raw.strip()
        for row in report.rows
    )
    if not complete_ranks or not complete_values:
        return "C"
    return "B" if report.recovery_used else "A"


def duplicate_statuses(dataset: DatasetResult) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for issue in dataset.issues:
        if issue.issue_code in {"EXACT_DUPLICATE_REPORT", "CONFLICTING_REPORT_VERSION"}:
            statuses[issue.source_file] = issue.issue_code
    return statuses


def manifest_rows(dataset: DatasetResult) -> list[dict[str, object]]:
    statuses = duplicate_statuses(dataset)
    output: list[dict[str, object]] = []
    for report in dataset.reports:
        ranks = {row.rank for row in report.rows if 1 <= row.rank <= 30}
        output.append(
            {
                "report_date": report.report_date or "",
                "telegram_posted_at": report.telegram_posted_at or "",
                "telegram_message_id": report.telegram_message_id if report.telegram_message_id is not None else "",
                "source_file": report.source_file,
                "report_type": report.report_type,
                "parse_status": report.parse_status,
                "parsed_rank_count": len(report.rows),
                "missing_rank_count": 30 - len(ranks) if report.report_type == "daily" else "",
                "return_pct_missing_count": sum(row.return_pct is None for row in report.rows),
                "recovery_used": str(report.recovery_used).lower(),
                "content_sha256": report.content_sha256,
                "analysis_grade": report_analysis_grade(report),
                "duplicate_status": statuses.get(report.source_file, ""),
            }
        )
    return output


def compare_reports(left: ReportResult, right: ReportResult) -> PairDifference:
    left_rows, right_rows = _rank_map(left), _rank_map(right)
    common = sorted(left_rows.keys() & right_rows.keys())
    stock_diff = tuple(rank for rank in common if left_rows[rank].stock_name != right_rows[rank].stock_name)
    return_diff = tuple(
        rank
        for rank in common
        if left_rows[rank].stock_name == right_rows[rank].stock_name
        and left_rows[rank].return_pct != right_rows[rank].return_pct
    )
    detail_diff = tuple(rank for rank in common if left_rows[rank].detail_raw != right_rows[rank].detail_raw)
    return PairDifference(
        stock_diff,
        return_diff,
        tuple(sorted(left_rows.keys() - right_rows.keys())),
        tuple(sorted(right_rows.keys() - left_rows.keys())),
        detail_diff,
    )


def _rank_text(ranks: tuple[int, ...]) -> str:
    return ",".join(map(str, ranks))


def conflicting_rows(dataset: DatasetResult) -> list[dict[str, object]]:
    conflicting_files = {
        issue.source_file
        for issue in dataset.issues
        if issue.issue_code == "CONFLICTING_REPORT_VERSION"
    }
    groups: dict[str, list[ReportResult]] = defaultdict(list)
    for report in dataset.reports:
        if report.source_file in conflicting_files and report.report_date:
            groups[report.report_date].append(report)

    output: list[dict[str, object]] = []
    for report_date in sorted(groups):
        group = sorted(groups[report_date], key=lambda report: report.source_file)
        for index, report in enumerate(group):
            other = group[1 - index] if len(group) == 2 else next(item for item in group if item is not report)
            diff = compare_reports(report, other)
            output.append(
                {
                    "report_date": report_date,
                    "telegram_posted_at": report.telegram_posted_at or "",
                    "telegram_message_id": report.telegram_message_id if report.telegram_message_id is not None else "",
                    "source_file": report.source_file,
                    "analysis_grade": report_analysis_grade(report),
                    "parsed_rank_count": len(report.rows),
                    "content_sha256": report.content_sha256,
                    "compared_source_file": other.source_file,
                    "stock_name_different_rank_count": len(diff.stock_name_different_ranks),
                    "stock_name_different_ranks": _rank_text(diff.stock_name_different_ranks),
                    "return_pct_different_rank_count": len(diff.return_pct_different_ranks),
                    "return_pct_different_ranks": _rank_text(diff.return_pct_different_ranks),
                    "rank_only_in_this_version_count": len(diff.ranks_only_left),
                    "ranks_only_in_this_version": _rank_text(diff.ranks_only_left),
                    "rank_only_in_other_version_count": len(diff.ranks_only_right),
                    "ranks_only_in_other_version": _rank_text(diff.ranks_only_right),
                    "detail_raw_different_rank_count": len(diff.detail_raw_different_ranks),
                    "detail_raw_different_ranks": _rank_text(diff.detail_raw_different_ranks),
                }
            )
    return output


def exact_duplicate_rows(dataset: DatasetResult) -> list[dict[str, object]]:
    exact_files = {
        issue.source_file for issue in dataset.issues if issue.issue_code == "EXACT_DUPLICATE_REPORT"
    }
    group_sizes = Counter(
        report.report_date
        for report in dataset.reports
        if report.source_file in exact_files and report.report_date
    )
    return [
        {
            "report_date": report.report_date or "",
            "telegram_posted_at": report.telegram_posted_at or "",
            "telegram_message_id": report.telegram_message_id if report.telegram_message_id is not None else "",
            "source_file": report.source_file,
            "analysis_grade": report_analysis_grade(report),
            "parsed_rank_count": len(report.rows),
            "content_sha256": report.content_sha256,
            "duplicate_group_size": group_sizes[report.report_date],
        }
        for report in dataset.reports
        if report.source_file in exact_files
    ]


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_investigation_outputs(dataset: DatasetResult, output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest_rows(dataset)
    conflicts = conflicting_rows(dataset)
    exact = exact_duplicate_rows(dataset)
    _write_csv(output_dir / "top30_report_manifest.csv", MANIFEST_FIELDS, manifest)
    _write_csv(output_dir / "top30_conflicting_reports.csv", CONFLICT_FIELDS, conflicts)
    _write_csv(output_dir / "top30_exact_duplicate_reports.csv", EXACT_FIELDS, exact)

    grades = Counter(row["analysis_grade"] for row in manifest)
    eligible_files = {
        row["source_file"] for row in manifest if row["analysis_grade"] in {"A", "B"}
    }
    eligible_reports = [
        report for report in dataset.reports if report.source_file in eligible_files
    ]
    eligible_dates = Counter(report.report_date for report in eligible_reports if report.report_date)
    return {
        "analysis_grade_counts": {grade: grades.get(grade, 0) for grade in "ABCN"},
        "analysis_ready_report_count": len(eligible_reports),
        "analysis_ready_row_count": sum(len(report.rows) for report in eligible_reports),
        "analysis_ready_duplicate_report_date_count": sum(count > 1 for count in eligible_dates.values()),
        "conflicting_report_date_count": len({row["report_date"] for row in conflicts}),
        "conflicting_file_count": len(conflicts),
        "exact_duplicate_file_count": len(exact),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build TOP30 report investigation outputs")
    parser.add_argument("--input-dir", type=Path, default=Path("data/raw/telegram/daily"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/telegram"))
    args = parser.parse_args(argv)
    summary = write_investigation_outputs(build_dataset(args.input_dir), args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
