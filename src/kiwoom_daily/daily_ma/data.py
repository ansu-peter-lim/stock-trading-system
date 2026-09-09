"""Local-only canonical DailyBar boundary for new Daily MA research."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from src.backtest_engine.models import DailyBar, Ohlcv
from src.backtest_engine.validation import validate_daily_bars

from ..models import DailyCollectionRequest, PriceBasis, require_stock_code
from ..parser import parse_daily_page

DEFAULT_RAW_ROOT = Path("data/raw/kiwoom/daily")


def load_local_daily_bars(
    stock_code: str,
    required_start: date,
    artifact_base_date: date,
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
) -> tuple[DailyBar, ...]:
    """Read existing RAW/ADJUSTED ka10081 pages without any API fallback."""

    require_stock_code(stock_code)
    if required_start > artifact_base_date:
        raise ValueError("required_start must not exceed artifact_base_date")
    rows_by_basis = {
        basis: _load_basis(
            stock_code, required_start, artifact_base_date, basis, raw_root
        )
        for basis in PriceBasis
    }
    raw, adjusted = rows_by_basis[PriceBasis.RAW], rows_by_basis[PriceBasis.ADJUSTED]
    if set(raw) != set(adjusted):
        raise ValueError("local Daily RAW/ADJUSTED dates differ")
    bars = tuple(
        DailyBar(
            stock_code=stock_code,
            trade_date=day,
            raw=Ohlcv(
                raw[day].open,
                raw[day].high,
                raw[day].low,
                raw[day].close,
                raw[day].volume,
            ),
            signal=Ohlcv(
                adjusted[day].open,
                adjusted[day].high,
                adjusted[day].low,
                adjusted[day].close,
                adjusted[day].volume,
            ),
        )
        for day in sorted(raw)
    )
    validate_daily_bars(bars)
    return bars


def slice_daily_bars(
    bars: tuple[DailyBar, ...], start_date: date, end_date: date
) -> tuple[DailyBar, ...]:
    """Return a deterministic inclusive date slice; never synthesize sessions."""

    if start_date > end_date:
        raise ValueError("start_date must not exceed end_date")
    canonical = tuple(sorted(bars, key=lambda bar: (bar.stock_code, bar.trade_date)))
    validate_daily_bars(canonical)
    return tuple(bar for bar in canonical if start_date <= bar.trade_date <= end_date)


def _load_basis(
    stock_code: str,
    required_start: date,
    artifact_base_date: date,
    basis: PriceBasis,
    raw_root: Path,
) -> dict[date, object]:
    request = DailyCollectionRequest(
        stock_code, required_start, artifact_base_date, basis
    )
    directory = raw_root / stock_code / basis.value.lower() / request.base_date
    files = sorted(directory.glob("page-*.json"))
    if not files:
        raise FileNotFoundError(
            f"missing local Daily artifacts for {stock_code}/{basis.value}"
        )
    rows: dict[date, object] = {}
    for page_index, path in enumerate(files, 1):
        raw_bytes = path.read_bytes()
        parsed = parse_daily_page(
            raw_bytes,
            request,
            source_page=page_index,
            artifact_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        )
        for row in parsed.rows:
            if not required_start <= row.trade_date <= artifact_base_date:
                continue
            if row.trade_date in rows:
                raise ValueError("duplicate local Daily trade date")
            rows[row.trade_date] = row
    return rows
