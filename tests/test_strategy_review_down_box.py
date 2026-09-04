from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.models import DailyBar, Ohlcv
from src.strategy_review.chart import (
    ChartType,
    prepare_review_chart,
    render_review_chart,
)
from src.strategy_review.down_box_review import run_down_box_review_proof


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
