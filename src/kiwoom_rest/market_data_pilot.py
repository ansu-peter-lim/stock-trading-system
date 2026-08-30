"""Read-only Kiwoom demo market-data pilot for daily and five-minute bars.

The pilot keeps all responses in memory, requests at most three pages per TR,
and prints only aggregate/schema diagnostics.  It never calls an order API.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .auth import (
    DEMO_BASE_URL,
    ConfigurationError,
    DemoConfig,
    KiwoomApiError,
    TokenInfo,
    issue_demo_token,
    load_demo_config,
)


LOGGER = logging.getLogger(__name__)
CHART_PATH = "/api/dostk/chart"
DAILY_API_ID = "ka10081"
MINUTE_API_ID = "ka10080"
DAILY_LIST_KEY = "stk_dt_pole_chart_qry"
MINUTE_LIST_KEY = "stk_min_pole_chart_qry"
DEFAULT_STOCK_CODE = "005930"
MAX_PAGES = 3
PAGE_DELAY_SECONDS = 1.1

OHLCV_FIELDS = ("open_pric", "high_pric", "low_pric", "cur_prc", "trde_qty")
SIGNED_INTEGER = re.compile(r"^[+-]?\d+$")


@dataclass(frozen=True)
class ChartHttpResult:
    status: int
    body: bytes
    headers: Mapping[str, str]


@dataclass(frozen=True)
class PageInfo:
    http_status: int
    row_count: int
    cont_yn: str
    has_next_key: bool


@dataclass(frozen=True)
class ChartDiagnostics:
    api_id: str
    endpoint: str
    page_http_statuses: list[int]
    api_succeeded: bool
    pages_requested: int
    page_row_counts: list[int]
    total_rows: int
    unique_timestamps: int
    duplicate_timestamps: int
    timestamp_field: str
    earliest_timestamp: str | None
    latest_timestamp: str | None
    timestamp_format: str
    sort_direction: str
    response_top_level_fields: list[str]
    row_fields: list[str]
    ohlcv_fields: list[str]
    numeric_conversion_failures: int
    signed_price_value_count: int
    negative_price_value_count: int
    empty_or_null_value_count: int
    malformed_row_count: int
    page_boundary_duplicate_count: int
    continuation_seen: bool
    last_page_cont_yn: str
    outside_regular_session_count: int | None


@dataclass(frozen=True)
class PilotDiagnostics:
    environment: str
    stock_code: str
    token_http_status: int
    token_type: str
    token_expires_at: str
    daily: ChartDiagnostics
    five_minute: ChartDiagnostics


ChartTransport = Callable[[str, Mapping[str, str], Mapping[str, str]], ChartHttpResult]


def _chart_transport(
    url: str, headers: Mapping[str, str], body: Mapping[str, str],
) -> ChartHttpResult:
    request = Request(
        url,
        data=json.dumps(dict(body), separators=(",", ":")).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return ChartHttpResult(
                response.status, response.read(),
                {key.lower(): value for key, value in response.headers.items()},
            )
    except HTTPError as exc:
        raise KiwoomApiError(f"Chart request returned HTTP status {exc.code}") from exc
    except URLError as exc:
        raise KiwoomApiError("Chart API connection failed") from exc


def parse_pagination_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    return lowered.get("cont-yn", ""), lowered.get("next-key", "")


def parse_chart_rows(payload: Mapping[str, object], list_key: str) -> tuple[list[dict[str, object]], int]:
    raw_rows = payload.get(list_key, [])
    if raw_rows is None:
        return [], 0
    if not isinstance(raw_rows, list):
        return [], 1
    rows: list[dict[str, object]] = []
    malformed = 0
    for row in raw_rows:
        if isinstance(row, dict):
            rows.append(row)
        else:
            malformed += 1
    return rows, malformed


def _decode_payload(result: ChartHttpResult) -> dict[str, object]:
    if not 200 <= result.status < 300:
        raise KiwoomApiError(f"Chart request returned HTTP status {result.status}")
    try:
        payload = json.loads(result.body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KiwoomApiError("Chart request returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise KiwoomApiError("Chart request returned an unexpected JSON shape")
    return payload


def _sort_direction(values: list[str]) -> str:
    if len(values) < 2:
        return "undetermined"
    if all(left >= right for left, right in zip(values, values[1:])):
        return "descending"
    if all(left <= right for left, right in zip(values, values[1:])):
        return "ascending"
    return "mixed"


def _diagnose(
    api_id: str,
    timestamp_field: str,
    timestamp_format: str,
    pages: list[tuple[ChartHttpResult, dict[str, object], list[dict[str, object]], int]],
) -> ChartDiagnostics:
    rows = [row for _, _, page_rows, _ in pages for row in page_rows]
    timestamps = [str(row.get(timestamp_field, "")) for row in rows if row.get(timestamp_field) not in (None, "")]
    fields = sorted({str(key) for row in rows for key in row})
    top_fields = sorted({str(key) for _, payload, _, _ in pages for key in payload})
    numeric_failures = signed_count = negative_count = empty_count = 0
    for row in rows:
        empty_count += sum(value is None or value == "" for value in row.values())
        for field in OHLCV_FIELDS:
            value = row.get(field)
            if value in (None, ""):
                continue
            text = str(value).strip()
            if not SIGNED_INTEGER.fullmatch(text):
                numeric_failures += 1
            if field != "trde_qty" and text.startswith(("-", "+")):
                signed_count += 1
            if field != "trde_qty" and text.startswith("-"):
                negative_count += 1

    page_boundary_duplicates = 0
    for before, after in zip(pages, pages[1:]):
        before_values = {str(row.get(timestamp_field)) for row in before[2] if row.get(timestamp_field)}
        after_values = {str(row.get(timestamp_field)) for row in after[2] if row.get(timestamp_field)}
        page_boundary_duplicates += len(before_values & after_values)

    cont_values = [parse_pagination_headers(result.headers)[0] for result, _, _, _ in pages]
    outside_regular: int | None = None
    if timestamp_field == "cntr_tm":
        outside_regular = sum(
            1 for stamp in timestamps
            if len(stamp) >= 14 and not "090000" <= stamp[-6:] <= "153000"
        )
    return ChartDiagnostics(
        api_id=api_id,
        endpoint=DEMO_BASE_URL + CHART_PATH,
        page_http_statuses=[result.status for result, _, _, _ in pages],
        api_succeeded=bool(pages) and all(
            200 <= result.status < 300 and payload.get("return_code") in (None, 0)
            for result, payload, _, _ in pages
        ),
        pages_requested=len(pages),
        page_row_counts=[len(page_rows) for _, _, page_rows, _ in pages],
        total_rows=len(rows),
        unique_timestamps=len(set(timestamps)),
        duplicate_timestamps=len(timestamps) - len(set(timestamps)),
        timestamp_field=timestamp_field,
        earliest_timestamp=min(timestamps) if timestamps else None,
        latest_timestamp=max(timestamps) if timestamps else None,
        timestamp_format=timestamp_format,
        sort_direction=_sort_direction(timestamps),
        response_top_level_fields=top_fields,
        row_fields=fields,
        ohlcv_fields=[field for field in OHLCV_FIELDS if field in fields],
        numeric_conversion_failures=numeric_failures,
        signed_price_value_count=signed_count,
        negative_price_value_count=negative_count,
        empty_or_null_value_count=empty_count,
        malformed_row_count=sum(item[3] for item in pages),
        page_boundary_duplicate_count=page_boundary_duplicates,
        continuation_seen=any(value == "Y" for value in cont_values),
        last_page_cont_yn=cont_values[-1] if cont_values else "",
        outside_regular_session_count=outside_regular,
    )


def fetch_chart_pages(
    config: DemoConfig,
    token: TokenInfo,
    api_id: str,
    body: Mapping[str, str],
    list_key: str,
    timestamp_field: str,
    timestamp_format: str,
    transport: ChartTransport = _chart_transport,
    max_pages: int = MAX_PAGES,
    page_delay: float = PAGE_DELAY_SECONDS,
) -> ChartDiagnostics:
    if config.environment != "demo" or config.base_url != DEMO_BASE_URL:
        raise ConfigurationError("Only the fixed Kiwoom demo endpoint is allowed")
    if not 1 <= max_pages <= MAX_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_PAGES}")

    pages: list[tuple[ChartHttpResult, dict[str, object], list[dict[str, object]], int]] = []
    cont_yn = next_key = ""
    for page_number in range(max_pages):
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"{token.token_type.title()} {token.token}",
            "api-id": api_id,
        }
        if page_number:
            headers["cont-yn"] = cont_yn
            headers["next-key"] = next_key
        result = transport(config.base_url + CHART_PATH, headers, body)
        payload = _decode_payload(result)
        rows, malformed = parse_chart_rows(payload, list_key)
        pages.append((result, payload, rows, malformed))
        if payload.get("return_code") not in (None, 0):
            break
        cont_yn, next_key = parse_pagination_headers(result.headers)
        if cont_yn != "Y" or not next_key or page_number + 1 >= max_pages:
            break
        if page_delay:
            time.sleep(page_delay)
    return _diagnose(api_id, timestamp_field, timestamp_format, pages)


def run_pilot(
    stock_code: str = DEFAULT_STOCK_CODE,
    base_date: str | None = None,
    max_pages: int = MAX_PAGES,
) -> PilotDiagnostics:
    if not re.fullmatch(r"\d{6}", stock_code):
        raise ValueError("stock_code must be a six-digit string")
    base_date = base_date or date.today().strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", base_date):
        raise ValueError("base_date must use YYYYMMDD")
    config = load_demo_config()
    token = issue_demo_token(config)
    daily = fetch_chart_pages(
        config, token, DAILY_API_ID,
        {"stk_cd": stock_code, "base_dt": base_date, "upd_stkpc_tp": "1"},
        DAILY_LIST_KEY, "dt", "YYYYMMDD", max_pages=max_pages,
    )
    five_minute = fetch_chart_pages(
        config, token, MINUTE_API_ID,
        {"stk_cd": stock_code, "tic_scope": "5", "upd_stkpc_tp": "1", "base_dt": base_date},
        MINUTE_LIST_KEY, "cntr_tm", "YYYYMMDDHHMMSS", max_pages=max_pages,
    )
    LOGGER.info("Read-only market-data pilot completed for one stock")
    return PilotDiagnostics(
        config.environment, stock_code, token.http_status, token.token_type,
        token.expires_at, daily, five_minute,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Kiwoom demo daily/5-minute read-only pilot")
    parser.add_argument("--stock-code", default=DEFAULT_STOCK_CODE)
    parser.add_argument("--base-date", help="YYYYMMDD; defaults to today")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, choices=range(1, MAX_PAGES + 1))
    args = parser.parse_args()
    result = run_pilot(args.stock_code, args.base_date, args.max_pages)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
