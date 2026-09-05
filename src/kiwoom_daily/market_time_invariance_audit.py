"""Report-only event-rate invariance audit for the V0.1 market-time clock."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import (
    calculate_daily_indicators,
    simple_moving_average,
)
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar

from .down_box_daily_execution_proof import _load_stock
from .market_clock_audit import _clock_series
from .market_clock_compression_audit_v0_2 import (
    OUTPUT_PATH as V02_OUTPUT_PATH,
)
from .market_clock_compression_audit_v0_2 import (
    RESEARCH_END,
    RESEARCH_START,
    STOCKS,
    _json_default,
)
from .market_time_normalization_audit import (
    HORIZONS,
    _assign_clock_quartiles,
    _cross,
    _distribution,
    market_time_series,
)
from .market_time_normalization_audit import (
    OUTPUT_PATH as V01_OUTPUT_PATH,
)

PROOF_VERSION = "MARKET_TIME_INVARIANCE_AUDIT_V0_2"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_time_invariance_audit_v0_2.json"
)


def _load_compression(path: Path) -> dict[tuple[str, date], str | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        (row["stock_code"], date.fromisoformat(row["trade_date"])): row.get(
            "compression_quartile"
        )
        for row in payload["rows"]
    }


def _prepare_rows(
    stocks: Sequence[str],
) -> tuple[dict[str, tuple[DailyBar, ...]], list[dict[str, Any]]]:
    bars_by_stock: dict[str, tuple[DailyBar, ...]] = {}
    rows: list[dict[str, Any]] = []
    for stock_code in sorted(stocks):
        bars = tuple(sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date))
        bars_by_stock[stock_code] = bars
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        points = tuple(calculate_daily_indicators(bars, calendar))
        clock = {row["trade_date"]: row for row in _clock_series(bars, points)}
        sma5 = simple_moving_average([bar.signal.close for bar in bars], 5)
        for row in market_time_series(bars):
            index = row["_index"]
            day = row["trade_date"]
            row.update(clock[day])
            row.update(
                {
                    "sma5": sma5[index],
                    "sma10": points[index].sma10,
                    "sma20": points[index].sma20,
                    "sma60": points[index].sma60,
                }
            )
            if RESEARCH_START <= day <= RESEARCH_END:
                rows.append(row)
    rows.sort(key=lambda row: (row["stock_code"], row["trade_date"]))
    _assign_clock_quartiles(rows)
    return bars_by_stock, rows


def _regime_rows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    return {
        "FAST_DIRECTIONAL_HIGH_EFF": [
            row
            for row in rows
            if row.get("range_speed_quartile") == "Q4"
            and row.get("direction_speed_quartile") == "Q4"
            and row.get("efficiency_10_quartile") == "Q4"
        ],
        "FAST_NOISY": [
            row
            for row in rows
            if row.get("range_speed_quartile") == "Q4"
            and row.get("efficiency_10_quartile") == "Q1"
        ],
        "SLOW": [row for row in rows if row.get("range_speed_quartile") == "Q1"],
    }


def _tau_exposure(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [row["delta_tau"] for row in rows if row.get("delta_tau") is not None]
    return {
        "calendar_session_count": len(rows),
        "tau_eligible_session_count": len(values),
        "total_tau": sum(values, Decimal(0)),
        "tau_per_calendar_session": sum(values, Decimal(0)) / Decimal(len(rows))
        if rows
        else None,
    }


def _events(
    rows: Sequence[Mapping[str, Any]],
    bars_by_stock: Mapping[str, Sequence[DailyBar]],
    *,
    ma_field: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["stock_code"], []).append(row)
    output: list[dict[str, Any]] = []
    for stock_code, stock_rows in grouped.items():
        stock_rows.sort(key=lambda row: row["trade_date"])
        close_by_date = {
            bar.trade_date: bar.signal.close for bar in bars_by_stock[stock_code]
        }
        for index in range(1, len(stock_rows)):
            current, previous = stock_rows[index], stock_rows[index - 1]
            direction = _cross(
                close_by_date[current["trade_date"]],
                close_by_date[previous["trade_date"]],
                current.get(ma_field),
                previous.get(ma_field),
            )
            if direction is not None:
                output.append(
                    {
                        "stock_code": stock_code,
                        "trade_date": current["trade_date"],
                        "direction": direction,
                        "delta_tau": current.get("delta_tau"),
                    }
                )
    return output


def _rate_summary(
    rows: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    keys = {(row["stock_code"], row["trade_date"]) for row in rows}
    selected = [
        event for event in events if (event["stock_code"], event["trade_date"]) in keys
    ]
    tau_eligible = [row for row in rows if row.get("delta_tau") is not None]
    total_tau = sum((row["delta_tau"] for row in tau_eligible), Decimal(0))
    tau_events = [event for event in selected if event.get("delta_tau") is not None]
    return {
        "calendar_session_count": len(rows),
        "total_tau": total_tau,
        "event_count_calendar": len(selected),
        "event_count_tau_eligible": len(tau_events),
        "events_per_100_calendar_sessions": Decimal(len(selected))
        * Decimal(100)
        / Decimal(len(rows))
        if rows
        else None,
        "events_per_100_tau": Decimal(len(tau_events)) * Decimal(100) / total_tau
        if total_tau
        else None,
    }


def _dispersion(rates: Sequence[Decimal | None]) -> dict[str, Decimal | int | None]:
    values = [value for value in rates if value is not None]
    if not values:
        return {"count": 0, "range": None, "coefficient_of_variation": None}
    mean = sum(values, Decimal(0)) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(
        len(values)
    )
    return {
        "count": len(values),
        "range": max(values) - min(values),
        "coefficient_of_variation": variance.sqrt() / mean if mean else None,
    }


def _compression_episodes(
    rows: Sequence[Mapping[str, Any]],
    compression: Mapping[tuple[str, date], str | None],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    by_stock: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_stock.setdefault(row["stock_code"], []).append(row)
    for stock_code, stock_rows in by_stock.items():
        stock_rows.sort(key=lambda row: row["trade_date"])
        current: list[Mapping[str, Any]] = []
        for row in stock_rows:
            is_c1 = compression.get((stock_code, row["trade_date"])) == "C1"
            if is_c1:
                current.append(row)
                continue
            if current:
                tau_values = [item.get("delta_tau") for item in current]
                result.append(
                    {
                        "stock_code": stock_code,
                        "start_date": current[0]["trade_date"],
                        "end_date": current[-1]["trade_date"],
                        "calendar_duration": len(current),
                        "tau_duration": sum(tau_values, Decimal(0))
                        if all(value is not None for value in tau_values)
                        else None,
                    }
                )
                current = []
        if current:
            tau_values = [item.get("delta_tau") for item in current]
            result.append(
                {
                    "stock_code": stock_code,
                    "start_date": current[0]["trade_date"],
                    "end_date": current[-1]["trade_date"],
                    "calendar_duration": len(current),
                    "tau_duration": sum(tau_values, Decimal(0))
                    if all(value is not None for value in tau_values)
                    else None,
                }
            )
    return result


def _stock_calibration(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for stock_code in sorted({row["stock_code"] for row in rows}):
        values = [
            row["delta_tau"]
            for row in rows
            if row["stock_code"] == stock_code and row.get("delta_tau") is not None
        ]
        distribution = _distribution(values)
        distribution["mean"] = (
            sum(values, Decimal(0)) / Decimal(len(values)) if values else None
        )
        result[stock_code] = distribution
    return result


def _scale_consistency(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    medians: list[Decimal] = []
    for horizon in HORIZONS:
        values = [
            row[f"clock_scale_{horizon}"]
            for row in rows
            if row.get(f"clock_scale_{horizon}") is not None
        ]
        distribution = _distribution(values)
        result[f"H{horizon}"] = distribution
        if distribution["median"] is not None:
            medians.append(distribution["median"])
    result["median_range"] = max(medians) - min(medians) if medians else None
    return result


def _cached_minute_dates(stock_code: str, root: Path) -> set[date]:
    """Read only existing raw minute artifacts; tolerate unrelated schemas."""
    dates: set[date] = set()
    for path in sorted((root / stock_code / "raw").glob("**/page-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        for value in payload.values() if isinstance(payload, dict) else ():
            if not isinstance(value, list):
                continue
            for row in value:
                label = row.get("cntr_tm") if isinstance(row, dict) else None
                if isinstance(label, str) and len(label) >= 8 and label[:8].isdigit():
                    try:
                        dates.add(
                            date.fromisoformat(f"{label[:4]}-{label[4:6]}-{label[6:8]}")
                        )
                    except ValueError:
                        pass
    return dates


def _invariance_hypotheses(report: Mapping[str, Any]) -> dict[str, str]:
    compression = report["compression_invariance"]
    calendar = compression["calendar_sma10"]
    market = compression["market_time_ma10"]
    calendar_session_dispersion = _dispersion(
        [
            calendar[bucket]["events_per_100_calendar_sessions"]
            for bucket in ("C1", "C2", "C3", "C4")
        ]
    )
    calendar_tau_dispersion = _dispersion(
        [calendar[bucket]["events_per_100_tau"] for bucket in ("C1", "C2", "C3", "C4")]
    )
    h1 = (
        "SUPPORTED"
        if calendar_tau_dispersion["range"] < calendar_session_dispersion["range"]
        and calendar_tau_dispersion["coefficient_of_variation"]
        < calendar_session_dispersion["coefficient_of_variation"]
        else "PARTIALLY_SUPPORTED"
        if calendar_tau_dispersion["range"] < calendar_session_dispersion["range"]
        else "NOT_SUPPORTED"
    )
    h2 = (
        "SUPPORTED"
        if _dispersion(
            [
                market[bucket]["events_per_100_tau"]
                for bucket in ("C1", "C2", "C3", "C4")
            ]
        )["range"]
        < _dispersion(
            [
                calendar[bucket]["events_per_100_tau"]
                for bucket in ("C1", "C2", "C3", "C4")
            ]
        )["range"]
        else "NOT_SUPPORTED"
    )
    duration = report["compression_duration"]
    h3 = (
        "SUPPORTED"
        if duration["tau_duration"]["coefficient_of_variation"]
        < duration["calendar_duration"]["coefficient_of_variation"]
        else "NOT_SUPPORTED"
    )
    # No fitted tolerance is introduced for an "approximately" common scale.
    h4 = "INCONCLUSIVE"
    h5 = (
        "SUPPORTED"
        if report["intraday_availability"]["h5_candidate_count"]
        < report["population"]["valid_delta_tau_count"]
        else "NOT_SUPPORTED"
    )
    return {
        "H1_TAU_RATE_REGIME_INVARIANCE": h1,
        "H2_MTMA_TAU_RATE_STABILITY": h2,
        "H3_C1_EPISODE_TAU_DURATION_STABILITY": h3,
        "H4_HORIZON_SCALE_CONSISTENCY": h4,
        "H5_SELECTIVE_INTRADAY_NEED": h5,
    }


def run_market_time_invariance_audit(
    *,
    output: Path = OUTPUT_PATH,
    v02_path: Path = V02_OUTPUT_PATH,
    minute_root: Path = Path("data/raw/kiwoom/minute"),
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    """Run V0.2 exclusively from cached Daily/minute artifacts."""
    bars_by_stock, rows = _prepare_rows(stocks)
    compression = _load_compression(v02_path)
    for row in rows:
        row["compression_quartile"] = compression.get(
            (row["stock_code"], row["trade_date"])
        )
    calendar10 = _events(rows, bars_by_stock, ma_field="sma10")
    mtma_events = {
        horizon: _events(rows, bars_by_stock, ma_field=f"mtma{horizon}")
        for horizon in (5, 10, 20)
    }
    compression_groups = {
        bucket: [row for row in rows if row["compression_quartile"] == bucket]
        for bucket in ("C1", "C2", "C3", "C4")
    }
    regimes = _regime_rows(rows)
    episodes = _compression_episodes(rows, compression)
    h5_candidates = [row for row in rows if row.get("day_exceeds_5") is True]
    minute_dates = {
        stock_code: _cached_minute_dates(stock_code, minute_root)
        for stock_code in sorted(stocks)
    }
    intraday_cases = [
        {
            "stock_code": row["stock_code"],
            "trade_date": row["trade_date"],
            "delta_tau": row["delta_tau"],
            "cached_minute_available": row["trade_date"]
            in minute_dates[row["stock_code"]],
        }
        for row in h5_candidates
    ]
    report: dict[str, Any] = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "research_period": {"start": RESEARCH_START, "end": RESEARCH_END},
        "source_v01": V01_OUTPUT_PATH.as_posix(),
        "source_v02": v02_path.as_posix(),
        "methodology": {
            "clock": "unchanged V0.1 DELTA_TAU = TR_PCT / trailing-252 prior median TR_PCT",
            "tau_rate": "only event/session T with valid DELTA_TAU contributes to events per 100 tau",
            "calendar_rate": "all existing research sessions/events retained",
            "minute_check": "presence-only scan of cached RAW minute artifact labels; no minute analysis",
            "strategy_changes": False,
            "orders": False,
            "pnl": False,
            "charts_generated": False,
        },
        "population": {
            "research_session_count": len(rows),
            "valid_delta_tau_count": sum(
                row.get("delta_tau") is not None for row in rows
            ),
        },
        "tau_exposure": {
            "regimes": {name: _tau_exposure(group) for name, group in regimes.items()},
            "compression": {
                name: _tau_exposure(group) for name, group in compression_groups.items()
            },
        },
        "compression_invariance": {
            "calendar_sma10": {
                bucket: _rate_summary(group, calendar10)
                for bucket, group in compression_groups.items()
            },
            "market_time_ma10": {
                bucket: _rate_summary(group, mtma_events[10])
                for bucket, group in compression_groups.items()
            },
        },
        "regime_invariance": {
            name: {
                f"mtma{horizon}": _rate_summary(group, mtma_events[horizon])
                for horizon in (5, 10, 20)
            }
            for name, group in regimes.items()
        },
        "rate_dispersion": {
            "calendar_sma10_per_calendar": _dispersion(
                [
                    _rate_summary(group, calendar10)["events_per_100_calendar_sessions"]
                    for group in compression_groups.values()
                ]
            ),
            "calendar_sma10_per_tau": _dispersion(
                [
                    _rate_summary(group, calendar10)["events_per_100_tau"]
                    for group in compression_groups.values()
                ]
            ),
            "mtma10_per_tau": _dispersion(
                [
                    _rate_summary(group, mtma_events[10])["events_per_100_tau"]
                    for group in compression_groups.values()
                ]
            ),
        },
        "compression_duration": {
            "episode_count": len(episodes),
            "calendar_duration": _dispersion(
                [Decimal(row["calendar_duration"]) for row in episodes]
            ),
            "tau_duration": _dispersion(
                [
                    row["tau_duration"]
                    for row in episodes
                    if row["tau_duration"] is not None
                ]
            ),
            "episodes": episodes,
        },
        "stock_clock_calibration": _stock_calibration(rows),
        "horizon_scale_consistency": _scale_consistency(rows),
        "intraday_availability": {
            "h5_candidate_count": len(h5_candidates),
            "cached_minute_available_count": sum(
                row["cached_minute_available"] for row in intraday_cases
            ),
            "cases": intraday_cases,
        },
    }
    report["hypotheses"] = _invariance_hypotheses(report)
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
    report = run_market_time_invariance_audit(output=args.output)
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "population": report["population"],
                "hypotheses": report["hypotheses"],
            },
            indent=2,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
