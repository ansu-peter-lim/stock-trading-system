from datetime import date
from decimal import Decimal

from src.kiwoom_daily.market_bar_zero_fetch_replication_materialization_v1_5 import (
    FROZEN_CANDIDATES,
    _consecutive_summary,
    _distribution,
    _regime_shares,
)


def test_frozen_candidates_are_exactly_the_zero_fetch_pair():
    assert set(FROZEN_CANDIDATES) == {"REPLICATION_B", "REPLICATION_C"}
    assert FROZEN_CANDIDATES["REPLICATION_B"]["stock_code"] == "035420"
    assert FROZEN_CANDIDATES["REPLICATION_C"]["stock_code"] == "105560"
    assert all(
        item["new_fetch_required_session_count"] == 0
        for item in FROZEN_CANDIDATES.values()
    )


def test_regime_shares_are_exact_and_pure_labels_are_not_thresholded():
    bar = {
        "provenance": [
            {
                "source_id": "035420:2025-12-15:DAILY",
                "source_tau_start": "0",
                "source_tau_end": "0.4",
            },
            {
                "source_id": "035420:2025-12-16:DAILY",
                "source_tau_start": "0.4",
                "source_tau_end": "1",
            },
        ]
    }
    shares, label = _regime_shares(
        bar,
        {date(2025, 12, 15): "SLOW", date(2025, 12, 16): "SLOW"},
    )
    assert sum(shares.values(), Decimal(0)) == Decimal(1)
    assert label == "PURE_SLOW"
    assert shares["SLOW"] == Decimal(1)


def test_mixed_source_regime_is_preserved_when_boundary_crosses_calendar_regimes():
    bar = {
        "provenance": [
            {
                "source_id": "035420:2025-12-15:DAILY",
                "source_tau_start": "0",
                "source_tau_end": "0.6",
            },
            {
                "source_id": "035420:2025-12-16:DAILY",
                "source_tau_start": "0.6",
                "source_tau_end": "1",
            },
        ]
    }
    shares, label = _regime_shares(
        bar,
        {date(2025, 12, 15): "SLOW", date(2025, 12, 16): "NORMAL_OTHER"},
    )
    assert sum(shares.values(), Decimal(0)) == Decimal(1)
    assert label == "MIXED_SOURCE_REGIME"
    assert shares["SLOW"] == Decimal("0.6")


def test_consecutive_summary_uses_market_bar_index_not_input_order():
    rows = [
        {"market_bar_index": 1, "primary_source_regime": "PURE_SLOW"},
        {"market_bar_index": 2, "primary_source_regime": "PURE_SLOW"},
        {"market_bar_index": 4, "primary_source_regime": "PURE_SLOW"},
    ]
    result = _consecutive_summary(rows, "PURE_SLOW")
    assert result["count"] == 3
    assert result["longest_consecutive_run"] == 2
    assert result["first_market_bar_index"] == 1
    assert result["last_market_bar_index"] == 4


def test_distribution_has_required_tau_percentiles():
    result = _distribution([Decimal("0.5"), Decimal(1), Decimal("1.5")])
    assert result["min"] == Decimal("0.5")
    assert result["median"] == Decimal(1)
    assert result["max"] == Decimal("1.5")
