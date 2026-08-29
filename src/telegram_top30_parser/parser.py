from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

REPORT_TYPES = ("daily", "weekly", "monthly", "quarterly", "intraday_summary", "link_only", "unrelated", "unknown")
FILENAME_RE = re.compile(r"^(?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2})_(?P<time>[0-9]{2}-[0-9]{2}-[0-9]{2})_(?P<id>[0-9]+)\.txt$")
DAILY_RE = re.compile(r"(?P<y>[0-9]{4})\s*년\s*(?P<m>[0-9]{1,2})\s*월\s*(?P<d>[0-9]{1,2})\s*일\s*상승률\s*TOP\s*30", re.I)
WEEKLY_RE = re.compile(r"주간\s*상승률\s*TOP\s*30", re.I)
MONTHLY_RE = re.compile(r"월간\s*상승률\s*TOP\s*30", re.I)
QUARTERLY_RE = re.compile(r"(?:[1-4]\s*분기|분기별)\s*상승률\s*TOP\s*30", re.I)
INTRADAY_RE = re.compile(r"[0-9]{1,2}\s*시(?:\s*[0-9]{1,2}\s*분)?\s*상승률\s*TOP\s*30\s*동향")
TOP30_RE = re.compile(r"상승률\s*TOP\s*30", re.I)
URL_RE = re.compile(r"https?://\S+", re.I)
HEADER_RE = re.compile(
    r"(?m)^\s*(?P<rank>[0-9０-９]{1,2})\s*[.]\s*(?P<stock>[^\r\n]+?)\s*"
    r"(?:\((?P<paren>[^)\r\n]*)\)|(?P<no_open>[+-]?[0-9０-９]+(?:[.][0-9０-９]+)?%?)\))"
    r"[ \t]*[:：][ \t]*"
)
STRICT_RE = re.compile(r"^\s*[0-9]{1,2}\s*[.]\s*.+?\s*\(\s*[+-]?[0-9]+(?:[.][0-9]+)?\s*%\s*\)[ \t]*:[ \t]*$")
RATE_RE = re.compile(r"^[+-]?[0-9]+(?:[.][0-9]+)?$")
ROW_FIELDS = ("report_date", "telegram_posted_at", "telegram_message_id", "rank", "stock_name", "return_pct", "detail_raw", "source_file")
QUALITY_FIELDS = ("source_file", "report_type", "report_date", "telegram_posted_at", "telegram_message_id", "file_size_bytes", "content_sha256", "parse_status", "severity", "issue_code", "rank", "message")

@dataclass
class ParsedRow:
    report_date: str
    telegram_posted_at: str
    telegram_message_id: int
    rank: int
    stock_name: str
    return_pct: Decimal | None
    detail_raw: str
    source_file: str

@dataclass
class QualityIssue:
    source_file: str
    report_type: str
    issue_code: str
    message: str
    severity: str = "error"
    rank: int | None = None
    report_date: str | None = None
    telegram_posted_at: str | None = None
    telegram_message_id: int | None = None
    file_size_bytes: int | None = None
    content_sha256: str | None = None
    parse_status: str | None = None

@dataclass
class ReportResult:
    source_file: str
    report_type: str
    file_size_bytes: int
    content_sha256: str
    telegram_posted_at: str | None = None
    telegram_message_id: int | None = None
    report_date: str | None = None
    parse_status: str = "classified_only"
    recovery_used: bool = False
    rows: list[ParsedRow] = field(default_factory=list)
    issues: list[QualityIssue] = field(default_factory=list)

@dataclass
class DatasetResult:
    reports: list[ReportResult]
    rows: list[ParsedRow]
    issues: list[QualityIssue]

def _token(value: str) -> str:
    return unicodedata.normalize("NFKC", value)

