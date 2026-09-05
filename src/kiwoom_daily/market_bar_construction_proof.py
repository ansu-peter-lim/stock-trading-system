"""Construct deterministic one-``ACTIVITY_TAU`` market bars.

This module is deliberately below strategy level.  It does not calculate an
indicator, signal, order, fill, or PnL.  A source segment can be apportioned to
two adjacent tau buckets, but its OHLC is never interpolated: provenance marks
the overlap and the source OHLC is retained verbatim in both enclosing bars.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.backtest_engine.validation import KOREA_TZ

from .down_box_daily_execution_proof import _load_stock
from .market_clock_compression_audit_v0_2 import STOCKS
from .market_time_normalization_audit import market_time_series
from .market_time_selective_intraday_decomposition import OUTPUT_PATH as V03_OUTPUT_PATH
from .market_time_selective_intraday_decomposition import _decimal as _v03_decimal
from .market_time_selective_intraday_decomposition import _load_cached_raw_rows

PROOF_VERSION = "MARKET_BAR_CONSTRUCTION_PROOF_V0_1"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_construction_proof_v0_1.json"
)
UNIT_TAU = Decimal(1)
TOLERANCE = Decimal("1e-20")


class MarketBarConstructionError(ValueError):
    """Source tau coordinates cannot form a deterministic proof input."""


@dataclass(frozen=True, slots=True)
class ActivitySegment:
    stock_code: str
    tau_start: Decimal
    tau_end: Decimal
    calendar_start_datetime: datetime
    calendar_end_datetime: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_resolution: str
    source_id: str

    def __post_init__(self) -> None:
        if self.tau_start < 0 or self.tau_end <= self.tau_start:
            raise MarketBarConstructionError("segment tau interval must be positive")
        if self.calendar_end_datetime < self.calendar_start_datetime:
            raise MarketBarConstructionError("segment calendar interval is invalid")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise MarketBarConstructionError("segment prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(
            self.open, self.close
        ):
            raise MarketBarConstructionError("segment OHLC relation is invalid")
        if self.volume < 0:
            raise MarketBarConstructionError("segment volume must be non-negative")


@dataclass(frozen=True, slots=True)
class MarketBar:
    market_bar_id: str
    tau_start: Decimal
    tau_end: Decimal
    calendar_start_datetime: datetime
    calendar_end_datetime: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_resolution: str
    source_segments: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if self.tau_end - self.tau_start != UNIT_TAU:
            raise MarketBarConstructionError(
                "market bar must represent exactly one tau"
            )
        if self.calendar_end_datetime < self.calendar_start_datetime:
            raise MarketBarConstructionError("market bar calendar interval is invalid")
        if self.high < max(self.open, self.close) or self.low > min(
            self.open, self.close
        ):
            raise MarketBarConstructionError("market bar OHLC relation is invalid")
        if self.volume < 0:
            raise MarketBarConstructionError("market bar volume must be non-negative")


def _stable_id(*parts: object) -> str:
    value = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def _session_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time(9, 0), tzinfo=KOREA_TZ),
        datetime.combine(day, time(15, 30), tzinfo=KOREA_TZ),
    )


def _source_segment_record(
    segment: ActivitySegment, overlap_start: Decimal, overlap_end: Decimal
) -> dict[str, Any]:
    overlap_tau = overlap_end - overlap_start
    fraction = overlap_tau / (segment.tau_end - segment.tau_start)
    return {
        "source_id": segment.source_id,
        "source_resolution": segment.source_resolution,
        "source_tau_start": segment.tau_start,
        "source_tau_end": segment.tau_end,
        "overlap_tau_start": overlap_start,
        "overlap_tau_end": overlap_end,
        "overlap_tau": overlap_tau,
        "overlap_fraction": fraction,
        "calendar_start_datetime": segment.calendar_start_datetime,
        "calendar_end_datetime": segment.calendar_end_datetime,
        "open": segment.open,
        "high": segment.high,
        "low": segment.low,
        "close": segment.close,
        "volume": segment.volume,
        "boundary_split": fraction != Decimal(1),
    }


def build_market_bars(
    segments: Sequence[ActivitySegment], *, identity_prefix: str = ""
) -> tuple[MarketBar, ...]:
    """Aggregate contiguous segments into exact unit-tau bars.

    A source segment that straddles a unit boundary is not price-interpolated;
    its source OHLC is included in each enclosing bucket and the overlap
    fraction is explicit in ``source_segments``.
    """
    ordered = tuple(
        sorted(
            segments, key=lambda item: (item.tau_start, item.tau_end, item.source_id)
        )
    )
    if not ordered:
        return ()
    previous_end = ordered[0].tau_start
    if previous_end != Decimal(0):
        raise MarketBarConstructionError("segments must begin at tau zero")
    for segment in ordered:
        if abs(segment.tau_start - previous_end) > TOLERANCE:
            raise MarketBarConstructionError(
                "segments must form a contiguous tau coordinate"
            )
        previous_end = segment.tau_end
    total_units = int(previous_end // UNIT_TAU)
    result: list[MarketBar] = []
    for unit in range(total_units):
        start, end = Decimal(unit), Decimal(unit + 1)
        selected: list[tuple[ActivitySegment, Decimal, Decimal]] = []
        for segment in ordered:
            overlap_start = max(start, segment.tau_start)
            overlap_end = min(end, segment.tau_end)
            if overlap_end > overlap_start:
                selected.append((segment, overlap_start, overlap_end))
        if not selected:
            raise MarketBarConstructionError("empty tau bucket")
        records = tuple(_source_segment_record(*item) for item in selected)
        first, last = selected[0][0], selected[-1][0]
        volume = sum(
            (
                item[0].volume
                * (item[2] - item[1])
                / (item[0].tau_end - item[0].tau_start)
                for item in selected
            ),
            Decimal(0),
        )
        result.append(
            MarketBar(
                market_bar_id=_stable_id(
                    "MARKET_BAR_V01", identity_prefix, first.stock_code, start, end
                ),
                tau_start=start,
                tau_end=end,
                calendar_start_datetime=min(
                    item[0].calendar_start_datetime for item in selected
                ),
                calendar_end_datetime=max(
                    item[0].calendar_end_datetime for item in selected
                ),
                open=first.open,
                high=max(item[0].high for item in selected),
                low=min(item[0].low for item in selected),
                close=last.close,
                volume=volume,
                source_resolution=(
                    first.source_resolution
                    if len({item[0].source_resolution for item in selected}) == 1
                    else "MIXED"
                ),
                source_segments=records,
            )
        )
    return tuple(result)


def _load_v03_details(path: Path) -> dict[tuple[str, date], Mapping[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    details: dict[tuple[str, date], Mapping[str, Any]] = {}
    for raw in payload.get("cases", []):
        if raw.get("status") == "OK":
            details[(raw["stock_code"], date.fromisoformat(raw["trade_date"]))] = raw
    return details


def _intraday_segments(
    bar: DailyBar,
    previous: DailyBar,
    delta_tau: Decimal,
    detail: Mapping[str, Any],
    raw_rows: Mapping[str, Any] | None = None,
) -> tuple[ActivitySegment, ...]:
    day_start, day_end = _session_bounds(bar.trade_date)
    previous_end = datetime.combine(previous.trade_date, time(15, 30), tzinfo=KOREA_TZ)
    cursor = Decimal(0)
    result: list[ActivitySegment] = []
    overnight_tau = _v03_decimal(detail.get("overnight_tau")) or Decimal(0)
    if overnight_tau > 0:
        result.append(
            ActivitySegment(
                bar.stock_code,
                cursor,
                cursor + overnight_tau,
                previous_end,
                day_start,
                bar.signal.open,
                bar.signal.open,
                bar.signal.open,
                bar.signal.open,
                Decimal(0),
                "OVERNIGHT_GAP",
                f"{bar.stock_code}:{bar.trade_date}:OVERNIGHT",
            )
        )
        cursor += overnight_tau
    for index, raw in enumerate(detail.get("five_minute_segments", [])):
        tau = _v03_decimal(raw.get("tau")) or Decimal(0)
        if tau <= 0:
            continue
        result.append(
            ActivitySegment(
                bar.stock_code,
                cursor,
                cursor + tau,
                day_start,
                day_end,
                _v03_decimal(raw["signal_open"]) or Decimal(0),
                _v03_decimal(raw["signal_high"]) or Decimal(0),
                _v03_decimal(raw["signal_low"]) or Decimal(0),
                _v03_decimal(raw["signal_close"]) or Decimal(0),
                Decimal(
                    raw_rows[raw["label"]].raw.volume
                    if raw_rows is not None and raw["label"] in raw_rows
                    else 0
                ),
                "5M_RAW_ACTIVITY_SIGNAL_ANCHORED",
                f"{bar.stock_code}:{bar.trade_date}:5M:{index}:{raw['label']}",
            )
        )
        cursor += tau
    if abs(cursor - delta_tau) > TOLERANCE:
        raise MarketBarConstructionError(
            "V0.3 detail tau does not match Daily delta tau"
        )
    return tuple(result)


def _daily_segments_for_stock(
    stock_code: str,
    *,
    details: Mapping[tuple[str, date], Mapping[str, Any]],
) -> tuple[list[tuple[ActivitySegment, ...]], list[dict[str, Any]], dict[str, int]]:
    bars = tuple(sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date))
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
    _ = calendar  # calendar identity remains source Daily session identity
    tau_rows = {row["trade_date"]: row for row in market_time_series(bars)}
    raw_by_date, _ = _load_cached_raw_rows(
        stock_code,
        {day for code, day in details if code == stock_code},
        Path("data/raw/kiwoom/minute"),
    )
    runs: list[tuple[ActivitySegment, ...]] = []
    unresolved: list[dict[str, Any]] = []
    current: list[ActivitySegment] = []
    stats = {"daily_segments": 0, "intraday_sessions": 0, "unresolved_fast_sessions": 0}
    for index, bar in enumerate(bars):
        delta = tau_rows[bar.trade_date]["delta_tau"]
        if delta is None:
            if current:
                runs.append(tuple(current))
                current = []
            continue
        if delta <= 0:
            if current:
                runs.append(tuple(current))
                current = []
            continue
        if delta > UNIT_TAU:
            detail = details.get((stock_code, bar.trade_date))
            if detail is None or index == 0:
                if current:
                    runs.append(tuple(current))
                    current = []
                unresolved.append(
                    {
                        "stock_code": stock_code,
                        "trade_date": bar.trade_date,
                        "delta_tau": delta,
                        "reason": "INSUFFICIENT_SOURCE_RESOLUTION",
                    }
                )
                stats["unresolved_fast_sessions"] += 1
                continue
            try:
                raw_by_label = {
                    row.source_label: row for row in raw_by_date.get(bar.trade_date, ())
                }
                current.extend(
                    _intraday_segments(
                        bar,
                        bars[index - 1],
                        delta,
                        detail,
                        raw_rows=raw_by_label,
                    )
                )
                stats["intraday_sessions"] += 1
            except MarketBarConstructionError as exc:
                if current:
                    runs.append(tuple(current))
                    current = []
                unresolved.append(
                    {
                        "stock_code": stock_code,
                        "trade_date": bar.trade_date,
                        "delta_tau": delta,
                        "reason": str(exc),
                    }
                )
                stats["unresolved_fast_sessions"] += 1
            continue
        start, end = _session_bounds(bar.trade_date)
        current.append(
            ActivitySegment(
                stock_code,
                sum((item.tau_end - item.tau_start for item in current), Decimal(0))
                if current
                else Decimal(0),
                (
                    sum((item.tau_end - item.tau_start for item in current), Decimal(0))
                    if current
                    else Decimal(0)
                )
                + delta,
                start,
                end,
                bar.signal.open,
                bar.signal.high,
                bar.signal.low,
                bar.signal.close,
                Decimal(bar.signal.volume),
                "DAILY_SIGNAL_ADJUSTED",
                f"{stock_code}:{bar.trade_date}:DAILY",
            )
        )
        stats["daily_segments"] += 1
    if current:
        runs.append(tuple(current))
    return runs, unresolved, stats


def run_market_bar_construction_proof(
    *,
    output: Path = OUTPUT_PATH,
    v03_path: Path = V03_OUTPUT_PATH,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    details = _load_v03_details(v03_path)
    all_bars: list[dict[str, Any]] = []
    runs_report: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    aggregate_stats = {
        "daily_segments": 0,
        "intraday_sessions": 0,
        "unresolved_fast_sessions": 0,
    }
    for stock_code in sorted(stocks):
        runs, missing, stats = _daily_segments_for_stock(stock_code, details=details)
        unresolved.extend(missing)
        for key, value in stats.items():
            aggregate_stats[key] += value
        for run_index, segments in enumerate(runs, start=1):
            # Rebase each source run so every independent proof interval starts at tau zero.
            rebased: list[ActivitySegment] = []
            cursor = Decimal(0)
            for segment in segments:
                length = segment.tau_end - segment.tau_start
                rebased.append(
                    ActivitySegment(
                        stock_code,
                        cursor,
                        cursor + length,
                        segment.calendar_start_datetime,
                        segment.calendar_end_datetime,
                        segment.open,
                        segment.high,
                        segment.low,
                        segment.close,
                        segment.volume,
                        segment.source_resolution,
                        segment.source_id,
                    )
                )
                cursor += length
            market_bars = build_market_bars(
                rebased, identity_prefix=f"{stock_code}:RUN:{run_index}"
            )
            all_bars.extend(asdict(item) for item in market_bars)
            runs_report.append(
                {
                    "stock_code": stock_code,
                    "run_index": run_index,
                    "source_segment_count": len(rebased),
                    "market_bar_count": len(market_bars),
                    "source_tau": cursor,
                    "materialized_tau": Decimal(len(market_bars)),
                    "unmaterialized_tail_tau": cursor - Decimal(len(market_bars)),
                    "spans_multiple_calendar_dates": len(
                        {item.calendar_start_datetime.date() for item in market_bars}
                    )
                    > 1
                    or len({item.calendar_end_datetime.date() for item in market_bars})
                    > 1,
                }
            )
    materialized = [item for item in all_bars]
    cross_date_count = sum(
        item["calendar_start_datetime"].date() != item["calendar_end_datetime"].date()
        for item in materialized
    )
    resolvable_source_tau = sum((row["source_tau"] for row in runs_report), Decimal(0))
    materialized_tau = Decimal(len(materialized))
    unresolved_tau = sum((row["delta_tau"] for row in unresolved), Decimal(0))
    report = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "methodology": {
            "market_bar_definition": "one exact ACTIVITY_TAU unit; calendar-day boundaries ignored",
            "fast_policy": "multiple same-session bars require cached V0.3 intraday source; unresolved otherwise",
            "slow_policy": "sub-unit Daily segments aggregate across sessions until one tau unit",
            "boundary_split_policy": "source OHLC is not interpolated; overlap fraction and source values remain in provenance",
            "calendar_bounds_policy": "enclosing source session bounds; no intraday START/END semantics inferred",
            "volume_policy": "tau-overlap prorated Decimal; source volume is preserved in provenance",
            "strategy": False,
            "indicators": False,
            "signals": False,
            "orders": False,
            "fills": False,
            "pnl": False,
        },
        "population": {
            "stock_count": len(stocks),
            "source_run_count": len(runs_report),
            "market_bar_count": len(materialized),
            "resolvable_source_tau": resolvable_source_tau,
            "materialized_tau": materialized_tau,
            "unmaterialized_tail_tau": resolvable_source_tau - materialized_tau,
            "unresolved_fast_tau": unresolved_tau,
            "cross_calendar_date_market_bar_count": cross_date_count,
            **aggregate_stats,
        },
        "runs": runs_report,
        "unresolved_sessions": unresolved,
        "market_bars": materialized,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            default=lambda value: (
                value.isoformat() if isinstance(value, (date, datetime)) else str(value)
            ),
        ),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--v03-path", type=Path, default=V03_OUTPUT_PATH)
    args = parser.parse_args()
    report = run_market_bar_construction_proof(
        output=args.output, v03_path=args.v03_path
    )
    print(
        json.dumps(
            {"output": args.output.as_posix(), "population": report["population"]},
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
