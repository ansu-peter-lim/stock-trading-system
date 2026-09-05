from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.backtest_engine.models import DailyBar, Ohlcv
from src.kiwoom_daily.market_speed_audit import (
    _coerce_baseline_row,
    _past_reference,
    _rolling_std,
    _speed_row,
    build_regime_outcomes,
)


def _bars(count: int = 40) -> tuple[DailyBar, ...]:
    output: list[DailyBar] = []
    for index in range(count):
        close = Decimal(100 + index)
        ohlcv = Ohlcv(close - 1, close + 1, close - 2, close, 100 + index)
        output.append(
            DailyBar(
                "005930",
                date(2024, 1, 1) + timedelta(days=index),
                ohlcv,
                ohlcv,
            )
        )
    return tuple(output)


def _points(bars: tuple[DailyBar, ...]):
    from src.backtest_engine.indicators import DailyIndicatorPoint

    return tuple(
        DailyIndicatorPoint(
            stock_code=bar.stock_code,
            trade_date=bar.trade_date,
            daily_return=None,
            sma10=Decimal(100),
            sma20=Decimal(100),
            sma60=Decimal(100),
            ma20_slope_5=None,
            ma60_slope_5=None,
        )
        for bar in bars
    )


def _baseline(d3: date) -> dict[str, object]:
    return {
        "stock_code": "005930",
        "setup_id": "setup",
        "entry_type": "MA5_TURN",
        "entry_signal_date": d3 - timedelta(days=10),
        "entry_fill_date": d3 - timedelta(days=9),
        "upper_exit_signal_date": d3 - timedelta(days=2),
        "upper_exit_fill_date": d3 - timedelta(days=1),
        "d1_date": d3 - timedelta(days=1),
        "d2_date": d3,
        "d3_date": d3,
        "box_floor": Decimal(80),
        "box_upper": Decimal(120),
        "entry_raw_price": Decimal(100),
        "entry_signal_close": Decimal(100),
        "exit_raw_price": Decimal(110),
        "future_5_session_return": Decimal("0.1"),
        "future_10_session_return": Decimal("0.2"),
        "future_20_session_return": Decimal("0.3"),
        "maximum_favorable_excursion": Decimal("0.3"),
        "maximum_adverse_excursion": Decimal("-0.1"),
        "sma60_flatness_5": Decimal("0.01"),
        "structural_up_transition": False,
    }


def test_rolling_std_uses_population_definition() -> None:
    values = [Decimal(1), Decimal(2)]
    assert _rolling_std(values, 1, 2) == Decimal("0.5")


def test_reference_uses_only_previous_values() -> None:
    values = [Decimal(index) for index in range(253)]
    assert _past_reference(values, 252, minimum=2) == Decimal("250.5")


def test_speed_row_marks_short_history_without_inference() -> None:
    bars = _bars()
    row = _speed_row(_baseline(bars[30].trade_date), bars, _points(bars))
    assert row["speed_status"] == "INSUFFICIENT_DATA"
    assert row["rv20"] is not None
    assert row["rv_ref"] is None
    assert row["variance_speed"] is None
    assert row["flow_speed"] is None
    assert row["ma20_effective"] is None
    assert row["atr20"] is not None


def test_regime_outcomes_keep_future_metrics_report_only() -> None:
    rows = [
        {
            "sma20_regime": "PERSISTENT_UP",
            "future_5_session_return": Decimal("0.1"),
            "future_10_session_return": Decimal("0.2"),
            "future_20_session_return": Decimal("0.3"),
            "maximum_favorable_excursion": Decimal("0.3"),
            "maximum_adverse_excursion": Decimal("-0.1"),
            "variance_speed": None,
            "flow_speed": None,
            "sma60_flatness_5": Decimal("0.01"),
        }
    ]
    result = build_regime_outcomes(rows)
    assert result["PERSISTENT_UP"]["count"] == 1
    assert result["PERSISTENT_UP"]["future_20"]["mean"] == Decimal("0.3")
    assert result["NEWLY_UP"]["count"] == 0


def test_baseline_coercion_preserves_identity_and_dates() -> None:
    row = _coerce_baseline_row(
        {
            "stock_code": "005930",
            "setup_id": "s",
            "upper_exit_fill_date": "2026-01-05",
            "future_20_session_return": "0.31",
        }
    )
    assert row["stock_code"] == "005930"
    assert row["upper_exit_fill_date"] == date(2026, 1, 5)
    assert row["future_20_session_return"] == Decimal("0.31")
