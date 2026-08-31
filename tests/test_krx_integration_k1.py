from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Self
from urllib.error import URLError

from src.krx_openapi.collector import collect_one
from src.krx_openapi.parser import (
    KrxSchemaError,
    ParValueKind,
    parse_krx_response,
)
from src.krx_openapi.services import KRX_SERVICES
from src.krx_openapi.smoke import require_network_access
from src.krx_openapi.store import (
    ArtifactDisposition,
    ImmutableRawStore,
    ManifestStatus,
)
from src.krx_openapi.transport import (
    EnvironmentAuthKeyProvider,
    KrxConfigurationError,
    KrxHttpError,
    KrxTransport,
    RequestIdentity,
    TransportResult,
)

SYNTHETIC_SECRET = "synthetic-dummy-secret"
AT = datetime(2026, 8, 31, 1, 2, 3, tzinfo=timezone.utc)


def basic_row() -> dict[str, str]:
    return {
        "ISU_CD": "KR7005930003",
        "ISU_SRT_CD": "005930",
        "ISU_NM": "삼성전자보통주",
        "ISU_ABBRV": "삼성전자",
        "ISU_ENG_NM": "SamsungElectronics",
        "LIST_DD": "19750611",
        "MKT_TP_NM": "KOSPI",
        "SECUGRP_NM": "주권",
        "SECT_TP_NM": "",
        "KIND_STKCERT_TP_NM": "보통주",
        "PARVAL": "100",
        "LIST_SHRS": "5,969,782,550",
    }


def daily_row() -> dict[str, str]:
    return {
        "BAS_DD": "20260828",
        "ISU_CD": "005930",
        "ISU_NM": "삼성전자",
        "MKT_NM": "KOSPI",
        "SECT_TP_NM": "",
        "TDD_CLSPRC": "70,500",
        "CMPPREVDD_PRC": "500",
        "FLUC_RT": "0.71",
        "TDD_OPNPRC": "70,000",
        "TDD_HGPRC": "71,000",
        "TDD_LWPRC": "69,000",
        "ACC_TRDVOL": "12,345,678",
        "ACC_TRDVAL": "870,000,000,000",
        "MKTCAP": "420,000,000,000,000",
        "LIST_SHRS": "5,969,782,550",
    }


def body(rows: list[dict[str, str]]) -> bytes:
    return json.dumps({"OutBlock_1": rows}, ensure_ascii=False).encode()


class FakeResponse:
    status = 200

    def __init__(self, response_body: bytes) -> None:
        self._body = response_body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class KrxServiceAndTransportTests(unittest.TestCase):
    def test_registry_contains_only_the_four_k1_services(self) -> None:
        self.assertEqual(
            {
                "stk_isu_base_info",
                "ksq_isu_base_info",
                "stk_bydd_trd",
                "ksq_bydd_trd",
            },
            set(KRX_SERVICES),
        )
        for service in KRX_SERVICES.values():
            self.assertEqual("OutBlock_1", service.expected_root)
            self.assertEqual(("basDd",), service.required_request_fields)

    def test_missing_key_is_a_safe_explicit_error(self) -> None:
        provider = EnvironmentAuthKeyProvider(env_path=None, environ={})
        with self.assertRaisesRegex(KrxConfigurationError, "KRX_AUTH_KEY") as caught:
            provider.get_auth_key()
        self.assertNotIn(SYNTHETIC_SECRET, str(caught.exception))

    def test_secret_is_header_only_and_absent_from_identity_repr(self) -> None:
        observed: dict[str, object] = {}

        def opener(request, *, timeout):
            observed["url"] = request.full_url
            observed["auth"] = request.get_header("Auth_key")
            observed["timeout"] = timeout
            return FakeResponse(body([basic_row()]))

        result = KrxTransport(opener=opener, clock=lambda: AT).fetch(
            KRX_SERVICES["stk_isu_base_info"],
            bas_dd="20260828",
            auth_key_provider=EnvironmentAuthKeyProvider(
                env_path=None, environ={"KRX_AUTH_KEY": SYNTHETIC_SECRET}
            ),
            timeout=7,
        )
        self.assertEqual(SYNTHETIC_SECRET, observed["auth"])
        self.assertNotIn(SYNTHETIC_SECRET, str(observed["url"]))
        self.assertEqual(7, observed["timeout"])
        self.assertNotIn(SYNTHETIC_SECRET, repr(result))
        self.assertNotIn(
            SYNTHETIC_SECRET,
            repr(
                EnvironmentAuthKeyProvider(
                    env_path=None, environ={"KRX_AUTH_KEY": SYNTHETIC_SECRET}
                )
            ),
        )

    def test_network_error_does_not_echo_secret(self) -> None:
        def opener(_request, *, timeout):
            raise URLError(f"failed with {SYNTHETIC_SECRET} at timeout {timeout}")

        with self.assertRaises(KrxHttpError) as caught:
            KrxTransport(opener=opener).fetch(
                KRX_SERVICES["stk_isu_base_info"],
                bas_dd="20260828",
                auth_key_provider=EnvironmentAuthKeyProvider(
                    env_path=None, environ={"KRX_AUTH_KEY": SYNTHETIC_SECRET}
                ),
            )
        self.assertNotIn(SYNTHETIC_SECRET, str(caught.exception))

    def test_network_gate_checks_flag_before_key(self) -> None:
        provider = EnvironmentAuthKeyProvider(env_path=None, environ={})
        with self.assertRaisesRegex(KrxConfigurationError, "explicit --network"):
            require_network_access(False, provider)
        with self.assertRaisesRegex(KrxConfigurationError, "KRX_AUTH_KEY"):
            require_network_access(True, provider)


