import csv
import json
import random
from decimal import Decimal

from src.kiwoom_daily.market_bar_compression_strategy_causal_signal_event_proof_v1_8 import (
    EVENTS_PATH,
    OUTPUT_PATH,
    PERCENTILE_METHOD,
    VARIANTS,
    _decorate_causal_features,
    _load_development_streams,
    _percentile,
    _simulate_variant,
    run_proof,
)


def test_percentile_contract_is_deterministic_type_7_equivalent():
    assert PERCENTILE_METHOD == "linear_interpolation_type_7_equivalent"
    assert _percentile(
        [Decimal(1), Decimal(2), Decimal(3), Decimal(4)], Decimal("0.25")
    ) == Decimal("1.75")


def test_causal_state_never_uses_current_width_in_reference():
    streams, _ = _load_development_streams()
    rows = streams["066570"]
    _decorate_causal_features(rows)
    first_available = next(
        index for index, row in enumerate(rows) if row["compression_available"]
    )
    assert first_available == 80
    assert rows[first_available]["past_q25"] == _percentile(
        [
            rows[index]["width"]
            for index in range(first_available - 60, first_available)
        ],
        Decimal("0.25"),
    )
    assert rows[79]["compression_available"] is False
    assert rows[79]["compressed"] is None


def test_recent_compression_unavailable_is_not_false():
    streams, _ = _load_development_streams()
    rows = streams["066570"]
    _decorate_causal_features(rows)
    assert rows[80]["recent_compressed"] is None
    assert rows[80]["h1_feature_available"] is False


def test_all_variants_have_next_bar_execution_contract():
    streams, _ = _load_development_streams()
    rows = streams["035420"]
    _decorate_causal_features(rows)
    for variant in VARIANTS:
        result = _simulate_variant("035420", rows, variant)
        by_id = {event["event_id"]: event for event in result["events"]}
        for event in result["events"]:
            if event["event_type"] != "SIGNAL" or not event["actionable"]:
                continue
            assert event["next_market_bar_index"] == event["market_bar_index"] + 1
            assert event["execution_open"] == rows[event["market_bar_index"]]["open"]
            assert event["execution_event_id"] in by_id
            execution = by_id[event["execution_event_id"]]
            assert execution["event_type"] == "EXECUTION"
            assert execution["market_bar_index"] > event["market_bar_index"]


def test_terminal_actionable_signal_is_not_filled_without_next_bar():
    streams, _ = _load_development_streams()
    rows = streams["035420"]
    _decorate_causal_features(rows)
    candidate = next(
        index
        for index, row in enumerate(rows)
        if index
        and rows[index - 1]["MMA20"] is not None
        and row["MMA20"] is not None
        and rows[index - 1]["close"] <= rows[index - 1]["MMA20"]
        and row["close"] > row["MMA20"]
    )
    truncated = [dict(row) for row in rows[: candidate + 1]]
    _decorate_causal_features(truncated)
    result = _simulate_variant("035420", truncated, "C0_RAW_MMA20")
    terminal = [
        event
        for event in result["events"]
        if event["event_type"] == "SIGNAL"
        and event["market_bar_index"] == candidate + 1
    ]
    assert terminal
    assert terminal[0]["execution_status"] == "NO_NEXT_MARKET_BAR"
    assert terminal[0]["execution_event_id"] is None
    assert result["counts"]["terminal_non_executable_signals"] == 1


def test_input_permutation_does_not_change_event_order():
    streams, _ = _load_development_streams()
    original = streams["105560"]
    shuffled = list(original)
    random.Random(7).shuffle(shuffled)
    shuffled.sort(key=lambda row: int(row["market_bar_index"]))
    _decorate_causal_features(original)
    _decorate_causal_features(shuffled)
    first = _simulate_variant("105560", original, "C0_RAW_MMA20")["events"]
    second = _simulate_variant("105560", shuffled, "C0_RAW_MMA20")["events"]
    assert [event["event_id"] for event in first] == [
        event["event_id"] for event in second
    ]
    assert [event["reason_code"] for event in first] == [
        event["reason_code"] for event in second
    ]


def test_v18_proof_has_no_future_outcome_fields(tmp_path):
    output = tmp_path / OUTPUT_PATH.name
    events = tmp_path / EVENTS_PATH.name
    report = run_proof(output_path=output, events_path=events)
    assert report["network_calls"] == 0
    assert report["invariants"]["causality_violations"] == 0
    assert report["invariants"]["execution_invariant_failures"] == 0
    assert report["backtest_readiness"]["overall"] == "V1_9_BACKTEST_READY"
    assert report["validation_registry"]["signal_calculated"] is False
    forbidden = {
        "future_return",
        "trade_return",
        "profit",
        "loss",
        "pnl",
        "win",
        "MFE",
        "MAE",
        "max_future_high",
        "min_future_low",
    }
    assert forbidden.isdisjoint(report["event_fields"])
    with events.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert forbidden.isdisjoint(rows[0])
    assert json.loads(output.read_text(encoding="utf-8"))["proof_version"].endswith(
        "V1_8"
    )
