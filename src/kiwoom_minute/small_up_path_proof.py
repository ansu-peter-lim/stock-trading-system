"""Explicit three-stock network entry point for the experimental sequence proof."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.backtest_engine.validation import validate_daily_bars
from src.kiwoom_daily import DailyCollectionRequest, PriceBasis, parse_daily_page
from src.kiwoom_rest.auth import DEMO_BASE_URL, issue_demo_token, load_demo_config

from .pipeline import (
    ASSUMPTION_ID,
    CollectedMinuteSeries,
    KiwoomMinuteStore,
    MinuteCollectionRequest,
    MinutePageProvenance,
    MinutePriceBasis,
    align_source_bars,
    collect_minute_series,
    parse_minute_page,
)
from .proof import run_up_path_sequence_proof

STOCK_CODES = ("005930", "000660", "035720")
MINUTE_REQUIRED_START = date(2025, 9, 1)
RESEARCH_START = date(2025, 9, 2)
RESEARCH_END = date(2026, 8, 28)
DAILY_REQUIRED_START = date(2023, 6, 1)
DAILY_REQUIRED_END = date(2026, 8, 31)
INITIAL_CAPITAL = Decimal(100000000)
STOCK_FULL_WEIGHT = Decimal("0.10")
PRIMARY_LATEST_LABEL = time(15, 30)
SENSITIVITY_LATEST_LABEL = time(15, 15)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--page-delay", type=float, default=1.1)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/kiwoom/small_up_path_sequence_proof.json"),
    )
    args = parser.parse_args()
    result = execute_network_proof(args.max_pages, args.page_delay)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, default=_json_default, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "assumption_id": result["assumption_id"],
                "http_requests": result["http_requests"],
                "output": args.output.as_posix(),
                "stocks": {
                    code: {
                        "primary_completed_trades": result["stocks"][code]["primary"][
                            "counts"
                        ]["exit_fills"],
                        "primary_cumulative_return_pct": result["stocks"][code][
                            "primary"
                        ]["metrics"]["cumulative_return_pct"],
                    }
                    for code in STOCK_CODES
                },
            },
            default=_json_default,
            ensure_ascii=False,
            indent=2,
        )
    )


def execute_network_proof(max_pages: int, page_delay: float) -> dict[str, Any]:
    config = load_demo_config()
    if config.environment != "demo" or config.base_url != DEMO_BASE_URL:
        raise RuntimeError("Small proof permits the fixed demo environment only")
    token = issue_demo_token(config)
    store = KiwoomMinuteStore()
    stock_results: dict[str, Any] = {}
    chart_requests = 0
    for stock_code in STOCK_CODES:
        daily_bars = _load_existing_daily_bars(stock_code)
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in daily_bars)
        raw, raw_requests = _load_or_collect(
            MinuteCollectionRequest(
                stock_code, MINUTE_REQUIRED_START, RESEARCH_END, MinutePriceBasis.RAW
            ),
            config=config,
            token=token,
            store=store,
            max_pages=max_pages,
            page_delay=page_delay,
        )
        adjusted, adjusted_requests = _load_or_collect(
            MinuteCollectionRequest(
                stock_code,
                MINUTE_REQUIRED_START,
                RESEARCH_END,
                MinutePriceBasis.ADJUSTED,
            ),
            config=config,
            token=token,
            store=store,
            max_pages=max_pages,
            page_delay=page_delay,
        )
        chart_requests += raw_requests + adjusted_requests
        primary_bars, primary_excluded = align_source_bars(
            raw, adjusted, latest_label_time=PRIMARY_LATEST_LABEL
        )
        sensitivity_bars, sensitivity_excluded = align_source_bars(
            raw, adjusted, latest_label_time=SENSITIVITY_LATEST_LABEL
        )
        primary = run_up_path_sequence_proof(
            daily_bars=daily_bars,
            source_bars=primary_bars,
            calendar=calendar,
            research_start=RESEARCH_START,
            research_end=RESEARCH_END,
            stock_full_weight=STOCK_FULL_WEIGHT,
            initial_capital=INITIAL_CAPITAL,
        )
        sensitivity = run_up_path_sequence_proof(
            daily_bars=daily_bars,
            source_bars=sensitivity_bars,
            calendar=calendar,
            research_start=RESEARCH_START,
            research_end=RESEARCH_END,
            stock_full_weight=STOCK_FULL_WEIGHT,
            initial_capital=INITIAL_CAPITAL,
        )
        stock_results[stock_code] = {
            "coverage": {
                "raw_pages": len(raw.pages),
                "adjusted_pages": len(adjusted.pages),
                "raw_rows": len(raw.rows),
                "adjusted_rows": len(adjusted.rows),
                "first_source_label": raw.rows[0].source_label,
                "last_source_label": raw.rows[-1].source_label,
                "primary_rows": len(primary_bars),
                "sensitivity_rows": len(sensitivity_bars),
                "primary_excluded_rows": primary_excluded,
                "sensitivity_excluded_rows": sensitivity_excluded,
                "raw_artifact_set_sha256": raw.artifact_set_sha256,
                "adjusted_artifact_set_sha256": adjusted.artifact_set_sha256,
            },
            "sign_normalization": {
                "raw": _sign_counts(raw.rows),
                "adjusted": _sign_counts(adjusted.rows),
            },
            "source_sequence": _sequence_summary(primary_bars),
            "primary": primary,
            "sensitivity_1515_cutoff": sensitivity,
            "primary_best_two": _ranked_trades(primary, reverse=True),
            "primary_worst_two": _ranked_trades(primary, reverse=False),
        }
    return {
        "proof_version": "SMALL_UP_PATH_SEQUENCE_PROOF_V1",
        "assumption_id": ASSUMPTION_ID,
        "official_timestamp_semantics": "UNRESOLVED",
        "source_label_policy": {
            "primary": "09:00<=cntr_tm.time<=15:30; 15:35 excluded",
            "sensitivity": "09:00<=cntr_tm.time<=15:15; 15:30/15:35 excluded",
            "synthetic_bars": False,
            "interpolation": False,
            "timestamp_shift": False,
        },
        "research_period": {
            "minute_required_start": MINUTE_REQUIRED_START,
            "research_start": RESEARCH_START,
            "research_end": RESEARCH_END,
            "daily_required_start": DAILY_REQUIRED_START,
            "daily_required_end": DAILY_REQUIRED_END,
        },
        "accounting": {
            "cost_profile": "ZERO_COST",
            "initial_capital": INITIAL_CAPITAL,
            "stock_full_weight": STOCK_FULL_WEIGHT,
            "core_fraction_of_full": Decimal("0.90"),
        },
        "http_requests": {
            "oauth_token": 1,
            "ka10080": chart_requests,
            "total": chart_requests + 1,
        },
        "stocks": stock_results,
    }


def _load_existing_daily_bars(stock_code: str) -> tuple[DailyBar, ...]:
    basis_rows: dict[PriceBasis, dict[date, Any]] = {}
    for basis in (PriceBasis.RAW, PriceBasis.ADJUSTED):
        request = DailyCollectionRequest(
            stock_code, DAILY_REQUIRED_START, DAILY_REQUIRED_END, basis
        )
        directory = (
            Path("data/raw/kiwoom/daily")
            / stock_code
            / basis.value.lower()
            / request.base_date
        )
        files = sorted(directory.glob("page-*.json"))
        if not files:
            raise FileNotFoundError(
                f"missing existing Daily artifacts for {stock_code}"
            )
        rows: dict[date, Any] = {}
        for page_index, path in enumerate(files, 1):
            raw = path.read_bytes()
            parsed = parse_daily_page(
                raw,
                request,
                source_page=page_index,
                artifact_sha256=hashlib.sha256(raw).hexdigest(),
            )
            for item in parsed.rows:
                if request.start_date <= item.trade_date <= request.end_date:
                    if item.trade_date in rows:
                        raise ValueError("duplicate existing Daily date")
                    rows[item.trade_date] = item
        basis_rows[basis] = rows
    if set(basis_rows[PriceBasis.RAW]) != set(basis_rows[PriceBasis.ADJUSTED]):
        raise ValueError("existing Daily RAW/ADJUSTED dates differ")
    bars = tuple(
        DailyBar(
            stock_code,
            day,
            _daily_ohlcv(basis_rows[PriceBasis.RAW][day]),
            _daily_ohlcv(basis_rows[PriceBasis.ADJUSTED][day]),
        )
        for day in sorted(basis_rows[PriceBasis.RAW])
    )
    validate_daily_bars(bars)
    return bars


def _load_or_collect(
    request: MinuteCollectionRequest,
    **collector_kwargs: Any,
) -> tuple[CollectedMinuteSeries, int]:
    cached = _load_cached_minute_series(request)
    if cached is not None:
        return cached, 0
    collected = collect_minute_series(request, **collector_kwargs)
    return collected, len(collected.pages)


def _load_cached_minute_series(
    request: MinuteCollectionRequest,
) -> CollectedMinuteSeries | None:
    directory = (
        Path("data/raw/kiwoom/minute")
        / request.stock_code
        / request.price_basis.value.lower()
        / request.base_date
    )
    files = sorted(directory.glob("page-*.json"))
    if not files:
        return None
    rows = []
    pages = []
    for expected_sequence, path in enumerate(files, 1):
        parts = path.name.split("-")
        if len(parts) < 3 or int(parts[1]) != expected_sequence:
            return None
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        parsed = parse_minute_page(
            raw,
            request,
            source_page=expected_sequence,
            artifact_sha256=digest,
        )
        rows.extend(
            row
            for row in parsed.rows
            if request.start_date <= row.trading_date <= request.end_date
        )
        pages.append(
            MinutePageProvenance(
                request.stock_code,
                request.price_basis,
                request.base_date,
                expected_sequence,
                "CACHED_IMMUTABLE_ARTIFACT",
                path.as_posix(),
                digest,
                len(parsed.rows),
                "",
                "",
            )
        )
    if not rows or min(row.trading_date for row in rows) > request.start_date:
        return None
    return CollectedMinuteSeries(request, tuple(rows), tuple(pages))


def _daily_ohlcv(row: Any) -> Ohlcv:
    return Ohlcv(row.open, row.high, row.low, row.close, row.volume)


def _sign_counts(rows: Any) -> dict[str, int]:
    result = Counter()
    for row in rows:
        for value in row.source_price_text:
            result[
                "plus"
                if value.startswith("+")
                else "minus"
                if value.startswith("-")
                else "unsigned"
            ] += 1
    return dict(sorted(result.items()))


def _sequence_summary(bars: Any) -> dict[str, Any]:
    by_date: dict[date, list[Any]] = {}
    for bar in bars:
        by_date.setdefault(bar.trading_date, []).append(bar)
    counts = Counter(len(day_bars) for day_bars in by_date.values())
    return {
        "dates": len(by_date),
        "rows_per_day_distribution": dict(sorted(counts.items())),
        "duplicate_labels": len(bars) - len({bar.source_label for bar in bars}),
        "continuous_sequences": [bar.source_bar_sequence for bar in bars]
        == list(range(len(bars))),
        "first_label": bars[0].source_label,
        "last_label": bars[-1].source_label,
    }


def _ranked_trades(result: dict[str, Any], *, reverse: bool) -> list[dict[str, Any]]:
    trades = sorted(
        result["completed_trades"], key=lambda trade: trade["pnl_pct"], reverse=reverse
    )
    return trades[:2]


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal)):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


if __name__ == "__main__":
    main()
