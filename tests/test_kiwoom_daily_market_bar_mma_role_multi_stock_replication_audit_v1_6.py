from decimal import Decimal

from src.kiwoom_daily.market_bar_mma_role_multi_stock_replication_audit_v1_6 import (
    BASELINE_CROSS_REFERENCE,
    EXPECTED_MARKET_BARS,
    REPLICATION_STOCKS,
    _compression_pattern,
    _fast_slow_comparison,
    _tvd_result,
)


def _role_group(probabilities, count):
    return {
        "nearest_role": {
            "count": count,
            "probabilities": probabilities,
        }
    }


def test_v16_stream_freeze_contains_only_two_replications_and_baseline_counts():
    assert REPLICATION_STOCKS == ("035420", "105560")
    assert EXPECTED_MARKET_BARS == {"066570": 201, "035420": 200, "105560": 200}


def test_tvd_uses_v13_half_l1_role_distance():
    first = _role_group(
        {
            "MMA5": Decimal("0.5"),
            "MMA10": Decimal("0.5"),
            "MMA20": Decimal(0),
            "MMA60": Decimal(0),
        },
        2,
    )
    second = _role_group(
        {
            "MMA5": Decimal(0),
            "MMA10": Decimal(0),
            "MMA20": Decimal("0.5"),
            "MMA60": Decimal("0.5"),
        },
        2,
    )
    result = _tvd_result(first, second)
    assert result["status"] == "AVAILABLE"
    assert result["value"] == Decimal(1)


def test_tvd_is_not_available_when_one_regime_has_no_primary_pivots():
    empty = _role_group({"MMA5": None, "MMA10": None, "MMA20": None, "MMA60": None}, 0)
    populated = _role_group(
        {
            "MMA5": Decimal(1),
            "MMA10": Decimal(0),
            "MMA20": Decimal(0),
            "MMA60": Decimal(0),
        },
        1,
    )
    assert _tvd_result(populated, empty)["status"] == "NOT_AVAILABLE"


def test_fast_slow_comparison_is_deterministic():
    probabilities = {
        "MMA5": Decimal("0.5"),
        "MMA10": Decimal("0.5"),
        "MMA20": Decimal(0),
        "MMA60": Decimal(0),
    }
    market = {
        "FAST_DIRECTIONAL_HIGH_EFF": _role_group(probabilities, 2),
        "SLOW": _role_group(probabilities, 2),
    }
    calendar = {
        "FAST_DIRECTIONAL_HIGH_EFF": _role_group(probabilities, 2),
        "SLOW": _role_group(probabilities, 2),
    }
    assert _fast_slow_comparison(market, calendar)["comparison"] == "EQUAL"


def test_compression_pattern_requires_all_four_nonempty_quartiles():
    report = {
        "periods": {
            "MMA10": {
                "by_compression_quartile": {
                    "C1": {
                        "market_bar_count": 1,
                        "events_per_100_market_bars": Decimal(20),
                    },
                    "C2": {
                        "market_bar_count": 1,
                        "events_per_100_market_bars": Decimal(15),
                    },
                    "C3": {
                        "market_bar_count": 1,
                        "events_per_100_market_bars": Decimal(5),
                    },
                    "C4": {
                        "market_bar_count": 1,
                        "events_per_100_market_bars": Decimal(1),
                    },
                }
            }
        }
    }
    assert _compression_pattern(report, 10) is True
    report["periods"]["MMA10"]["by_compression_quartile"]["C4"]["market_bar_count"] = 0
    assert _compression_pattern(report, 10) is None


def test_frozen_baseline_reference_is_rounded_to_one_decimal():
    assert BASELINE_CROSS_REFERENCE["MMA10"]["C1"] == Decimal(25)
    assert BASELINE_CROSS_REFERENCE["MMA20"]["C4"] == Decimal(0)
