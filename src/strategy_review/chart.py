"""Minimal PNG charts for auditing Strategy V1 signals and fills.

The price axis is always adjusted ``DailyBar.signal`` OHLC.  RAW execution
prices are retained in event metadata and represented only by vertical date
markers, never by a y-coordinate on the adjusted axis.
"""

from __future__ import annotations

import binascii
import json
import re
import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import (
    DailyIndicatorPoint,
    calculate_daily_indicators,
)
from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import TradingCalendar
from src.backtest_engine.validation import validate_daily_bars


class ChartType(str, Enum):
    """The only two review outputs supported by V1."""

    STOCK_OVERVIEW = "STOCK_OVERVIEW"
    EVENT_REVIEW = "EVENT_REVIEW"


class ReviewEventType(str, Enum):
    """Event vocabulary supported by UP and DOWN review charts."""

    BASELINE_ENTRY_CANDIDATE = "BASELINE_ENTRY_CANDIDATE"
    LOW_REQUIRED_ENTRY_CANDIDATE = "LOW_REQUIRED_ENTRY_CANDIDATE"
    DOWN_CONTEXT_SATISFIED = "DOWN_CONTEXT_SATISFIED"
    SMA10_BREAKOUT = "SMA10_BREAKOUT"
    REJECTED_STEEP_MA20 = "REJECTED_STEEP_MA20"
    REJECTED_MA20_RESISTANCE = "REJECTED_MA20_RESISTANCE"
    REJECTED_MA60_RESISTANCE = "REJECTED_MA60_RESISTANCE"
    RED_THREE_SOLDIERS = "RED_THREE_SOLDIERS"
    SURGE_SETUP = "SURGE_SETUP"
    PULLBACK_TOUCH = "PULLBACK_TOUCH"
    DAILY_BUY_CANDIDATE = "DAILY_BUY_CANDIDATE"
    ENTRY_FILL = "ENTRY_FILL"
    DAILY_FULL_EXIT = "DAILY_FULL_EXIT"
    EXIT_FILL = "EXIT_FILL"


FILL_EVENT_TYPES = frozenset({ReviewEventType.ENTRY_FILL, ReviewEventType.EXIT_FILL})


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    """One proof-derived event placed on a Daily review chart."""

    event_type: ReviewEventType
    event_date: date
    label: str
    adjusted_plot_price: Decimal | None = None
    raw_fill_price: Decimal | None = None
    source_label: str | None = None
    details: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("event label must not be empty")
        is_fill = self.event_type in FILL_EVENT_TYPES
        if is_fill:
            if self.adjusted_plot_price is not None:
                raise ValueError("RAW fill must not have an adjusted-axis y price")
            if self.raw_fill_price is None or self.raw_fill_price <= 0:
                raise ValueError("fill event requires a positive RAW price")
            if not self.source_label:
                raise ValueError("fill event requires its source label/time")
        elif self.raw_fill_price is not None or self.source_label is not None:
            raise ValueError("RAW fill fields are reserved for fill events")
        if self.adjusted_plot_price is not None and self.adjusted_plot_price <= 0:
            raise ValueError("adjusted plot price must be positive")


@dataclass(frozen=True, slots=True)
class ReviewWindow:
    """Inclusive session window selected from canonical Daily bars."""

    bars: tuple[DailyBar, ...]
    start_index: int
    end_index: int


@dataclass(frozen=True, slots=True)
class PreparedReviewChart:
    """Validated chart inputs with full-history SMA values aligned by date."""

    chart_type: ChartType
    stock_code: str
    window: ReviewWindow
    indicators: tuple[DailyIndicatorPoint, ...]
    events: tuple[ReviewEvent, ...]
    event_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ChartArtifact:
    """Files and backend produced by one chart render."""

    png_path: Path
    metadata_path: Path
    backend: str


def _canonical_bars(bars: Sequence[DailyBar]) -> tuple[DailyBar, ...]:
    canonical = tuple(sorted(bars, key=lambda bar: (bar.stock_code, bar.trade_date)))
    validate_daily_bars(canonical)
    if not canonical:
        raise ValueError("Daily bars must not be empty")
    if len({bar.stock_code for bar in canonical}) != 1:
        raise ValueError("review chart supports exactly one stock")
    return canonical


