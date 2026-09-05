"""Report-only Daily market-time normalization audit (V0.1).

This deliberately does not replace any strategy moving average.  It compares
fixed-session SMAs with a tau-weighted Daily-close approximation, using only
adjusted Daily OHLC and information available at each observed session.
"""

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
from .market_clock_audit import (
    _clock_series,
    _median,
    _pivot_rows,
    _true_range_pct,
)
from .market_clock_compression_audit_v0_2 import (
    OUTPUT_PATH as V02_OUTPUT_PATH,
)
from .market_clock_compression_audit_v0_2 import (
    RESEARCH_END,
    RESEARCH_START,
    STOCKS,
    _json_default,
)
from .market_clock_t_event_visual_review_pack import MAPPING_PATH as VISUAL_MAPPING_PATH

PROOF_VERSION = "MARKET_TIME_NORMALIZATION_AUDIT_V0_1"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_time_normalization_audit_v0_1.json"
)
HORIZONS = (5, 10, 20, 60)
REFERENCE_WINDOW = 252
MA_NAMES = tuple(f"MA{horizon}" for horizon in HORIZONS)


def _percentile(values: Sequence[Decimal], quantile: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    position = Decimal(len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _distribution(values: Sequence[Decimal]) -> dict[str, Decimal | int | None]:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "p10": _percentile(values, Decimal("0.10")),
        "p25": _percentile(values, Decimal("0.25")),
        "median": _percentile(values, Decimal("0.50")),
        "p75": _percentile(values, Decimal("0.75")),
        "p90": _percentile(values, Decimal("0.90")),
        "max": max(values) if values else None,
    }


def _assign_clock_quartiles(rows: Sequence[dict[str, Any]]) -> None:
    """Assign the existing pooled descriptive buckets without repeated sorts."""
    fields = (
        ("range_speed", "range_speed_quartile"),
        ("abs_net_move_atr_10", "direction_speed_quartile"),
        ("efficiency_10", "efficiency_10_quartile"),
    )
    for field, bucket in fields:
        values = sorted(row[field] for row in rows if row.get(field) is not None)
        thresholds = tuple(
            _percentile(values, quantile)
            for quantile in (Decimal("0.25"), Decimal("0.50"), Decimal("0.75"))
        )
        for row in rows:
            value = row.get(field)
            row[bucket] = (
                None
                if value is None or any(threshold is None for threshold in thresholds)
                else "Q1"
                if value <= thresholds[0]
                else "Q2"
                if value <= thresholds[1]
                else "Q3"
                if value <= thresholds[2]
                else "Q4"
            )


def _prior_valid_median(
    values: Sequence[Decimal | None], index: int, *, window: int = REFERENCE_WINDOW
) -> Decimal | None:
    """Return the median of exactly ``window`` valid observations before T."""
    prior = [value for value in values[:index] if value is not None]
    if len(prior) < window:
        return None
    return _median(prior[-window:])


def _tau_weighted_mean(
    closes: Sequence[Decimal],
    tau_ends: Sequence[Decimal | None],
    delta_tau: Sequence[Decimal | None],
    index: int,
    horizon: int,
) -> dict[str, Decimal | None | bool]:
    """Calculate an overlap-weighted Daily-close approximation at T.

    A Daily close represents its complete tau interval; it is never expanded
    into synthetic intraday bars.  The current and prior interval endpoints
    are all available at T.
    """
    end = tau_ends[index]
    if end is None or end < Decimal(horizon):
        return {
            "mtma": None,
            "calendar_equiv_sessions": None,
            "max_single_day_tau_share": None,
            "current_day_tau_share": None,
            "day_exceeds_horizon": None,
        }
    start = end - Decimal(horizon)
    weighted_close = Decimal(0)
    total_weight = Decimal(0)
    calendar_equiv = Decimal(0)
    max_share = Decimal(0)
    for candidate in range(index, -1, -1):
        interval_end = tau_ends[candidate]
        increment = delta_tau[candidate]
        if interval_end is None or increment is None:
            continue
        interval_start = interval_end - increment
        overlap = min(interval_end, end) - max(interval_start, start)
        if overlap > 0:
            weighted_close += closes[candidate] * overlap
            total_weight += overlap
            calendar_equiv += overlap / increment
            max_share = max(max_share, overlap / Decimal(horizon))
        if interval_start <= start:
            break
    if total_weight != Decimal(horizon):
        return {
            "mtma": None,
            "calendar_equiv_sessions": None,
            "max_single_day_tau_share": None,
            "current_day_tau_share": None,
            "day_exceeds_horizon": None,
        }
    current_increment = delta_tau[index]
    assert current_increment is not None
    return {
        "mtma": weighted_close / total_weight,
        "calendar_equiv_sessions": calendar_equiv,
        "max_single_day_tau_share": max_share,
        "current_day_tau_share": min(current_increment, Decimal(horizon))
        / Decimal(horizon),
        "day_exceeds_horizon": current_increment >= Decimal(horizon),
    }


def market_time_series(bars: Sequence[DailyBar]) -> tuple[dict[str, Any], ...]:
    """Return adjusted-price tau and MTMA observations in calendar order."""
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    tr_pct = _true_range_pct(canonical)
    references = [_prior_valid_median(tr_pct, index) for index in range(len(canonical))]
    delta_tau = [
        value / reference
        if value is not None and reference not in (None, Decimal(0))
        else None
        for value, reference in zip(tr_pct, references, strict=True)
    ]
    tau_ends: list[Decimal | None] = []
    cumulative = Decimal(0)
    for increment in delta_tau:
        if increment is None:
            tau_ends.append(None)
        else:
            cumulative += increment
            tau_ends.append(cumulative)
    closes = [bar.signal.close for bar in canonical]
    rows: list[dict[str, Any]] = []
    for index, bar in enumerate(canonical):
        row: dict[str, Any] = {
            "stock_code": bar.stock_code,
            "trade_date": bar.trade_date,
            "_index": index,
            "tr_pct": tr_pct[index],
            "reference_tr": references[index],
            "delta_tau": delta_tau[index],
            "tau": tau_ends[index],
            "clock_status": (
                "OK" if delta_tau[index] is not None else "INSUFFICIENT_CLOCK_HISTORY"
            ),
        }
        for horizon in HORIZONS:
            result = _tau_weighted_mean(closes, tau_ends, delta_tau, index, horizon)
            row[f"mtma{horizon}"] = result["mtma"]
            row[f"cal_eq_{horizon}"] = result["calendar_equiv_sessions"]
            row[f"clock_scale_{horizon}"] = (
                result["calendar_equiv_sessions"] / Decimal(horizon)
                if result["calendar_equiv_sessions"] is not None
                else None
            )
            row[f"current_day_tau_share_{horizon}"] = result["current_day_tau_share"]
            row[f"max_single_day_tau_share_{horizon}"] = result[
                "max_single_day_tau_share"
            ]
            row[f"day_exceeds_{horizon}"] = result["day_exceeds_horizon"]
        rows.append(row)
    return tuple(rows)


def _nearest_role(
    price: Decimal,
    values: Mapping[str, Decimal | None],
    atr: Decimal | None,
) -> tuple[str | None, Decimal | None]:
    if atr in (None, Decimal(0)):
        return None, None
    periods = {f"MA{horizon}": horizon for horizon in HORIZONS}
    candidates = [
        (name, abs(price - value) / atr)
        for name, value in values.items()
        if value is not None
    ]
    if not candidates:
        return None, None
    name, distance = min(candidates, key=lambda item: (item[1], periods[item[0]]))
    return name, distance


def _reaction(kind: str, bar: DailyBar, ma: Decimal | None) -> bool:
    if ma is None:
        return False
    return (
        bar.signal.low <= ma and bar.signal.close >= ma
        if kind == "LOW"
        else bar.signal.high >= ma and bar.signal.close <= ma
    )


def _role_distribution(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    counts = {name: sum(row.get(field) == name for row in rows) for name in MA_NAMES}
    total = sum(counts.values())
    return {
        "count": total,
        "counts": counts,
        "probabilities": {
            name: Decimal(count) / Decimal(total) if total else None
            for name, count in counts.items()
        },
    }


def _role_period_summary(
    rows: Sequence[Mapping[str, Any]], field: str
) -> dict[str, Any]:
    period = {f"MA{horizon}": Decimal(horizon) for horizon in HORIZONS}
    values = [period[row[field]] for row in rows if row.get(field) in period]
    return {
        "count": len(values),
        "median": _median(values),
        "mean": sum(values, Decimal(0)) / Decimal(len(values)) if values else None,
    }


def _tvd(first: Mapping[str, Any], second: Mapping[str, Any]) -> Decimal | None:
    if not first["count"] or not second["count"]:
        return None
    return sum(
        (
            abs(first["probabilities"][name] - second["probabilities"][name])
            for name in MA_NAMES
        ),
        Decimal(0),
    ) / Decimal(2)


def _pivot_records(
    bars: Sequence[DailyBar],
    calendar_rows: Sequence[Mapping[str, Any]],
    tau_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
    points = tuple(calculate_daily_indicators(bars, calendar))
    base = _pivot_rows(bars, points, calendar_rows)
    index_by_date = {bar.trade_date: index for index, bar in enumerate(bars)}
    calendar_by_date = {row["trade_date"]: row for row in calendar_rows}
    tau_by_date = {row["trade_date"]: row for row in tau_rows}
    output: list[dict[str, Any]] = []
    for source in base:
        day = source["pivot_trade_date"]
        bar = bars[index_by_date[day]]
        tau = tau_by_date[day]
        market = calendar_by_date[day]
        calendar_values = {
            f"MA{horizon}": market.get(f"sma{horizon}") for horizon in HORIZONS
        }
        mt_values = {f"MA{horizon}": tau.get(f"mtma{horizon}") for horizon in HORIZONS}
        calendar_name, calendar_distance = _nearest_role(
            source["pivot_price"], calendar_values, source.get("atr20")
        )
        mt_name, mt_distance = _nearest_role(
            source["pivot_price"], mt_values, source.get("atr20")
        )
        row = {
            **source,
            "calendar_nearest_ma": calendar_name,
            "calendar_nearest_distance_atr": calendar_distance,
            "market_time_nearest_ma": mt_name,
            "market_time_nearest_distance_atr": mt_distance,
        }
        for horizon in HORIZONS:
            name = f"MA{horizon}"
            row[f"calendar_reaction_{horizon}"] = _reaction(
                row["pivot_kind"], bar, calendar_values[name]
            )
            row[f"market_time_reaction_{horizon}"] = _reaction(
                row["pivot_kind"], bar, mt_values[name]
            )
        output.append(row)
    return output


def _regimes(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
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


def _reaction_matrix(
    rows: Sequence[Mapping[str, Any]], *, basis: str
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind in ("LOW", "HIGH"):
        group = [row for row in rows if row["pivot_kind"] == kind]
        result[kind] = {
            f"MA{horizon}": {
                "reaction_count": sum(
                    row[f"{basis}_reaction_{horizon}"] for row in group
                ),
                "pivot_count": len(group),
            }
            for horizon in HORIZONS
        }
    return result


def _cross(
    current_close: Decimal,
    previous_close: Decimal,
    current_ma: Decimal | None,
    previous_ma: Decimal | None,
) -> int | None:
    if current_ma is None or previous_ma is None:
        return None
    if previous_close <= previous_ma and current_close > current_ma:
        return 1
    if previous_close >= previous_ma and current_close < current_ma:
        return -1
    return None


def _cross_event_summary(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in (1, 3, 5):
        available = [
            row for row in events if row.get(f"same_side_d{horizon}") is not None
        ]
        same = sum(row.get(f"same_side_d{horizon}") is True for row in available)
        recross = sum(
            row.get(f"opposite_recross_within_{horizon}") is True for row in available
        )
        result[f"d{horizon}"] = {
            "available_count": len(available),
            "same_side_rate": Decimal(same) / Decimal(len(available))
            if available
            else None,
            "opposite_recross_rate": Decimal(recross) / Decimal(len(available))
            if available
            else None,
        }
    return result


def _cross_regimes(
    rows: Sequence[Mapping[str, Any]],
    compression: Mapping[tuple[str, date], str | None],
    bars_by_stock: Mapping[str, Sequence[DailyBar]],
) -> dict[str, Any]:
    """Compare exact calendar/market-time MA10 crosses by existing C1..C4."""
    output: dict[str, Any] = {}
    for source_name, field in (("calendar", "sma10"), ("market_time", "mtma10")):
        events: list[dict[str, Any]] = []
        by_stock: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            by_stock.setdefault(row["stock_code"], []).append(row)
        for stock_code, stock_rows in by_stock.items():
            stock_rows.sort(key=lambda row: row["trade_date"])
            index_by_date = {
                bar.trade_date: index
                for index, bar in enumerate(bars_by_stock[stock_code])
            }
            for index in range(1, len(stock_rows)):
                row, previous = stock_rows[index], stock_rows[index - 1]
                direction = _cross(
                    bars_by_stock[stock_code][
                        index_by_date[row["trade_date"]]
                    ].signal.close,
                    bars_by_stock[stock_code][
                        index_by_date[previous["trade_date"]]
                    ].signal.close,
                    row.get(field),
                    previous.get(field),
                )
                if direction is None:
                    continue
                event = {
                    "stock_code": stock_code,
                    "trade_date": row["trade_date"],
                    "bucket": compression.get((stock_code, row["trade_date"])),
                    "direction": direction,
                }
                for horizon in (1, 3, 5):
                    later = (
                        stock_rows[index + horizon]
                        if index + horizon < len(stock_rows)
                        else None
                    )
                    event[f"same_side_d{horizon}"] = (
                        direction
                        * (
                            bars_by_stock[stock_code][
                                index_by_date[later["trade_date"]]
                            ].signal.close
                            - later[field]
                        )
                        >= 0
                        if later is not None and later.get(field) is not None
                        else None
                    )
                    future = stock_rows[index + 1 : index + horizon + 1]
                    event[f"opposite_recross_within_{horizon}"] = (
                        any(
                            _cross(
                                bars_by_stock[stock_code][
                                    index_by_date[candidate["trade_date"]]
                                ].signal.close,
                                bars_by_stock[stock_code][
                                    index_by_date[
                                        stock_rows[stock_rows.index(candidate) - 1][
                                            "trade_date"
                                        ]
                                    ]
                                ].signal.close,
                                candidate.get(field),
                                stock_rows[stock_rows.index(candidate) - 1].get(field),
                            )
                            == -direction
                            for candidate in future
                        )
                        if len(future) == horizon
                        else None
                    )
                events.append(event)
        buckets: dict[str, Any] = {}
        for bucket in ("C1", "C2", "C3", "C4"):
            sessions = [
                row
                for row in rows
                if compression.get((row["stock_code"], row["trade_date"])) == bucket
            ]
            group = [event for event in events if event["bucket"] == bucket]
            buckets[bucket] = {
                "session_count": len(sessions),
                "event_count": len(group),
                "events_per_100_sessions": Decimal(len(group))
                * Decimal(100)
                / Decimal(len(sessions))
                if sessions
                else None,
                **_cross_event_summary(group),
            }
        output[source_name] = buckets
    return output


def _load_v02_compression(path: Path) -> dict[tuple[str, date], str | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        (row["stock_code"], date.fromisoformat(row["trade_date"])): row.get(
            "compression_quartile"
        )
        for row in payload["rows"]
    }


def _visual_anchors(
    tau_by_key: Mapping[tuple[str, date], Mapping[str, Any]], mapping_path: Path
) -> list[dict[str, Any]]:
    if not mapping_path.exists():
        return []
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    result: list[dict[str, Any]] = []
    for case in payload.get("cases", []):
        key = (case["stock_code"], date.fromisoformat(case["event_date"]))
        row = tau_by_key[key]
        item = {
            "case_id": case["case_id"],
            "stock_code": key[0],
            "event_date": key[1],
            "delta_tau_t": row["delta_tau"],
        }
        for horizon in HORIZONS:
            item[f"cal_eq_{horizon}"] = row[f"cal_eq_{horizon}"]
            item[f"mtma{horizon}"] = row[f"mtma{horizon}"]
            item[f"calendar_vs_mtma_gap_{horizon}"] = (
                row.get(f"sma{horizon}") - row.get(f"mtma{horizon}")
                if row.get(f"sma{horizon}") is not None
                and row.get(f"mtma{horizon}") is not None
                else None
            )
        result.append(item)
    return result


def _hypotheses(report: Mapping[str, Any]) -> dict[str, str]:
    role = report["role_normalization"]
    calendar_tvd, market_tvd = (
        role["calendar_tvd_fast_vs_slow"],
        role["market_time_tvd_fast_vs_slow"],
    )
    h1 = (
        "SUPPORTED"
        if calendar_tvd is not None
        and market_tvd is not None
        and market_tvd < calendar_tvd
        else "NOT_SUPPORTED"
    )
    crosses = report["compression_crosses"]
    calendar_gap = abs(
        crosses["calendar"]["C1"]["events_per_100_sessions"]
        - crosses["calendar"]["C4"]["events_per_100_sessions"]
    )
    market_gap = abs(
        crosses["market_time"]["C1"]["events_per_100_sessions"]
        - crosses["market_time"]["C4"]["events_per_100_sessions"]
    )
    h2 = "SUPPORTED" if market_gap < calendar_gap else "NOT_SUPPORTED"
    horizons = report["horizon_by_regime"]
    fast = horizons["FAST_DIRECTIONAL_HIGH_EFF"]["cal_eq_20"]["median"]
    slow = horizons["SLOW"]["cal_eq_20"]["median"]
    h3 = (
        "SUPPORTED"
        if fast is not None and slow is not None and fast < slow
        else "NOT_SUPPORTED"
    )
    h4 = (
        "SUPPORTED"
        if fast is not None and slow is not None and slow > fast
        else "NOT_SUPPORTED"
    )
    resolution = report["daily_resolution"]
    h5 = "SUPPORTED" if resolution["day_exceeds_counts"]["H5"] else "NOT_SUPPORTED"
    return {
        "H1_ROLE_NORMALIZATION": h1,
        "H2_COMPRESSION_CROSS_NORMALIZATION": h2,
        "H3_FAST_SHORTER_CALENDAR_EQUIVALENT": h3,
        "H4_SLOW_LONGER_CALENDAR_EQUIVALENT": h4,
        "H5_INTRADAY_RESOLUTION_EVIDENCE": h5,
    }


def run_market_time_normalization_audit(
    *,
    output: Path = OUTPUT_PATH,
    v02_path: Path = V02_OUTPUT_PATH,
    visual_mapping_path: Path = VISUAL_MAPPING_PATH,
    stocks: Sequence[str] = STOCKS,
) -> dict[str, Any]:
    """Run the cached-Daily, no-network normalization comparison."""
    bars_by_stock: dict[str, tuple[DailyBar, ...]] = {}
    tau_by_stock: dict[str, tuple[dict[str, Any], ...]] = {}
    rows: list[dict[str, Any]] = []
    pivot_rows: list[dict[str, Any]] = []
    for stock_code in sorted(stocks):
        bars = tuple(sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date))
        bars_by_stock[stock_code] = bars
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        points = tuple(calculate_daily_indicators(bars, calendar))
        clock = list(_clock_series(bars, points))
        tau = [dict(row) for row in market_time_series(bars)]
        clock_by_date = {row["trade_date"]: row for row in clock}
        for row in tau:
            row.update(
                {
                    key: value
                    for key, value in clock_by_date[row["trade_date"]].items()
                    if key not in row
                }
            )
            row["sma5"] = simple_moving_average([bar.signal.close for bar in bars], 5)[
                row["_index"]
            ]
            row["sma10"] = points[row["_index"]].sma10
            row["sma20"] = points[row["_index"]].sma20
            row["sma60"] = points[row["_index"]].sma60
        tau_by_stock[stock_code] = tuple(tau)
        research_tau = [
            row for row in tau if RESEARCH_START <= row["trade_date"] <= RESEARCH_END
        ]
        rows.extend(research_tau)
        pivot_rows.extend(_pivot_records(bars, clock, tau))
    rows.sort(key=lambda row: (row["stock_code"], row["trade_date"]))
    _assign_clock_quartiles(rows)
    quartile_by_key = {(row["stock_code"], row["trade_date"]): row for row in rows}
    for pivot in pivot_rows:
        clock_row = quartile_by_key.get(
            (pivot["stock_code"], pivot["pivot_trade_date"])
        )
        if clock_row:
            pivot.update(
                {
                    field: clock_row.get(field)
                    for field in (
                        "range_speed_quartile",
                        "direction_speed_quartile",
                        "efficiency_10_quartile",
                    )
                }
            )
    valid_pivots = [row for row in pivot_rows if row.get("market_time_nearest_ma")]
    groups = _regimes(valid_pivots)
    calendar_roles = {
        name: _role_distribution(group, "calendar_nearest_ma")
        for name, group in groups.items()
    }
    market_roles = {
        name: _role_distribution(group, "market_time_nearest_ma")
        for name, group in groups.items()
    }
    fast_name = "FAST_DIRECTIONAL_HIGH_EFF"
    slow_name = "SLOW"
    compression = _load_v02_compression(v02_path)
    crosses = _cross_regimes(rows, compression, bars_by_stock)
    tau_by_key = {(row["stock_code"], row["trade_date"]): row for row in rows}
    horizon_by_regime = {
        name: {
            f"cal_eq_{horizon}": _distribution(
                [
                    row[f"cal_eq_{horizon}"]
                    for row in group
                    if row.get(f"cal_eq_{horizon}") is not None
                ]
            )
            for horizon in HORIZONS
        }
        for name, group in {"ALL_VALID": rows, **_regimes(rows)}.items()
    }
    resolution_rows = [row for row in rows if row.get("delta_tau") is not None]
    resolution = {
        "day_exceeds_counts": {
            f"H{horizon}": sum(
                row.get(f"day_exceeds_{horizon}") is True for row in resolution_rows
            )
            for horizon in (5, 10, 20)
        },
        "max_single_day_tau_share": {
            f"H{horizon}": _distribution(
                [
                    row[f"max_single_day_tau_share_{horizon}"]
                    for row in resolution_rows
                    if row.get(f"max_single_day_tau_share_{horizon}") is not None
                ]
            )
            for horizon in (5, 10, 20)
        },
        "top20": [
            {
                "stock_code": row["stock_code"],
                "trade_date": row["trade_date"],
                "delta_tau": row["delta_tau"],
                **{
                    f"max_single_day_tau_share_{horizon}": row.get(
                        f"max_single_day_tau_share_{horizon}"
                    )
                    for horizon in (5, 10, 20)
                },
            }
            for row in sorted(
                resolution_rows,
                key=lambda item: (
                    -(item.get("max_single_day_tau_share_5") or Decimal(-1)),
                    item["stock_code"],
                    item["trade_date"],
                ),
            )[:20]
        ],
    }
    report: dict[str, Any] = {
        "proof_version": PROOF_VERSION,
        "network_calls": 0,
        "research_period": {"start": RESEARCH_START, "end": RESEARCH_END},
        "source_v02": v02_path.as_posix(),
        "methodology": {
            "signal_price_basis": "ADJUSTED_DAILY_OHLC",
            "reference_tr": "median of previous 252 valid TR_PCT sessions; current T excluded",
            "delta_tau": "TR_PCT[T] / REFERENCE_TR[T]; no clip/cap/log transform",
            "mtma": "overlap-weighted Daily-close approximation over tau intervals; no synthetic intraday bars",
            "outcome_or_pnl": "not used",
            "strategy_changes": False,
            "orders": False,
            "pnl": False,
            "charts_generated": False,
        },
        "population": {
            "stock_count": len(stocks),
            "research_session_count": len(rows),
            "valid_delta_tau_count": len(resolution_rows),
            "insufficient_clock_history_count": sum(
                row["delta_tau"] is None for row in rows
            ),
            "valid_mtma60_count": sum(row.get("mtma60") is not None for row in rows),
            "pivot_count": len(pivot_rows),
            "valid_market_time_pivot_count": len(valid_pivots),
        },
        "delta_tau_distribution": _distribution(
            [row["delta_tau"] for row in resolution_rows]
        ),
        "calendar_equivalent_distributions": {
            f"cal_eq_{horizon}": _distribution(
                [
                    row[f"cal_eq_{horizon}"]
                    for row in rows
                    if row.get(f"cal_eq_{horizon}") is not None
                ]
            )
            for horizon in HORIZONS
        },
        "horizon_by_regime": horizon_by_regime,
        "role_normalization": {
            "groups": {
                name: {
                    "calendar": calendar_roles[name],
                    "market_time": market_roles[name],
                    "calendar_median_period": _role_period_summary(
                        groups[name], "calendar_nearest_ma"
                    ),
                    "market_time_median_period": _role_period_summary(
                        groups[name], "market_time_nearest_ma"
                    ),
                }
                for name in groups
            },
            "calendar_tvd_fast_vs_slow": _tvd(
                calendar_roles[fast_name], calendar_roles[slow_name]
            ),
            "market_time_tvd_fast_vs_slow": _tvd(
                market_roles[fast_name], market_roles[slow_name]
            ),
        },
        "reaction_matrix": {
            "calendar": _reaction_matrix(valid_pivots, basis="calendar"),
            "market_time": _reaction_matrix(valid_pivots, basis="market_time"),
        },
        "compression_crosses": crosses,
        "daily_resolution": resolution,
        "visual_anchors": _visual_anchors(tau_by_key, visual_mapping_path),
    }
    report["hypotheses"] = _hypotheses(report)
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
    report = run_market_time_normalization_audit(output=args.output)
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
