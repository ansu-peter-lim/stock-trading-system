from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.telegram_top30_parser.krx_openapi_pilot import (
    FetchResult,
    collect_pilot,
    load_auth_key,
    normalized_search_name,
    response_rows,
)


class KrxOpenApiPilotTests(unittest.TestCase):
    def test_key_is_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "KRX_AUTH_KEY"):
                load_auth_key(Path("missing.env"))

    def test_nfkc_is_search_only(self):
        original = "형지I＆C"
        self.assertEqual("형지I&C", normalized_search_name(original))
        self.assertEqual("형지I＆C", original)

    def test_schema_requires_outblock_list(self):
        self.assertEqual([{"ISU_SRT_CD": "005930"}], response_rows(
            json.dumps({"OutBlock_1": [{"ISU_SRT_CD": "005930"}]}).encode()
        ))
        with self.assertRaises(ValueError):
            response_rows(b"{}")

    def test_raw_bytes_and_secret_are_not_persisted(self):
        body = b'{"OutBlock_1":[{"ISU_SRT_CD":"005930"}]}\n'

        def fake_fetch(endpoint: str, base_date: str, auth_key: str) -> FetchResult:
            self.assertEqual("secret-value", auth_key)
            return FetchResult(body, 200, "2026-08-30T00:00:00Z")

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"KRX_AUTH_KEY": "secret-value"}, clear=True
        ):
            root = Path(directory) / "raw"
            records = collect_pilot(root=root, dates=("2024-04-19",), fetcher=fake_fetch)
            self.assertEqual(2, len(records))
            self.assertEqual(body, (root / "kospi" / "2024-04-19.json").read_bytes())
            self.assertEqual(hashlib.sha256(body).hexdigest(), records[0]["raw_file_sha256"])
            manifest_text = (root / "manifest.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn("secret-value", manifest_text)
            with (root / "manifest.csv").open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({"KOSPI", "KOSDAQ"}, {row["market"] for row in rows})


if __name__ == "__main__":
    unittest.main()
