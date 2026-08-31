"""Schema validation separated from byte-exact KRX transport and storage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType

from .services import KrxServiceDefinition


class KrxSchemaError(ValueError):
    """A KRX response does not satisfy the selected service contract."""


class ParValueKind(str, Enum):
    NUMERIC = "NUMERIC"
    NO_PAR_VALUE = "NO_PAR_VALUE"


@dataclass(frozen=True, slots=True)
class ParValue:
    kind: ParValueKind
    value: Decimal | None


@dataclass(frozen=True, slots=True)
class KrxParsedRow:
    stock_code: str
    raw_fields: Mapping[str, str]
    decimal_fields: Mapping[str, Decimal]
    integer_fields: Mapping[str, int]
    par_value: ParValue | None


@dataclass(frozen=True, slots=True)
class KrxParsedResponse:
    rows: tuple[KrxParsedRow, ...]

    @property
    def row_count(self) -> int:
        return len(self.rows)


def parse_response_root(
    raw_bytes: bytes, expected_root: str
) -> list[dict[str, object]]:
    try:
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KrxSchemaError("KRX response is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise KrxSchemaError("KRX response root must be an object")
    if expected_root not in payload:
        raise KrxSchemaError(f"KRX response is missing expected root {expected_root}")
    rows = payload[expected_root]
    if not isinstance(rows, list):
        raise KrxSchemaError(f"KRX response root {expected_root} must be a list")
    if not all(isinstance(row, dict) for row in rows):
        raise KrxSchemaError("KRX response contains a non-object row")
    return rows


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise KrxSchemaError(f"KRX field {field} must be a string")
    return value


def _numeric_text(value: str, field: str) -> str:
    normalized = value.replace(",", "").strip()
    if not normalized:
        raise KrxSchemaError(f"KRX numeric field {field} must not be blank")
    return normalized


def parse_krx_response(
    raw_bytes: bytes, service: KrxServiceDefinition
) -> KrxParsedResponse:
    parsed_rows: list[KrxParsedRow] = []
    for index, source in enumerate(
        parse_response_root(raw_bytes, service.expected_root), start=1
    ):
        missing = [
            field for field in service.required_response_fields if field not in source
        ]
        if missing:
            raise KrxSchemaError(
                f"KRX row {index} is missing required field(s): {', '.join(missing)}"
            )
        raw = {
            field: _text(source[field], field)
            for field in service.required_response_fields
        }
        stock_code = raw[service.stock_code_field]
        if len(stock_code) != 6 or not stock_code.isascii() or not stock_code.isalnum():
            raise KrxSchemaError(
                "KRX source stock code must be six ASCII alphanumeric characters"
            )
        decimals: dict[str, Decimal] = {}
        integers: dict[str, int] = {}
        par_value: ParValue | None = None
        try:
            for field in service.decimal_fields:
                if field == "PARVAL":
                    if raw[field] == "무액면":
                        par_value = ParValue(ParValueKind.NO_PAR_VALUE, None)
                    else:
                        value = Decimal(_numeric_text(raw[field], field))
                        par_value = ParValue(ParValueKind.NUMERIC, value)
                        decimals[field] = value
                    continue
                decimals[field] = Decimal(_numeric_text(raw[field], field))
            for field in service.integer_fields:
                normalized = _numeric_text(raw[field], field)
                if any(char in normalized for char in ".eE"):
                    raise ValueError
                integers[field] = int(normalized)
        except (InvalidOperation, ValueError) as exc:
            raise KrxSchemaError(
                "KRX response contains an invalid numeric value"
            ) from exc
        parsed_rows.append(
            KrxParsedRow(
                stock_code,
                MappingProxyType(raw),
                MappingProxyType(decimals),
                MappingProxyType(integers),
                par_value,
            )
        )
    return KrxParsedResponse(tuple(parsed_rows))
