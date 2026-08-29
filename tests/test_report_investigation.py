from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.telegram_top30_parser.parser import build_dataset, parse_report
from src.telegram_top30_parser.report_investigation import (
    compare_reports,
    conflicting_rows,
    exact_duplicate_rows,
    manifest_rows,
    report_analysis_grade,
)


def report_lines(*, recovery: bool = False, missing: bool = False) -> list[str]:
    result = [f"{rank}. 종목{rank} ({rank}.10%) : 상세 {rank}" for rank in range(1, 31)]
    if recovery:
        result[0] = "１. 종목1 (1.10%)：상세 1"
    if missing:
        result.pop()
    return result


def report_text(lines: list[str], day: str = "2026년 8월 29일") -> str:
    return f"{day} 상승률 TOP30\n\n" + "\n".join(lines)


class InvestigationTests(unittest.TestCase):
    def parse_text(self, body: str, name: str = "2026-08-29_01-00-00_1.txt"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_text(body, encoding="utf-8")
            return parse_report(path)

    def test_analysis_grades(self):
        self.assertEqual("A", report_analysis_grade(self.parse_text(report_text(report_lines()))))
        self.assertEqual("B", report_analysis_grade(self.parse_text(report_text(report_lines(recovery=True)))))
        self.assertEqual("C", report_analysis_grade(self.parse_text(report_text(report_lines(missing=True)))))
        self.assertEqual("N", report_analysis_grade(self.parse_text("9시 30분 상승률 TOP30 동향")))

    def test_manifest_has_one_row_per_report_and_duplicate_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = report_text(report_lines())
            (root / "2026-08-29_01-00-00_1.txt").write_text(body, encoding="utf-8")
            (root / "2026-08-29_01-01-00_2.txt").write_text(body, encoding="utf-8")
            rows = manifest_rows(build_dataset(root))
            self.assertEqual(2, len(rows))
            self.assertTrue(all(row["duplicate_status"] == "EXACT_DUPLICATE_REPORT" for row in rows))

    def test_pair_difference(self):
        left_lines = report_lines()
        right_lines = report_lines()
        right_lines[0] = "1. 다른종목 (1.10%) : 상세 1"
        right_lines[1] = "2. 종목2 (9.99%) : 상세 2"
        right_lines[2] = "3. 종목3 (3.10%) : 변경 상세"
        right_lines.pop(3)
        left = self.parse_text(report_text(left_lines), "2026-08-29_01-00-00_1.txt")
        right = self.parse_text(report_text(right_lines), "2026-08-29_01-01-00_2.txt")
        diff = compare_reports(left, right)
        self.assertEqual((1,), diff.stock_name_different_ranks)
        self.assertEqual((2,), diff.return_pct_different_ranks)
        self.assertEqual((4,), diff.ranks_only_left)
        self.assertEqual((), diff.ranks_only_right)
        self.assertEqual((3,), diff.detail_raw_different_ranks)

    def test_exact_and_conflicting_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = report_text(report_lines())
            changed_lines = report_lines()
            changed_lines[0] = "1. 변경종목 (1.10%) : 다른 상세"
            (root / "2026-08-29_01-00-00_1.txt").write_text(first, encoding="utf-8")
            (root / "2026-08-29_01-01-00_2.txt").write_text(first, encoding="utf-8")
            dataset = build_dataset(root)
            self.assertEqual(2, len(exact_duplicate_rows(dataset)))
            self.assertEqual(0, len(conflicting_rows(dataset)))

            (root / "2026-08-29_01-02-00_3.txt").write_text(report_text(changed_lines), encoding="utf-8")
            dataset = build_dataset(root)
            self.assertEqual(3, len(conflicting_rows(dataset)))
            self.assertEqual(0, len(exact_duplicate_rows(dataset)))


if __name__ == "__main__":
    unittest.main()
