from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.down_box_strategy import (
    BoxEvent,
    BoxEventType,
    BoxSignal,
    BoxSignalType,
)
from src.backtest_engine.indicators import calculate_daily_indicators
from src.backtest_engine.models import DailyBar, Ohlcv
from src.strategy_review.chart import (
    ChartType,
    prepare_review_chart,
    render_review_chart,
)
from src.strategy_review.down_box_review import (
    box_relative_position,
    build_entry_location_rows,
    run_down_box_review_proof,
    summarize_entry_locations,
)


def _bars(count: int = 80) -> tuple[DailyBar, ...]:
    start = date(2026, 1, 2)
    rows = []
    for index in range(count):
        close = Decimal(100 + index)
        ohlcv = Ohlcv(close - 1, close + 2, close - 2, close, 1000)
        rows.append(DailyBar("005930", start + timedelta(days=index), ohlcv, ohlcv))
    return tuple(rows)


def test_down_box_chart_contains_sma5_and_frozen_box_levels(tmp_path) -> None:
    bars = _bars()
    prepared = prepare_review_chart(
        bars,
        chart_type=ChartType.EVENT_REVIEW,
        focus_date=bars[40].trade_date,
        post_sessions=30,
        show_sma5=True,
        horizontal_levels={
            "BOX_FLOOR": Decimal(100),
            "LOWER_ZONE_UPPER": Decimal(103),
            "BOX_UPPER": Decimal(140),
            "UPPER_SELL_LEVEL": Decimal("135.8"),
        },
    )
    assert prepared.show_sma5 is True
    assert prepared.sma5[-1] == Decimal(168)
    assert tuple(label for label, _ in prepared.horizontal_levels) == (
        "BOX_FLOOR",
        "BOX_UPPER",
        "LOWER_ZONE_UPPER",
        "UPPER_SELL_LEVEL",
    )
    artifact = render_review_chart(
        prepared,
        tmp_path / "box.png",
        strategy_policy="DOWN_BOX_REVERSAL_V0_1_SIGNAL_ONLY",
    )
    metadata = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
    assert metadata["show_sma5"] is True
    assert metadata["horizontal_levels"]["BOX_UPPER"] == "140"


def test_empty_offline_review_has_no_network_or_execution(tmp_path) -> None:
    result = run_down_box_review_proof(
        output=tmp_path / "proof.json",
        chart_root=tmp_path / "charts",
        stocks=(),
    )
    assert result["network_calls"] == 0
    assert result["execution"] == {"five_minute": False, "fills": False, "pnl": False}
    assert result["selected_chart_count"] == 0


def test_box_relative_position_is_decimal_and_not_clamped() -> None:
    floor = Decimal(100)
    upper = Decimal(120)
    assert box_relative_position(floor, floor, upper) == Decimal(0)
    assert box_relative_position(upper, floor, upper) == Decimal(1)
    assert box_relative_position(Decimal(90), floor, upper) == Decimal("-0.5")
    assert box_relative_position(Decimal(130), floor, upper) == Decimal("1.5")


def test_entry_location_uses_latest_past_touch_and_ignores_future_touch() -> None:
    bars = list(_bars(14))
    for index, low in ((9, Decimal(102)), (11, Decimal(101)), (13, Decimal(100))):
        bar = bars[index]
        raw = Ohlcv(bar.raw.open, bar.raw.high, low, bar.raw.close, bar.raw.volume)
        bars[index] = DailyBar(bar.stock_code, bar.trade_date, raw, raw)
    bars = tuple(bars)
    setup_id = "setup-location-1"
    origin = bars[10].trade_date
    signal_date = bars[12].trade_date
    result = {
        "setup_origins": (
            {
                "trade_date": origin,
                "issue": None,
                "box_floor": Decimal(100),
                "box_upper": Decimal(140),
                "floor_pivot_date": bars[5].trade_date,
                "upper_pivot_date": bars[0].trade_date,
            },
        ),
        "events": (
            BoxEvent(
                BoxEventType.REVERSAL_SETUP_CREATED,
                "005930",
                origin,
                setup_id,
                "DOWN_ORIGIN",
            ),
        ),
        "signals": (
            BoxSignal(
                "signal-location-1",
                BoxSignalType.ENTRY_CANDIDATE_MA5_TURN,
                "005930",
                signal_date,
                setup_id,
                "MA5_TURN",
            ),
        ),
    }
    rows = build_entry_location_rows(
        result, bars, tuple(calculate_daily_indicators(bars))
    )
    assert len(rows) == 1
    assert rows[0]["most_recent_lower_zone_touch_date"] == bars[11].trade_date
    assert rows[0]["sessions_since_lower_zone_touch"] == 1


def test_entry_location_percentiles_ignore_input_order() -> None:
    rows = (
        {
            "entry_type": "MA5_TURN",
            "box_position_close": Decimal("0.1"),
            "box_position_low": Decimal("-0.1"),
            "sessions_since_lower_zone_touch": 2,
        },
        {
            "entry_type": "MA5_TURN",
            "box_position_close": Decimal("0.5"),
            "box_position_low": Decimal("0.3"),
            "sessions_since_lower_zone_touch": 0,
        },
    )
    forward = summarize_entry_locations(rows)
    reverse = summarize_entry_locations(tuple(reversed(rows)))
    assert forward == reverse
    assert forward["MA5_TURN"]["box_position_close"]["median"] == Decimal("0.30")
