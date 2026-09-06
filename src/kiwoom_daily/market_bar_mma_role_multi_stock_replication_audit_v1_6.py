"""Multi-stock replication of the frozen V1.3 Market-Bar MMA audit.

The audit is deliberately descriptive.  It consumes the confirmed V1.2C and
V1.5 artifacts, reuses V1.3's MMA/ATR/pivot/reaction/cross/turn definitions,
and never fetches data or produces strategy/PnL output.
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
    detect_daily_pivots,
)
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar

from .down_box_daily_execution_proof import _load_stock
from .market_bar_200_pilot_resume_materialization import (
    OUTPUT_PATH as BASELINE_INPUT_PATH,
)
from .market_bar_mma_role_replication_candidate_plan_v1_4 import (
    MARKET_CLOCK_REFERENCE_PATH,
    _daily_timeline,
    _load_frozen_cuts,
)
from .market_bar_mma_role_stability_audit import (
    MIXED_REGIME,
    PERIODS,
    REGIME_NAMES,
    _attach_source_regimes,
    _calendar_reaction_report,
    _calendar_role_report,
    _compression_cross_report,
    _daily_regime_label,
    _decorate_market_values,
    _market_pivots,
    _nearest_role,
    _reaction_profile_distance,
    _reaction_regime_report,
    _reaction_signatures,
    _role_comparison,
    _role_regime_report,
    _turn_report,
    _tvd,
)
from .market_bar_mma_visual_proof import _render_chart
from .market_bar_zero_fetch_replication_materialization_v1_5 import (
    OUTPUT_PATH as V15_INPUT_PATH,
)
from .market_speed_audit import _atr20

OUTPUT_PATH = Path(
    "data/processed/strategy_review/"
    "market_bar_mma_role_multi_stock_replication_audit_v1_6.json"
)
VISUAL_ROOT = Path(
    "data/processed/strategy_charts/"
    "market_bar_mma_role_multi_stock_replication_audit_v1_6"
)
V15_CHECKPOINT = "9ff1ad816b14c86cee146797e296927d69db863e"
BASELINE_STOCK = "066570"
REPLICATION_STOCKS = ("035420", "105560")
STREAMS = (BASELINE_STOCK, *REPLICATION_STOCKS)
STREAM_WINDOWS = {
    "066570": (date(2026, 2, 2), date(2026, 5, 28)),
    "035420": (date(2025, 12, 15), date(2026, 6, 19)),
    "105560": (date(2026, 1, 5), date(2026, 7, 13)),
}
EXPECTED_MARKET_BARS = {"066570": 201, "035420": 200, "105560": 200}
BASELINE_CROSS_REFERENCE = {
    "MMA10": {
        "C1": Decimal(25),
        "C2": Decimal("23.8"),
        "C3": Decimal("9.4"),
        "C4": Decimal(8),
    },
    "MMA20": {
        "C1": Decimal("22.2"),
        "C2": Decimal("9.5"),
        "C3": Decimal("1.9"),
        "C4": Decimal(0),
    },
}


class MultiStockReplicationError(ValueError):
    """The frozen V1.6 input streams or baseline regression are invalid."""


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _load_v15_streams(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("network_calls") != 0:
        raise MultiStockReplicationError("V1.5 network_calls is not zero")
    streams: dict[str, dict[str, Any]] = {}
    for label, expected_stock in (
        ("REPLICATION_B", "035420"),
        ("REPLICATION_C", "105560"),
    ):
        stream = payload.get("streams", {}).get(label)
        if not isinstance(stream, dict) or stream.get("stock_code") != expected_stock:
            raise MultiStockReplicationError(f"V1.5 stream missing: {label}")
        if stream.get("replication_readiness") != "REPLICATION_READY":
            raise MultiStockReplicationError(f"V1.5 stream is not ready: {label}")
        start, end = STREAM_WINDOWS[expected_stock]
        if (
            stream.get("calendar_start") != start.isoformat()
            or stream.get("calendar_end") != end.isoformat()
        ):
            raise MultiStockReplicationError(f"V1.5 window changed: {label}")
        bars = stream.get("market_bars")
        if (
            not isinstance(bars, list)
            or len(bars) != EXPECTED_MARKET_BARS[expected_stock]
        ):
            raise MultiStockReplicationError(f"V1.5 bar count changed: {label}")
        streams[expected_stock] = {
            "label": label,
            "calendar_start": start,
            "calendar_end": end,
            "bars": bars,
            "source": path.as_posix(),
        }
    return streams


def _normalize_market_rows(
    raw_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for expected, item in enumerate(raw_rows, start=1):
        row = dict(item)
        row["market_bar_index"] = expected
        row["open"] = _decimal(row["open"])
        row["high"] = _decimal(row["high"])
        row["low"] = _decimal(row["low"])
        row["close"] = _decimal(row["close"])
        row["volume"] = _decimal(row["volume"])
        row["tau_length"] = _decimal(row["tau_length"])
        row["boundary_error"] = _decimal(row["boundary_error"])
        rows.append(row)
    return rows


def _load_baseline_rows(path: Path = BASELINE_INPUT_PATH) -> list[dict[str, Any]]:
    from .market_bar_mma_role_stability_audit import _load_market_rows

    rows = _load_market_rows(path)
    if len(rows) != EXPECTED_MARKET_BARS[BASELINE_STOCK]:
        raise MultiStockReplicationError("baseline Market-Bar count changed")
    regimes, _, _ = _daily_regime_reference(BASELINE_STOCK)
    _attach_source_regimes(rows, regimes)
    return rows


def _daily_regime_reference(
    stock_code: str,
) -> tuple[dict[date, str], tuple[Any, ...], tuple[dict[str, Any], ...]]:
    bars = tuple(sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date))
    cuts = _load_frozen_cuts(MARKET_CLOCK_REFERENCE_PATH)
    timeline = _daily_timeline(stock_code, cuts)
    regimes = {
        row["trade_date"]: str(row["source_calendar_regime"]) for row in timeline
    }
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
    indicators = tuple(calculate_daily_indicators(bars, calendar))
    del indicators  # the frozen timeline already carries the same indicator stream.
    return regimes, bars, tuple(timeline)


def _load_stream_rows(
    stock_code: str,
    v15_streams: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], date, date, str]:
    if stock_code == BASELINE_STOCK:
        rows = _load_baseline_rows()
        start, end = STREAM_WINDOWS[stock_code]
        return rows, start, end, BASELINE_INPUT_PATH.as_posix()
    stream = v15_streams[stock_code]
    rows = _normalize_market_rows(stream["bars"])
    return rows, stream["calendar_start"], stream["calendar_end"], stream["source"]


def _calendar_pivots_for_window(
    bars: Sequence[Any],
    timeline: Sequence[Mapping[str, Any]],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    clock_by_date = {row["trade_date"]: row for row in timeline}
    index_by_date = {bar.trade_date: index for index, bar in enumerate(canonical)}
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in canonical)
    result: list[dict[str, Any]] = []
    for pivot in detect_daily_pivots(canonical, calendar):
        if not start <= pivot.pivot_trade_date <= end:
            continue
        if pivot.confirmed_at.date() > end:
            continue
        index = index_by_date[pivot.pivot_trade_date]
        row = clock_by_date[pivot.pivot_trade_date]
        bar = canonical[index]
        atr = _atr20(canonical, index)
        values = {f"MMA{period}": row.get(f"sma{period}") for period in PERIODS}
        role, distance, ties = _nearest_role(pivot.price, values, atr)
        item: dict[str, Any] = {
            "pivot_kind": pivot.kind.value,
            "pivot_trade_date": pivot.pivot_trade_date,
            "confirmed_at": pivot.confirmed_at,
            "pivot_price": pivot.price,
            "nearest_mma_role": role,
            "nearest_distance_atr": distance,
            "nearest_tie_count": ties,
            "primary_source_regime": _daily_regime_label(row),
        }
        for name, value in values.items():
            for label, signal in _reaction_signatures(
                pivot.kind.value,
                low=bar.signal.low,
                high=bar.signal.high,
                close=bar.signal.close,
                ma=value,
            ).items():
                item[f"{name}_{label}"] = signal
        result.append(item)
    return result


def _tvd_result(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    value = _tvd(first["nearest_role"], second["nearest_role"])
    return {
        "value": value,
        "status": "AVAILABLE" if value is not None else "NOT_AVAILABLE",
        "first_count": first["nearest_role"]["count"],
        "second_count": second["nearest_role"]["count"],
    }


def _fast_slow_comparison(
    market_roles: Mapping[str, Any], calendar_roles: Mapping[str, Any]
) -> dict[str, Any]:
    market = _tvd_result(
        market_roles["FAST_DIRECTIONAL_HIGH_EFF"], market_roles["SLOW"]
    )
    calendar = _tvd_result(
        calendar_roles["FAST_DIRECTIONAL_HIGH_EFF"], calendar_roles["SLOW"]
    )
    if market["value"] is None or calendar["value"] is None:
        status = "INSUFFICIENT_SAMPLE"
    elif market["value"] < calendar["value"]:
        status = "MARKET_BAR_SMALLER"
    elif market["value"] > calendar["value"]:
        status = "CALENDAR_SMALLER"
    else:
        status = "EQUAL"
    return {"market_bar": market, "calendar_daily": calendar, "comparison": status}


def _reaction_stability(
    market_reaction: Mapping[str, Any], calendar_reaction: Mapping[str, Any]
) -> dict[str, Any]:
    market_distance = _reaction_profile_distance(
        market_reaction["FAST_DIRECTIONAL_HIGH_EFF"],
        market_reaction["SLOW"],
    )
    calendar_distance = _reaction_profile_distance(
        calendar_reaction["FAST_DIRECTIONAL_HIGH_EFF"],
        calendar_reaction["SLOW"],
    )
    if market_distance is None or calendar_distance is None:
        status = "INSUFFICIENT_SAMPLE"
    elif market_distance < calendar_distance:
        status = "MARKET_BAR_SMALLER"
    elif market_distance > calendar_distance:
        status = "CALENDAR_SMALLER"
    else:
        status = "EQUAL"
    return {
        "market_fast_vs_slow_distance": market_distance,
        "calendar_fast_vs_slow_distance": calendar_distance,
        "comparison": status,
    }


def _compression_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for period in (10, 20):
        period_report = report["periods"][f"MMA{period}"]
        result[f"MMA{period}"] = {
            bucket: {
                "market_bar_count": period_report["by_compression_quartile"][bucket][
                    "market_bar_count"
                ],
                "event_count": period_report["by_compression_quartile"][bucket][
                    "event_count"
                ],
                "events_per_100_market_bars": period_report["by_compression_quartile"][
                    bucket
                ]["events_per_100_market_bars"],
            }
            for bucket in ("C1", "C2", "C3", "C4")
        }
    return result


def _compression_pattern(report: Mapping[str, Any], period: int) -> bool | None:
    values = report["periods"][f"MMA{period}"]["by_compression_quartile"]
    if any(
        values[bucket]["market_bar_count"] == 0 for bucket in ("C1", "C2", "C3", "C4")
    ):
        return None
    compressed = [
        values[bucket]["events_per_100_market_bars"] for bucket in ("C1", "C2")
    ]
    expanded = [values[bucket]["events_per_100_market_bars"] for bucket in ("C3", "C4")]
    return all(value is not None for value in compressed + expanded) and min(
        compressed
    ) > max(expanded)


def _visual_cases(
    rows: Sequence[Mapping[str, Any]], stock_code: str, output_root: Path
) -> list[dict[str, Any]]:
    """Choose first qualifying bars from deterministic regime categories."""

    output_root.mkdir(parents=True, exist_ok=True)
    categories = (
        ("PURE_FAST_WINDOW", "PURE_FAST_DIRECTIONAL_HIGH_EFF"),
        ("PURE_SLOW_WINDOW", "PURE_SLOW"),
        ("FAST_SLOW_TRANSITION", MIXED_REGIME),
    )
    cases: list[dict[str, Any]] = []
    for number, (category, regime) in enumerate(categories, start=1):
        candidates = [
            row
            for row in rows
            if row.get("primary_source_regime") == regime
            and row.get("MMA60") is not None
        ]
        if not candidates:
            continue
        anchor = candidates[0]
        anchor_index = int(anchor["market_bar_index"]) - 1
        start = max(0, anchor_index - 20)
        end = min(len(rows), anchor_index + 21)
        window = list(rows[start:end])
        tick_indexes = list(range(0, len(window), 10))
        if (
            tick_indexes[-1] != len(window) - 1
            and len(window) - 1 - tick_indexes[-1] >= 5
        ):
            tick_indexes.append(len(window) - 1)
        case_id = f"{stock_code}_CASE_{number:02d}_{category}"
        chart_path = output_root / f"{case_id}.png"
        _render_chart(
            output_path=chart_path,
            title=f"{stock_code} MARKET BAR {category}",
            rows=window,
            ma_fields=("MMA5", "MMA10", "MMA20", "MMA60"),
            tick_indexes=tick_indexes,
            tick_labels=[
                str(window[index]["market_bar_index"]) for index in tick_indexes
            ],
        )
        metadata = {
            "case_id": case_id,
            "stock_code": stock_code,
            "category": category,
            "selection_basis": "first MMA60-valid bar in deterministic source-regime category",
            "anchor_market_bar_index": anchor["market_bar_index"],
            "anchor_market_bar_id": anchor["market_bar_id"],
            "window_market_bar_start": window[0]["market_bar_index"],
            "window_market_bar_end": window[-1]["market_bar_index"],
            "primary_source_regime": anchor["primary_source_regime"],
            "chart_path": chart_path.as_posix(),
            "x_axis": "MARKET_BAR_INDEX",
        }
        metadata_path = output_root / f"{case_id}.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        cases.append({**metadata, "metadata_path": metadata_path.as_posix()})
    return cases


def _turn_summary(turn_report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: {
            "origin_count": value["overall"]["origin_count"],
            "matched_count": value["overall"]["matched_count"],
            "unmatched_count": value["overall"]["unmatched_count"],
            "lag": value["overall"]["lag"],
            "by_origin_source_regime": {
                regime: value["by_origin_source_regime"][regime]
                for regime in ("PURE_FAST_DIRECTIONAL_HIGH_EFF", "PURE_SLOW")
            },
        }
        for name, value in turn_report["propagation"].items()
    }


def _stream_audit(
    stock_code: str,
    rows: list[dict[str, Any]],
    start: date,
    end: date,
    source: str,
    *,
    timeline: Sequence[Mapping[str, Any]],
    visual_root: Path,
    render_visual: bool = True,
) -> dict[str, Any]:
    _decorate_market_values(rows)
    market_pivots = _market_pivots(rows)
    market_roles = _role_regime_report(market_pivots)
    market_reaction = _reaction_regime_report(market_pivots)
    calendar_bars = tuple(
        sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date)
    )
    calendar_pivots = _calendar_pivots_for_window(calendar_bars, timeline, start, end)
    calendar_roles = _calendar_role_report(calendar_pivots)
    calendar_reaction = _calendar_reaction_report(calendar_pivots)
    compression = _compression_cross_report(rows)
    turns = _turn_report(rows)
    cases = (
        _visual_cases(rows, stock_code, visual_root / stock_code)
        if render_visual
        else []
    )
    primary_market = [row for row in market_pivots if row.get("nearest_mma_role")]
    primary_calendar = [row for row in calendar_pivots if row.get("nearest_mma_role")]
    market_valid = {
        f"MMA{period}": sum(row[f"MMA{period}"] is not None for row in rows)
        for period in PERIODS
    }
    market_valid["MB_ATR20"] = sum(row.get("mb_atr20") is not None for row in rows)
    market_regime_counts = {
        regime: sum(
            row.get("primary_source_regime") == f"PURE_{regime}" for row in rows
        )
        for regime in REGIME_NAMES
    }
    market_regime_counts[MIXED_REGIME] = sum(
        row.get("primary_source_regime") == MIXED_REGIME for row in rows
    )
    role_comparison = _role_comparison(market_roles, calendar_roles)
    return {
        "stock_code": stock_code,
        "source_artifact": source,
        "calendar_window": {"start": start, "end": end},
        "market_bar_count": len(rows),
        "continuous_island_count": 1,
        "mma_valid_counts": market_valid,
        "market_bar_pivots": {
            "pivot_count": len(market_pivots),
            "low_count": sum(row["pivot_kind"] == "LOW" for row in market_pivots),
            "high_count": sum(row["pivot_kind"] == "HIGH" for row in market_pivots),
            "primary_valid_count": len(primary_market),
            "pure_fast_directional_primary_count": sum(
                row.get("primary_source_regime") == "PURE_FAST_DIRECTIONAL_HIGH_EFF"
                for row in primary_market
            ),
            "pure_slow_primary_count": sum(
                row.get("primary_source_regime") == "PURE_SLOW"
                for row in primary_market
            ),
        },
        "calendar_daily_reference": {
            "pivot_count": len(calendar_pivots),
            "primary_valid_count": len(primary_calendar),
            "role_by_regime": calendar_roles,
            "reaction_by_regime": calendar_reaction,
        },
        "source_calendar_regimes": {
            "market_bar_counts": market_regime_counts,
            "role_by_regime": market_roles,
            "reaction_by_regime": market_reaction,
        },
        "nearest_role_distribution": {
            "market_bar": market_roles,
            "calendar_daily": calendar_roles,
        },
        "fast_slow_tvd": _fast_slow_comparison(market_roles, calendar_roles),
        "calendar_vs_market_role_stability": role_comparison,
        "reaction_stability": _reaction_stability(market_reaction, calendar_reaction),
        "compression": {
            "quartile_counts": {
                bucket: sum(row.get("compression_quartile") == bucket for row in rows)
                for bucket in ("C1", "C2", "C3", "C4")
            },
            "crosses": _compression_summary(compression),
            "patterns": {
                "MMA10_C1_C2_higher_than_C3_C4": _compression_pattern(compression, 10),
                "MMA20_C1_C2_higher_than_C3_C4": _compression_pattern(compression, 20),
            },
            "frozen_v13_compression_report": compression,
        },
        "whipsaw_persistence": {
            f"MMA{period}": compression["periods"][f"MMA{period}"][
                "by_compression_quartile"
            ]
            for period in (10, 20)
        },
        "turn_propagation": {
            "turn_counts": turns["turn_counts"],
            "propagation": _turn_summary(turns),
        },
        "visual_pack": {
            "case_count": len(cases),
            "max_case_count": 3,
            "cases": cases,
            "x_axis": "MARKET_BAR_INDEX",
            "calendar_datetime": "metadata_only",
            "reused_v13_pack": not render_visual,
            "reused_source": (
                "data/processed/strategy_charts/market_bar_mma_role_stability_audit_v1_3"
                if not render_visual
                else None
            ),
        },
        "strategy_buy_sell_pnl_changed": False,
    }


def _validate_baseline_regression(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    report = _compression_cross_report(rows)
    observed: dict[str, dict[str, Decimal]] = {}
    observed_rounded: dict[str, dict[str, Decimal]] = {}
    matches = True
    for period in (10, 20):
        observed[f"MMA{period}"] = {}
        observed_rounded[f"MMA{period}"] = {}
        for bucket in ("C1", "C2", "C3", "C4"):
            value = report["periods"][f"MMA{period}"]["by_compression_quartile"][
                bucket
            ]["events_per_100_market_bars"]
            observed[f"MMA{period}"][bucket] = value
            rounded = value.quantize(Decimal("0.1"))
            observed_rounded[f"MMA{period}"][bucket] = rounded
            matches &= rounded == BASELINE_CROSS_REFERENCE[f"MMA{period}"][bucket]
    if not matches:
        raise MultiStockReplicationError(
            "066570 V1.3 compression baseline regression mismatch"
        )
    return {
        "matches_frozen_reference": True,
        "reference_rounding": "one_decimal_place",
        "observed": observed,
        "observed_rounded": observed_rounded,
    }


def _h1(streams: Mapping[str, Mapping[str, Any]]) -> str:
    statuses = [
        streams[stock]["fast_slow_tvd"]["comparison"] for stock in REPLICATION_STOCKS
    ]
    if all(status == "MARKET_BAR_SMALLER" for status in statuses):
        return "SUPPORTED"
    if any(status == "MARKET_BAR_SMALLER" for status in statuses):
        return "PARTIALLY_SUPPORTED"
    if all(status == "INSUFFICIENT_SAMPLE" for status in statuses):
        return "INSUFFICIENT_SAMPLE"
    return "NOT_SUPPORTED"


def _h2(streams: Mapping[str, Mapping[str, Any]]) -> str:
    statuses = [
        streams[stock]["reaction_stability"]["comparison"]
        for stock in REPLICATION_STOCKS
    ]
    if all(status == "MARKET_BAR_SMALLER" for status in statuses):
        return "SUPPORTED"
    if any(status == "MARKET_BAR_SMALLER" for status in statuses):
        return "PARTIALLY_SUPPORTED"
    if all(status == "INSUFFICIENT_SAMPLE" for status in statuses):
        return "INSUFFICIENT_SAMPLE"
    return "NOT_SUPPORTED"


def _h3(streams: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for period in (10, 20):
        values = [
            streams[stock]["compression"]["patterns"][
                f"MMA{period}_C1_C2_higher_than_C3_C4"
            ]
            for stock in REPLICATION_STOCKS
        ]
        result[f"MMA{period}"] = (
            "SUPPORTED"
            if all(value is True for value in values)
            else "PARTIALLY_SUPPORTED"
            if any(value is True for value in values)
            else "INCONCLUSIVE"
            if any(value is None for value in values)
            else "NOT_SUPPORTED"
        )
    return result


def _h4(streams: Mapping[str, Mapping[str, Any]]) -> str:
    available = []
    for stock in REPLICATION_STOCKS:
        for item in streams[stock]["turn_propagation"]["propagation"].values():
            overall = item["origin_count"]
            fast = item["by_origin_source_regime"]["PURE_FAST_DIRECTIONAL_HIGH_EFF"][
                "origin_count"
            ]
            slow = item["by_origin_source_regime"]["PURE_SLOW"]["origin_count"]
            available.append(overall > 0 and fast > 0 and slow > 0)
    if all(available):
        return "SUPPORTED"
    if any(available):
        return "PARTIALLY_SUPPORTED"
    return "INCONCLUSIVE"


def run_audit(
    *,
    output_path: Path = OUTPUT_PATH,
    visual_root: Path = VISUAL_ROOT,
    baseline_input: Path = BASELINE_INPUT_PATH,
    v15_input: Path = V15_INPUT_PATH,
) -> dict[str, Any]:
    v15_streams = _load_v15_streams(v15_input)
    streams: dict[str, dict[str, Any]] = {}
    baseline_rows = _load_baseline_rows(baseline_input)
    _decorate_market_values(baseline_rows)
    baseline_regression = _validate_baseline_regression(baseline_rows)
    for stock_code in STREAMS:
        if stock_code == BASELINE_STOCK:
            rows = baseline_rows
            source = BASELINE_INPUT_PATH.as_posix()
        else:
            rows = _normalize_market_rows(v15_streams[stock_code]["bars"])
            source = v15_streams[stock_code]["source"]
        start, end = STREAM_WINDOWS[stock_code]
        _, _daily_bars, timeline = _daily_regime_reference(stock_code)
        streams[stock_code] = _stream_audit(
            stock_code,
            rows,
            start,
            end,
            source,
            timeline=timeline,
            visual_root=visual_root,
            render_visual=stock_code != BASELINE_STOCK,
        )
    report = {
        "audit_version": "MARKET_BAR_MMA_ROLE_MULTI_STOCK_REPLICATION_AUDIT_V1_6",
        "v15_checkpoint": V15_CHECKPOINT,
        "network_calls": 0,
        "input_streams": {
            stock: {
                "stock_code": stock,
                "calendar_window": {
                    "start": STREAM_WINDOWS[stock][0],
                    "end": STREAM_WINDOWS[stock][1],
                },
                "expected_market_bars": EXPECTED_MARKET_BARS[stock],
                "actual_market_bars": streams[stock]["market_bar_count"],
                "continuous_island_count": streams[stock]["continuous_island_count"],
                "source": streams[stock]["source_artifact"],
            }
            for stock in STREAMS
        },
        "streams": streams,
        "baseline_v13_regression": baseline_regression,
        "primary_comparison": {
            stock: streams[stock]["fast_slow_tvd"] for stock in REPLICATION_STOCKS
        },
        "cross_stock_summary": [
            {
                "stock_code": stock,
                "fast_pivot_count": streams[stock]["market_bar_pivots"][
                    "pure_fast_directional_primary_count"
                ],
                "slow_pivot_count": streams[stock]["market_bar_pivots"][
                    "pure_slow_primary_count"
                ],
                "calendar_tvd": streams[stock]["fast_slow_tvd"]["calendar_daily"][
                    "value"
                ],
                "market_bar_tvd": streams[stock]["fast_slow_tvd"]["market_bar"][
                    "value"
                ],
                "difference": (
                    streams[stock]["fast_slow_tvd"]["market_bar"]["value"]
                    - streams[stock]["fast_slow_tvd"]["calendar_daily"]["value"]
                    if streams[stock]["fast_slow_tvd"]["market_bar"]["value"]
                    is not None
                    and streams[stock]["fast_slow_tvd"]["calendar_daily"]["value"]
                    is not None
                    else None
                ),
                "direction": streams[stock]["fast_slow_tvd"]["comparison"],
            }
            for stock in REPLICATION_STOCKS
        ],
        "hypotheses": {
            "H1_market_bar_fast_slow_tvd_smaller_than_calendar": _h1(streams),
            "H2_market_bar_reaction_shift_smaller_than_calendar": _h2(streams),
            "H3_compressed_c1_c2_crosses_exceed_c3_c4": _h3(streams),
            "H4_turn_propagation_scale_exists_without_regime_collapse": _h4(streams),
            "H5_visual_market_time_hierarchy_readable": "SUPPORTED"
            if all(
                streams[stock]["visual_pack"]["case_count"] == 3
                for stock in REPLICATION_STOCKS
            )
            and streams[BASELINE_STOCK]["visual_pack"]["reused_v13_pack"]
            else "INCONCLUSIVE",
        },
        "overall_replication_result": (
            "REPLICATION_SUPPORTED"
            if _h1(streams) == "SUPPORTED"
            else "REPLICATION_PARTIAL"
            if _h1(streams) == "PARTIALLY_SUPPORTED"
            else "REPLICATION_INCONCLUSIVE"
            if _h1(streams) == "INSUFFICIENT_SAMPLE"
            else "REPLICATION_NOT_SUPPORTED"
        ),
        "limitations": {
            "three_stock_replication_only": True,
            "market_population_generalization": False,
            "statistical_significance_claimed": False,
            "pooled_primary_claim": False,
            "pooled_secondary_table": "not used for primary judgments",
        },
        "frozen_infrastructure": {
            "global_activity_tau": True,
            "integer_target_lattice": True,
            "market_bar_geometry_changed": False,
            "regime_semantics_changed": False,
            "mma_periods": [5, 10, 20, 60],
            "pivot_left_right": [2, 2],
            "strategy_buy_sell_pnl_changed": False,
        },
        "notes": [
            "066570 is supplementary because it has no PURE_SLOW Market-Bar population.",
            "035420 and 105560 are the primary FAST-vs-SLOW replication stocks.",
            "All role, reaction, compression, cross, turn, and matching semantics are reused from V1.3.",
            "No pooled primary claim or optimization threshold was introduced.",
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--visual-root", type=Path, default=VISUAL_ROOT)
    parser.add_argument("--baseline-input", type=Path, default=BASELINE_INPUT_PATH)
    parser.add_argument("--v15-input", type=Path, default=V15_INPUT_PATH)
    args = parser.parse_args()
    report = run_audit(
        output_path=args.output,
        visual_root=args.visual_root,
        baseline_input=args.baseline_input,
        v15_input=args.v15_input,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "network_calls": report["network_calls"],
                "overall_replication_result": report["overall_replication_result"],
                "hypotheses": report["hypotheses"],
                "market_bar_counts": {
                    stock: report["streams"][stock]["market_bar_count"]
                    for stock in STREAMS
                },
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
