from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.telegram_top30_parser.parser import build_dataset, classify_report, parse_report

def lines(count: int = 30) -> list[str]:
    return [f"{n}. 종목{n} ({n}.10%) : 상세 {n}" for n in range(1, count + 1)]

def text(items: list[str]) -> str:
    return "2026년 8월 29일 상승률 TOP30\n\n" + "\n".join(items)

class ParserTests(unittest.TestCase):
    def parse(self, body: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-08-29_01-02-03_123.txt"
            path.write_text(body, encoding="utf-8")
            return parse_report(path)

    def test_normal_30(self):
        result = self.parse(text(lines()))
        self.assertEqual("valid", result.parse_status)
        self.assertEqual(30, len(result.rows))

    def test_fullwidth_rank(self):
        items = lines(); items[7] = "８. 그린생명과학 (28.57%) : 원문 상세"
        result = self.parse(text(items))
        self.assertEqual(8, result.rows[7].rank)
        self.assertTrue(result.recovery_used)

    def test_false_rank_2_point_2_trillion(self):
        items = lines(); items[0] += "\n2.2조원 규모 기술수출 소식"
        result = self.parse(text(items))
        self.assertEqual(30, len(result.rows))
        self.assertIn("2.2조원", result.rows[0].detail_raw)

    def test_missing_percent(self):
        items = lines(); items[0] = "1. 에이치시티 (12.10) : 상세"
        result = self.parse(text(items))
        self.assertEqual("12.10", str(result.rows[0].return_pct))
        self.assertTrue(result.recovery_used)

    def test_empty_percent_is_none(self):
        items = lines(); items[0] = "1. 서부T&D (%) : 상세"
        result = self.parse(text(items))
        self.assertIsNone(result.rows[0].return_pct)
        self.assertIn("RETURN_PCT_MISSING_OR_INVALID", {i.issue_code for i in result.issues})

    def test_missing_open_parenthesis(self):
        items = lines(); items[0] = "1. 대한광통신 11.08%) : 상세"
        result = self.parse(text(items))
        self.assertEqual("11.08", str(result.rows[0].return_pct))

    def test_bad_decimal_is_none(self):
        items = lines(); items[0] = "1. 에스와이스텔텍 (17..12%) : 상세"
        self.assertIsNone(self.parse(text(items)).rows[0].return_pct)

    def test_fullwidth_colon(self):
        items = lines(); items[0] = "1. 엠케이전자 (26.77%)：상세"
        result = self.parse(text(items))
        self.assertEqual("26.77", str(result.rows[0].return_pct))
        self.assertTrue(result.recovery_used)

    def test_29_stocks(self):
        result = self.parse(text(lines(29)))
        self.assertEqual("invalid", result.parse_status)
        self.assertIn("MISSING_RANK", {i.issue_code for i in result.issues})

    def test_duplicate_rank(self):
        items = lines(); items[-1] = "29. 중복종목 (1.00%) : 상세"
        codes = {i.issue_code for i in self.parse(text(items)).issues}
        self.assertIn("DUPLICATE_RANK", codes)
        self.assertIn("MISSING_RANK", codes)

    def test_classification_priority(self):
        self.assertEqual("weekly", classify_report("2026년 8월 29일 주간상승률 TOP30"))
        self.assertEqual("monthly", classify_report("2026년 8월 월간상승률 TOP30"))
        self.assertEqual("quarterly", classify_report("2026년 3분기 상승률 TOP30"))
        self.assertEqual("intraday_summary", classify_report("9시 30분 상승률 TOP30 동향"))
        self.assertEqual("unrelated", classify_report("08월 07일 장마감 시황\n상승률 TOP30 자료"))

    def test_duplicate_same_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); body = text(lines())
            (root / "2026-08-29_01-00-00_1.txt").write_text(body, encoding="utf-8")
            (root / "2026-08-29_01-01-00_2.txt").write_text(body, encoding="utf-8")
            dataset = build_dataset(root)
            self.assertEqual(2, sum(i.issue_code == "EXACT_DUPLICATE_REPORT" for i in dataset.issues))

    def test_duplicate_different_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); first = text(lines()); changed = lines(); changed[0] = "1. 변경종목 (1.10%) : 다른 상세"
            (root / "2026-08-29_01-00-00_1.txt").write_text(first, encoding="utf-8")
            (root / "2026-08-29_01-01-00_2.txt").write_text(text(changed), encoding="utf-8")
            dataset = build_dataset(root)
            self.assertEqual(2, sum(i.issue_code == "CONFLICTING_REPORT_VERSION" for i in dataset.issues))

if __name__ == "__main__":
    unittest.main()