def select_review_window(
    bars: Sequence[DailyBar],
    *,
    chart_type: ChartType,
    focus_date: date | None = None,
    event_end_date: date | None = None,
    pre_sessions: int = 60,
    post_sessions: int = 20,
    open_ended: bool = False,
) -> ReviewWindow:
    """Select an inclusive overview, trade, or blocked-event window."""

    canonical = _canonical_bars(bars)
    if chart_type is ChartType.STOCK_OVERVIEW:
        return ReviewWindow(canonical, 0, len(canonical) - 1)
    if chart_type is not ChartType.EVENT_REVIEW:
        raise TypeError("chart_type must be a ChartType")
    if focus_date is None:
        raise ValueError("EVENT_REVIEW requires focus_date")
    if pre_sessions < 0 or post_sessions < 0:
        raise ValueError("pre/post sessions must be non-negative")
    date_to_index = {bar.trade_date: index for index, bar in enumerate(canonical)}
    if focus_date not in date_to_index:
        raise ValueError("focus_date is not a Daily trading session")
    focus_index = date_to_index[focus_date]
    if event_end_date is not None:
        if event_end_date not in date_to_index:
            raise ValueError("event_end_date is not a Daily trading session")
        end_anchor = date_to_index[event_end_date]
        if end_anchor < focus_index:
            raise ValueError("event_end_date must not precede focus_date")
    else:
        end_anchor = focus_index
    start_index = max(0, focus_index - pre_sessions)
    end_index = (
        len(canonical) - 1
        if open_ended and event_end_date is None
        else min(len(canonical) - 1, end_anchor + post_sessions)
    )
    return ReviewWindow(canonical[start_index : end_index + 1], start_index, end_index)


def prepare_review_chart(
    bars: Sequence[DailyBar],
    *,
    chart_type: ChartType,
    events: Sequence[ReviewEvent] = (),
    calendar: TradingCalendar | None = None,
    focus_date: date | None = None,
    event_end_date: date | None = None,
    pre_sessions: int = 60,
    post_sessions: int = 20,
    open_ended: bool = False,
) -> PreparedReviewChart:
    """Validate events and align existing engine SMA values to the window."""

    canonical = _canonical_bars(bars)
    full_points = tuple(calculate_daily_indicators(canonical, calendar))
    window = select_review_window(
        canonical,
        chart_type=chart_type,
        focus_date=focus_date,
        event_end_date=event_end_date,
        pre_sessions=pre_sessions,
        post_sessions=post_sessions,
        open_ended=open_ended,
    )
    points_by_date = {point.trade_date: point for point in full_points}
    window_points = tuple(points_by_date[bar.trade_date] for bar in window.bars)
    date_to_index = {bar.trade_date: index for index, bar in enumerate(window.bars)}
    canonical_events = tuple(
        sorted(
            events,
            key=lambda item: (item.event_date, item.event_type.value, item.label),
        )
    )
    missing = [
        event.event_date
        for event in canonical_events
        if event.event_date not in date_to_index
    ]
    if missing:
        raise ValueError(f"event dates outside selected review window: {missing}")
    return PreparedReviewChart(
        chart_type=chart_type,
        stock_code=canonical[0].stock_code,
        window=window,
        indicators=window_points,
        events=canonical_events,
        event_indexes=tuple(
            date_to_index[event.event_date] for event in canonical_events
        ),
    )


def deterministic_chart_filename(
    stock_code: str,
    chart_type: ChartType,
    focus_date: date | None = None,
    *,
    slug: str | None = None,
) -> str:
    """Return a stable, filesystem-safe PNG filename."""

    if not re.fullmatch(r"[0-9]{6}", stock_code, flags=re.ASCII):
        raise ValueError("stock_code must be a six-digit ASCII string")
    if chart_type is ChartType.EVENT_REVIEW and focus_date is None:
        raise ValueError("EVENT_REVIEW filename requires focus_date")
    stem = (
        "overview"
        if chart_type is ChartType.STOCK_OVERVIEW
        else f"event-{focus_date.isoformat()}"
    )
    if slug:
        safe_slug = re.sub(r"[^a-z0-9]+", "-", slug.casefold()).strip("-")
        if not safe_slug:
            raise ValueError("slug has no filesystem-safe characters")
        stem = f"{stem}-{safe_slug}"
    return f"{stock_code}-{stem}.png"


