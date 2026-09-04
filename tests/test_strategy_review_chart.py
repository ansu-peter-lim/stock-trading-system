from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest

from src.backtest_engine.indicators import calculate_daily_indicators
from src.backtest_engine.models import DailyBar, Ohlcv
from src.strategy_review.chart import (
    ChartType,
    ReviewEvent,
    ReviewEventType,
    deterministic_chart_filename,
    prepare_review_chart,
    render_review_chart,
    select_review_window,
)


def _bars(count: int = 100) -> tuple[DailyBar, ...]:
    start = date(2026, 1, 1)
    output = []
    for index in range(count):
        close = Decimal(100 + index)
        output.append(
            DailyBar(
                "005930",
                start + timedelta(days=index),
                Ohlcv(close - 1, close + 2, close - 2, close, 1000 + index),
                Ohlcv(close, close + 3, close - 3, close + 1, 2000 + index),
            )
        )
    return tuple(output)


def test_review_window_selects_60_before_and_20_after() -> None:
    bars = _bars()
    window = select_review_window(
        bars,
        chart_type=ChartType.EVENT_REVIEW,
        focus_date=bars[70].trade_date,
    )
    assert window.start_index == 10
    assert window.end_index == 90
    assert len(window.bars) == 81


def test_review_window_clamps_pre_and_post_boundaries() -> None:
    bars = _bars(30)
    early = select_review_window(
        bars,
        chart_type=ChartType.EVENT_REVIEW,
        focus_date=bars[2].trade_date,
    )
    late = select_review_window(
        bars,
        chart_type=ChartType.EVENT_REVIEW,
        focus_date=bars[-2].trade_date,
    )
    assert (early.start_index, early.end_index) == (0, 22)
    assert (late.start_index, late.end_index) == (0, 29)


def test_sma_alignment_reuses_full_existing_engine_history() -> None:
    bars = _bars()
    expected = calculate_daily_indicators(bars)
    prepared = prepare_review_chart(
        bars,
        chart_type=ChartType.EVENT_REVIEW,
        focus_date=bars[70].trade_date,
    )
    for offset, point in enumerate(prepared.indicators):
        source = expected[prepared.window.start_index + offset]
        assert point == source


def test_events_map_to_canonical_window_dates() -> None:
    bars = _bars()
    event = ReviewEvent(
        ReviewEventType.DAILY_BUY_CANDIDATE,
        bars[70].trade_date,
        "UP BUY",
        adjusted_plot_price=bars[70].signal.close,
    )
    prepared = prepare_review_chart(
        tuple(reversed(bars)),
        chart_type=ChartType.EVENT_REVIEW,
        focus_date=bars[70].trade_date,
        events=(event,),
    )
    assert (
        prepared.window.bars[prepared.event_indexes[0]].trade_date == event.event_date
    )


def test_raw_fill_is_metadata_only_and_cannot_use_adjusted_y() -> None:
    with pytest.raises(ValueError, match="adjusted-axis"):
        ReviewEvent(
            ReviewEventType.ENTRY_FILL,
            date(2026, 1, 2),
            "ENTRY",
            adjusted_plot_price=Decimal(110),
            raw_fill_price=Decimal(95),
            source_label="20260102090500",
        )
    event = ReviewEvent(
        ReviewEventType.ENTRY_FILL,
        date(2026, 1, 2),
        "ENTRY",
        raw_fill_price=Decimal(95),
        source_label="20260102090500",
    )
    assert event.adjusted_plot_price is None
    assert event.raw_fill_price == Decimal(95)


def test_deterministic_filename() -> None:
    first = deterministic_chart_filename(
        "005930", ChartType.EVENT_REVIEW, date(2026, 1, 2), slug="Close Only"
    )
    second = deterministic_chart_filename(
        "005930", ChartType.EVENT_REVIEW, date(2026, 1, 2), slug="Close Only"
    )
    assert first == second == "005930-event-2026-01-02-close-only.png"


def test_empty_optional_events_and_metadata_contract(tmp_path) -> None:
    bars = _bars(70)
    prepared = prepare_review_chart(
        bars,
        chart_type=ChartType.STOCK_OVERVIEW,
    )
    artifact = render_review_chart(
        prepared,
        tmp_path / "overview.png",
        strategy_policy="TEST",
    )
    metadata = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
    assert artifact.png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert metadata["events"] == []
    assert metadata["price_axis_basis"] == "SIGNAL_ADJUSTED_DAILY_OHLC"
    assert metadata["fill_price_policy"] == "RAW_METADATA_ONLY_VERTICAL_DATE_MARKER"


def test_rendered_fill_keeps_raw_price_without_adjusted_coordinate(tmp_path) -> None:
    bars = _bars(70)
    fill = ReviewEvent(
        ReviewEventType.ENTRY_FILL,
        bars[60].trade_date,
        "ENTRY",
        raw_fill_price=Decimal(95),
        source_label="20260302090500",
    )
    prepared = prepare_review_chart(
        bars,
        chart_type=ChartType.EVENT_REVIEW,
        focus_date=bars[60].trade_date,
        events=(fill,),
    )
    artifact = render_review_chart(
        prepared,
        tmp_path / "fill.png",
        strategy_policy="TEST",
    )
    event = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))["events"][0]
    assert event["raw_fill_price"] == "95"
    assert event["adjusted_plot_price"] is None


def test_up_band_and_down_context_overlay_flags_are_serialized(tmp_path) -> None:
    bars = _bars(70)
    prepared = prepare_review_chart(
        bars,
        chart_type=ChartType.STOCK_OVERVIEW,
        show_ma20_band=True,
        shade_below_sma10_context=True,
    )
    artifact = render_review_chart(
        prepared, tmp_path / "overlays.png", strategy_policy="TEST"
    )
    metadata = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
    assert metadata["show_ma20_band"] is True
    assert metadata["shade_below_sma10_context"] is True


def test_event_outside_window_is_rejected() -> None:
    bars = _bars()
    event = ReviewEvent(
        ReviewEventType.DAILY_BUY_CANDIDATE,
        bars[0].trade_date,
        "UP BUY",
        adjusted_plot_price=bars[0].signal.close,
    )
    with pytest.raises(ValueError, match="outside selected review window"):
        prepare_review_chart(
            bars,
            chart_type=ChartType.EVENT_REVIEW,
            focus_date=bars[90].trade_date,
            events=(event,),
            pre_sessions=5,
            post_sessions=5,
        )
