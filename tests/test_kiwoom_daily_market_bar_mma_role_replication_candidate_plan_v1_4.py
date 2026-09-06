from datetime import date, timedelta
from decimal import Decimal

from src.kiwoom_daily.market_bar_mma_role_replication_candidate_plan_v1_4 import (
    _candidate_sort_key,
    _pure_capacity,
    _regime_capacity,
    _window_candidates,
    run_plan,
)


def _row(
    index: int,
    *,
    delta: str = "1",
    regime: str = "NORMAL_OTHER",
    tau: str | None = None,
) -> dict[str, object]:
    increment = Decimal(delta)
    end = Decimal(tau) if tau is not None else increment * index
    return {
        "stock_code": "005930",
        "trade_date": date(2025, 1, 2) + timedelta(days=index),
        "delta_tau": increment,
        "tau": end,
        "source_calendar_regime": regime,
    }


def test_pure_capacity_uses_integer_lattice_boundaries():
    assert _pure_capacity(Decimal(0), Decimal(2)) == 2
    assert _pure_capacity(Decimal("0.4"), Decimal("2.4")) == 1
    assert _pure_capacity(Decimal(2), Decimal("2.9")) == 0


def test_regime_capacity_preserves_absolute_tau_and_counts_runs():
    rows = [
        _row(1, delta="0.6", tau="10.6", regime="SLOW"),
        _row(2, delta="0.6", tau="11.2", regime="SLOW"),
        _row(3, delta="1.2", tau="12.4", regime="FAST_DIRECTIONAL_HIGH_EFF"),
    ]
    result = _regime_capacity(rows)
    assert result["total_tau"] == Decimal("2.4")
    assert result["session_count"]["SLOW"] == 2
    assert result["longest_run"]["SLOW"] == 2
    assert result["pure_capacity"]["SLOW"] == 1


def test_candidate_window_requires_target_and_exposes_regime_balance():
    rows = [
        _row(1, regime="SLOW", tau="1"),
        _row(2, regime="SLOW", tau="2"),
        _row(3, regime="FAST_DIRECTIONAL_HIGH_EFF", tau="3"),
        _row(4, regime="FAST_DIRECTIONAL_HIGH_EFF", tau="4"),
    ]
    candidates = _window_candidates(
        rows,
        target=3,
        cached_dates={
            rows[0]["trade_date"],
            rows[1]["trade_date"],
            rows[2]["trade_date"],
        },
        structural_dates=set(),
    )
    assert candidates
    candidate = candidates[-1]
    assert candidate["expected_market_bar_capacity"] >= 3
    assert candidate["balanced_regime_candidate"] is True
    assert candidate["new_fetch_required_session_count"] == 1


def test_candidate_sort_key_is_independent_of_input_permutation():
    first = {
        "structural_gap_count": 0,
        "expected_pure_slow_mb_capacity": 20,
        "fast_directional_present": True,
        "expected_market_bar_capacity": 160,
        "new_fetch_required_session_count": 2,
        "min_fast_slow_capacity": 10,
        "calendar_session_count": 100,
        "expected_pure_fast_directional_mb_capacity": 10,
        "source_quality_anomaly_count": 0,
        "stock_code": "005930",
        "calendar_start": date(2025, 1, 1),
        "calendar_end": date(2025, 5, 1),
        "candidate_target": 160,
    }
    second = {**first, "stock_code": "000660"}
    assert min(first, second, key=_candidate_sort_key)["stock_code"] == "000660"


def test_full_offline_plan_has_no_network_or_market_bar_materialization(tmp_path):
    report = run_plan(output_path=tmp_path / "plan.json", stocks=("066570",))
    assert report["network_calls"] == 0
    assert report["scope"]["market_bar_materialization"] is False
    assert report["scope"]["strategy"] is False
    assert report["population"]["candidate_window_count_160"] > 0
    assert (tmp_path / "plan.json").exists()
