"""Bounded, read-only probe of Kiwoom demo five-minute chart history.

All rows stay in memory.  Signed price strings are counted but never altered.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from typing import Mapping

from .auth import DEMO_BASE_URL, ConfigurationError, KiwoomApiError, issue_demo_token, load_demo_config
from .market_data_pilot import (
    CHART_PATH,
    MINUTE_API_ID,
    MINUTE_LIST_KEY,
    PAGE_DELAY_SECONDS,
    ChartTransport,
    _chart_transport,
    _decode_payload,
    parse_chart_rows,
    parse_pagination_headers,
)


LOGGER = logging.getLogger(__name__)
DEFAULT_STOCK_CODE = "005930"
DEFAULT_TARGET_DATE = "20230901"
DEFAULT_MAX_PAGES = 30
PRICE_FIELDS = ("cur_prc", "open_pric", "high_pric", "low_pric")
STAMP_PATTERN = re.compile(r"^\d{14}$")


@dataclass(frozen=True)
class MinuteHistoryReport:
    environment: str
    api_id: str
    endpoint: str
    stock_code: str
    target_date: str
    pages_requested: int
    page_http_statuses: list[int]
    page_row_counts: list[int]
    total_rows: int
    newest_timestamp: str | None
    oldest_timestamp: str | None
    stop_reason: str
    actual_api_cutoff_reached: bool
    target_date_reached: bool
    continuation_available_at_stop: bool
    page_boundary_duplicate_count: int
    duplicate_timestamp_count: int
    plus_price_string_count: int
    minus_price_string_count: int
    unsigned_price_string_count: int
    empty_price_string_count: int
    malformed_row_count: int
    cur_price_sign_by_pred_pre_sig: dict[str, int]
    price_sign_semantics: str
    api_error: bool


def _price_sign(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        return "empty"
    if text.startswith("+"):
        return "plus"
    if text.startswith("-"):
        return "minus"
    return "unsigned"


def probe_minute_history(
    stock_code: str = DEFAULT_STOCK_CODE,
    base_date: str | None = None,
    target_date: str = DEFAULT_TARGET_DATE,
    max_pages: int = DEFAULT_MAX_PAGES,
    transport: ChartTransport = _chart_transport,
    page_delay: float = PAGE_DELAY_SECONDS,
) -> MinuteHistoryReport:
    if not re.fullmatch(r"\d{6}", stock_code):
        raise ValueError("stock_code must be a six-digit string")
    base_date = base_date or date.today().strftime("%Y%m%d")
    if not re.fullmatch(r"\d{8}", base_date) or not re.fullmatch(r"\d{8}", target_date):
        raise ValueError("base_date and target_date must use YYYYMMDD")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")

    config = load_demo_config()
    if config.environment != "demo" or config.base_url != DEMO_BASE_URL:
        raise ConfigurationError("Only the fixed Kiwoom demo endpoint is allowed")
    token = issue_demo_token(config)
    body = {
        "stk_cd": stock_code,
        "tic_scope": "5",
        "upd_stkpc_tp": "1",
        "base_dt": base_date,
    }

    all_rows: list[dict[str, object]] = []
    page_timestamp_sets: list[set[str]] = []
    page_statuses: list[int] = []
    page_counts: list[int] = []
    malformed_total = 0
    cont_yn = next_key = ""
    stop_reason = "max_pages"
    api_error = False

    for page_index in range(max_pages):
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"{token.token_type.title()} {token.token}",
            "api-id": MINUTE_API_ID,
        }
        if page_index:
            headers["cont-yn"] = cont_yn
            headers["next-key"] = next_key
        try:
            result = transport(config.base_url + CHART_PATH, headers, body)
            payload = _decode_payload(result)
        except KiwoomApiError:
            stop_reason = "api_error"
            api_error = True
            break
        page_statuses.append(result.status)
        rows, malformed = parse_chart_rows(payload, MINUTE_LIST_KEY)
        malformed_total += malformed
        page_counts.append(len(rows))
        all_rows.extend(rows)
        stamps = {
            str(row.get("cntr_tm")) for row in rows
            if STAMP_PATTERN.fullmatch(str(row.get("cntr_tm", "")))
        }
        page_timestamp_sets.append(stamps)
        oldest_on_page = min(stamps) if stamps else None
        cont_yn, next_key = parse_pagination_headers(result.headers)
        LOGGER.info("page=%d rows=%d oldest_timestamp=%s continuation=%s",
                    page_index + 1, len(rows), oldest_on_page, cont_yn == "Y")

        if payload.get("return_code") not in (None, 0):
            stop_reason = "api_error"
            api_error = True
            break
        if oldest_on_page and oldest_on_page[:8] <= target_date:
            stop_reason = "target_date_reached"
            break
        if cont_yn != "Y":
            stop_reason = "continuation_exhausted"
            break
        if not next_key:
            stop_reason = "next_key_missing"
            break
        if page_index + 1 >= max_pages:
            stop_reason = "max_pages"
            break
        if page_delay:
            time.sleep(page_delay)

    timestamps = [
        str(row.get("cntr_tm")) for row in all_rows
        if STAMP_PATTERN.fullmatch(str(row.get("cntr_tm", "")))
    ]
    boundary_duplicates = sum(
        len(left & right) for left, right in zip(page_timestamp_sets, page_timestamp_sets[1:])
    )
    sign_counts = {"plus": 0, "minus": 0, "unsigned": 0, "empty": 0}
    sign_vs_signal: dict[str, int] = {}
    for row in all_rows:
        for field in PRICE_FIELDS:
            sign_counts[_price_sign(row.get(field))] += 1
        cur_sign = _price_sign(row.get("cur_prc"))
        signal = str(row.get("pred_pre_sig", ""))
        key = f"{cur_sign}|{signal or '<empty>'}"
        sign_vs_signal[key] = sign_vs_signal.get(key, 0) + 1

    cutoff_reached = stop_reason in {"continuation_exhausted", "next_key_missing"}
    target_reached = bool(timestamps) and min(timestamps)[:8] <= target_date
    return MinuteHistoryReport(
        environment=config.environment,
        api_id=MINUTE_API_ID,
        endpoint=config.base_url + CHART_PATH,
        stock_code=stock_code,
        target_date=target_date,
        pages_requested=len(page_counts),
        page_http_statuses=page_statuses,
        page_row_counts=page_counts,
        total_rows=len(all_rows),
        newest_timestamp=max(timestamps) if timestamps else None,
        oldest_timestamp=min(timestamps) if timestamps else None,
        stop_reason=stop_reason,
        actual_api_cutoff_reached=cutoff_reached,
        target_date_reached=target_reached,
        continuation_available_at_stop=cont_yn == "Y" and bool(next_key),
        page_boundary_duplicate_count=boundary_duplicates,
        duplicate_timestamp_count=len(timestamps) - len(set(timestamps)),
        plus_price_string_count=sign_counts["plus"],
        minus_price_string_count=sign_counts["minus"],
        unsigned_price_string_count=sign_counts["unsigned"],
        empty_price_string_count=sign_counts["empty"],
        malformed_row_count=malformed_total,
        cur_price_sign_by_pred_pre_sig=dict(sorted(sign_vs_signal.items())),
        price_sign_semantics="unconfirmed_by_explicit_official_definition",
        api_error=api_error,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded Kiwoom demo five-minute history probe")
    parser.add_argument("--stock-code", default=DEFAULT_STOCK_CODE)
    parser.add_argument("--base-date", help="YYYYMMDD; defaults to today")
    parser.add_argument("--target-date", default=DEFAULT_TARGET_DATE)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    args = parser.parse_args()
    report = probe_minute_history(
        args.stock_code, args.base_date, args.target_date, args.max_pages,
    )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
