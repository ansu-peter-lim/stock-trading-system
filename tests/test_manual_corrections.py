from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from src.telegram_top30_parser.manual_corrections import (
    CORRECTION_FIELDS,
    Correction,
    apply_corrections,
    ensure_correction_file,
    load_corrections,
)
from src.telegram_top30_parser.parser import build_dataset


def body(day: str = "2026년 8월 29일") -> str:
    lines = [f"{rank}. 종목{rank} ({rank}.10%) : 상세 {rank}" for rank in range(1, 31)]
    return f"{day} 상승률 TOP30\n\n" + "\n".join(lines)


def correction(action: str, *, message_id: int = 1, rank: int | None = None,
               report_date: str = "", stock_name: str = "", return_pct: str = "",
               reason: str = "수동 확인") -> Correction:
    return Correction(2, message_id, rank, action, report_date, stock_name, return_pct,
                      reason, "", str(message_id), "" if rank is None else str(rank))


class ManualCorrectionTests(unittest.TestCase):
    def dataset(self, directory: Path, *, two_dates: bool = False):
        (directory / "2026-08-29_01-00-00_1.txt").write_text(body(), encoding="utf-8")
        if two_dates:
            (directory / "2026-08-30_01-00-00_2.txt").write_text(
                body("2026년 8월 30일"), encoding="utf-8"
            )
        return build_dataset(directory)

    def test_missing_or_header_only_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "corrections.csv"
            self.assertEqual([], load_corrections(path))
            ensure_correction_file(path)
            self.assertEqual([], load_corrections(path))

    def test_exclude_report(self):
        with tempfile.TemporaryDirectory() as directory:
            result = apply_corrections(self.dataset(Path(directory)), [correction("exclude_report")])
            self.assertEqual([], result.rows)
            self.assertEqual("APPLIED", result.logs[0].status)

    def test_correct_report_date(self):
        with tempfile.TemporaryDirectory() as directory:
            result = apply_corrections(self.dataset(Path(directory)), [correction("correct_report_date", report_date="2026-08-28")])
            self.assertEqual({"2026-08-28"}, {row.report_date for row in result.rows})
            self.assertEqual({"2026-08-29"}, {row.original_report_date for row in result.rows})

    def test_correct_stock_name(self):
        with tempfile.TemporaryDirectory() as directory:
            result = apply_corrections(self.dataset(Path(directory)), [correction("correct_stock_name", rank=1, stock_name="정정종목")])
            row = result.rows[0]
            self.assertEqual("종목1", row.original_stock_name)
            self.assertEqual("정정종목", row.stock_name)
            self.assertTrue(row.manual_correction_applied)

    def test_correct_return_pct(self):
        with tempfile.TemporaryDirectory() as directory:
            result = apply_corrections(self.dataset(Path(directory)), [correction("correct_return_pct", rank=1, return_pct="99.25")])
            self.assertEqual("1.10", str(result.rows[0].original_return_pct))
            self.assertEqual("99.25", str(result.rows[0].return_pct))

    def test_correct_stock_combined(self):
        with tempfile.TemporaryDirectory() as directory:
            result = apply_corrections(self.dataset(Path(directory)), [correction("correct_stock", rank=1, stock_name="정정종목", return_pct="88.5")])
            self.assertEqual("정정종목", result.rows[0].stock_name)
            self.assertEqual("88.5", str(result.rows[0].return_pct))
            self.assertEqual("correct_stock", result.logs[0].action)

    def assert_validation_error(self, item: Correction):
        with tempfile.TemporaryDirectory() as directory:
            result = apply_corrections(self.dataset(Path(directory)), [item])
            self.assertTrue(any(log.status.startswith("VALIDATION_ERROR") for log in result.logs))

    def test_nonexistent_message_id(self):
        self.assert_validation_error(correction("exclude_report", message_id=999))

    def test_invalid_action(self):
        self.assert_validation_error(correction("overwrite_everything"))

    def test_invalid_rank(self):
        self.assert_validation_error(correction("correct_stock_name", rank=31, stock_name="정정"))

    def test_invalid_date(self):
        self.assert_validation_error(correction("correct_report_date", report_date="2026-02-30"))

    def test_invalid_number(self):
        self.assert_validation_error(correction("correct_return_pct", rank=1, return_pct="not-number"))

    def test_conflicting_corrections(self):
        with tempfile.TemporaryDirectory() as directory:
            items = [
                correction("correct_stock_name", rank=1, stock_name="첫번째"),
                correction("correct_stock", rank=1, stock_name="두번째", return_pct="2.0"),
            ]
            result = apply_corrections(self.dataset(Path(directory)), items)
            self.assertEqual(2, sum(log.status.startswith("VALIDATION_ERROR") for log in result.logs))
            self.assertEqual("종목1", result.rows[0].stock_name)

    def test_use_report_resolves_duplicate_date(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "2026-08-29_01-00-00_1.txt").write_text(body(), encoding="utf-8")
            changed = body().replace("1. 종목1", "1. 다른종목")
            (root / "2026-08-29_02-00-00_2.txt").write_text(changed, encoding="utf-8")
            dataset = build_dataset(root)
            self.assertEqual([], apply_corrections(dataset, []).rows)
            result = apply_corrections(dataset, [correction("use_report", message_id=2)])
            self.assertEqual({2}, {row.telegram_message_id for row in result.rows})


if __name__ == "__main__":
    unittest.main()
