import json
import random
from decimal import Decimal

import src.kiwoom_daily.market_bar_compression_strategy_zero_cost_dev_proof_v1_9 as proof


def _streams():
    streams, _ = proof._load_development_streams()
    for rows in streams.values():
        proof._decorate_causal_features(rows)
    return streams


def test_v18_signal_and_execution_regression_passes():
    streams = _streams()
    regression = proof._v18_regression(streams)
    assert regression["status"] == "PASS"
    assert regression["mismatches"] == []


def test_common_window_starts_flat_without_pre_window_carry_in():
    streams = _streams()
    rows = streams["066570"]
    common = proof._common_window(rows)
    result = proof._simulate_common("066570", rows, "C0_RAW_MMA20", common)
    assert result["common_window"]["first_common_eligible_MB"] == 86
    assert all(int(event["market_bar_index"]) >= 86 for event in result["executions"])
    assert result["equity_rows"][0]["position"] == "FLAT"


def test_trade_arithmetic_and_zero_cost_contract(tmp_path):
    report = proof.run_proof(
        output_path=tmp_path / "proof.json",
        trade_ledger_path=tmp_path / "trades.csv",
        equity_path=tmp_path / "equity.csv",
        create_charts=False,
    )
    for stock in proof.STREAMS:
        for variant in proof.VARIANTS:
            item = report["stocks"][stock][variant]
            assert item["metrics"]["completed_trades"] == len(
                [
                    row
                    for row in proof._simulate_common(
                        stock,
                        _streams()[stock],
                        variant,
                        proof._common_window(_streams()[stock]),
                    )["trades"]
                ]
            )
    assert report["scope"]["transaction_costs"] == {
        "commission": Decimal(0),
        "tax": Decimal(0),
        "slippage": Decimal(0),
    }
    assert (
        report["accounting_contract"]["position_sizing"]
        == "FULL_NOTIONAL_LONG_FRACTIONAL_QUANTITY"
    )


def test_trade_return_formula_for_all_completed_trades():
    streams = _streams()
    for stock in proof.STREAMS:
        rows = streams[stock]
        common = proof._common_window(rows)
        for variant in proof.VARIANTS:
            for trade in proof._simulate_common(stock, rows, variant, common)["trades"]:
                expected = (
                    Decimal(trade["exit_price"]) / Decimal(trade["entry_price"]) - 1
                )
                assert trade["trade_return"] == expected


def test_open_at_end_is_not_forced_closed():
    streams = _streams()
    result = proof._simulate_common(
        "105560",
        streams["105560"],
        "C0_RAW_MMA20",
        proof._common_window(streams["105560"]),
    )
    assert result["open_position"] is not None
    assert result["open_position"]["status"] == "OPEN_AT_RESEARCH_END"
    assert result["terminal_position"] == "LONG"
    assert not any(
        int(event["market_bar_index"]) == 200 and event["signal_type"] == "SELL"
        for event in result["executions"]
    )


def test_mark_to_market_and_mdd_are_deterministic():
    values = [
        Decimal(1),
        Decimal("1.2"),
        Decimal("1.1"),
        Decimal("0.9"),
        Decimal("1.0"),
    ]
    assert proof._mdd(values) == Decimal("0.25") * Decimal(-1)
    assert proof._mdd(values) == proof._mdd(list(values))


def test_input_order_does_not_change_result():
    streams = _streams()
    rows = streams["035420"]
    shuffled = list(rows)
    random.Random(19).shuffle(shuffled)
    first = proof._simulate_common(
        "035420", rows, "C1_FILTER_COMPRESSED", proof._common_window(rows)
    )
    second = proof._simulate_common(
        "035420", shuffled, "C1_FILTER_COMPRESSED", proof._common_window(rows)
    )
    assert [trade["trade_id"] for trade in first["trades"]] == [
        trade["trade_id"] for trade in second["trades"]
    ]
    assert first["metrics"] == second["metrics"]
    assert first["equity_curve"] == second["equity_curve"]


def test_stock_and_variant_runs_are_independent():
    streams = _streams()
    before = proof._simulate_common(
        "066570",
        streams["066570"],
        "C0_RAW_MMA20",
        proof._common_window(streams["066570"]),
    )
    altered = _streams()
    for row in altered["105560"]:
        row["close"] += Decimal(100000)
    after = proof._simulate_common(
        "066570",
        altered["066570"],
        "C0_RAW_MMA20",
        proof._common_window(altered["066570"]),
    )
    assert before["metrics"] == after["metrics"]


def test_h1_sparse_and_validation_scope(tmp_path):
    report = proof.run_proof(
        output_path=tmp_path / "proof.json",
        trade_ledger_path=tmp_path / "trades.csv",
        equity_path=tmp_path / "equity.csv",
        create_charts=False,
    )
    assert report["h1_sparse"]["completed_cycles"] == 1
    assert report["h1_sparse"]["status"] == "SAMPLE_SPARSE"
    assert report["scope"]["validation_accessed"] is False
    assert report["scope"]["validation_stocks"] == []
    assert "005380" not in json.dumps(report["stocks"], default=str)
    assert "068270" not in json.dumps(report["stocks"], default=str)


def test_no_strategy_parameter_mutation_and_v18_contract():
    assert proof.NORMALIZED_CAPITAL == Decimal("1.0")
    assert proof.COMMON_EXPECTED == {"066570": 116, "035420": 115, "105560": 115}
    assert proof.VARIANTS == (
        "C0_RAW_MMA20",
        "C1_FILTER_COMPRESSED",
        "H1_COMPRESSION_RELEASE",
    )
