from decimal import Decimal

from src.kiwoom_daily.market_bar_stream_integrity_audit import audit_proof


def _proof():
    return {
        "population": {
            "resolvable_source_tau": "3.5",
            "unresolved_fast_tau": "2.25",
            "market_bar_count": 3,
        },
        "runs": [
            {
                "stock_code": "000001",
                "run_index": 1,
                "source_segment_count": 1,
                "market_bar_count": 1,
                "source_tau": "1.25",
                "materialized_tau": "1",
                "unmaterialized_tail_tau": "0.25",
            },
            {
                "stock_code": "000001",
                "run_index": 2,
                "source_segment_count": 1,
                "market_bar_count": 1,
                "source_tau": "2.25",
                "materialized_tau": "2",
                "unmaterialized_tail_tau": "0.25",
            },
        ],
        "unresolved_sessions": [
            {
                "stock_code": "000001",
                "trade_date": "2024-01-02",
                "delta_tau": "2.25",
                "reason": "INSUFFICIENT_SOURCE_RESOLUTION",
            }
        ],
        "market_bars": [
            {
                "market_bar_id": "a",
                "open": "10",
                "high": "12",
                "low": "9",
                "close": "11",
                "source_segments": [
                    {
                        "source_id": "000001:2024-01-01:DAILY",
                        "source_resolution": "DAILY_SIGNAL_ADJUSTED",
                        "overlap_fraction": "1",
                        "boundary_split": False,
                        "open": "10",
                        "high": "12",
                        "low": "9",
                        "close": "11",
                    }
                ],
            },
            {
                "market_bar_id": "b",
                "open": "11",
                "high": "13",
                "low": "10",
                "close": "12",
                "source_segments": [
                    {
                        "source_id": "000001:2024-01-03:5M",
                        "source_resolution": "5M_RAW_ACTIVITY_SIGNAL_ANCHORED",
                        "overlap_fraction": "0.5",
                        "boundary_split": True,
                        "open": "11",
                        "high": "13",
                        "low": "10",
                        "close": "12",
                    }
                ],
            },
        ],
    }


def test_run_tau_invariant_and_tail_fragmentation():
    report = audit_proof(_proof())
    assert report["run_invariant"]["all_hold"]
    assert Decimal(report["tail_analysis"]["total_tail_tau"]) == Decimal("0.5")
    assert report["tail_analysis"]["run_reset_fragmented_residual"]


def test_boundary_and_volume_quality_are_explicit():
    report = audit_proof(_proof())
    assert report["boundary_quality"]["counts"]["EXACT_SOURCE_BOUNDARY"] == 1
    assert report["boundary_quality"]["counts"]["FRACTIONAL_5M_SEGMENT"] == 1
    assert report["volume_quality"]["PRORATED_ESTIMATE"] == 1
    assert report["ohlc_provenance"]["fractional_boundary_price_unknown_count"] == 1


def test_theoretical_count_does_not_materialize_prices():
    report = audit_proof(_proof())
    assert report["theoretical_full_stream"]["one_tau_market_bar_count_floor"] == 5
    assert report["theoretical_full_stream"]["price_path_materialized"] is False