def render_review_chart(
    prepared: PreparedReviewChart,
    output_path: Path,
    *,
    strategy_policy: str,
    summary: Mapping[str, Any] | None = None,
) -> ChartArtifact:
    """Render PNG plus small audit JSON, preferring matplotlib when installed."""

    output_path = Path(output_path)
    if output_path.suffix.casefold() != ".png":
        raise ValueError("output_path must end in .png")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _render_matplotlib(prepared, output_path)
        backend = "matplotlib"
    except ModuleNotFoundError as exc:
        if exc.name != "matplotlib":
            raise
        _render_stdlib_png(prepared, output_path)
        backend = "stdlib_png_fallback"
    metadata_path = output_path.with_suffix(".json")
    metadata = _chart_metadata(prepared, strategy_policy, summary, backend)
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return ChartArtifact(output_path, metadata_path, backend)


def _render_matplotlib(prepared: PreparedReviewChart, output_path: Path) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    figure, axis = plt.subplots(figsize=(14, 8), constrained_layout=True)
    for index, bar in enumerate(prepared.window.bars):
        rising = bar.signal.close >= bar.signal.open
        color = "#d62728" if rising else "#1f77b4"
        axis.vlines(index, float(bar.signal.low), float(bar.signal.high), color=color)
        body_low = min(bar.signal.open, bar.signal.close)
        height = max(abs(bar.signal.close - bar.signal.open), Decimal("0.000001"))
        axis.add_patch(
            Rectangle(
                (index - 0.3, float(body_low)),
                0.6,
                float(height),
                facecolor=color,
                edgecolor=color,
                linewidth=0.7,
            )
        )
    for field, label, color in (
        ("sma10", "SMA10", "#9467bd"),
        ("sma20", "SMA20", "#ff7f0e"),
        ("sma60", "SMA60", "#2ca02c"),
    ):
        values = [getattr(point, field) for point in prepared.indicators]
        axis.plot(
            range(len(values)),
            [float(value) if value is not None else float("nan") for value in values],
            label=label,
            color=color,
            linewidth=1.2,
        )
    for ordinal, (event, event_index) in enumerate(
        zip(prepared.events, prepared.event_indexes, strict=True)
    ):
        if event.event_type in FILL_EVENT_TYPES:
            axis.axvline(event_index, color="#111111", linestyle="--", alpha=0.75)
            text = f"{event.label} RAW {event.raw_fill_price} {event.source_label}"
            axis.annotate(
                text,
                xy=(event_index, 0.98 - (ordinal % 4) * 0.045),
                xycoords=("data", "axes fraction"),
                fontsize=7,
                rotation=90,
                va="top",
            )
        else:
            price = event.adjusted_plot_price
            if price is not None:
                axis.scatter(event_index, float(price), marker="^", s=45, zorder=5)
                axis.annotate(event.label, (event_index, float(price)), fontsize=7)
    axis.set_title(f"{prepared.stock_code} {prepared.chart_type.value}")
    axis.set_ylabel("SIGNAL_ADJUSTED price")
    axis.grid(alpha=0.18)
    axis.legend(loc="upper left")
    tick_step = max(1, len(prepared.window.bars) // 8)
    ticks = list(range(0, len(prepared.window.bars), tick_step))
    axis.set_xticks(ticks)
    axis.set_xticklabels(
        [prepared.window.bars[index].trade_date.isoformat() for index in ticks],
        rotation=35,
        ha="right",
    )
    figure.savefig(output_path, dpi=120)
    plt.close(figure)


def _chart_metadata(
    prepared: PreparedReviewChart,
    strategy_policy: str,
    summary: Mapping[str, Any] | None,
    backend: str,
) -> dict[str, Any]:
    return {
        "chart_type": prepared.chart_type.value,
        "stock_code": prepared.stock_code,
        "strategy_policy": strategy_policy,
        "price_axis_basis": "SIGNAL_ADJUSTED_DAILY_OHLC",
        "moving_average_basis": "SIGNAL_ADJUSTED_DAILY_CLOSE",
        "fill_price_policy": "RAW_METADATA_ONLY_VERTICAL_DATE_MARKER",
        "window_start": prepared.window.bars[0].trade_date,
        "window_end": prepared.window.bars[-1].trade_date,
        "render_backend": backend,
        "events": [asdict(event) for event in prepared.events],
        "summary": dict(summary or {}),
    }


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal, Enum)):
        return (
            value.isoformat()
            if isinstance(value, date)
            else str(value.value if isinstance(value, Enum) else value)
        )
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