def parse_filename(path: Path) -> tuple[str, int]:
    match = FILENAME_RE.fullmatch(path.name)
    if not match:
        raise ValueError("filename does not match YYYY-MM-DD_HH-MM-SS_messageid.txt")
    posted = datetime.strptime(f"{match['day']}_{match['time']}", "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
    return posted.isoformat(timespec="seconds").replace("+00:00", "Z"), int(match["id"])

def classify_report(text: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    # Period types have priority over the embedded calendar date.
    for pattern, kind in ((WEEKLY_RE, "weekly"), (MONTHLY_RE, "monthly"), (QUARTERLY_RE, "quarterly")):
        if pattern.search(text):
            return "link_only" if URL_RE.search(text) and not HEADER_RE.search(text) else kind
    if INTRADAY_RE.search(text):
        return "intraday_summary"
    if DAILY_RE.search(text):
        return "daily"
    if "장마감 시황" in first_line or first_line.startswith("백억대학"):
        return "unrelated"
    if TOP30_RE.search(text) and URL_RE.search(text) and not HEADER_RE.search(text):
        return "link_only"
    if TOP30_RE.search(text):
        return "unknown"
    return "unrelated"

def extract_report_date(text: str) -> str:
    matches = list(DAILY_RE.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"expected one daily report date, found {len(matches)}")
    match = matches[0]
    return date(int(match["y"]), int(match["m"]), int(match["d"])).isoformat()

def add_issue(result: ReportResult, code: str, message: str, rank: int | None = None, severity: str = "error") -> None:
    result.issues.append(QualityIssue(result.source_file, result.report_type, code, message, severity, rank, result.report_date, result.telegram_posted_at, result.telegram_message_id, result.file_size_bytes, result.content_sha256))

def parse_rate(raw: str) -> Decimal | None:
    value = _token(raw).strip()
    if value.endswith("%"):
        value = value[:-1].strip()
    if not RATE_RE.fullmatch(value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None

def parse_daily_rows(text: str, result: ReportResult) -> list[ParsedRow]:
    matches = list(HEADER_RE.finditer(text))
    rows: list[ParsedRow] = []
    for index, match in enumerate(matches):
        rank = int(_token(match["rank"]))
        stock = match["stock"].strip()
        rate_raw = match["paren"] if match["paren"] is not None else (match["no_open"] or "")
        header = text[match.start():match.end()]
        if not STRICT_RE.fullmatch(header):
            result.recovery_used = True
            add_issue(result, "HEADER_RECOVERY_USED", "non-strict header parsed by recovery rules", rank, "warning")
        rate = parse_rate(rate_raw)
        if rate is None:
            add_issue(result, "RETURN_PCT_MISSING_OR_INVALID", f"could not parse return percentage from {rate_raw!r}", rank)
        if not stock:
            add_issue(result, "STOCK_NAME_PARSE_FAILED", "stock name is empty", rank)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        detail = text[match.end():end].rstrip("\r\n")
        if not detail.strip():
            add_issue(result, "EMPTY_DETAIL_RAW", "detail_raw is empty", rank)
        rows.append(ParsedRow(result.report_date or "", result.telegram_posted_at or "", result.telegram_message_id or 0, rank, stock, rate, detail, result.source_file))
    return rows

def validate_daily(result: ReportResult) -> None:
    counts = Counter(row.rank for row in result.rows)
    missing = [rank for rank in range(1, 31) if rank not in counts]
    duplicates = sorted(rank for rank, count in counts.items() if count > 1)
    outside = sorted(rank for rank in counts if rank < 1 or rank > 30)
    if len(result.rows) != 30:
        add_issue(result, "RANK_COUNT_NOT_30", f"parsed {len(result.rows)} rank blocks")
    if missing:
        add_issue(result, "MISSING_RANK", "missing ranks: " + ",".join(map(str, missing)))
    if duplicates:
        add_issue(result, "DUPLICATE_RANK", "duplicate ranks: " + ",".join(map(str, duplicates)))
    if outside:
        add_issue(result, "OUT_OF_RANGE_RANK", "out-of-range ranks: " + ",".join(map(str, outside)))

def finalize(result: ReportResult) -> None:
    for issue in result.issues:
        issue.report_date = result.report_date
        issue.telegram_posted_at = result.telegram_posted_at
        issue.telegram_message_id = result.telegram_message_id
        issue.parse_status = result.parse_status

def parse_report(path: Path) -> ReportResult:
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        result = ReportResult(path.name, "unknown", len(content), digest, parse_status="invalid")
        add_issue(result, "INVALID_UTF8", str(exc)); finalize(result); return result
    result = ReportResult(path.name, classify_report(text), len(content), digest)
    try:
        result.telegram_posted_at, result.telegram_message_id = parse_filename(path)
    except ValueError as exc:
        add_issue(result, "INVALID_FILENAME", str(exc))
    if len(content) < 500:
        add_issue(result, "SUSPICIOUSLY_SMALL_FILE", f"file is only {len(content)} bytes", severity="warning")
    if result.report_type != "daily":
        add_issue(result, "NON_DAILY_REPORT", f"classified as {result.report_type}; excluded from daily rows", severity="info")
        finalize(result); return result
    try:
        result.report_date = extract_report_date(text)
    except (ValueError, OverflowError) as exc:
        add_issue(result, "REPORT_DATE_PARSE_FAILED", str(exc))
    if result.report_date and result.telegram_posted_at and result.telegram_message_id is not None:
        result.rows = parse_daily_rows(text, result)
        validate_daily(result)
    if any(issue.severity == "error" for issue in result.issues):
        result.parse_status = "invalid"
    elif result.recovery_used:
        result.parse_status = "recovered"
    else:
        result.parse_status = "valid"
    finalize(result)
    return result

def detect_duplicate_dates(reports: Iterable[ReportResult]) -> None:
    groups: dict[str, list[ReportResult]] = defaultdict(list)
    for report in reports:
        if report.report_type == "daily" and report.report_date:
            groups[report.report_date].append(report)
    for report_date, group in groups.items():
        if len(group) < 2:
            continue
        code = "EXACT_DUPLICATE_REPORT" if len({r.content_sha256 for r in group}) == 1 else "CONFLICTING_REPORT_VERSION"
        names = ", ".join(r.source_file for r in group)
        for report in group:
            report.parse_status = "invalid"
            add_issue(report, code, f"report_date {report_date} appears in: {names}")
            finalize(report)

def build_dataset(input_dir: Path) -> DatasetResult:
    reports = [parse_report(path) for path in sorted(input_dir.glob("*.txt"))]
    detect_duplicate_dates(reports)
    return DatasetResult(reports, [row for r in reports if r.report_type == "daily" for row in r.rows], [issue for r in reports for issue in r.issues])

def write_outputs(dataset: DatasetResult, output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "top30_daily_rows.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ROW_FIELDS); writer.writeheader()
        for row in dataset.rows:
            data = asdict(row); data["return_pct"] = "" if row.return_pct is None else str(row.return_pct); writer.writerow(data)
    with (output_dir / "top30_quality_report.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=QUALITY_FIELDS); writer.writeheader()
        for issue in dataset.issues:
            writer.writerow({name: getattr(issue, name) for name in QUALITY_FIELDS})
    types = Counter(r.report_type for r in dataset.reports); daily = [r for r in dataset.reports if r.report_type == "daily"]; issues = Counter(i.issue_code for i in dataset.issues)
    summary = {
        "source_file_count": len(dataset.reports),
        "report_type_counts": {name: types.get(name, 0) for name in REPORT_TYPES},
        "daily_report_count": len(daily),
        "valid_report_count": sum(r.parse_status == "valid" for r in daily),
        "recovered_report_count": sum(r.parse_status == "recovered" for r in daily),
        "recovery_used_report_count": sum(r.recovery_used for r in daily),
        "invalid_report_count": sum(r.parse_status == "invalid" for r in daily),
        "generated_row_count": len(dataset.rows),
        "return_pct_none_count": sum(row.return_pct is None for row in dataset.rows),
        "exact_duplicate_report_count": issues.get("EXACT_DUPLICATE_REPORT", 0),
        "conflicting_report_version_count": issues.get("CONFLICTING_REPORT_VERSION", 0),
        "issue_code_counts": dict(sorted(issues.items())),
    }
    (output_dir / "top30_parse_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse Telegram daily TOP30 reports")
    parser.add_argument("--input-dir", type=Path, default=Path("data/raw/telegram/daily"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/telegram"))
    args = parser.parse_args(argv)
    print(json.dumps(write_outputs(build_dataset(args.input_dir), args.output_dir), ensure_ascii=False, indent=2))
    return 0
