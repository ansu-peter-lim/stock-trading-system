from datetime import date
from decimal import Decimal

from src.kiwoom_daily.market_bar_mma_role_stability_audit import (
    _attach_source_regimes,
    _cross_direction,
    _decorate_market_values,
    _market_pivots,
    _nearest_role,
    _turns,
    run_audit,
)


def _row(index: int, *, close: int = 100) -> dict[str, object]:
    value = Decimal(close)
    return {
        "stock_code": "005930",
        "market_bar_index": index,
        "market_bar_id": f"005930:V06:{index - 1:06d}",
        "open": value,
        "high": value + 2,
        "low": value - 2,
        "close": value,
        "volume": Decimal(1),
        "tau_length": Decimal(1),
        "boundary_error": Decimal(0),
        "calendar_start_datetime": "2026-01-02T09:00:00+09:00",
        "calendar_end_datetime": "2026-01-02T15:30:00+09:00",
        "provenance": [
            {
                "source_id": "005930:2026-01-02:5M:0",
                "source_tau_start": "0",
                "source_tau_end": "1",
            }
        ],
    }


def test_mma_and_market_atr_warmup_counts_are_bar_count_based():
    rows = [_row(index, close=100 + index) for index in range(1, 202)]
    _decorate_market_values(rows)

    assert sum(row["MMA5"] is not None for row in rows) == 197
    assert sum(row["MMA10"] is not None for row in rows) == 192
    assert sum(row["MMA20"] is not None for row in rows) == 182
    assert sum(row["MMA60"] is not None for row in rows) == 142
    assert rows[19]["mb_atr20"] is None
    assert rows[20]["mb_atr20"] is not None


def test_source_regime_shares_are_tau_weighted_and_pure_only_at_one():
    row = _row(1)
    row["provenance"] = [
        {
            "source_id": "005930:2026-01-02:5M:0",
            "source_tau_start": "0",
            "source_tau_end": "2",
        },
        {
            "source_id": "005930:2026-01-03:5M:0",
            "source_tau_start": "2",
            "source_tau_end": "3",
        },
    ]
    _attach_source_regimes(
        [row],
        {
            date(2026, 1, 2): "FAST_DIRECTIONAL_HIGH_EFF",
            date(2026, 1, 3): "SLOW",
        },
    )

    shares = row["source_calendar_regime_tau_share"]
    assert shares["FAST_DIRECTIONAL_HIGH_EFF"] == Decimal(2) / Decimal(3)
    assert shares["SLOW"] == Decimal(1) / Decimal(3)
    assert sum(shares.values(), Decimal(0)) == Decimal(1)
    assert row["primary_source_regime"] == "MIXED_SOURCE_REGIME"


def test_nearest_role_breaks_exact_ties_to_shorter_horizon():
    role, distance, ties = _nearest_role(
        Decimal(100),
        {
            "MMA5": Decimal(99),
            "MMA10": Decimal(101),
            "MMA20": Decimal(110),
            "MMA60": Decimal(120),
        },
        Decimal(1),
    )

    assert (role, distance, ties) == ("MMA5", Decimal(1), 2)


def test_market_pivot_is_confirmed_at_p_plus_two():
    lows = (5, 4, 1, 4, 5, 6, 7)
    rows = [_row(index + 1, close=100) for index in range(len(lows))]
    for row, low in zip(rows, lows, strict=True):
        row["low"] = Decimal(low)
        row["high"] = Decimal(200 - low)
        row["primary_source_regime"] = "PURE_NORMAL_OTHER"
        row["source_calendar_regime_tau_share"] = {"NORMAL_OTHER": Decimal(1)}
        row["mb_atr20"] = None
        for period in (5, 10, 20, 60):
            row[f"MMA{period}"] = None

    pivots = _market_pivots(rows)
    low = next(row for row in pivots if row["pivot_kind"] == "LOW")
    assert low["pivot_market_bar_index"] == 3
    assert low["confirmed_market_bar_index"] == 5


def test_cross_and_turn_semantics_are_exact_and_do_not_use_same_bar_target():
    assert _cross_direction(Decimal(100), Decimal(101), Decimal(100), Decimal(100)) == 1
    assert _cross_direction(Decimal(100), Decimal(99), Decimal(100), Decimal(100)) == -1
    assert (
        _cross_direction(Decimal(101), Decimal(102), Decimal(100), Decimal(100)) is None
    )

    rows = [_row(index) for index in range(1, 5)]
    values = (Decimal(10), Decimal(10), Decimal(11), Decimal(12))
    for row, value in zip(rows, values, strict=True):
        row["MMA5"] = value
        row["primary_source_regime"] = "PURE_NORMAL_OTHER"
    turns = _turns(rows, 5)
    assert [(row["market_bar_index"], row["direction"]) for row in turns] == [(3, "UP")]


def test_full_frozen_audit_writes_report_only_outputs(tmp_path):
    report = run_audit(
        output_path=tmp_path / "audit.json",
        visual_root=tmp_path / "charts",
    )

    assert report["scope"]["market_bar_count"] == 201
    assert report["mma_valid_counts"] == {
        "MMA5": 197,
        "MMA10": 192,
        "MMA20": 182,
        "MMA60": 142,
    }
    assert report["market_bar_atr20_valid_count"] == 181
    assert report["scope"]["strategy"] is False
    assert report["scope"]["buy_sell"] is False
    assert report["scope"]["pnl"] is False
    assert report["network_calls"] == 0
    assert report["visual_pack"]["case_count"] <= 6
    assert (tmp_path / "audit.json").exists()
    assert all(
        (tmp_path / "charts" / f"{case['case_id']}.png").exists()
        for case in report["visual_pack"]["cases"]
    )