# A deliberately tiny domain-specific fallback for offline Windows workspaces
# where matplotlib is unavailable and package installation would require network.
_FONT = {
    " ": ("00000",) * 7,
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    ":": ("00000", "00110", "00110", "00000", "00110", "00110", "00000"),
    "%": ("11001", "11010", "00100", "01000", "10110", "00110", "00000"),
    **{
        char: tuple(rows.split("/"))
        for char, rows in {
            "0": "01110/10001/10011/10101/11001/10001/01110",
            "1": "00100/01100/00100/00100/00100/00100/01110",
            "2": "01110/10001/00001/00010/00100/01000/11111",
            "3": "11110/00001/00001/01110/00001/00001/11110",
            "4": "00010/00110/01010/10010/11111/00010/00010",
            "5": "11111/10000/10000/11110/00001/00001/11110",
            "6": "01110/10000/10000/11110/10001/10001/01110",
            "7": "11111/00001/00010/00100/01000/01000/01000",
            "8": "01110/10001/10001/01110/10001/10001/01110",
            "9": "01110/10001/10001/01111/00001/00001/01110",
            "A": "01110/10001/10001/11111/10001/10001/10001",
            "B": "11110/10001/10001/11110/10001/10001/11110",
            "C": "01111/10000/10000/10000/10000/10000/01111",
            "D": "11110/10001/10001/10001/10001/10001/11110",
            "E": "11111/10000/10000/11110/10000/10000/11111",
            "F": "11111/10000/10000/11110/10000/10000/10000",
            "G": "01111/10000/10000/10111/10001/10001/01111",
            "H": "10001/10001/10001/11111/10001/10001/10001",
            "I": "01110/00100/00100/00100/00100/00100/01110",
            "J": "00111/00010/00010/00010/10010/10010/01100",
            "K": "10001/10010/10100/11000/10100/10010/10001",
            "L": "10000/10000/10000/10000/10000/10000/11111",
            "M": "10001/11011/10101/10101/10001/10001/10001",
            "N": "10001/11001/10101/10011/10001/10001/10001",
            "O": "01110/10001/10001/10001/10001/10001/01110",
            "P": "11110/10001/10001/11110/10000/10000/10000",
            "Q": "01110/10001/10001/10001/10101/10010/01101",
            "R": "11110/10001/10001/11110/10100/10010/10001",
            "S": "01111/10000/10000/01110/00001/00001/11110",
            "T": "11111/00100/00100/00100/00100/00100/00100",
            "U": "10001/10001/10001/10001/10001/10001/01110",
            "V": "10001/10001/10001/10001/10001/01010/00100",
            "W": "10001/10001/10001/10101/10101/10101/01010",
            "X": "10001/10001/01010/00100/01010/10001/10001",
            "Y": "10001/10001/01010/00100/00100/00100/00100",
            "Z": "11111/00001/00010/00100/01000/10000/11111",
        }.items()
    },
}


