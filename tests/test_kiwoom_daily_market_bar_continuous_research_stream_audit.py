from src.kiwoom_daily.market_bar_continuous_research_stream_audit import audit_proof


def test_readiness_is_bar_count_only_and_no_gap_bridge():
    proof = {
        "market_bars": [],
        "unresolved_sessions": [
            {"stock_code": "000001", "trade_date": "2024-01-03", "delta_tau": "2.2"}
        ],
    }
    report = audit_proof(proof)
    assert report["contract"]["gap_bridging"] is False
    assert report["contract"]["market_ma_calculation"] is False
    assert report["population"]["resolved_island_count"] == 0
