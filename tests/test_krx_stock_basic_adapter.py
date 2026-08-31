from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from src.krx_openapi.parser import parse_krx_response
from src.krx_openapi.services import KRX_SERVICES
from src.stock_mapping.historical_master import ValidationError
from src.stock_mapping.krx_stock_basic_adapter import (
    CanonicalCodeEligibility,
    EffectiveDateBasis,
    MappingNameSource,
    SnapshotInput,
    SnapshotProvenance,
    SnapshotStatus,
    adapt_stock_basic_snapshot,
    build_absence_observations,
    build_observation_runs,
    build_transition_candidates,
    classify_canonical_code,
    require_confirmed_effective_date,
)
from src.stock_mapping.normalization import normalize_stock_name


def raw_row(**changes: str) -> dict[str, str]:
    row = {
        "ISU_CD": "KR7005930003",
        "ISU_SRT_CD": "005930",
        "ISU_NM": "삼성전자보통주",
        "ISU_ABBRV": " 삼성전자  ",
        "ISU_ENG_NM": "SamsungElectronics",
        "LIST_DD": "19750611",
        "MKT_TP_NM": "KOSPI",
        "SECUGRP_NM": "주권",
        "SECT_TP_NM": "",
        "KIND_STKCERT_TP_NM": "보통주",
        "PARVAL": "100",
        "LIST_SHRS": "5969782550",
    }
    row.update(changes)
    return row


def raw_body(rows: list[dict[str, str]]) -> bytes:
    return json.dumps({"OutBlock_1": rows}, ensure_ascii=False).encode()


class KrxStockBasicAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def snapshot(
        self,
        rows: list[dict[str, str]],
        *,
        bas_dd: str = "20260828",
        market: str = "KOSPI",
        status: SnapshotStatus = SnapshotStatus.SUCCESS_WITH_ROWS,
        filename: str | None = None,
    ):
        body = raw_body(rows)
        path = self.root / (filename or f"{bas_dd}-{market}.json")
        path.write_bytes(body)
        provenance = SnapshotProvenance(
            service_id=(
                "stk_isu_base_info" if market == "KOSPI" else "ksq_isu_base_info"
            ),
            bas_dd=bas_dd,
            raw_artifact_path=path.as_posix(),
            raw_sha256=hashlib.sha256(body).hexdigest(),
            retrieved_at="2026-08-31T00:00:00Z",
            byte_size=len(body),
            collector_version="K1",
            schema_version="1",
            manifest_status=(
                "SUCCESS"
                if status is SnapshotStatus.SUCCESS_WITH_ROWS
                else "EMPTY_RESPONSE"
            ),
        )
        parsed = parse_krx_response(
            body,
            KRX_SERVICES[
                "stk_isu_base_info" if market == "KOSPI" else "ksq_isu_base_info"
            ],
        )
        return adapt_stock_basic_snapshot(
            parsed, SnapshotInput(status, market, provenance)
        )

    def test_code_eligibility_preserves_leading_zero_and_excludes_alpha(self) -> None:
        numeric = classify_canonical_code("005930")
        alpha = classify_canonical_code("12345A")
        self.assertEqual(CanonicalCodeEligibility.ELIGIBLE, numeric.eligibility)
        self.assertEqual("005930", numeric.canonical_stock_code)
        self.assertEqual(
            CanonicalCodeEligibility.INELIGIBLE_NON_NUMERIC_CODE,
            alpha.eligibility,
        )
        self.assertIsNone(alpha.canonical_stock_code)
        self.assertEqual("12345A", alpha.raw_source_code)

    def test_observation_preserves_facts_without_effective_date(self) -> None:
        result = self.snapshot([raw_row(), raw_row(ISU_SRT_CD="12345A")])
        numeric, alpha = result.observations
        self.assertEqual(date(2026, 8, 28), numeric.observed_on)
        self.assertFalse(hasattr(numeric, "effective_date"))
        self.assertEqual("삼성전자보통주", numeric.raw_isu_nm)
        self.assertEqual(" 삼성전자  ", numeric.raw_isu_abbrv)
        self.assertEqual(MappingNameSource.ISU_ABBRV, numeric.mapping_name_source)
        self.assertEqual(
            normalize_stock_name(numeric.raw_isu_abbrv),
            numeric.normalized_mapping_name,
        )
        self.assertEqual("19750611", numeric.list_dd_raw)
        self.assertEqual(date(1975, 6, 11), numeric.list_dd_candidate)
        self.assertIsNone(alpha.canonical_stock_code)
        self.assertEqual((("NON_NUMERIC_KRX_SOURCE_CODE", 1),), result.exclusion_counts)

    def test_list_dd_is_candidate_and_conflicts_are_rejected(self) -> None:
        first = self.snapshot([raw_row()], bas_dd="20260801").observations[0]
        changed = self.snapshot(
            [raw_row(LIST_DD="19750612")], bas_dd="20260802"
        ).observations[0]
        self.assertFalse(hasattr(first, "listing_date"))
        with self.assertRaisesRegex(ValidationError, "conflicting LIST_DD"):
            build_observation_runs([first, changed])

    def test_market_match_mismatch_and_same_date_cross_market_conflict(self) -> None:
        kospi = self.snapshot([raw_row()]).observations[0]
        with self.assertRaisesRegex(ValidationError, "does not match"):
            self.snapshot([raw_row(MKT_TP_NM="KOSDAQ")])
        kosdaq = self.snapshot(
            [raw_row(MKT_TP_NM="KOSDAQ")],
            market="KOSDAQ",
            filename="kosdaq.json",
        ).observations[0]
        with self.assertRaisesRegex(ValidationError, "multiple markets"):
            build_observation_runs([kosdaq, kospi])

    def test_runs_and_name_market_transitions_are_observed_windows(self) -> None:
        first = self.snapshot([raw_row()], bas_dd="20260801").observations[0]
        repeated = self.snapshot([raw_row()], bas_dd="20260802").observations[0]
        renamed = self.snapshot(
            [raw_row(ISU_ABBRV="새이름")], bas_dd="20260810"
        ).observations[0]
        moved = replace(
            self.snapshot(
                [raw_row(ISU_SRT_CD="005930", MKT_TP_NM="KOSDAQ")],
                bas_dd="20260820",
                market="KOSDAQ",
            ).observations[0],
            normalized_mapping_name="새이름",
        )
        runs = build_observation_runs([moved, renamed, repeated, first])
        self.assertEqual(3, len(runs))
        self.assertEqual(date(2026, 8, 1), runs[0].first_observed_on)
        self.assertEqual(date(2026, 8, 2), runs[0].last_observed_on)
        candidates = build_transition_candidates(runs)
        self.assertEqual(("normalized_mapping_name",), candidates[0].changed_fields)
        self.assertEqual(("observed_market",), candidates[1].changed_fields)
        self.assertTrue(
            all(
                item.effective_date_basis is EffectiveDateBasis.OBSERVED_WINDOW
                for item in candidates
            )
        )
        self.assertTrue(
            all(item.confirmed_effective_from is None for item in candidates)
        )
        self.assertEqual(date(2026, 8, 10), candidates[0].current_observed_on)

    def test_status_gate_absence_and_no_termination_semantics(self) -> None:
        previous = self.snapshot([raw_row()], bas_dd="20260801")
        empty = self.snapshot(
            [], bas_dd="20260802", status=SnapshotStatus.SUCCESS_EMPTY
        )
        absence = build_absence_observations(previous, empty)
        self.assertEqual(1, len(absence))
        self.assertFalse(hasattr(absence[0], "valid_to"))
        self.assertFalse(hasattr(absence[0], "delisting_date"))
        for status in (
            SnapshotStatus.NOT_ATTEMPTED,
            SnapshotStatus.HTTP_ERROR,
            SnapshotStatus.SCHEMA_ERROR,
            SnapshotStatus.RAW_CONFLICT,
        ):
            failed = SnapshotResultForTest(status)
            self.assertEqual((), build_absence_observations(previous, failed))
            with self.assertRaisesRegex(ValidationError, "blocks processing"):
                adapt_stock_basic_snapshot(None, SnapshotInput(status, "KOSPI", None))

    def test_observed_window_promotion_is_blocked(self) -> None:
        first = self.snapshot([raw_row()], bas_dd="20260801").observations[0]
        renamed = self.snapshot(
            [raw_row(ISU_ABBRV="새이름")], bas_dd="20260810"
        ).observations[0]
        candidate = build_transition_candidates(
            build_observation_runs([first, renamed])
        )[0]
        with self.assertRaisesRegex(ValidationError, "promotion blocked"):
            require_confirmed_effective_date(candidate)

    def test_invalid_provenance_and_order_independent_canonical_result(self) -> None:
        result = self.snapshot([raw_row(), raw_row(ISU_SRT_CD="12345A")])
        invalid = replace(result.provenance, raw_sha256="0" * 64)
        with self.assertRaisesRegex(ValidationError, "SHA-256 mismatch"):
            adapt_stock_basic_snapshot(
                parse_krx_response(
                    Path(result.provenance.raw_artifact_path).read_bytes(),
                    KRX_SERVICES["stk_isu_base_info"],
                ),
                SnapshotInput(SnapshotStatus.SUCCESS_WITH_ROWS, "KOSPI", invalid),
            )
        reversed_result = self.snapshot(
            [raw_row(ISU_SRT_CD="12345A"), raw_row()], filename="reversed.json"
        )
        self.assertEqual(result.observations, reversed_result.observations)
        self.assertEqual(result.exclusion_counts, reversed_result.exclusion_counts)


def SnapshotResultForTest(status: SnapshotStatus):
    from src.stock_mapping.krx_stock_basic_adapter import SnapshotResult

    return SnapshotResult(status, None, "KOSPI", (), (), None)


if __name__ == "__main__":
    unittest.main()
