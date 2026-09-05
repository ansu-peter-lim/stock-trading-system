"""Offline selective 5-minute decomposition of high-activity Daily sessions.

This is a research audit, not a market-data adapter or a strategy component.
It keeps V0.1's Daily ``DELTA_TAU`` fixed and only apportions it between an
adjusted-Daily opening gap and cached ka10080 RAW intraday price activity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import calculate_daily_indicators
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.kiwoom_minute.pipeline import (
    MinuteCollectionRequest,
    MinutePriceBasis,
    ParsedMinuteRow,
    parse_minute_page,
)

from .down_box_daily_execution_proof import _load_stock
from .market_clock_audit import _clock_series, _json_default
from .market_clock_compression_audit_v0_2 import STOCKS
from .market_time_invariance_audit import OUTPUT_PATH as V02_OUTPUT_PATH
from .market_time_normalization_audit import market_time_series

PROOF_VERSION = "MARKET_TIME_SELECTIVE_INTRADAY_DECOMPOSITION_V0_3"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/"
    "market_time_selective_intraday_decomposition_v0_3.json"
)
MINUTE_ROOT = Path("data/raw/kiwoom/minute")
HORIZON = Decimal(5)
TOLERANCE = Decimal("1e-20")


class IntradayDecompositionError(ValueError):
    """A cached source artifact cannot support an auditable decomposition."""


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _load_h5_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload["intraday_availability"]["cases"]
    selected = [
        {
            "stock_code": item["stock_code"],
            "trade_date": date.fromisoformat(item["trade_date"]),
            "delta_tau": _decimal(item["delta_tau"]),
        }
        for item in cases
        if item["cached_minute_available"]
    ]
    return sorted(selected, key=lambda item: (item["stock_code"], item["trade_date"]))


def _load_cached_raw_rows(
    stock_code: str,
    target_dates: set[date],
    root: Path,
) -> tuple[dict[date, tuple[ParsedMinuteRow, ...]], tuple[dict[str, str], ...]]:
    """Parse existing immutable RAW artifacts only; no store/manifest is used."""
    if not target_dates:
        return {}, ()
    request = MinuteCollectionRequest(
        stock_code=stock_code,
        start_date=min(target_dates),
        end_date=max(target_dates),
        price_basis=MinutePriceBasis.RAW,
    )
    by_date: dict[date, list[ParsedMinuteRow]] = defaultdict(list)
    provenance: list[dict[str, str]] = []
    labels: set[str] = set()
    for sequence, path in enumerate(
        sorted((root / stock_code / "raw").glob("**/page-*.json")), start=1
    ):
        raw_bytes = path.read_bytes()
        digest = hashlib.sha256(raw_bytes).hexdigest()
        page = parse_minute_page(
            raw_bytes, request, source_page=sequence, artifact_sha256=digest
        )
        used = False
        for row in page.rows:
            if row.trading_date not in target_dates:
                continue
            if row.source_label in labels:
                raise IntradayDecompositionError("duplicate cached source label")
            labels.add(row.source_label)
            by_date[row.trading_date].append(row)
            used = True
        if used:
            provenance.append(
                {"raw_file_path": path.as_posix(), "raw_file_sha256": digest}
            )
    return (
        {
            day: tuple(sorted(rows, key=lambda row: row.source_label))
            for day, rows in by_date.items()
        },
        tuple(provenance),
    )


def _activity_segments(
    *,
    delta_tau: Decimal,
    previous_adjusted_close: Decimal,
    adjusted_open: Decimal,
    minute_rows: Sequence[ParsedMinuteRow],
) -> tuple[list[dict[str, Any]], Decimal, Decimal]:
    """Allocate the immutable Daily tau across opening gap and 5-minute TR.

    A RAW 5-minute path is transformed into *research-only signal space* by
    anchoring raw prices to adjusted Daily open.  This is only needed to compare
    MTMA levels; execution/accounting RAW prices are never altered.
    """
    if not minute_rows or previous_adjusted_close <= 0 or adjusted_open <= 0:
        raise IntradayDecompositionError("missing positive Daily/minute input")
    session_raw_open = minute_rows[0].raw.open
    if session_raw_open <= 0 or delta_tau <= 0:
        raise IntradayDecompositionError("invalid raw opening price or delta tau")
    gap_activity = abs(adjusted_open / previous_adjusted_close - Decimal(1))
    raw_activity: list[Decimal] = []
    previous_close = session_raw_open
    for row in minute_rows:
        tr = max(
            row.raw.high - row.raw.low,
            abs(row.raw.high - previous_close),
            abs(row.raw.low - previous_close),
        )
        raw_activity.append(tr / session_raw_open)
        previous_close = row.raw.close
    total_activity = gap_activity + sum(raw_activity, Decimal(0))
    if total_activity <= 0:
        raise IntradayDecompositionError("zero total price activity")
    segments: list[dict[str, Any]] = [
        {
            "kind": "OVERNIGHT_GAP",
            "label": "SESSION_OPEN_OVERNIGHT",
            "tau": delta_tau * gap_activity / total_activity,
            "signal_close": adjusted_open,
            "time_bucket": "OVERNIGHT",
        }
    ]
    for row, activity in zip(minute_rows, raw_activity, strict=True):
        scale = adjusted_open / session_raw_open
        segments.append(
            {
                "kind": "INTRADAY_5M",
                "label": row.source_label,
                "label_at": row.source_label_at,
                "tau": delta_tau * activity / total_activity,
                "signal_open": row.raw.open * scale,
                "signal_high": row.raw.high * scale,
                "signal_low": row.raw.low * scale,
                "signal_close": row.raw.close * scale,
                "intra_tr_pct": activity,
                "time_bucket": _time_bucket(
                    row.source_label_at.hour, row.source_label_at.minute
                ),
            }
        )
    return segments, gap_activity, total_activity


def _time_bucket(hour: int, minute: int) -> str:
    value = hour * 60 + minute
    if value < 570:
        return "OPENING_LABELS"
    if value < 690:
        return "MORNING"
    if value < 810:
        return "MIDDAY"
    if value < 900:
        return "AFTERNOON"
    return "CLOSE_LABELS"


def _label_quality(rows: Sequence[ParsedMinuteRow]) -> dict[str, Any]:
    first = rows[0].source_label_at
    last = rows[-1].source_label_at
    span = int((last - first).total_seconds() // 300) + 1
    gaps = [
        int((current.source_label_at - previous.source_label_at).total_seconds() // 60)
        for previous, current in pairwise(rows)
    ]
    return {
        "first_label": rows[0].source_label,
        "last_label": rows[-1].source_label,
        "first_label_basis": (
            "SESSION_OPEN_09_00"
            if first.hour == 9 and first.minute == 0
            else "FIRST_AVAILABLE_SAME_SESSION_ROW"
        ),
        "opening_label_missing": not (first.hour == 9 and first.minute == 0),
        "observed_row_count": len(rows),
        "expected_5m_slots_between_first_last": span,
        "missing_5m_slot_count_by_label": max(0, span - len(rows)),
        "non_5m_spacing_minutes": sorted({gap for gap in gaps if gap != 5}),
    }


def _bucket_30m(segments: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for segment in segments:
        if segment["kind"] != "INTRADAY_5M":
            continue
        label_at = segment["label_at"]
        grouped[f"{label_at.hour:02d}:{(label_at.minute // 30) * 30:02d}"].append(
            segment
        )
    result: list[dict[str, Any]] = []
    for bucket in sorted(grouped):
        rows = sorted(grouped[bucket], key=lambda row: row["label"])
        result.append(
            {
                "bucket": bucket,
                "first_label": rows[0]["label"],
                "last_label": rows[-1]["label"],
                "signal_open": rows[0]["signal_open"],
                "signal_high": max(row["signal_high"] for row in rows),
                "signal_low": min(row["signal_low"] for row in rows),
                "signal_close": rows[-1]["signal_close"],
                "tau": sum((row["tau"] for row in rows), Decimal(0)),
            }
        )
    return result


def _trailing_tau_mean(
    segments: Sequence[Mapping[str, Any]], horizon: Decimal
) -> Decimal:
    remaining = horizon
    weighted = Decimal(0)
    for segment in reversed(segments):
        overlap = min(segment["tau"], remaining)
        weighted += overlap * segment["signal_close"]
        remaining -= overlap
        if remaining <= 0:
            return weighted / horizon
    raise IntradayDecompositionError("not enough allocated tau for horizon")


def _trajectory(
    segments: Sequence[Mapping[str, Any]], delta_tau: Decimal
) -> dict[str, Any]:
    thresholds = {
        25: delta_tau * Decimal("0.25"),
        50: delta_tau * Decimal("0.5"),
        75: delta_tau * Decimal("0.75"),
    }
    reached: dict[str, str | None] = {str(key): None for key in thresholds}
    cumulative = Decimal(0)
    path: list[dict[str, Any]] = []
    for segment in segments:
        cumulative += segment["tau"]
        path.append(
            {
                "label": segment["label"],
                "kind": segment["kind"],
                "cumulative_tau": cumulative,
            }
        )
        for percent, target in thresholds.items():
            if reached[str(percent)] is None and cumulative >= target:
                reached[str(percent)] = segment["label"]
    return {"cumulative_tau": cumulative, "tau_percent_time": reached, "path": path}


def _resolution_classification(
    thirty: Sequence[Mapping[str, Any]], five: Sequence[Mapping[str, Any]]
) -> str:
    thirty_insufficient = any(row["tau"] >= HORIZON for row in thirty)
    five_insufficient = any(row["tau"] >= HORIZON for row in five)
    if not thirty_insufficient:
        return "A_DAILY_INSUFFICIENT_30M_SUFFICIENT"
    if not five_insufficient:
        return "B_30M_INSUFFICIENT_5M_SUFFICIENT"
    return "C_5M_INSUFFICIENT"


def _case_record(
    *,
    stock_code: str,
    trade_date: date,
    delta_tau: Decimal,
    previous_adjusted_close: Decimal,
    adjusted_open: Decimal,
    daily_mtma5: Decimal,
    atr20: Decimal,
    minute_rows: Sequence[ParsedMinuteRow],
) -> dict[str, Any]:
    segments, gap_activity, total_activity = _activity_segments(
        delta_tau=delta_tau,
        previous_adjusted_close=previous_adjusted_close,
        adjusted_open=adjusted_open,
        minute_rows=minute_rows,
    )
    five = [segment for segment in segments if segment["kind"] == "INTRADAY_5M"]
    thirty = _bucket_30m(five)
    overnight = segments[0]
    mtma5_5m = _trailing_tau_mean(segments, HORIZON)
    mtma5_30m = _trailing_tau_mean([overnight, *thirty], HORIZON)
    trajectory = _trajectory(segments, delta_tau)
    total_allocated = trajectory["cumulative_tau"]
    if abs(total_allocated - delta_tau) > TOLERANCE:
        raise IntradayDecompositionError("tau allocation failed conservation")
    classification = _resolution_classification(thirty, five)
    max_30m_tau = max((row["tau"] for row in thirty), default=Decimal(0))
    max_5m_tau = max((row["tau"] for row in five), default=Decimal(0))
    label_quality = _label_quality(minute_rows)
    return {
        "stock_code": stock_code,
        "trade_date": trade_date,
        "status": "OK",
        "daily_delta_tau": delta_tau,
        "gap_activity": gap_activity,
        "intraday_activity": total_activity - gap_activity,
        "total_activity": total_activity,
        "overnight_tau": overnight["tau"],
        "intraday_tau": sum((row["tau"] for row in five), Decimal(0)),
        "thirty_minute_tau": sum((row["tau"] for row in thirty), Decimal(0)),
        "allocation_conservation_error": total_allocated - delta_tau,
        "thirty_minute_conservation_error": sum(
            (row["tau"] for row in thirty), Decimal(0)
        )
        - sum((row["tau"] for row in five), Decimal(0)),
        "daily_mtma5": daily_mtma5,
        "mtma5_30m": mtma5_30m,
        "mtma5_5m": mtma5_5m,
        "daily_vs_30m_abs_error_atr": abs(daily_mtma5 - mtma5_30m) / atr20,
        "daily_vs_5m_abs_error_atr": abs(daily_mtma5 - mtma5_5m) / atr20,
        "thirty_vs_5m_abs_error_atr": abs(mtma5_30m - mtma5_5m) / atr20,
        "thirty_min_insufficient": any(row["tau"] >= HORIZON for row in thirty),
        "five_min_insufficient": any(row["tau"] >= HORIZON for row in five),
        "max_30m_tau": max_30m_tau,
        "max_30m_h5_share": max_30m_tau / HORIZON,
        "max_5m_tau": max_5m_tau,
        "max_5m_h5_share": max_5m_tau / HORIZON,
        "resolution": classification,
        "minute_row_count": len(five),
        "source_label_quality": label_quality,
        "thirty_minute_bucket_count": len(thirty),
        "trajectory": trajectory,
        "time_of_day_tau": {
            bucket: sum(
                (row["tau"] for row in five if row["time_bucket"] == bucket), Decimal(0)
            )
            for bucket in (
                "OPENING_LABELS",
                "MORNING",
                "MIDDAY",
                "AFTERNOON",
                "CLOSE_LABELS",
            )
        },
        "corporate_action_ambiguity": "NOT_ASSESSED_NO_EVENT_EVIDENCE",
        "source_timestamp_use": "OPAQUE_CHRONOLOGICAL_SOURCE_LABEL_ONLY",
        "thirty_minute_buckets": thirty,
        "five_minute_segments": [
            {
                key: value
                for key, value in segment.items()
                if key not in {"kind", "label_at"}
            }
            for segment in five
        ],
    }


def _representatives(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    def selection(key: str, reverse: bool, limit: int = 3) -> list[dict[str, Any]]:
        ordered = sorted(
            rows,
            key=lambda row: (row[key], row["stock_code"], row["trade_date"]),
            reverse=reverse,
        )
        return [
            {"stock_code": row["stock_code"], "trade_date": row["trade_date"]}
            for row in ordered[:limit]
        ]

    return {
        "largest_daily_vs_5m_difference": selection("daily_vs_5m_abs_error_atr", True),
        "closest_30m_to_5m": selection("thirty_vs_5m_abs_error_atr", False),
        "thirty_minute_insufficient": [
            {"stock_code": row["stock_code"], "trade_date": row["trade_date"]}
            for row in sorted(
                rows, key=lambda row: (row["stock_code"], row["trade_date"])
            )
            if row["thirty_min_insufficient"]
        ][:3],
    }


def run_market_time_selective_intraday_decomposition(
    *,
    output: Path = OUTPUT_PATH,
    v02_path: Path = V02_OUTPUT_PATH,
    minute_root: Path = MINUTE_ROOT,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    """Run V0.3 exclusively from existing Daily and RAW minute artifacts."""
    source_v02 = json.loads(v02_path.read_text(encoding="utf-8"))
    candidates = _load_h5_cases(v02_path)
    by_stock: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        by_stock[candidate["stock_code"]].append(candidate)
    records: list[dict[str, Any]] = []
    provenance: dict[str, tuple[dict[str, str], ...]] = {}
    for stock_code in sorted(by_stock):
        target_dates = {candidate["trade_date"] for candidate in by_stock[stock_code]}
        raw_by_date, raw_provenance = _load_cached_raw_rows(
            stock_code, target_dates, minute_root
        )
        provenance[stock_code] = raw_provenance
        bars = tuple(sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date))
        index_by_date = {bar.trade_date: index for index, bar in enumerate(bars)}
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        points = tuple(calculate_daily_indicators(bars, calendar))
        tau_rows = {row["trade_date"]: row for row in market_time_series(bars)}
        clock_rows = {row["trade_date"]: row for row in _clock_series(bars, points)}
        for candidate in by_stock[stock_code]:
            day = candidate["trade_date"]
            index = index_by_date[day]
            try:
                if index == 0 or day not in raw_by_date:
                    raise IntradayDecompositionError("missing cached minute session")
                tau = tau_rows[day]
                if tau["delta_tau"] != candidate["delta_tau"] or tau["mtma5"] is None:
                    raise IntradayDecompositionError("Daily tau source mismatch")
                atr20 = clock_rows[day]["atr20"]
                if atr20 is None or atr20 <= 0:
                    raise IntradayDecompositionError("missing adjusted ATR20")
                records.append(
                    _case_record(
                        stock_code=stock_code,
                        trade_date=day,
                        delta_tau=candidate["delta_tau"],
                        previous_adjusted_close=bars[index - 1].signal.close,
                        adjusted_open=bars[index].signal.open,
                        daily_mtma5=tau["mtma5"],
                        atr20=atr20,
                        minute_rows=raw_by_date[day],
                    )
                )
            except IntradayDecompositionError as exc:
                records.append(
                    {
                        "stock_code": stock_code,
                        "trade_date": day,
                        "status": "EXCLUDED",
                        "reason": str(exc),
                    }
                )
    records.sort(key=lambda row: (row["stock_code"], row["trade_date"]))
    valid = [row for row in records if row["status"] == "OK"]
    resolution_counts = {
        name: sum(row["resolution"] == name for row in valid)
        for name in (
            "A_DAILY_INSUFFICIENT_30M_SUFFICIENT",
            "B_30M_INSUFFICIENT_5M_SUFFICIENT",
            "C_5M_INSUFFICIENT",
        )
    }
    time_totals = {
        bucket: sum((row["time_of_day_tau"][bucket] for row in valid), Decimal(0))
        for bucket in (
            "OPENING_LABELS",
            "MORNING",
            "MIDDAY",
            "AFTERNOON",
            "CLOSE_LABELS",
        )
    }
    report: dict[str, Any] = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "methodology": {
            "clock_name": "PRICE_ACTIVITY_CLOCK / ACTIVITY_TAU",
            "daily_tau": "unchanged V0.1 DELTA_TAU",
            "gap_activity": "abs(adjusted_open / previous_adjusted_close - 1)",
            "intraday_activity": "RAW intra-session true range / first RAW session open",
            "signal_space_price": "RAW minute price scaled by adjusted_open / first RAW session open, research-only",
            "timestamp_semantics": "opaque chronological source labels; no START/END inference",
            "corporate_action": "no event inference; known ambiguity would be excluded, absent event evidence is marked not assessed",
            "strategy_changes": False,
            "orders": False,
            "pnl": False,
            "charts_generated": False,
        },
        "population": {
            "v02_h5_candidate_count": len(source_v02["intraday_availability"]["cases"]),
            "cached_h5_case_count": len(candidates),
            "cached_case_count": len(records),
            "valid_case_count": len(valid),
            "excluded_case_count": len(records) - len(valid),
        },
        "resolution_counts": resolution_counts,
        "time_of_day_tau_totals": time_totals,
        "raw_provenance": provenance,
        "cases": records,
        "representative_chart_candidates": _representatives(valid),
        "hypotheses": {
            "H1_DAILY_MTMA5_APPROXIMATION_DIFFERENCE": "SUPPORTED"
            if any(row["daily_vs_5m_abs_error_atr"] > 0 for row in valid)
            else "NOT_SUPPORTED",
            "H2_MOST_H5_DAYS_30M_SUFFICIENT": "SUPPORTED"
            if resolution_counts["A_DAILY_INSUFFICIENT_30M_SUFFICIENT"] > len(valid) / 2
            else "NOT_SUPPORTED",
            "H3_ONLY_SOME_SESSIONS_NEED_5M": "SUPPORTED"
            if resolution_counts["B_30M_INSUFFICIENT_5M_SUFFICIENT"]
            else "NOT_SUPPORTED",
            "H4_HIERARCHICAL_CLOCK_CONSERVATION": "SUPPORTED"
            if all(
                abs(row["allocation_conservation_error"]) <= TOLERANCE for row in valid
            )
            else "NOT_SUPPORTED",
            "H5_TIME_OF_DAY_CONCENTRATION": "DESCRIPTIVE_ONLY",
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    report = run_market_time_selective_intraday_decomposition(output=args.output)
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "population": report["population"],
                "resolution_counts": report["resolution_counts"],
            },
            indent=2,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
