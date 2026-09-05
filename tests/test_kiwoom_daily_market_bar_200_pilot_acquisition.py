import json
from datetime import date

from src.kiwoom_daily.market_bar_200_pilot_acquisition import (
    OUTPUT_PATH,
    V11_PLAN_PATH,
    _coverage_rows,
    _load_plan,
    run_pilot,
)
from src.kiwoom_daily.market_bar_continuous_pilot_extension_plan import (
    ANCHOR_END,
)


def test_v11_plan_freezes_the_preferred_200_window():
    plan = _load_plan(V11_PLAN_PATH)
    candidate = plan["preferred_candidate_200"]
    assert candidate["stock_code"] == "066570"
    assert candidate["calendar_start"] == "2026-02-02"
    assert candidate["calendar_end"] == "2026-05-28"
    assert candidate["target_market_bars"] == 200
    assert candidate["anchor_included"] is True
    assert ANCHOR_END.isoformat() <= candidate["calendar_end"]


def test_coverage_distinguishes_cached_network_missing_and_quality():
    rows, anomalies = _coverage_rows(
        ["2026-02-02", "2026-02-03", "2026-02-04"],
        cached_rows={date(2026, 2, 2): (object(),)},
        fetched_rows={date(2026, 2, 3): (object(),)},
    )
    assert anomalies == 0
    assert rows[0]["source"] == "cached"
    assert rows[1]["source"] == "network"
    assert rows[2]["source"] == "missing"


def test_default_run_is_network_free_and_preserves_plan(tmp_path):
    output = tmp_path / OUTPUT_PATH.name
    report = run_pilot(output_path=output, allow_network=False)
    assert report["contract"]["strategy"] is False
    assert report["acquisition"]["actual_api_request_count"] == 0
    assert report["plan_freeze"]["exact_fetch_dates_reused"] is True
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["acquisition"]["api_id"] == "ka10080"
