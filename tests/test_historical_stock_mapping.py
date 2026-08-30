from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.stock_mapping.historical_master import (
    OVERRIDE_FIELDS,
    ManualOverride,
    Observation,
    ValidationError,
    load_historical_master,
    load_overrides,
    map_observations,
    validate_master_intervals,
    write_mapping_results,
)
from src.stock_mapping.normalization import normalize_stock_name


FIXTURE = Path(__file__).parent / "fixtures" / "historical_stock_master.csv"


class HistoricalStockMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master = load_historical_master(FIXTURE)

    def map_one(self, day: str, name: str):
        observation = Observation(date.fromisoformat(day), name)
        return map_observations([observation], self.master)[0]

    def test_exact_temporal_match_and_leading_zero(self):
        result = self.map_one("2026-08-28", "삼성전자")
        self.assertEqual("AUTO_EXACT_TEMPORAL", result.mapping_status)
        self.assertEqual("005930", result.stock_code)
        self.assertEqual(6, len(result.stock_code))

    def test_normalized_temporal_match(self):
        result = self.map_one("2025-01-24", "형지I＆C")
        self.assertEqual("AUTO_NORMALIZED_TEMPORAL", result.mapping_status)
        self.assertEqual("012345", result.stock_code)
        self.assertEqual("형지I&C", result.stock_name_normalized)

    def test_nfkc_and_whitespace_normalization(self):
        self.assertEqual("형지I&C", normalize_stock_name("  형지I＆C  "))
        self.assertEqual("테스트 종목", normalize_stock_name("테스트\t  종목"))

    def test_name_change_before_and_after(self):
        before = self.map_one("2024-04-18", "레고켐바이오")
        after = self.map_one("2024-04-19", "리가켐바이오")
        self.assertEqual("141080", before.stock_code)
        self.assertEqual("141080", after.stock_code)
        self.assertEqual("UNMAPPED", self.map_one("2024-04-19", "레고켐바이오").mapping_status)

    def test_before_listing_is_unmapped(self):
        self.assertEqual("UNMAPPED", self.map_one("2024-12-31", "미래상장").mapping_status)

    def test_delisting_date_is_exclusive(self):
        self.assertEqual("AUTO_EXACT_TEMPORAL", self.map_one("2024-01-09", "폐지회사").mapping_status)
        self.assertEqual("UNMAPPED", self.map_one("2024-01-10", "폐지회사").mapping_status)

    def test_multiple_normalized_candidates_require_review(self):
        result = self.map_one("2026-01-01", "ＡＢＣ")
        self.assertEqual("REVIEW_REQUIRED", result.mapping_status)
        self.assertEqual("", result.stock_code)

    def test_unmapped(self):
        self.assertEqual("UNMAPPED", self.map_one("2026-01-01", "없는회사").mapping_status)

    def test_spac_security_type_requires_review(self):
        result = self.map_one("2026-01-01", "검토스팩")
        self.assertEqual("REVIEW_REQUIRED", result.mapping_status)
        self.assertEqual("", result.stock_code)

    def test_manual_confirmed(self):
        observation = Observation(date(2026, 1, 1), "없는회사")
        override = ManualOverride(date(2026, 1, 1), "없는회사", "005930",
                                  "confirm_mapping", "공식 공시 확인", "")
        result = map_observations([observation], self.master, [override])[0]
        self.assertEqual("MANUAL_CONFIRMED", result.mapping_status)
        self.assertEqual("005930", result.stock_code)

    def write_override(self, directory: str, rows: list[dict[str, str]]) -> Path:
        path = Path(directory) / "overrides.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=OVERRIDE_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def valid_override(self, **changes):
        row = {"report_date": "2026-01-01", "observed_stock_name": "없는회사",
               "stock_code": "005930", "action": "confirm_mapping",
               "reason": "공식 공시 확인", "note": ""}
        row.update(changes)
        return row

    def test_invalid_manual_code_and_missing_date(self):
        observations = [Observation(date(2026, 1, 1), "없는회사")]
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_override(directory, [self.valid_override(stock_code="123")])
            with self.assertRaisesRegex(ValidationError, "six ASCII digits"):
                load_overrides(path, observations, self.master)
            path = self.write_override(directory, [self.valid_override(report_date="2026-01-02")])
            with self.assertRaisesRegex(ValidationError, "does not exist"):
                load_overrides(path, observations, self.master)

    def test_duplicate_override_is_error(self):
        observations = [Observation(date(2026, 1, 1), "없는회사")]
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_override(directory, [self.valid_override(), self.valid_override(stock_code="141080")])
            with self.assertRaisesRegex(ValidationError, "duplicate or conflicting"):
                load_overrides(path, observations, self.master)

    def test_manual_code_missing_from_master_is_error(self):
        observations = [Observation(date(2026, 1, 1), "없는회사")]
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_override(directory, [self.valid_override(stock_code="000001")])
            with self.assertRaisesRegex(ValidationError, "does not exist in historical master"):
                load_overrides(path, observations, self.master)

    def test_overlapping_intervals_are_invalid(self):
        duplicate = self.master[0]
        with self.assertRaisesRegex(ValidationError, "overlapping"):
            validate_master_intervals([duplicate, duplicate])

    def test_override_template_header(self):
        path = Path("data/manual/stock_mapping_overrides.csv")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            self.assertEqual(list(OVERRIDE_FIELDS), next(reader))
            self.assertEqual([], list(reader))

    def test_mapping_csv_preserves_leading_zero(self):
        result = self.map_one("2026-08-28", "삼성전자")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mapping.csv"
            write_mapping_results(path, [result])
            with path.open(encoding="utf-8-sig", newline="") as stream:
                saved = next(csv.DictReader(stream))
            self.assertEqual("005930", saved["stock_code"])


if __name__ == "__main__":
    unittest.main()