class KrxParserTests(unittest.TestCase):
    def test_basic_and_daily_numeric_types_and_leading_zero(self) -> None:
        basic = parse_krx_response(
            body([basic_row()]), KRX_SERVICES["stk_isu_base_info"]
        ).rows[0]
        daily = parse_krx_response(
            body([daily_row()]), KRX_SERVICES["stk_bydd_trd"]
        ).rows[0]
        self.assertEqual("005930", basic.stock_code)
        self.assertEqual("005930", daily.stock_code)
        self.assertEqual(Decimal(100), basic.decimal_fields["PARVAL"])
        self.assertEqual(5969782550, basic.integer_fields["LIST_SHRS"])
        self.assertEqual(Decimal(70500), daily.decimal_fields["TDD_CLSPRC"])
        self.assertEqual(12345678, daily.integer_fields["ACC_TRDVOL"])
        self.assertEqual("70,500", daily.raw_fields["TDD_CLSPRC"])

    def test_source_stock_code_contract(self) -> None:
        service = KRX_SERVICES["stk_isu_base_info"]
        for code in ("123456", "005930", "12345A", "1234AB"):
            with self.subTest(code=code):
                parsed = parse_krx_response(
                    body([basic_row() | {"ISU_SRT_CD": code}]), service
                ).rows[0]
                self.assertEqual(code, parsed.stock_code)

        for code in ("12345", "1234567", "12345-"):
            with (
                self.subTest(code=code),
                self.assertRaises(KrxSchemaError),
            ):
                parse_krx_response(body([basic_row() | {"ISU_SRT_CD": code}]), service)

        with self.assertRaises(KrxSchemaError):
            parse_krx_response(body([basic_row() | {"ISU_SRT_CD": 5930}]), service)

    def test_par_value_typed_contract(self) -> None:
        service = KRX_SERVICES["stk_isu_base_info"]
        for source, expected in (
            ("5000", Decimal(5000)),
            (".5", Decimal("0.5")),
            (".25", Decimal("0.25")),
        ):
            with self.subTest(source=source):
                parsed = parse_krx_response(
                    body([basic_row() | {"PARVAL": source}]), service
                ).rows[0]
                self.assertEqual(ParValueKind.NUMERIC, parsed.par_value.kind)
                self.assertEqual(expected, parsed.par_value.value)
                self.assertEqual(expected, parsed.decimal_fields["PARVAL"])
                self.assertEqual(source, parsed.raw_fields["PARVAL"])

        no_par = parse_krx_response(
            body([basic_row() | {"PARVAL": "무액면"}]), service
        ).rows[0]
        self.assertEqual(ParValueKind.NO_PAR_VALUE, no_par.par_value.kind)
        self.assertIsNone(no_par.par_value.value)
        self.assertNotIn("PARVAL", no_par.decimal_fields)
        self.assertEqual("무액면", no_par.raw_fields["PARVAL"])

        with self.assertRaises(KrxSchemaError):
            parse_krx_response(body([basic_row() | {"PARVAL": "unknown"}]), service)

    def test_zero_daily_values_are_valid_and_raw_text_is_preserved(self) -> None:
        source = daily_row() | {
            "TDD_CLSPRC": "0",
            "TDD_OPNPRC": "0",
            "TDD_HGPRC": "0",
            "TDD_LWPRC": "0",
            "ACC_TRDVOL": "0",
        }
        parsed = parse_krx_response(body([source]), KRX_SERVICES["stk_bydd_trd"]).rows[
            0
        ]
        self.assertEqual(Decimal(0), parsed.decimal_fields["TDD_OPNPRC"])
        self.assertEqual(0, parsed.integer_fields["ACC_TRDVOL"])
        self.assertEqual("0", parsed.raw_fields["TDD_OPNPRC"])
        self.assertEqual("0", parsed.raw_fields["ACC_TRDVOL"])

    def test_empty_response_is_valid_and_distinct(self) -> None:
        parsed = parse_krx_response(body([]), KRX_SERVICES["stk_isu_base_info"])
        self.assertEqual(0, parsed.row_count)

    def test_malformed_missing_root_wrong_type_and_missing_field_fail(self) -> None:
        service = KRX_SERVICES["stk_isu_base_info"]
        invalid = (
            b"not-json",
            b"{}",
            b'{"OutBlock_1":{}}',
            body([{"ISU_SRT_CD": "005930"}]),
        )
        for response_body in invalid:
            with (
                self.subTest(response_body=response_body),
                self.assertRaises(KrxSchemaError),
            ):
                parse_krx_response(response_body, service)


