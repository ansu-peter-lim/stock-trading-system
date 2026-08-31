from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.stock_mapping.historical_master import ValidationError
from src.stock_mapping.krx_historical_master_builder import (
    build_historical_master,
    load_source_states,
    provenance_for_file,
    require_explicit_network_access,
    validate_provenance,
    write_master,
    write_top30_mapping_bundle,
)


FIXTURE = Path(__file__).parent / "fixtures" / "krx_master_states.csv"
INVALID_FIXTURE = Path(__file__).parent / "fixtures" / "krx_master_states_invalid.csv"


class KrxHistoricalMasterBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = build_historical_master(load_source_states(FIXTURE))
        cls.by_code = {}
        for record in cls.result.records:
            cls.by_code.setdefault(record.stock_code, []).append(record)

    def test_general_listing_and_leading_zero(self):
        record = self.by_code["005930"][0]
        self.assertEqual("005930", record.stock_code)
        self.assertEqual("COMMON", record.security_type)
        self.assertEqual("보통주", record.security_type_raw)

    def test_name_change_uses_effective_date(self):
        old, new = self.by_code["012345"]
        self.assertEqual("2024-04-18", old.valid_to.isoformat())
        self.assertEqual("2024-04-19", new.valid_from.isoformat())

    def test_delisting_is_exclusive(self):
        record = self.by_code["001230"][0]
        self.assertEqual("2024-01-10", record.delisting_date.isoformat())
        self.assertEqual("2024-01-09", record.valid_to.isoformat())
        self.assertFalse(record.active_on(record.delisting_date))

    def test_market_transfer_creates_separate_intervals(self):
        before, after = self.by_code["333333"]
        self.assertEqual(("KOSPI", "2025-01-02"), (before.market, before.valid_to.isoformat()))
        self.assertEqual(("KOSDAQ", "2025-01-03"), (after.market, after.valid_from.isoformat()))

    def test_preferred_and_spac_types(self):
        self.assertEqual("PREFERRED", self.by_code["000001"][0].security_type)
        self.assertEqual("SPAC", self.by_code["000002"][0].security_type)

    def test_normalized_collision_is_preserved(self):
        names = {self.by_code[code][0].stock_name_normalized for code in ("111111", "222222")}
        self.assertEqual({"ABC"}, names)

    def test_invalid_interval_is_reported_and_blocks_output(self):
        result = build_historical_master(load_source_states(INVALID_FIXTURE))
        self.assertFalse(result.valid)
        self.assertEqual("INTERVAL_BEFORE_LISTING", result.issues[0].issue_code)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValidationError, "output blocked"):
                write_master(Path(directory) / "master.csv", result)

    def test_last_trading_date_semantics_are_not_inferred(self):
        rows = FIXTURE.read_text(encoding="utf-8").replace(
            "2024-01-10,EFFECTIVE_DATE", "2024-01-09,LAST_TRADING_DATE"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "states.csv"
            path.write_text(rows, encoding="utf-8")
            result = build_historical_master(load_source_states(path))
        self.assertFalse(result.valid)
        self.assertIn("UNRESOLVED_DELISTING_DATE_SEMANTICS",
                      {issue.issue_code for issue in result.issues})

    def test_provenance_hash_and_no_credentials(self):
        provenance = provenance_for_file(
            FIXTURE, source_name="KRX", service_name="mock", requested_base_date="2026-08-28",
            retrieved_at="2026-08-30T00:00:00Z", request_parameters={"basDd": "20260828"},
            row_count=10,
        )
        self.assertEqual(FIXTURE.as_posix(), provenance.raw_file_path)
        self.assertEqual(
            hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            provenance.raw_file_sha256,
        )
        self.assertEqual(64, len(provenance.raw_file_sha256))
        self.assertNotIn("AUTH", provenance.request_parameters.upper())
        validate_provenance(load_source_states(FIXTURE), [provenance])
        with self.assertRaisesRegex(ValidationError, "credentials"):
            provenance_for_file(
                FIXTURE, source_name="KRX", service_name="mock", requested_base_date="",
                retrieved_at="", request_parameters={"AUTH_KEY": "secret"}, row_count=10,
            )

    def test_provenance_hash_mismatch_blocks_output_with_source_path(self):
        provenance = provenance_for_file(
            FIXTURE, source_name="KRX", service_name="mock", requested_base_date="2026-08-28",
            retrieved_at="2026-08-30T00:00:00Z", request_parameters={"basDd": "20260828"},
            row_count=10,
        )
        invalid = replace(provenance, raw_file_sha256="0" * 64)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "master.csv"
            with self.assertRaisesRegex(
                ValidationError,
                r"raw provenance SHA-256 mismatch: .*krx_master_states\.csv",
            ):
                validate_provenance(load_source_states(FIXTURE), [invalid])
                write_master(output, self.result)
            self.assertFalse(output.exists())

    def test_missing_provenance_is_error(self):
        with self.assertRaisesRegex(ValidationError, "missing provenance"):
            validate_provenance(load_source_states(FIXTURE), [])

    def test_network_requires_flag_and_key(self):
        with self.assertRaisesRegex(ValidationError, "explicit"):
            require_explicit_network_access(False, {})
        with self.assertRaisesRegex(ValidationError, "KRX_AUTH_KEY"):
            require_explicit_network_access(True, {})
        require_explicit_network_access(True, {"KRX_AUTH_KEY": "configured"})

    def test_valid_master_csv_schema_and_leading_zero(self):
        self.assertTrue(self.result.valid)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.csv"
            write_master(path, self.result)
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual("005930", next(row for row in rows if row["stock_code"] == "005930")["stock_code"])
        self.assertIn("security_type_raw", rows[0])


if __name__ == "__main__":
    unittest.main()
