"""Visual, non-strategy proof for the V0.9 Market-Bar pilot (V1.0).

This module consumes the immutable V0.9 JSON output and produces a compact
Market-Bar MMA view, a matching adjusted-Daily reference, and a MB60--80
zoom.  It intentionally contains no signal, order, fill, PnL, or threshold
logic.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import simple_moving_average
from src.backtest_engine.models import DailyBar
from src.strategy_review.chart import _Canvas, _draw_centered_multiline_text

from .down_box_daily_execution_proof import _load_stock

V09_PATH = Path("data/processed/strategy_review/market_bar_pilot_acquisition_v0_9.json")
OUTPUT_ROOT = Path("data/processed/strategy_review/market_bar_mma_visual_proof_v1_0")
SUMMARY_PATH = OUTPUT_ROOT / "market_bar_mma_visual_proof_v1_0.json"
VALUES_CSV_PATH = OUTPUT_ROOT / "market_bar_mma_values.csv"
VALUES_JSON_PATH = OUTPUT_ROOT / "market_bar_mma_values.json"
DENSITY_CSV_PATH = OUTPUT_ROOT / "market_bar_density.csv"
DENSITY_JSON_PATH = OUTPUT_ROOT / "market_bar_density.json"
CALENDAR_CHART_PATH = OUTPUT_ROOT / "CALENDAR_REFERENCE.png"
MARKET_CHART_PATH = OUTPUT_ROOT / "MARKET_BAR_MMA.png"
ZOOM_CHART_PATH = OUTPUT_ROOT / "MARKET_BAR_MMA_MB50_80.png"
PERIODS = (5, 10, 20, 60)
PILOT_START = date(2026, 4, 20)
PILOT_END = date(2026, 5, 26)
V09_CHECKPOINT = "0246f65"


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _mmas(values: Sequence[Decimal]) -> dict[int, tuple[Decimal | None, ...]]:
    return {period: tuple(simple_moving_average(values, period)) for period in PERIODS}


def _market_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("materialization", {}).get("market_bars", [])
    if not isinstance(raw, list) or not raw:
        raise ValueError("V0.9 materialization has no Market Bars")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise TypeError("Market Bar row must be an object")
        rows.append(
            {
                **item,
                "market_bar_index": index,
                "open": _decimal(item["open"]),
                "high": _decimal(item["high"]),
                "low": _decimal(item["low"]),
                "close": _decimal(item["close"]),
                "volume": _decimal(item["volume"]),
            }
        )
    return rows


def _values_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    close = [_decimal(row["close"]) for row in rows]
    averages = _mmas(close)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        result.append(
            {
                "market_bar_index": row["market_bar_index"],
                "market_bar_id": row["market_bar_id"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "MMA5": averages[5][index],
                "MMA10": averages[10][index],
                "MMA20": averages[20][index],
                "MMA60": averages[60][index],
                "tau_length": _decimal(row["tau_length"]),
                "boundary_error": _decimal(row["boundary_error"]),
                "calendar_start_datetime": row["calendar_start_datetime"],
                "calendar_end_datetime": row["calendar_end_datetime"],
                "calendar_start_date": row["calendar_start_date"],
                "calendar_end_date": row["calendar_end_date"],
                "source_segment_count": row["source_segment_count"],
                "source_resolutions_used": row["source_resolutions_used"],
            }
        )
    return result


def _calendar_rows(stock_code: str) -> tuple[list[DailyBar], list[dict[str, Any]]]:
    bars = tuple(sorted(_load_stock(stock_code)[0], key=lambda bar: bar.trade_date))
    selected = [bar for bar in bars if PILOT_START <= bar.trade_date <= PILOT_END]
    if not selected:
        raise ValueError("Daily pilot window is empty")
    closes = [bar.signal.close for bar in bars]
    full_mmas = _mmas(closes)
    index_by_date = {bar.trade_date: index for index, bar in enumerate(bars)}
    records: list[dict[str, Any]] = []
    for bar in selected:
        index = index_by_date[bar.trade_date]
        records.append(
            {
                "trade_date": bar.trade_date,
                "open": bar.signal.open,
                "high": bar.signal.high,
                "low": bar.signal.low,
                "close": bar.signal.close,
                "SMA5": full_mmas[5][index],
                "SMA10": full_mmas[10][index],
                "SMA20": full_mmas[20][index],
                "SMA60": full_mmas[60][index],
            }
        )
    return selected, records


def _draw_series(
    canvas: _Canvas,
    values: Sequence[Decimal | None],
    *,
    x_of: Any,
    y_of: Any,
    color: tuple[int, int, int],
    width: int = 2,
) -> None:
    previous: tuple[int, int] | None = None
    for index, value in enumerate(values):
        current = None if value is None else (x_of(index), y_of(value))
        if previous is not None and current is not None:
            canvas.line(*previous, *current, color, width)
        previous = current


def _render_chart(
    *,
    output_path: Path,
    title: str,
    rows: Sequence[Mapping[str, Any]],
    ma_fields: Sequence[str],
    tick_labels: Sequence[str],
    tick_indexes: Sequence[int],
    y_min: Decimal | None = None,
    y_max: Decimal | None = None,
) -> None:
    if not rows:
        raise ValueError("chart rows must not be empty")
    canvas = _Canvas(1400, 800)
    left, top, right, bottom = 90, 100, 1330, 680
    values = [_decimal(row[field]) for row in rows for field in ("low", "high")]
    for row in rows:
        values.extend(
            _decimal(row[field]) for field in ma_fields if row.get(field) is not None
        )
    minimum = min(values) if y_min is None else y_min
    maximum = max(values) if y_max is None else y_max
    if y_min is None or y_max is None:
        padding = max((maximum - minimum) * Decimal("0.05"), Decimal(1))
        minimum -= padding
        maximum += padding
    if maximum <= minimum:
        maximum = minimum + Decimal(1)

    def x_of(index: int) -> int:
        return (
            left
            if len(rows) == 1
            else left + round(index * (right - left) / (len(rows) - 1))
        )

    def y_of(value: Decimal) -> int:
        return bottom - round(
            float((value - minimum) / (maximum - minimum)) * (bottom - top)
        )

    canvas.line(left, top, left, bottom, (70, 70, 70))
    canvas.line(left, bottom, right, bottom, (70, 70, 70))
    for grid in range(1, 5):
        y = top + grid * (bottom - top) // 5
        canvas.line(left, y, right, y, (225, 225, 225))
    candle_width = max(1, min(5, (right - left) // max(1, len(rows) * 3)))
    for index, row in enumerate(rows):
        x = x_of(index)
        open_price = _decimal(row["open"])
        close_price = _decimal(row["close"])
        color = (205, 55, 55) if close_price >= open_price else (40, 95, 185)
        canvas.line(
            x, y_of(_decimal(row["low"])), x, y_of(_decimal(row["high"])), color
        )
        canvas.rectangle(
            x - candle_width,
            y_of(max(open_price, close_price)),
            x + candle_width,
            y_of(min(open_price, close_price)),
            color,
        )
    colors = {
        "MMA5": (140, 85, 75),
        "MMA10": (145, 80, 175),
        "MMA20": (240, 125, 25),
        "MMA60": (45, 150, 75),
        "SMA5": (140, 85, 75),
        "SMA10": (145, 80, 175),
        "SMA20": (240, 125, 25),
        "SMA60": (45, 150, 75),
    }
    for field in ma_fields:
        _draw_series(
            canvas,
            tuple(
                None if row.get(field) is None else _decimal(row[field]) for row in rows
            ),
            x_of=x_of,
            y_of=y_of,
            color=colors[field],
        )
    canvas.text(left, 25, title, scale=2)
    canvas.text(left, 55, "ADJUSTED OHLC  " + "  ".join(ma_fields), scale=1)
    for index, label in zip(tick_indexes, tick_labels, strict=True):
        _draw_centered_multiline_text(canvas, x_of(index), bottom + 12, label)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.write_png(output_path)


def _write_values(rows: Sequence[Mapping[str, Any]]) -> None:
    fields = (
        "market_bar_index",
        "market_bar_id",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "MMA5",
        "MMA10",
        "MMA20",
        "MMA60",
        "tau_length",
        "boundary_error",
        "calendar_start_datetime",
        "calendar_end_datetime",
        "calendar_start_date",
        "calendar_end_date",
        "source_segment_count",
        "source_resolutions_used",
    )
    with VALUES_CSV_PATH.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            output = dict(row)
            output["source_resolutions_used"] = ",".join(row["source_resolutions_used"])
            writer.writerow(
                {
                    field: "" if output[field] is None else output[field]
                    for field in fields
                }
            )
    VALUES_JSON_PATH.write_text(
        json.dumps(list(rows), ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _write_density(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = list(payload.get("calendar_market_bar_mapping", []))
    fields = (
        "calendar_session",
        "daily_delta_tau",
        "mapping_category",
        "market_bar_boundaries_crossed",
        "market_bars_completed",
    )
    with DENSITY_CSV_PATH.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field) for field in fields} for row in rows)
    DENSITY_JSON_PATH.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return rows


def run_proof(
    *,
    input_path: Path = V09_PATH,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, Any]:
    global VALUES_CSV_PATH, VALUES_JSON_PATH, DENSITY_CSV_PATH, DENSITY_JSON_PATH
    global CALENDAR_CHART_PATH, MARKET_CHART_PATH, ZOOM_CHART_PATH, SUMMARY_PATH
    output_root.mkdir(parents=True, exist_ok=True)
    VALUES_CSV_PATH = output_root / "market_bar_mma_values.csv"
    VALUES_JSON_PATH = output_root / "market_bar_mma_values.json"
    DENSITY_CSV_PATH = output_root / "market_bar_density.csv"
    DENSITY_JSON_PATH = output_root / "market_bar_density.json"
    CALENDAR_CHART_PATH = output_root / "CALENDAR_REFERENCE.png"
    MARKET_CHART_PATH = output_root / "MARKET_BAR_MMA.png"
    ZOOM_CHART_PATH = output_root / "MARKET_BAR_MMA_MB50_80.png"
    SUMMARY_PATH = output_root / "market_bar_mma_visual_proof_v1_0.json"
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    final = payload["final_pilot"]
    stock_code = str(final["stock_code"])
    market_rows = _market_rows(payload)
    value_rows = _values_rows(market_rows)
    if len(value_rows) != 80:
        raise ValueError("V1.0 requires exactly the V0.9 80-bar pilot")
    _write_values(value_rows)
    density_rows = _write_density(payload)
    daily_bars, calendar_rows = _calendar_rows(stock_code)

    _render_chart(
        output_path=CALENDAR_CHART_PATH,
        title=f"{stock_code} CALENDAR REFERENCE",
        rows=calendar_rows,
        ma_fields=("SMA5", "SMA10", "SMA20", "SMA60"),
        tick_indexes=list(range(0, len(calendar_rows), 10)),
        tick_labels=[
            f"{calendar_rows[index]['trade_date'].day:02d}"
            for index in range(0, len(calendar_rows), 10)
        ],
    )
    # Market-Bar labels are one-based and follow the requested 1, 10, 20,
    # ... cadence.  The first label is MB1; subsequent labels are every ten
    # bars, while the renderer itself uses zero-based row coordinates.
    market_ticks = [0] + list(range(9, len(value_rows), 10))
    _render_chart(
        output_path=MARKET_CHART_PATH,
        title=f"{stock_code} MARKET BAR MMA",
        rows=value_rows,
        ma_fields=("MMA5", "MMA10", "MMA20", "MMA60"),
        tick_indexes=market_ticks,
        tick_labels=[str(index + 1) for index in market_ticks],
    )
    zoom_start = 49
    zoom_rows = value_rows[zoom_start:]
    _render_chart(
        output_path=ZOOM_CHART_PATH,
        title=f"{stock_code} MARKET BAR MMA MB50-80",
        rows=zoom_rows,
        ma_fields=("MMA5", "MMA10", "MMA20", "MMA60"),
        tick_indexes=[0, 10, 20, 30],
        tick_labels=["50", "60", "70", "80"],
        y_min=min(_decimal(row["low"]) for row in value_rows),
        y_max=max(_decimal(row["high"]) for row in value_rows),
    )
    valid_counts = {
        f"MMA{period}": sum(row[f"MMA{period}"] is not None for row in value_rows)
        for period in PERIODS
    }
    summary: dict[str, Any] = {
        "proof_version": "MARKET_BAR_MMA_VISUAL_PROOF_V1_0",
        "v09_checkpoint": V09_CHECKPOINT,
        "input_artifact": str(input_path),
        "contract": {
            "global_activity_tau": True,
            "integer_target_lattice": True,
            "one_target_one_market_bar": True,
            "source_ohlc_exact_aggregation": True,
            "source_volume_exact_aggregation": True,
            "interpolation": False,
            "synthetic_bar": False,
            "strategy": False,
            "buy_sell": False,
            "pnl": False,
        },
        "pilot": {
            "stock_code": stock_code,
            "calendar_start": final["calendar_start"],
            "calendar_end": final["calendar_end"],
            "market_bar_count": len(value_rows),
            "continuous_island_count": payload["materialization"][
                "resolved_island_count"
            ],
        },
        "mma_definition": "simple arithmetic mean of Market-Bar close; no calendar/tau weighting",
        "mma_valid_counts": valid_counts,
        "MMA60_observation_window": {
            "market_bar_start": 60,
            "market_bar_end": 80,
            "rows": value_rows[59:80],
        },
        "calendar_reference": {
            "daily_session_count": len(daily_bars),
            "chart_path": str(CALENDAR_CHART_PATH),
            "price_basis": "SIGNAL_ADJUSTED_DAILY_OHLC",
            "moving_average_basis": "SIGNAL_ADJUSTED_DAILY_CLOSE",
        },
        "market_bar_chart": {
            "chart_path": str(MARKET_CHART_PATH),
            "x_axis_policy": "MARKET_BAR_INDEX",
            "x_axis_tick_indexes_zero_based": market_ticks,
            "x_axis_tick_labels": [str(index + 1) for index in market_ticks],
            "calendar_datetime_policy": "metadata_only",
        },
        "market_bar_zoom": {
            "chart_path": str(ZOOM_CHART_PATH),
            "market_bar_start": 50,
            "market_bar_end": 80,
        },
        "value_artifacts": {
            "csv_path": str(VALUES_CSV_PATH),
            "json_path": str(VALUES_JSON_PATH),
            "density_csv_path": str(DENSITY_CSV_PATH),
            "density_json_path": str(DENSITY_JSON_PATH),
        },
        "calendar_market_bar_mapping": density_rows,
        "visual_interpretation": {
            "labels": [
                "MARKET_TIME_UNIFICATION_VISIBLE",
                "FAST_EXPANSION_VISIBLE",
                "SLOW_COMPRESSION_VISIBLE",
                "MMA_STRUCTURE_READABLE",
                "MMA_STRUCTURE_NOISY",
                "CALENDAR_STRUCTURE_CLEARER",
                "MARKET_BAR_STRUCTURE_CLEARER",
                "NO_CLEAR_DIFFERENCE",
            ],
            "status": "HUMAN_REVIEW_REQUIRED",
        },
    }
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=V09_PATH)
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    args = parser.parse_args()
    summary = run_proof(input_path=args.input, output_root=args.output_root)
    print(
        json.dumps(
            {
                "proof_version": summary["proof_version"],
                "market_bar_count": summary["pilot"]["market_bar_count"],
                "MMA_valid_counts": summary["mma_valid_counts"],
                "charts": {
                    "calendar": summary["calendar_reference"]["chart_path"],
                    "market": summary["market_bar_chart"]["chart_path"],
                    "zoom": summary["market_bar_zoom"]["chart_path"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
