from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from src.kiwoom_daily.market_time_invariance_audit import (
    _cached_minute_dates,
    _compression_episodes,
    _dispersion,
    _rate_summary,
    _tau_exposure,
)


def test_tau_exposure_preserves_calendar_and_tau_denominators() -> None:
    summary = _tau_exposure(
        [
            {"delta_tau": Decimal(1)},
            {"delta_tau": None},
            {"delta_tau": Decimal(2)},
        ]
    )
    assert summary == {
        "calendar_session_count": 3,
        "tau_eligible_session_count": 2,
        "total_tau": Decimal(3),
        "tau_per_calendar_session": Decimal(1),
    }


def test_tau_rate_excludes_event_without_tau_but_keeps_calendar_rate() -> None:
    rows = [
        {
            "stock_code": "005930",
            "trade_date": date(2024, 1, 2),
            "delta_tau": Decimal(2),
        },
        {"stock_code": "005930", "trade_date": date(2024, 1, 3), "delta_tau": None},
    ]
    events = [
        {
            "stock_code": "005930",
            "trade_date": date(2024, 1, 2),
            "delta_tau": Decimal(2),
        },
        {"stock_code": "005930", "trade_date": date(2024, 1, 3), "delta_tau": None},
    ]
    result = _rate_summary(rows, events)
    assert result["events_per_100_calendar_sessions"] == Decimal(100)
    assert result["events_per_100_tau"] == Decimal(50)


def test_c1_episodes_are_per_stock_and_require_complete_tau_duration() -> None:
    rows = [
        {
            "stock_code": "005930",
            "trade_date": date(2024, 1, 2),
            "delta_tau": Decimal(1),
        },
        {
            "stock_code": "005930",
            "trade_date": date(2024, 1, 3),
            "delta_tau": Decimal(2),
        },
        {"stock_code": "005930", "trade_date": date(2024, 1, 4), "delta_tau": None},
    ]
    compression = {
        ("005930", date(2024, 1, 2)): "C1",
        ("005930", date(2024, 1, 3)): "C1",
        ("005930", date(2024, 1, 4)): "C2",
    }
    assert _compression_episodes(rows, compression) == [
        {
            "stock_code": "005930",
            "start_date": date(2024, 1, 2),
            "end_date": date(2024, 1, 3),
            "calendar_duration": 2,
            "tau_duration": Decimal(3),
        }
    ]


def test_dispersion_has_no_statistical_significance_interpretation() -> None:
    result = _dispersion([Decimal(1), Decimal(3)])
    assert result["range"] == Decimal(2)
    assert result["coefficient_of_variation"] is not None


def test_cached_minute_presence_scan_is_read_only_and_date_based(tmp_path) -> None:
    path = tmp_path / "005930" / "raw" / "20240102"
    path.mkdir(parents=True)
    (path / "page-001-test.json").write_text(
        json.dumps({"rows": [{"cntr_tm": "20240102100000"}]}), encoding="utf-8"
    )
    assert _cached_minute_dates("005930", tmp_path) == {date(2024, 1, 2)}