class _Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray([255]) * (width * height * 3)

    def point(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            offset = (y * self.width + x) * 3
            self.pixels[offset : offset + 3] = bytes(color)

    def line(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
        thickness: int = 1,
    ) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx + dy
        while True:
            for ox in range(-(thickness // 2), thickness - thickness // 2):
                for oy in range(-(thickness // 2), thickness - thickness // 2):
                    self.point(x0 + ox, y0 + oy, color)
            if x0 == x1 and y0 == y1:
                return
            twice = 2 * error
            if twice >= dy:
                error += dy
                x0 += sx
            if twice <= dx:
                error += dx
                y0 += sy

    def rectangle(
        self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]
    ) -> None:
        for y in range(max(0, min(y0, y1)), min(self.height, max(y0, y1) + 1)):
            for x in range(max(0, min(x0, x1)), min(self.width, max(x0, x1) + 1)):
                self.point(x, y, color)

    def text(
        self,
        x: int,
        y: int,
        value: str,
        color: tuple[int, int, int] = (20, 20, 20),
        scale: int = 2,
    ) -> None:
        cursor = x
        for character in value.upper():
            glyph = _FONT.get(character, _FONT[" "])
            for row, pattern in enumerate(glyph):
                for column, bit in enumerate(pattern):
                    if bit == "1":
                        self.rectangle(
                            cursor + column * scale,
                            y + row * scale,
                            cursor + (column + 1) * scale - 1,
                            y + (row + 1) * scale - 1,
                            color,
                        )
            cursor += 6 * scale

    def write_png(self, path: Path) -> None:
        scanlines = bytearray()
        stride = self.width * 3
        for y in range(self.height):
            scanlines.append(0)
            scanlines.extend(self.pixels[y * stride : (y + 1) * stride])
        payload = bytearray(b"\x89PNG\r\n\x1a\n")
        payload.extend(
            _png_chunk(
                b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
            )
        )
        payload.extend(_png_chunk(b"IDAT", zlib.compress(bytes(scanlines), 9)))
        payload.extend(_png_chunk(b"IEND", b""))
        path.write_bytes(payload)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    )


def _render_stdlib_png(prepared: PreparedReviewChart, output_path: Path) -> None:
    canvas = _Canvas(1400, 800)
    left, top, right, bottom = 80, 90, 1360, 720
    bars = prepared.window.bars
    values = [value for bar in bars for value in (bar.signal.low, bar.signal.high)]
    for point in prepared.indicators:
        values.extend(
            value
            for value in (point.sma10, point.sma20, point.sma60)
            if value is not None
        )
    minimum, maximum = min(values), max(values)
    padding = max((maximum - minimum) * Decimal("0.05"), Decimal(1))
    minimum -= padding
    maximum += padding

    def x_of(index: int) -> int:
        return (
            left
            if len(bars) == 1
            else left + round(index * (right - left) / (len(bars) - 1))
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
    candle_width = max(1, min(5, (right - left) // max(1, len(bars) * 3)))
    for index, bar in enumerate(bars):
        x = x_of(index)
        color = (205, 55, 55) if bar.signal.close >= bar.signal.open else (40, 95, 185)
        canvas.line(x, y_of(bar.signal.low), x, y_of(bar.signal.high), color)
        canvas.rectangle(
            x - candle_width,
            y_of(max(bar.signal.open, bar.signal.close)),
            x + candle_width,
            y_of(min(bar.signal.open, bar.signal.close)),
            color,
        )
    for field, color in (
        ("sma10", (145, 80, 175)),
        ("sma20", (240, 125, 25)),
        ("sma60", (45, 150, 75)),
    ):
        previous: tuple[int, int] | None = None
        for index, point in enumerate(prepared.indicators):
            value = getattr(point, field)
            current = None if value is None else (x_of(index), y_of(value))
            if previous is not None and current is not None:
                canvas.line(*previous, *current, color, 2)
            previous = current
    for ordinal, (event, index) in enumerate(
        zip(prepared.events, prepared.event_indexes, strict=True)
    ):
        x = x_of(index)
        if event.event_type in FILL_EVENT_TYPES:
            canvas.line(x, top, x, bottom, (30, 30, 30))
            annotation = (
                f"{event.label} RAW {event.raw_fill_price} {event.source_label}"
            )
            canvas.text(
                max(left, min(x + 4, right - len(annotation) * 12)),
                top + (ordinal % 5) * 18,
                annotation,
                scale=1,
            )
        elif event.adjusted_plot_price is not None:
            y = y_of(event.adjusted_plot_price)
            canvas.line(x - 6, y + 6, x, y - 6, (0, 0, 0), 2)
            canvas.line(x, y - 6, x + 6, y + 6, (0, 0, 0), 2)
            canvas.text(
                max(left, min(x + 7, right - len(event.label) * 12)),
                max(top, y - 18),
                event.label,
                scale=1,
            )
    canvas.text(left, 25, f"{prepared.stock_code} {prepared.chart_type.value}", scale=2)
    canvas.text(left, 55, "SIGNAL ADJUSTED OHLC  SMA10  SMA20  SMA60", scale=1)
    for index in sorted({0, len(bars) // 2, len(bars) - 1}):
        canvas.text(
            max(left, x_of(index) - 30),
            bottom + 10,
            bars[index].trade_date.isoformat(),
            scale=1,
        )
    canvas.write_png(output_path)
