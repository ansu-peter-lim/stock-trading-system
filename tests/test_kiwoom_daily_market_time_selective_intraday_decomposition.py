from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from src.backtest_engine.models import Ohlcv
from src.backtest_engine.validation import KOREA_TZ
from src.kiwoom_daily.market_time_selective_intraday_decomposition import (
    HORIZON,
    _activity_segments,
    _bucket_30m,
    _resolution_classification,
    _trailing_tau_mean,
)


def _row(label: str, open_: str, high: str, low: str, close: str) -> SimpleNamespace:
    parsed = datetime.strptime(label, "%Y%m%d%H%M%S").replace(tzinfo=KOREA_TZ)
    return SimpleNamespace(
        source_label=label,
        source_label_at=parsed,
        raw=Ohlcv(Decimal(open_), Decimal(high), Decimal(low), Decimal(close), 1),
    )


def test_activity_allocation_conserves_daily_tau_and_uses_session_open() -> None:
    rows = (
        _row("20240102090000", "100", "110", "100", "105"),
        _row("20240102090500", "105", "106", "104", "105"),
    )
    segments, gap, total = _activity_segments(
        delta_tau=Decimal(6),
        previous_adjusted_close=Decimal(90),
        adjusted_open=Decimal(100),
        minute_rows=rows,
    )
    assert abs(gap - Decimal(10) / Decimal(90)) < Decimal("1e-25")
    assert sum((row["tau"] for row in segments), Decimal(0)) == Decimal(6)
    assert segments[1]["signal_open"] == Decimal(100)
    assert total > gap


def test_30m_aggregation_is_chronological_and_conserves_tau() -> None:
    rows = (
        _row("20240102093500", "10", "12", "9", "11"),
        _row("20240102093000", "9", "10", "8", "10"),
        _row("20240102094500", "11", "13", "10", "12"),
    )
    segments, _, _ = _activity_segments(
        delta_tau=Decimal(6),
        previous_adjusted_close=Decimal(10),
        adjusted_open=Decimal(10),
        minute_rows=tuple(sorted(rows, key=lambda row: row.source_label)),
    )
    five = [row for row in segments if row["kind"] == "INTRADAY_5M"]
    buckets = _bucket_30m(five)
    assert [row["bucket"] for row in buckets] == ["09:30"]
    assert buckets[0]["first_label"] == "20240102093000"
    assert buckets[0]["last_label"] == "20240102094500"
    assert buckets[0]["tau"] == sum((row["tau"] for row in five), Decimal(0))


def test_trailing_mtma_uses_only_last_five_tau() -> None:
    segments = [
        {"tau": Decimal(2), "signal_close": Decimal(10)},
        {"tau": Decimal(3), "signal_close": Decimal(20)},
        {"tau": Decimal(4), "signal_close": Decimal(30)},
    ]
    # 5 units = 1 from the middle and 4 from the last segment.
    assert _trailing_tau_mean(segments, HORIZON) == Decimal(28)


def test_resolution_hierarchy_is_deterministic() -> None:
    assert _resolution_classification(
        [{"tau": Decimal(4)}], [{"tau": Decimal(4)}]
    ).startswith("A_")
    assert _resolution_classification(
        [{"tau": Decimal(5)}], [{"tau": Decimal(4)}]
    ).startswith("B_")
    assert _resolution_classification(
        [{"tau": Decimal(5)}], [{"tau": Decimal(5)}]
    ).startswith("C_")
