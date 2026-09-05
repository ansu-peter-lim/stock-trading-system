"""Descriptive MMA-role stability audit for the frozen V1.2C Market-Bar pilot.

This module consumes only the completed 066570 / 201-bar V1.2C stream for
Market-Bar calculations.  Calendar-Daily data is used solely as source-regime
metadata and as a report-only reference.  It never changes Market-Bar
geometry, acquires data, creates trading signals, or calculates returns/PnL.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import (
    calculate_daily_indicators,
    detect_daily_pivots,
    simple_moving_average,
)
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar

from .down_box_daily_execution_proof import _load_stock
from .market_bar_200_pilot_resume_materialization import (
    ANCHOR_STOCK,
    TARGET,
    WINDOW_END,
    WINDOW_START,
)
from .market_bar_200_pilot_resume_materialization import OUTPUT_PATH as V12C_PATH
from .market_bar_mma_visual_proof import _render_chart
from .market_clock_audit import (
    RESEARCH_END,
    RESEARCH_START,
    _clock_series,
    _median,
    _percentile,
)
from .market_speed_audit import _atr20

PROOF_VERSION = "MARKET_BAR_MMA_ROLE_STABILITY_AUDIT_V1_3"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/market_bar_mma_role_stability_audit_v1_3.json"
)
VISUAL_ROOT = Path(
    "data/processed/strategy_charts/market_bar_mma_role_stability_audit_v1_3"
)
MARKET_CLOCK_REFERENCE_PATH = Path(
    "data/processed/strategy_review/market_clock_ma_role_audit_v0_1.json"
)
PERIODS = (5, 10, 20, 60)
ROLE_NAMES = tuple(f"MMA{period}" for period in PERIODS)
REGIME_NAMES = (
    "FAST_DIRECTIONAL_HIGH_EFF",
    "FAST_NOISY",
    "SLOW",
    "NORMAL_OTHER",
)
PURE_REGIME_NAMES = tuple(f"PURE_{name}" for name in REGIME_NAMES)
MIXED_REGIME = "MIXED_SOURCE_REGIME"


class MarketBarRoleAuditError(ValueError):
    """The immutable V1.2C pilot cannot satisfy the V1.3 input contract."""


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _distribution(values: Sequence[Decimal]) -> dict[str, Any]:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "p25": _percentile(values, Decimal("0.25")),
        "median": _percentile(values, Decimal("0.50")),
        "p75": _percentile(values, Decimal("0.75")),
        "p90": _percentile(values, Decimal("0.90")),
        "max": max(values) if values else None,
    }


def _quartile(value: Decimal | None, values: Sequence[Decimal]) -> str | None:
    if value is None or not values:
        return None
    q25 = _percentile(values, Decimal("0.25"))
    q50 = _percentile(values, Decimal("0.50"))
    q75 = _percentile(values, Decimal("0.75"))
    if value <= q25:
        return "C1"
    if value <= q50:
        return "C2"
    if value <= q75:
        return "C3"
    return "C4"


def _load_market_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    freeze = payload.get("plan_freeze", {})
    materialization = payload.get("materialization", {})
    if (
        payload.get("audit_version")
        != "MARKET_BAR_200_PILOT_RESUME_MATERIALIZATION_V1_2C"
        or payload.get("result") != "MARKET_BAR_200_STREAM_READY"
        or freeze.get("stock_code") != ANCHOR_STOCK
        or freeze.get("calendar_start") != WINDOW_START.isoformat()
        or freeze.get("calendar_end") != WINDOW_END.isoformat()
        or materialization.get("success") is not True
    ):
        raise MarketBarRoleAuditError("V1.2C stream freeze does not match V1.3")
    raw = materialization.get("market_bars")
    if not isinstance(raw, list) or len(raw) < TARGET:
        raise MarketBarRoleAuditError("V1.2C has fewer than 200 Market Bars")
    rows: list[dict[str, Any]] = []
    for expected, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise MarketBarRoleAuditError("Market Bar must be an object")
        index = int(item.get("emitted_bar_index", expected))
        if index != expected:
            raise MarketBarRoleAuditError("Market Bar indexes are not continuous")
        rows.append(
            {
                **item,
                "market_bar_index": index,
                "open": _decimal(item["open"]),
                "high": _decimal(item["high"]),
                "low": _decimal(item["low"]),
                "close": _decimal(item["close"]),
                "volume": _decimal(item["volume"]),
                "tau_length": _decimal(item["tau_length"]),
                "boundary_error": _decimal(item["boundary_error"]),
            }
        )
    return rows


def _daily_regime_reference() -> tuple[
    dict[date, str], tuple[DailyBar, ...], list[dict[str, Any]]
]:
    """Recreate the existing global MARKET_CLOCK quartile semantics.

    The persisted V0.1 report supplies the original ten-stock global quartile
    cut points.  No other stock is reloaded or added to this pilot.
    """

    reference = json.loads(MARKET_CLOCK_REFERENCE_PATH.read_text(encoding="utf-8"))
    distributions = reference.get("daily_distributions", {})
    fields = {
        "range_speed": "range_speed_quartile",
        "abs_net_move_atr_10": "direction_speed_quartile",
        "efficiency_10": "efficiency_10_quartile",
        "flow_speed": "flow_speed_quartile",
    }
    cuts: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
    for field in fields:
        source = distributions.get(field)
        if not isinstance(source, Mapping) or any(
            source.get(key) is None for key in ("p25", "median", "p75")
        ):
            raise MarketBarRoleAuditError("frozen MARKET_CLOCK quartiles unavailable")
        cuts[field] = tuple(_decimal(source[key]) for key in ("p25", "median", "p75"))
    anchor_bars = tuple(
        sorted(_load_stock(ANCHOR_STOCK)[0], key=lambda bar: bar.trade_date)
    )
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in anchor_bars)
    anchor_clock = list(
        _clock_series(
            anchor_bars,
            tuple(calculate_daily_indicators(anchor_bars, calendar)),
        )
    )
    for row in anchor_clock:
        for field, quartile_field in fields.items():
            value = row.get(field)
            if value is None:
                row[quartile_field] = None
                continue
            q25, q50, q75 = cuts[field]
            row[quartile_field] = (
                "Q1"
                if value <= q25
                else "Q2"
                if value <= q50
                else "Q3"
                if value <= q75
                else "Q4"
            )
    by_date = {row["trade_date"]: row for row in anchor_clock}
    regimes = {
        day: _daily_regime_label(row)
        for day, row in by_date.items()
        if RESEARCH_START <= day <= RESEARCH_END
    }
    return regimes, anchor_bars, anchor_clock


def _daily_regime_label(row: Mapping[str, Any] | None) -> str:
    if row is None:
        return "NORMAL_OTHER"
    if (
        row.get("range_speed_quartile") == "Q4"
        and row.get("direction_speed_quartile") == "Q4"
        and row.get("efficiency_10_quartile") == "Q4"
    ):
        return "FAST_DIRECTIONAL_HIGH_EFF"
    if (
        row.get("range_speed_quartile") == "Q4"
        and row.get("efficiency_10_quartile") == "Q1"
    ):
        return "FAST_NOISY"
    if row.get("range_speed_quartile") == "Q1":
        return "SLOW"
    return "NORMAL_OTHER"


def _source_date(source_id: object) -> date | None:
    parts = str(source_id).split(":")
    if len(parts) < 2:
        return None
    try:
        return date.fromisoformat(parts[1])
    except ValueError:
        return None


def _attach_source_regimes(
    rows: Sequence[dict[str, Any]], daily_regimes: Mapping[date, str]
) -> None:
    for row in rows:
        contributions = {name: Decimal(0) for name in REGIME_NAMES}
        for item in row.get("provenance", []):
            if not isinstance(item, Mapping):
                continue
            start = _decimal(item["source_tau_start"])
            end = _decimal(item["source_tau_end"])
            if end < start:
                raise MarketBarRoleAuditError("source tau contribution is negative")
            label = daily_regimes.get(
                _source_date(item.get("source_id")), "NORMAL_OTHER"
            )
            contributions[label] += end - start
        total = sum(contributions.values(), Decimal(0))
        if total <= 0:
            raise MarketBarRoleAuditError("Market Bar has no source tau contribution")
        shares = {name: contributions[name] / total for name in REGIME_NAMES}
        # Retain an exact unit sum despite Decimal division rounding.  The
        # residual is representational only; it does not classify a source
        # segment or introduce a threshold.
        shares["NORMAL_OTHER"] = Decimal(1) - sum(
            (shares[name] for name in REGIME_NAMES if name != "NORMAL_OTHER"),
            Decimal(0),
        )
        pure = [name for name, share in shares.items() if share == Decimal(1)]
        row["source_calendar_regime_tau_share"] = shares
        row["primary_source_regime"] = (
            f"PURE_{pure[0]}" if len(pure) == 1 else MIXED_REGIME
        )


def _market_atr20(rows: Sequence[Mapping[str, Any]]) -> list[Decimal | None]:
    result: list[Decimal | None] = [None] * len(rows)
    true_ranges: list[Decimal | None] = [None] * len(rows)
    for index in range(1, len(rows)):
        current, previous = rows[index], rows[index - 1]
        true_ranges[index] = max(
            _decimal(current["high"]) - _decimal(current["low"]),
            abs(_decimal(current["high"]) - _decimal(previous["close"])),
            abs(_decimal(current["low"]) - _decimal(previous["close"])),
        )
    for index in range(20, len(rows)):
        sample = true_ranges[index - 19 : index + 1]
        if all(value is not None for value in sample):
            result[index] = sum(sample, Decimal(0)) / Decimal(20)
    return result


def _decorate_market_values(rows: Sequence[dict[str, Any]]) -> None:
    closes = [_decimal(row["close"]) for row in rows]
    averages = {period: simple_moving_average(closes, period) for period in PERIODS}
    atrs = _market_atr20(rows)
    for index, row in enumerate(rows):
        row["mb_tr"] = (
            None
            if index == 0
            else max(
                row["high"] - row["low"],
                abs(row["high"] - rows[index - 1]["close"]),
                abs(row["low"] - rows[index - 1]["close"]),
            )
        )
        row["mb_atr20"] = atrs[index]
        for period in PERIODS:
            row[f"MMA{period}"] = averages[period][index]
        values = [row[f"MMA{period}"] for period in (5, 10, 20)]
        row["mma_cluster_width_atr"] = (
            (max(values) - min(values)) / atrs[index]
            if atrs[index] not in (None, Decimal(0))
            and all(value is not None for value in values)
            else None
        )
    valid_widths = [
        row["mma_cluster_width_atr"]
        for row in rows
        if row["mb_atr20"] not in (None, Decimal(0))
        and row["MMA60"] is not None
        and row["mma_cluster_width_atr"] is not None
    ]
    for row in rows:
        row["compression_quartile"] = _quartile(
            row["mma_cluster_width_atr"], valid_widths
        )


def _nearest_role(
    price: Decimal,
    values: Mapping[str, Decimal | None],
    atr: Decimal | None,
) -> tuple[str | None, Decimal | None, int]:
    if atr in (None, Decimal(0)) or any(value is None for value in values.values()):
        return None, None, 0
    candidates = [
        (name, abs(price - value) / atr)
        for name, value in values.items()
        if value is not None
    ]
    minimum = min(value for _, value in candidates)
    tied = [name for name, value in candidates if value == minimum]
    name = min(tied, key=lambda item: int(item[3:]))
    return name, minimum, len(tied)


def _reaction_signatures(
    kind: str,
    *,
    low: Decimal,
    high: Decimal,
    close: Decimal,
    ma: Decimal | None,
) -> dict[str, bool | None]:
    if ma is None:
        return {
            "WICK_RECLAIM": None,
            "FULL_HOLD_ABOVE": None,
            "CLOSE_BELOW": None,
            "WICK_REJECTION": None,
            "FULL_HOLD_BELOW": None,
            "CLOSE_ABOVE": None,
        }
    if kind == "LOW":
        return {
            "WICK_RECLAIM": low <= ma and close >= ma,
            "FULL_HOLD_ABOVE": low >= ma,
            "CLOSE_BELOW": close < ma,
            "WICK_REJECTION": None,
            "FULL_HOLD_BELOW": None,
            "CLOSE_ABOVE": None,
        }
    return {
        "WICK_RECLAIM": None,
        "FULL_HOLD_ABOVE": None,
        "CLOSE_BELOW": None,
        "WICK_REJECTION": high >= ma and close <= ma,
        "FULL_HOLD_BELOW": high <= ma,
        "CLOSE_ABOVE": close > ma,
    }


def _market_pivots(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index in range(2, len(rows) - 2):
        center = rows[index]
        neighbours = (
            rows[index - 2],
            rows[index - 1],
            rows[index + 1],
            rows[index + 2],
        )
        for kind, field in (("LOW", "low"), ("HIGH", "high")):
            price = _decimal(center[field])
            if not all(
                price < _decimal(item[field])
                if kind == "LOW"
                else price > _decimal(item[field])
                for item in neighbours
            ):
                continue
            values = {name: center[name] for name in ROLE_NAMES}
            role, distance, tie_count = _nearest_role(
                price, values, center.get("mb_atr20")
            )
            pivot = {
                "stock_code": center["stock_code"],
                "pivot_kind": kind,
                "pivot_market_bar_index": center["market_bar_index"],
                "pivot_market_bar_id": center["market_bar_id"],
                "pivot_calendar_end_datetime": center["calendar_end_datetime"],
                "pivot_price": price,
                "confirmed_market_bar_index": rows[index + 2]["market_bar_index"],
                "confirmed_market_bar_id": rows[index + 2]["market_bar_id"],
                "confirmed_at": rows[index + 2]["calendar_end_datetime"],
                "mb_atr20": center.get("mb_atr20"),
                "nearest_mma_role": role,
                "nearest_distance_atr": distance,
                "nearest_tie_count": tie_count,
                "primary_source_regime": center["primary_source_regime"],
                "source_calendar_regime_tau_share": center[
                    "source_calendar_regime_tau_share"
                ],
                "compression_quartile": center.get("compression_quartile"),
            }
            for name, value in values.items():
                pivot[name] = value
                pivot[f"distance_to_{name.lower()}_atr"] = (
                    abs(price - value) / center["mb_atr20"]
                    if value is not None
                    and center.get("mb_atr20") not in (None, Decimal(0))
                    else None
                )
                for label, signal in _reaction_signatures(
                    kind,
                    low=center["low"],
                    high=center["high"],
                    close=center["close"],
                    ma=value,
                ).items():
                    pivot[f"{name}_{label}"] = signal
            result.append(pivot)
    return result


def _role_distribution(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    counts = {name: sum(row.get(field) == name for row in rows) for name in ROLE_NAMES}
    total = sum(counts.values())
    return {
        "count": total,
        "counts": counts,
        "probabilities": {
            name: Decimal(count) / Decimal(total) if total else None
            for name, count in counts.items()
        },
        "median_nearest_horizon": _median(
            [
                Decimal(int(str(row[field])[3:]))
                for row in rows
                if row.get(field) in ROLE_NAMES
            ]
        ),
    }


def _pivot_group_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if row.get("nearest_mma_role")]
    return {
        "pivot_count": len(rows),
        "low_count": sum(row["pivot_kind"] == "LOW" for row in rows),
        "high_count": sum(row["pivot_kind"] == "HIGH" for row in rows),
        "eligible_nearest_role_count": len(eligible),
        "nearest_role": _role_distribution(eligible, "nearest_mma_role"),
    }


def _reaction_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind in ("LOW", "HIGH"):
        subset = [row for row in rows if row["pivot_kind"] == kind]
        labels = (
            ("WICK_RECLAIM", "FULL_HOLD_ABOVE", "CLOSE_BELOW")
            if kind == "LOW"
            else ("WICK_REJECTION", "FULL_HOLD_BELOW", "CLOSE_ABOVE")
        )
        result[kind] = {
            "pivot_count": len(subset),
            "per_mma": {
                name: {
                    label: {
                        "count": sum(
                            row.get(f"{name}_{label}") is True for row in subset
                        ),
                        "rate": Decimal(
                            sum(row.get(f"{name}_{label}") is True for row in subset)
                        )
                        / Decimal(len(subset))
                        if subset
                        else None,
                    }
                    for label in labels
                }
                for name in ROLE_NAMES
            },
        }
    return result


def _tvd(first: Mapping[str, Any], second: Mapping[str, Any]) -> Decimal | None:
    if not first["count"] or not second["count"]:
        return None
    return sum(
        (
            abs(first["probabilities"][name] - second["probabilities"][name])
            for name in ROLE_NAMES
        ),
        Decimal(0),
    ) / Decimal(2)


def _role_regime_report(pivots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups = {
        name: [
            row for row in pivots if row.get("primary_source_regime") == f"PURE_{name}"
        ]
        for name in REGIME_NAMES
    }
    groups[MIXED_REGIME] = [
        row for row in pivots if row.get("primary_source_regime") == MIXED_REGIME
    ]
    groups["OVERALL"] = list(pivots)
    return {name: _pivot_group_summary(group) for name, group in groups.items()}


def _reaction_regime_report(pivots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in (*REGIME_NAMES, MIXED_REGIME, "OVERALL"):
        source_label = f"PURE_{name}" if name in REGIME_NAMES else name
        group = (
            list(pivots)
            if name == "OVERALL"
            else [
                row
                for row in pivots
                if row.get("primary_source_regime") == source_label
            ]
        )
        result[name] = _reaction_summary(group)
    return result


def _cross_direction(
    previous_close: Decimal,
    current_close: Decimal,
    previous_ma: Decimal | None,
    current_ma: Decimal | None,
) -> int | None:
    if previous_ma is None or current_ma is None:
        return None
    if previous_close <= previous_ma and current_close > current_ma:
        return 1
    if previous_close >= previous_ma and current_close < current_ma:
        return -1
    return None


def _cross_events(
    rows: Sequence[Mapping[str, Any]], period: int
) -> list[dict[str, Any]]:
    field = f"MMA{period}"
    directions: dict[int, int] = {}
    events: list[dict[str, Any]] = []
    for index in range(1, len(rows)):
        direction = _cross_direction(
            _decimal(rows[index - 1]["close"]),
            _decimal(rows[index]["close"]),
            rows[index - 1].get(field),
            rows[index].get(field),
        )
        if direction is not None:
            directions[index] = direction
        if direction is None or rows[index].get("compression_quartile") is None:
            continue
        event: dict[str, Any] = {
            "market_bar_index": rows[index]["market_bar_index"],
            "market_bar_id": rows[index]["market_bar_id"],
            "period": period,
            "direction": "UP" if direction > 0 else "DOWN",
            "direction_value": direction,
            "compression_quartile": rows[index]["compression_quartile"],
            "primary_source_regime": rows[index]["primary_source_regime"],
        }
        for horizon in (1, 3, 5):
            target = index + horizon
            if target >= len(rows) or rows[target].get(field) is None:
                event[f"same_side_m{horizon}"] = None
                event[f"opposite_recross_within_m{horizon}"] = None
                continue
            event[f"same_side_m{horizon}"] = (
                direction * (_decimal(rows[target]["close"]) - rows[target][field]) >= 0
            )
            event[f"opposite_recross_within_m{horizon}"] = any(
                directions.get(candidate) == -direction
                for candidate in range(index + 1, target + 1)
            )
        events.append(event)
    return events


def _cross_quality(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in (1, 3, 5):
        available = [
            row for row in events if row.get(f"same_side_m{horizon}") is not None
        ]
        same = sum(row[f"same_side_m{horizon}"] is True for row in available)
        recross = sum(
            row[f"opposite_recross_within_m{horizon}"] is True for row in available
        )
        result[f"m{horizon}"] = {
            "available_count": len(available),
            "same_side_persistence_rate": Decimal(same) / Decimal(len(available))
            if available
            else None,
            "opposite_recross_whipsaw_rate": Decimal(recross) / Decimal(len(available))
            if available
            else None,
        }
    return result


def _cross_summary(
    rows: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    count = len(events)
    return {
        "market_bar_count": len(rows),
        "event_count": count,
        "up_count": sum(event["direction"] == "UP" for event in events),
        "down_count": sum(event["direction"] == "DOWN" for event in events),
        "events_per_100_market_bars": Decimal(count) * Decimal(100) / Decimal(len(rows))
        if rows
        else None,
        "quality": _cross_quality(events),
    }


def _compression_cross_report(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    primary = [row for row in rows if row.get("compression_quartile") is not None]
    result: dict[str, Any] = {"population_count": len(primary), "periods": {}}
    for period in (10, 20):
        events = _cross_events(rows, period)
        buckets: dict[str, Any] = {}
        for bucket in ("C1", "C2", "C3", "C4"):
            group_rows = [
                row for row in primary if row["compression_quartile"] == bucket
            ]
            group_events = [
                event for event in events if event["compression_quartile"] == bucket
            ]
            buckets[bucket] = _cross_summary(group_rows, group_events)
        by_regime = {}
        for regime in (*PURE_REGIME_NAMES, MIXED_REGIME):
            group_rows = [
                row for row in primary if row["primary_source_regime"] == regime
            ]
            group_events = [
                event for event in events if event["primary_source_regime"] == regime
            ]
            by_regime[regime] = _cross_summary(group_rows, group_events)
        result["periods"][f"MMA{period}"] = {
            "events": events,
            "by_compression_quartile": buckets,
            "by_source_regime": by_regime,
        }
    return result


def _turns(rows: Sequence[Mapping[str, Any]], period: int) -> list[dict[str, Any]]:
    field = f"MMA{period}"
    deltas: list[Decimal | None] = [None] * len(rows)
    for index in range(1, len(rows)):
        if (
            rows[index].get(field) is not None
            and rows[index - 1].get(field) is not None
        ):
            deltas[index] = rows[index][field] - rows[index - 1][field]
    events: list[dict[str, Any]] = []
    for index in range(2, len(rows)):
        previous, current = deltas[index - 1], deltas[index]
        if previous is None or current is None:
            continue
        direction = (
            "UP"
            if previous <= 0 and current > 0
            else "DOWN"
            if previous >= 0 and current < 0
            else None
        )
        if direction is not None:
            events.append(
                {
                    "market_bar_index": rows[index]["market_bar_index"],
                    "market_bar_id": rows[index]["market_bar_id"],
                    "period": period,
                    "direction": direction,
                    "primary_source_regime": rows[index]["primary_source_regime"],
                }
            )
    return events


def _propagation(
    source: Sequence[Mapping[str, Any]],
    target: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_direction: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in target:
        by_direction[str(event["direction"])].append(event)
    result: list[dict[str, Any]] = []
    for event in source:
        later = next(
            (
                candidate
                for candidate in by_direction[str(event["direction"])]
                if candidate["market_bar_index"] > event["market_bar_index"]
            ),
            None,
        )
        result.append(
            {
                "source_period": event["period"],
                "target_period": target[0]["period"] if target else None,
                "direction": event["direction"],
                "origin_market_bar_index": event["market_bar_index"],
                "origin_market_bar_id": event["market_bar_id"],
                "origin_primary_source_regime": event["primary_source_regime"],
                "target_market_bar_index": later["market_bar_index"] if later else None,
                "target_market_bar_id": later["market_bar_id"] if later else None,
                "lag_market_bars": (
                    later["market_bar_index"] - event["market_bar_index"]
                    if later
                    else None
                ),
                "status": "MATCHED" if later else "UNMATCHED",
            }
        )
    return result


def _lag_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [
        row["lag_market_bars"] for row in rows if row["lag_market_bars"] is not None
    ]
    return {
        "origin_count": len(rows),
        "matched_count": len(values),
        "unmatched_count": len(rows) - len(values),
        "lag": _distribution([Decimal(value) for value in values]),
    }


def _turn_report(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    turns = {period: _turns(rows, period) for period in PERIODS}
    pairs = ((5, 10), (10, 20), (20, 60))
    propagation = {
        f"MMA{source}_TO_MMA{target}": _propagation(turns[source], turns[target])
        for source, target in pairs
    }
    return {
        "turn_counts": {
            f"MMA{period}": {
                "total": len(events),
                "up": sum(event["direction"] == "UP" for event in events),
                "down": sum(event["direction"] == "DOWN" for event in events),
            }
            for period, events in turns.items()
        },
        "propagation": {
            name: {
                "overall": _lag_summary(events),
                "by_origin_source_regime": {
                    regime: _lag_summary(
                        [
                            event
                            for event in events
                            if event["origin_primary_source_regime"] == regime
                        ]
                    )
                    for regime in (*PURE_REGIME_NAMES, MIXED_REGIME)
                },
                "records": events,
            }
            for name, events in propagation.items()
        },
    }


def _calendar_pivots(
    bars: Sequence[DailyBar], clock: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
    clock_by_date = {row["trade_date"]: row for row in clock}
    index_by_date = {bar.trade_date: index for index, bar in enumerate(canonical)}
    calendar = ExplicitTradingCalendar(bar.trade_date for bar in canonical)
    result: list[dict[str, Any]] = []
    for pivot in detect_daily_pivots(canonical, calendar):
        if not WINDOW_START <= pivot.pivot_trade_date <= WINDOW_END:
            continue
        if pivot.confirmed_at.date() > WINDOW_END:
            continue
        index = index_by_date[pivot.pivot_trade_date]
        row, bar = clock_by_date[pivot.pivot_trade_date], canonical[index]
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


def _calendar_role_report(pivots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in (*REGIME_NAMES, "OVERALL"):
        group = (
            list(pivots)
            if name == "OVERALL"
            else [row for row in pivots if row["primary_source_regime"] == name]
        )
        result[name] = _pivot_group_summary(group)
    return result


def _calendar_reaction_report(pivots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in (*REGIME_NAMES, "OVERALL"):
        group = (
            list(pivots)
            if name == "OVERALL"
            else [row for row in pivots if row["primary_source_regime"] == name]
        )
        result[name] = _reaction_summary(group)
    return result


def _reaction_profile_distance(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> Decimal | None:
    differences: list[Decimal] = []
    for kind, label in (("LOW", "WICK_RECLAIM"), ("HIGH", "WICK_REJECTION")):
        if not first[kind]["pivot_count"] or not second[kind]["pivot_count"]:
            continue
        for name in ROLE_NAMES:
            first_rate = first[kind]["per_mma"][name][label]["rate"]
            second_rate = second[kind]["per_mma"][name][label]["rate"]
            if first_rate is not None and second_rate is not None:
                differences.append(abs(first_rate - second_rate))
    return (
        sum(differences, Decimal(0)) / Decimal(len(differences))
        if differences
        else None
    )


def _role_comparison(
    market: Mapping[str, Any], calendar: Mapping[str, Any]
) -> dict[str, Any]:
    pairs = (
        ("FAST_DIRECTIONAL_HIGH_EFF", "SLOW"),
        ("FAST_NOISY", "SLOW"),
        ("FAST_DIRECTIONAL_HIGH_EFF", "FAST_NOISY"),
    )
    output: dict[str, Any] = {}
    for first, second in pairs:
        market_first = market[first]["nearest_role"]
        market_second = market[second]["nearest_role"]
        calendar_first = calendar[first]["nearest_role"]
        calendar_second = calendar[second]["nearest_role"]
        output[f"{first}_VS_{second}"] = {
            "market_bar_tvd": _tvd(market_first, market_second),
            "calendar_daily_tvd": _tvd(calendar_first, calendar_second),
        }
    return output


def _select_visual_cases(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    categories = (
        ("FAST_ORIGIN", "PURE_FAST_DIRECTIONAL_HIGH_EFF"),
        ("SLOW_ORIGIN", "PURE_SLOW"),
        ("MIXED_TRANSITION", MIXED_REGIME),
    )
    for category, regime in categories:
        candidates = [
            row
            for row in rows
            if row.get("primary_source_regime") == regime
            and row.get("MMA60") is not None
        ]
        if not candidates:
            continue
        positions = [0] if len(candidates) == 1 else [0, len(candidates) - 1]
        for position in positions:
            selected.append({"category": category, "anchor": candidates[position]})
    return selected[:6]


def _render_visual_pack(
    rows: Sequence[Mapping[str, Any]], output_root: Path
) -> list[dict[str, Any]]:
    output_root.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    for number, selected in enumerate(_select_visual_cases(rows), start=1):
        anchor = selected["anchor"]
        anchor_index = int(anchor["market_bar_index"]) - 1
        start, end = max(0, anchor_index - 20), min(len(rows), anchor_index + 21)
        window = list(rows[start:end])
        indexes = list(range(0, len(window), 10))
        if indexes[-1] != len(window) - 1 and len(window) - 1 - indexes[-1] >= 5:
            indexes.append(len(window) - 1)
        case_id = f"CASE_{number:02d}_{selected['category']}"
        chart_path = output_root / f"{case_id}.png"
        _render_chart(
            output_path=chart_path,
            title=f"{ANCHOR_STOCK} MARKET BAR {case_id}",
            rows=window,
            ma_fields=("MMA5", "MMA10", "MMA20", "MMA60"),
            tick_indexes=indexes,
            tick_labels=[str(window[index]["market_bar_index"]) for index in indexes],
        )
        metadata = {
            "case_id": case_id,
            "category": selected["category"],
            "anchor_market_bar_index": anchor["market_bar_index"],
            "anchor_market_bar_id": anchor["market_bar_id"],
            "window_market_bar_start": window[0]["market_bar_index"],
            "window_market_bar_end": window[-1]["market_bar_index"],
            "source_calendar_regime_tau_share": anchor[
                "source_calendar_regime_tau_share"
            ],
            "primary_source_regime": anchor["primary_source_regime"],
            "calendar_start_datetime": anchor["calendar_start_datetime"],
            "calendar_end_datetime": anchor["calendar_end_datetime"],
            "chart_path": chart_path.as_posix(),
            "x_axis_policy": "MARKET_BAR_INDEX",
        }
        metadata_path = output_root / f"{case_id}.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        cases.append({**metadata, "metadata_path": metadata_path.as_posix()})
    return cases


def _hypotheses(report: Mapping[str, Any]) -> dict[str, str]:
    comparisons = report["calendar_vs_market_role_stability"]
    fast_slow = comparisons["FAST_DIRECTIONAL_HIGH_EFF_VS_SLOW"]
    market_tvd, calendar_tvd = (
        fast_slow["market_bar_tvd"],
        fast_slow["calendar_daily_tvd"],
    )
    h1 = (
        "SUPPORTED"
        if market_tvd is not None
        and calendar_tvd is not None
        and market_tvd < calendar_tvd
        else "NOT_SUPPORTED"
        if market_tvd is not None and calendar_tvd is not None
        else "INSUFFICIENT_SAMPLE"
    )
    reaction = report["reaction_stability"]
    market_distance = reaction["market_fast_vs_slow_distance"]
    calendar_distance = reaction["calendar_fast_vs_slow_distance"]
    h2 = (
        "SUPPORTED"
        if market_distance is not None
        and calendar_distance is not None
        and market_distance < calendar_distance
        else "NOT_SUPPORTED"
        if market_distance is not None and calendar_distance is not None
        else "INSUFFICIENT_SAMPLE"
    )
    # This pilot contains no pure-SLOW Market Bars, so source-speed
    # independence cannot be assessed without pooling or inventing a share
    # threshold.  Both are explicitly out of scope.
    h3 = "INSUFFICIENT_SAMPLE"
    propagation = report["turn_propagation"]["propagation"]
    fast = propagation["MMA5_TO_MMA10"]["by_origin_source_regime"][
        "PURE_FAST_DIRECTIONAL_HIGH_EFF"
    ]["lag"]["median"]
    slow = propagation["MMA5_TO_MMA10"]["by_origin_source_regime"]["PURE_SLOW"]["lag"][
        "median"
    ]
    h4 = (
        "INCONCLUSIVE"
        if fast is not None and slow is not None
        else "INSUFFICIENT_SAMPLE"
    )
    h5 = "SUPPORTED" if report["visual_pack"]["case_count"] else "INSUFFICIENT_SAMPLE"
    return {
        "H1_ROLE_DIFFERENCE_SMALLER_THAN_CALENDAR": h1,
        "H2_REACTION_DIFFERENCE_SMALLER_THAN_CALENDAR": h2,
        "H3_COMPRESSION_STRUCTURE_REGIME_STABLE": h3,
        "H4_TURN_PROPAGATION_LAG_REGIME_STABLE": h4,
        "H5_MARKET_TIME_MMA_HIERARCHY_READABLE": h5,
    }


def run_audit(
    *,
    input_path: Path = V12C_PATH,
    output_path: Path = OUTPUT_PATH,
    visual_root: Path = VISUAL_ROOT,
) -> dict[str, Any]:
    """Run the frozen, no-network one-stock V1.3 descriptive audit."""

    market_rows = _load_market_rows(input_path)
    daily_regimes, daily_bars, daily_clock = _daily_regime_reference()
    _attach_source_regimes(market_rows, daily_regimes)
    _decorate_market_values(market_rows)
    market_pivots = _market_pivots(market_rows)
    role_by_regime = _role_regime_report(market_pivots)
    reaction_by_regime = _reaction_regime_report(market_pivots)
    calendar_pivots = _calendar_pivots(daily_bars, daily_clock)
    calendar_roles = _calendar_role_report(calendar_pivots)
    calendar_reactions = _calendar_reaction_report(calendar_pivots)
    comparison = _role_comparison(role_by_regime, calendar_roles)
    reaction_stability = {
        "market_fast_vs_slow_distance": _reaction_profile_distance(
            reaction_by_regime["FAST_DIRECTIONAL_HIGH_EFF"],
            reaction_by_regime["SLOW"],
        ),
        "calendar_fast_vs_slow_distance": _reaction_profile_distance(
            calendar_reactions["FAST_DIRECTIONAL_HIGH_EFF"],
            calendar_reactions["SLOW"],
        ),
    }
    compression = _compression_cross_report(market_rows)
    turns = _turn_report(market_rows)
    cases = _render_visual_pack(market_rows, visual_root)
    primary_rows = [row for row in market_rows if row.get("compression_quartile")]
    report: dict[str, Any] = {
        "proof_version": PROOF_VERSION,
        "v12c_checkpoint": "000b5f1",
        "input_artifact": input_path.as_posix(),
        "network_calls": 0,
        "frozen_research_infrastructure": {
            "global_activity_tau": True,
            "integer_target_lattice": True,
            "actual_source_endpoint": True,
            "one_resolved_target_one_market_bar": True,
            "source_ohlcv_exact": True,
            "fractional_split": False,
            "interpolation": False,
            "synthetic_price": False,
            "tau_formula_changed": False,
            "acquisition_changed": False,
        },
        "scope": {
            "stock_code": ANCHOR_STOCK,
            "calendar_start": WINDOW_START,
            "calendar_end": WINDOW_END,
            "market_bar_count": len(market_rows),
            "continuous_island_count": 1,
            "strategy": False,
            "buy_sell": False,
            "pnl": False,
            "future_return_label": False,
            "period_optimization": False,
        },
        "methodology": {
            "mma": "unweighted simple moving average of sequential Market-Bar closes",
            "market_bar_atr": "20 true ranges from Market-Bar OHLC; first valid at MB21",
            "pivot": "strict left=2/right=2; P confirmed at P+2; descriptive only",
            "nearest_role": "absolute pivot-price distance / MB_ATR20; exact tie -> shorter horizon",
            "source_calendar_regime": "existing ten-stock MARKET_CLOCK global quartiles, carried as source-segment tau shares",
            "pure_regime": "one source-calendar regime tau share exactly equals 1; mixed rows excluded from primary comparisons",
            "calendar_reference": "adjusted Daily OHLC/SMA reference only; no Market-Bar geometry change",
        },
        "mma_valid_counts": {
            f"MMA{period}": sum(row[f"MMA{period}"] is not None for row in market_rows)
            for period in PERIODS
        },
        "market_bar_atr20_valid_count": sum(
            row["mb_atr20"] is not None for row in market_rows
        ),
        "market_bar_pivots": {
            "pivot_count": len(market_pivots),
            "low_count": sum(row["pivot_kind"] == "LOW" for row in market_pivots),
            "high_count": sum(row["pivot_kind"] == "HIGH" for row in market_pivots),
            "primary_valid_count": sum(
                row.get("nearest_mma_role") is not None for row in market_pivots
            ),
            "rows": market_pivots,
        },
        "source_calendar_regimes": {
            "market_bar_counts": {
                name: sum(row["primary_source_regime"] == name for row in market_rows)
                for name in (*PURE_REGIME_NAMES, MIXED_REGIME)
            },
            "pivot_role_by_regime": role_by_regime,
            "reaction_by_regime": reaction_by_regime,
        },
        "calendar_daily_reference": {
            "signal_price_basis": "ADJUSTED_DAILY_OHLC",
            "pivot_count": len(calendar_pivots),
            "primary_valid_count": sum(
                row.get("nearest_mma_role") is not None for row in calendar_pivots
            ),
            "role_by_regime": calendar_roles,
            "reaction_by_regime": calendar_reactions,
        },
        "calendar_vs_market_role_stability": comparison,
        "reaction_stability": reaction_stability,
        "compression": {
            "primary_valid_population_count": len(primary_rows),
            "quartile_counts": {
                bucket: sum(
                    row["compression_quartile"] == bucket for row in primary_rows
                )
                for bucket in ("C1", "C2", "C3", "C4")
            },
            "cluster_width_atr_distribution": _distribution(
                [row["mma_cluster_width_atr"] for row in primary_rows]
            ),
        },
        "compression_crosses": compression,
        "turn_propagation": turns,
        "market_bar_values": market_rows,
        "visual_pack": {
            "case_count": len(cases),
            "max_case_count": 6,
            "x_axis": "MARKET_BAR_INDEX",
            "calendar_datetime": "metadata_only",
            "cases": cases,
        },
        "limitations": {
            "one_stock": True,
            "market_bar_count": len(market_rows),
            "statistical_significance_claimed": False,
            "generalization_claimed": False,
        },
        "market_bar_infrastructure_changed": False,
        "strategy_buy_sell_pnl_changed": False,
    }
    report["hypotheses"] = _hypotheses(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=V12C_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--visual-root", type=Path, default=VISUAL_ROOT)
    args = parser.parse_args()
    report = run_audit(
        input_path=args.input,
        output_path=args.output,
        visual_root=args.visual_root,
    )
    print(
        json.dumps(
            {
                "output": args.output.as_posix(),
                "market_bar_count": report["scope"]["market_bar_count"],
                "mma_valid_counts": report["mma_valid_counts"],
                "pivot_count": report["market_bar_pivots"]["pivot_count"],
                "visual_case_count": report["visual_pack"]["case_count"],
                "hypotheses": report["hypotheses"],
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