class KrxRawStoreAndManifestTests(unittest.TestCase):
    def result(self, response_body: bytes) -> TransportResult:
        return TransportResult(
            RequestIdentity("stk_isu_base_info", "KOSPI", "20260828"),
            200,
            response_body,
            "2026-08-31T01:02:03Z",
        )

    def test_same_identity_same_bytes_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableRawStore(Path(directory))
            service = KRX_SERVICES["stk_isu_base_info"]
            response_body = body([basic_row()])
            first = store.store(service, self.result(response_body))
            second = store.store(service, self.result(response_body))
            self.assertEqual(ArtifactDisposition.CREATED, first.disposition)
            self.assertEqual(ArtifactDisposition.IDEMPOTENT, second.disposition)
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(response_body, Path(first.raw_file_path).read_bytes())

    def test_conflict_preserves_primary_and_uses_deterministic_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableRawStore(Path(directory))
            service = KRX_SERVICES["stk_isu_base_info"]
            original = body([basic_row()])
            changed_row = basic_row() | {"ISU_NM": "변경된이름"}
            conflict_body = body([changed_row])
            first = store.store(service, self.result(original))
            conflict = store.store(service, self.result(conflict_body))
            repeated = store.store(service, self.result(conflict_body))
            self.assertEqual(ArtifactDisposition.CONFLICT, conflict.disposition)
            self.assertEqual(conflict.raw_file_path, repeated.raw_file_path)
            self.assertEqual(original, Path(first.raw_file_path).read_bytes())
            self.assertEqual(conflict_body, Path(conflict.raw_file_path).read_bytes())

    def test_collector_manifest_is_append_only_and_secret_free(self) -> None:
        service = KRX_SERVICES["stk_isu_base_info"]
        provider = EnvironmentAuthKeyProvider(
            env_path=Path("private.env"),
            environ={"KRX_AUTH_KEY": SYNTHETIC_SECRET},
        )

        def opener(_request, *, timeout):
            self.assertEqual(30.0, timeout)
            return FakeResponse(body([]))

        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableRawStore(Path(directory))
            event = collect_one(
                service,
                bas_dd="20260828",
                auth_key_provider=provider,
                transport=KrxTransport(opener=opener, clock=lambda: AT),
                store=store,
            )
            collect_one(
                service,
                bas_dd="20260828",
                auth_key_provider=provider,
                transport=KrxTransport(opener=opener, clock=lambda: AT),
                store=store,
            )
            self.assertEqual(ManifestStatus.EMPTY_RESPONSE, event.status)
            manifest = Path(directory) / "manifest" / "requests.jsonl"
            content = manifest.read_text(encoding="utf-8")
            self.assertEqual(2, len(content.splitlines()))
            self.assertNotIn(SYNTHETIC_SECRET, content)
            self.assertNotIn("private.env", content)
            self.assertNotIn("AUTH_KEY", content)

    def test_schema_error_keeps_raw_bytes_and_records_distinct_status(self) -> None:
        service = KRX_SERVICES["stk_isu_base_info"]

        def opener(_request, *, timeout):
            return FakeResponse(b"malformed")

        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableRawStore(Path(directory))
            event = collect_one(
                service,
                bas_dd="20260828",
                auth_key_provider=EnvironmentAuthKeyProvider(
                    env_path=None, environ={"KRX_AUTH_KEY": SYNTHETIC_SECRET}
                ),
                transport=KrxTransport(opener=opener, clock=lambda: AT),
                store=store,
            )
            self.assertEqual(ManifestStatus.SCHEMA_ERROR, event.status)
            self.assertEqual(b"malformed", Path(event.raw_file_path).read_bytes())

    def test_http_failure_is_manifested_without_raw_artifact(self) -> None:
        service = KRX_SERVICES["stk_isu_base_info"]

        def opener(_request, *, timeout):
            raise URLError(f"transport details {SYNTHETIC_SECRET} {timeout}")

        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableRawStore(Path(directory))
            event = collect_one(
                service,
                bas_dd="20260828",
                auth_key_provider=EnvironmentAuthKeyProvider(
                    env_path=None, environ={"KRX_AUTH_KEY": SYNTHETIC_SECRET}
                ),
                transport=KrxTransport(opener=opener),
                store=store,
            )
            self.assertEqual(ManifestStatus.HTTP_ERROR, event.status)
            self.assertEqual("", event.raw_file_path)
            manifest = Path(directory) / "manifest" / "requests.jsonl"
            self.assertNotIn(SYNTHETIC_SECRET, manifest.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
