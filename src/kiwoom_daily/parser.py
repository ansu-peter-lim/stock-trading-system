"""Fail-safe parser for byte-preserved Kiwoom ``ka10081`` responses."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.backtest_engine.models import Ohlcv
from src.backtest_engine.validation import MarketDataValidationError, validate_ohlcv

from .models import (
    DailyCollectionRequest,
    DailyPipelineIssue,
    KiwoomDailyValidationError,
    ParsedDailyRow,
    require_sha256,
)

DAILY_LIST_KEY = "stk_dt_pole_chart_qry"
SOURCE_FIELDS = ("dt", "open_pric", "high_pric", "low_pric", "cur_prc", "trde_qty")
UNSIGNED_ASCII_INTEGER = re.compile(r"[0-9]+", flags=re.ASCII)


@dataclass(frozen=True, slots=True)
class ParsedDailyPage:
    stock_code: str
    rows: tuple[ParsedDailyRow, ...]


def parse_daily_page(
    raw_bytes: bytes,
    request: DailyCollectionRequest,
    *,
    source_page: int,
    artifact_sha256: str,
) -> ParsedDailyPage:
    """Parse only the unsigned-string format observed in the R4 proof."""

    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes must be bytes")
    if source_page < 1:
        raise ValueError("source_page must be one-based")
    require_sha256(artifact_sha256)
    try:
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.SCHEMA_ERROR,
            "ka10081 response is not a UTF-8 JSON object",
        ) from exc
    if not isinstance(payload, dict):
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.SCHEMA_ERROR,
            "ka10081 response root must be an object",
        )
    if payload.get("return_code") != 0:
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.API_ERROR,
            "ka10081 response return_code is not zero",
        )
    response_code = payload.get("stk_cd")
    if response_code != request.stock_code:
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.STOCK_MISMATCH,
            "ka10081 response stock code differs from the request",
        )
    source_rows = payload.get(DAILY_LIST_KEY)
    if not isinstance(source_rows, list):
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.SCHEMA_ERROR,
            "ka10081 Daily row collection must be a list",
        )

    parsed: list[ParsedDailyRow] = []
    seen_dates: set[date] = set()
    for source_row_index, source_row in enumerate(source_rows):
        if not isinstance(source_row, dict) or any(
            field not in source_row for field in SOURCE_FIELDS
        ):
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.MALFORMED_ROW,
                "ka10081 Daily row is not an object with all required fields",
            )
        trade_date = _parse_date(source_row["dt"])
        if trade_date in seen_dates:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.DUPLICATE_DAILY_DATE,
                "duplicate trade date within one ka10081 page",
            )
        open_price = _parse_price(source_row["open_pric"], "open_pric")
        high_price = _parse_price(source_row["high_pric"], "high_pric")
        low_price = _parse_price(source_row["low_pric"], "low_pric")
        close_price = _parse_price(source_row["cur_prc"], "cur_prc")
        volume = _parse_volume(source_row["trde_qty"])
        ohlcv = Ohlcv(open_price, high_price, low_price, close_price, volume)
        try:
            validate_ohlcv(ohlcv, request.price_basis.value.lower(), "ka10081 row")
        except MarketDataValidationError as exc:
            raise KiwoomDailyValidationError(
                DailyPipelineIssue.INVALID_DAILY_OHLCV,
                "ka10081 row violates the positive OHLCV contract",
            ) from exc
        parsed.append(
            ParsedDailyRow(
                stock_code=request.stock_code,
                trade_date=trade_date,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                price_basis=request.price_basis,
                source_page=source_page,
                source_row_index=source_row_index,
                artifact_sha256=artifact_sha256,
            )
        )
        seen_dates.add(trade_date)
    return ParsedDailyPage(request.stock_code, tuple(parsed))


def _parse_date(value: object) -> date:
    if (
        not isinstance(value, str)
        or len(value) != 8
        or not value.isascii()
        or not value.isdigit()
    ):
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.MALFORMED_ROW,
            "dt must be an eight-digit ASCII YYYYMMDD string",
        )
    try:
        return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
    except ValueError as exc:
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.MALFORMED_ROW,
            "dt must be a valid calendar date",
        ) from exc


def _parse_price(value: object, field_name: str) -> Decimal:
    text = _require_unsigned_text(value, field_name)
    parsed = Decimal(text)
    if parsed <= 0:
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.INVALID_DAILY_OHLCV,
            f"{field_name} must be positive",
        )
    return parsed


def _parse_volume(value: object) -> int:
    text = _require_unsigned_text(value, "trde_qty")
    return int(text)


def _require_unsigned_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or UNSIGNED_ASCII_INTEGER.fullmatch(value) is None:
        raise KiwoomDailyValidationError(
            DailyPipelineIssue.MALFORMED_NUMERIC,
            f"{field_name} must use the observed unsigned ASCII integer format",
        )
    return value
