"""Outcome-free V0.3 Daily MA BUY visual review.

V0.3 isolates a one-percent close/MA crossing contract, a bounded ten-session
BUY-B episode, and locally reproducible market-cap review sampling.  It reads
only existing ka10081 and KRX artifacts and never evaluates execution or
future performance.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from src.backtest_engine.models import DailyBar
from src.backtest_engine.trading_calendar import ExplicitTradingCalendar
from src.strategy_review.chart import (
    ChartType,
    ReviewEvent,
    ReviewEventType,
    prepare_review_chart,
    render_review_chart,
)

from .daily_ma.core import confirmed_breakdown, confirmed_breakout
from .daily_ma.data import DEFAULT_RAW_ROOT, load_local_daily_bars, slice_daily_bars
from .daily_ma_unified_buy_visual_proof_v0_2 import (
    DailyMaPoint,
    calculate_daily_ma_points,
)
from .daily_ma_unified_buy_visual_proof_v0_2 import (
    accepted_signals as accepted_v0_2_signals,
)
from .daily_ma_unified_buy_visual_proof_v0_2 import (
    detect_buy_candidates as detect_v0_2_candidates,
)

PROOF_VERSION = "DAILY_MA_UNIFIED_BUY_VISUAL_PROOF_V0_3"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/daily_ma_unified_buy_visual_proof_v0_3.json"
)
REGISTRY_PATH = Path(
    "data/processed/strategy_review/daily_ma_unified_buy_signal_registry_v0_3.csv"
)
COMPARISON_PATH = Path(
    "data/processed/strategy_review/daily_ma_unified_buy_v0_2_v0_3_same11_comparison.json"
)
CHART_ROOT = Path(
    "data/processed/strategy_charts/daily_ma_unified_buy_visual_proof_v0_3"
)
MARKET_CAP_PATHS = (
    Path("data/raw/krx/daily_trade/kospi/2026/08/28.json"),
    Path("data/raw/krx/daily_trade/kosdaq/2026/08/28.json"),
)
MARKET_CAP_SOURCE = "KRX_OPENAPI_DAILY_TRADE_LOCAL_ARTIFACT"
MARKET_CAP_AS_OF = date(2026, 8, 28)
RESEARCH_START = date(2023, 9, 1)
RESEARCH_END = date(2026, 8, 28)
LOCAL_DATA_START = date(2023, 6, 1)
LOCAL_ARTIFACT_BASE_DATE = date(2026, 8, 31)
LEGACY_STOCK_CODES = (
    "000660",
    "005380",
    "005930",
    "012450",
    "034020",
    "035420",
    "035720",
    "066570",
    "068270",
    "105560",
    "207940",
)
CROSS_UP_RATIO = Decimal("1.01")
CROSS_DOWN_RATIO = Decimal("0.99")
CROSS_THRESHOLD_PCT = Decimal(1)
SLOPE_RESTRICTION_PCT = Decimal(-20)
BUY_A_MIN_BELOW_RUN = 10
BUY_C_MIN_BELOW_RUN = 15
BUY_B_EPISODE_BARS = 10
MAX_PRIMARY_PER_BUCKET = 6
MAX_SAMPLE_WINDOWS = 9
MAX_SAMPLES_PER_TYPE = 2
FULL_CONTEXT_PRE_SESSIONS = 200
FULL_CONTEXT_POST_SESSIONS = 100
SIGNAL_ZOOM_PRE_SESSIONS = 30
SIGNAL_ZOOM_POST_SESSIONS = 30
FORBIDDEN_OUTCOME_FIELDS = frozenset(
    {
        "future_return",
        "next_5_day_return",
        "next_10_day_return",
        "next_20_day_return",
        "win",
        "loss",
        "profit",
        "pnl",
        "mfe",
        "mae",
        "future_max_price",
        "future_drawdown",
        "future_ma_outcome",
        "success_score",
    }
)


class BuyType(str, Enum):
    A = "BUY_A_LONG_RECOVERY_10"
    B = "BUY_B_A_FAILURE_TEN_BAR_RECLAIM"
    C = "BUY_C_LONG_RECOVERY_20"


class SizeBucket(str, Enum):
    LARGE = "LARGE"
    MID = "MID"
    SMALL = "SMALL"


@dataclass(frozen=True, slots=True)
class MarketCapRecord:
    stock_code: str
    market_cap: int
    market_cap_as_of: date
    source: str = MARKET_CAP_SOURCE


@dataclass(frozen=True, slots=True)
class BucketedStock:
    stock_code: str
    market_cap: int
    bucket: SizeBucket


@dataclass(frozen=True, slots=True)
class BuyCandidate:
    signal_id: str
    stock_code: str
    signal_date: date
    buy_type: BuyType
    close: Decimal
    ma5: Decimal | None
    ma10: Decimal | None
    ma20: Decimal | None
    ma60: Decimal | None
    ma120: Decimal | None
    ma10_slope10_pct: Decimal | None
    ma20_slope10_pct: Decimal | None
    ma60_slope10_pct: Decimal | None
    below_ma10_run: int
    below_ma20_run: int
    ma5_inflection_date: date | None
    ma10_inflection_date: date | None
    breakout_ma: int
    breakdown_date: date | None
    bars_since_parent_a: int | None
    parent_buy_a_signal_id: str | None
    parent_buy_a_date: date | None
    other_qualifying_parent_ids: tuple[str, ...]
    slope_guard_pass: bool
    cross_threshold_pct: Decimal
    ma10_breakout_pct: Decimal | None
    ma20_breakout_pct: Decimal | None
    ma10_breakdown_pct: Decimal | None
    distance_to_ma60_pct: Decimal | None
    distance_to_ma120_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class _RecoverySetup:
    inflection_index: int
    below_run: int


@dataclass(frozen=True, slots=True)
class _BuyBEpisode:
    parent: BuyCandidate
    parent_index: int
    breakdown_index: int | None = None


def load_local_market_caps(
    paths: Sequence[Path] = MARKET_CAP_PATHS,
) -> tuple[MarketCapRecord, ...]:
    """Read official local KRX rows without network or inferred values."""

    records: dict[str, MarketCapRecord] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("OutBlock_1")
        if not isinstance(rows, list):
            raise TypeError(f"invalid KRX daily-trade artifact: {path.as_posix()}")
        for row in rows:
            stock_code = str(row.get("ISU_CD", ""))
            base_date = str(row.get("BAS_DD", ""))
            market_cap_text = str(row.get("MKTCAP", ""))
            if not re.fullmatch(r"[0-9]{6}", stock_code, flags=re.ASCII):
                continue
            if base_date != MARKET_CAP_AS_OF.strftime("%Y%m%d"):
                raise ValueError(
                    "KRX market-cap artifact date is not the configured as-of"
                )
            if not re.fullmatch(r"[0-9]+", market_cap_text, flags=re.ASCII):
                continue
            record = MarketCapRecord(
                stock_code=stock_code,
                market_cap=int(market_cap_text),
                market_cap_as_of=MARKET_CAP_AS_OF,
            )
            if stock_code in records and records[stock_code] != record:
                raise ValueError("conflicting local KRX market-cap rows")
            records[stock_code] = record
    return tuple(records[code] for code in sorted(records))


def assign_market_cap_terciles(
    records: Sequence[MarketCapRecord],
) -> tuple[BucketedStock, ...]:
    """Assign equal-count review buckets after descending market-cap sort."""

    canonical = tuple(
        sorted(records, key=lambda row: (-row.market_cap, row.stock_code))
    )
    if len({row.stock_code for row in canonical}) != len(canonical):
        raise ValueError("duplicate market-cap stock code")
    buckets = (SizeBucket.LARGE, SizeBucket.MID, SizeBucket.SMALL)
    total = len(canonical)
    if total == 0:
        return ()
    return tuple(
        BucketedStock(
            row.stock_code,
            row.market_cap,
            buckets[min(2, index * 3 // total)],
        )
        for index, row in enumerate(canonical)
    )


def _evenly_select(
    rows: Sequence[BucketedStock], limit: int
) -> tuple[BucketedStock, ...]:
    canonical = tuple(sorted(rows, key=lambda row: (-row.market_cap, row.stock_code)))
    if len(canonical) <= limit:
        return canonical
    if limit == 1:
        return canonical[:1]
    indexes = tuple(
        index * (len(canonical) - 1) // (limit - 1) for index in range(limit)
    )
    return tuple(canonical[index] for index in indexes)


def select_balanced_review_universe(
    records: Sequence[MarketCapRecord],
    *,
    per_bucket: int = MAX_PRIMARY_PER_BUCKET,
) -> tuple[BucketedStock, ...]:
    if per_bucket <= 0:
        raise ValueError("per_bucket must be positive")
    bucketed = assign_market_cap_terciles(records)
    return tuple(
        stock
        for bucket in SizeBucket
        for stock in _evenly_select(
            tuple(row for row in bucketed if row.bucket is bucket), per_bucket
        )
    )


def _distance_pct(close: Decimal, ma: Decimal | None) -> Decimal | None:
    return None if ma is None or ma == 0 else (close / ma - 1) * 100


def _is_breakout(points: Sequence[DailyMaPoint], index: int, field: str) -> bool:
    if index == 0:
        return False
    return confirmed_breakout(
        points[index].close,
        getattr(points[index], field),
        points[index - 1].close,
        getattr(points[index - 1], field),
        ratio=CROSS_UP_RATIO,
    )


def _is_breakdown(points: Sequence[DailyMaPoint], index: int) -> bool:
    if index == 0:
        return False
    return confirmed_breakdown(
        points[index].close,
        points[index].ma10,
        points[index - 1].close,
        points[index - 1].ma10,
        ratio=CROSS_DOWN_RATIO,
    )


def _signal_id(
    buy_type: BuyType, stock_code: str, signal_date: date, parent_id: str | None = None
) -> str:
    suffix = "" if parent_id is None else f":{parent_id}"
    return f"V0.3:{buy_type.value}:{stock_code}:{signal_date.isoformat()}{suffix}"


def _candidate(
    stock_code: str,
    point: DailyMaPoint,
    buy_type: BuyType,
    *,
    below_ma10_run: int,
    below_ma20_run: int,
    ma5_inflection_date: date | None = None,
    ma10_inflection_date: date | None = None,
    breakdown_point: DailyMaPoint | None = None,
    bars_since_parent_a: int | None = None,
    parent: BuyCandidate | None = None,
    other_parent_ids: tuple[str, ...] = (),
) -> BuyCandidate:
    parent_id = None if parent is None else parent.signal_id
    guard_pass = (
        point.ma10_slope10_pct is not None
        and point.ma10_slope10_pct >= SLOPE_RESTRICTION_PCT
    )
    return BuyCandidate(
        signal_id=_signal_id(buy_type, stock_code, point.trade_date, parent_id),
        stock_code=stock_code,
        signal_date=point.trade_date,
        buy_type=buy_type,
        close=point.close,
        ma5=point.ma5,
        ma10=point.ma10,
        ma20=point.ma20,
        ma60=point.ma60,
        ma120=point.ma120,
        ma10_slope10_pct=point.ma10_slope10_pct,
        ma20_slope10_pct=point.ma20_slope10_pct,
        ma60_slope10_pct=point.ma60_slope10_pct,
        below_ma10_run=below_ma10_run,
        below_ma20_run=below_ma20_run,
        ma5_inflection_date=ma5_inflection_date,
        ma10_inflection_date=ma10_inflection_date,
        breakout_ma=20 if buy_type is BuyType.C else 10,
        breakdown_date=(
            None if breakdown_point is None else breakdown_point.trade_date
        ),
        bars_since_parent_a=bars_since_parent_a,
        parent_buy_a_signal_id=parent_id,
        parent_buy_a_date=None if parent is None else parent.signal_date,
        other_qualifying_parent_ids=other_parent_ids,
        slope_guard_pass=guard_pass,
        cross_threshold_pct=CROSS_THRESHOLD_PCT,
        ma10_breakout_pct=_distance_pct(point.close, point.ma10),
        ma20_breakout_pct=_distance_pct(point.close, point.ma20),
        ma10_breakdown_pct=(
            None
            if breakdown_point is None
            else _distance_pct(breakdown_point.close, breakdown_point.ma10)
        ),
        distance_to_ma60_pct=_distance_pct(point.close, point.ma60),
        distance_to_ma120_pct=_distance_pct(point.close, point.ma120),
    )


def detect_buy_candidates(
    stock_code: str, points: Sequence[DailyMaPoint]
) -> tuple[BuyCandidate, ...]:
    """Detect causal V0.3 candidates while retaining guard rejections."""

    canonical = tuple(sorted(points, key=lambda point: point.trade_date))
    if len({point.trade_date for point in canonical}) != len(canonical):
        raise ValueError("Daily MA points must not contain duplicate dates")
    output: list[BuyCandidate] = []
    a_setup: _RecoverySetup | None = None
    c_setup: _RecoverySetup | None = None
    b_episodes: list[_BuyBEpisode] = []
    for index, point in enumerate(canonical):
        previous = canonical[index - 1] if index else None
        breakout10 = _is_breakout(canonical, index, "ma10")
        breakout20 = _is_breakout(canonical, index, "ma20")
        breakdown10 = _is_breakdown(canonical, index)

        retained_episodes: list[_BuyBEpisode] = []
        qualifying_episodes: list[_BuyBEpisode] = []
        for episode in b_episodes:
            elapsed = index - episode.parent_index
            if elapsed > BUY_B_EPISODE_BARS:
                continue
            if episode.breakdown_index is None:
                retained_episodes.append(
                    _BuyBEpisode(episode.parent, episode.parent_index, index)
                    if elapsed >= 1 and breakdown10
                    else episode
                )
            elif index > episode.breakdown_index and breakout10:
                qualifying_episodes.append(episode)
            else:
                retained_episodes.append(episode)
        if qualifying_episodes:
            canonical_episode = max(
                qualifying_episodes,
                key=lambda episode: (
                    episode.parent_index,
                    episode.parent.signal_id,
                ),
            )
            other_parent_ids = tuple(
                sorted(
                    episode.parent.signal_id
                    for episode in qualifying_episodes
                    if episode is not canonical_episode
                )
            )
            breakdown_index = canonical_episode.breakdown_index
            if breakdown_index is None:
                raise AssertionError("qualifying BUY-B episode has no breakdown")
            output.append(
                _candidate(
                    stock_code,
                    point,
                    BuyType.B,
                    below_ma10_run=(
                        previous.ma10_below_run if previous is not None else 0
                    ),
                    below_ma20_run=(
                        previous.ma20_below_run if previous is not None else 0
                    ),
                    breakdown_point=canonical[breakdown_index],
                    bars_since_parent_a=index - canonical_episode.parent_index,
                    parent=canonical_episode.parent,
                    other_parent_ids=other_parent_ids,
                )
            )
        b_episodes = retained_episodes

        prior_run10 = previous.ma10_below_run if previous is not None else 0
        prior_run20 = previous.ma20_below_run if previous is not None else 0
        if (
            a_setup is None
            and point.ma5_inflection_event
            and max(point.ma10_below_run, prior_run10) >= BUY_A_MIN_BELOW_RUN
        ):
            a_setup = _RecoverySetup(index, max(point.ma10_below_run, prior_run10))
        if (
            c_setup is None
            and point.ma10_inflection_event
            and max(point.ma20_below_run, prior_run20) >= BUY_C_MIN_BELOW_RUN
        ):
            c_setup = _RecoverySetup(index, max(point.ma20_below_run, prior_run20))

        if breakout10 and a_setup is not None:
            candidate = _candidate(
                stock_code,
                point,
                BuyType.A,
                below_ma10_run=a_setup.below_run,
                below_ma20_run=prior_run20,
                ma5_inflection_date=canonical[a_setup.inflection_index].trade_date,
            )
            output.append(candidate)
            if candidate.slope_guard_pass:
                b_episodes.append(_BuyBEpisode(candidate, index))
            a_setup = None
        if breakout20 and c_setup is not None:
            output.append(
                _candidate(
                    stock_code,
                    point,
                    BuyType.C,
                    below_ma10_run=prior_run10,
                    below_ma20_run=c_setup.below_run,
                    ma10_inflection_date=canonical[c_setup.inflection_index].trade_date,
                )
            )
            c_setup = None
    return tuple(
        sorted(
            output, key=lambda item: (item.stock_code, item.signal_date, item.signal_id)
        )
    )


def accepted_signals(candidates: Iterable[BuyCandidate]) -> tuple[BuyCandidate, ...]:
    return tuple(candidate for candidate in candidates if candidate.slope_guard_pass)


def _counts(signals: Sequence[Any]) -> dict[str, int]:
    return {
        buy_type.name: sum(signal.buy_type.name == buy_type.name for signal in signals)
        for buy_type in BuyType
    }


def _overlap_counts(signals: Sequence[Any]) -> dict[str, int]:
    groups: dict[tuple[str, date], list[str]] = defaultdict(list)
    for signal in signals:
        groups[(signal.stock_code, signal.signal_date)].append(signal.buy_type.value)
    return dict(
        sorted(
            Counter(
                "+".join(sorted(types)) for types in groups.values() if len(types) > 1
            ).items()
        )
    )


def _candidate_groups(
    signals: Sequence[BuyCandidate],
) -> tuple[tuple[str, date, tuple[BuyCandidate, ...]], ...]:
    groups: dict[tuple[str, date], list[BuyCandidate]] = defaultdict(list)
    for signal in signals:
        groups[(signal.stock_code, signal.signal_date)].append(signal)
    return tuple(
        (code, day, tuple(sorted(group, key=lambda item: item.signal_id)))
        for (code, day), group in sorted(
            groups.items(), key=lambda item: (item[0][1], item[0][0])
        )
    )


def select_sample_windows(
    signals: Sequence[BuyCandidate],
    buckets: Mapping[str, SizeBucket],
) -> tuple[tuple[str, date, tuple[BuyCandidate, ...]], ...]:
    """Select outcome-free structural coverage in a deterministic order."""

    groups = _candidate_groups(signals)
    selected: list[tuple[str, date, tuple[BuyCandidate, ...]]] = []

    def add_first(predicate: Any) -> None:
        if len(selected) >= MAX_SAMPLE_WINDOWS:
            return
        existing = {(code, day) for code, day, _ in selected}
        match = next(
            (
                group
                for group in groups
                if (group[0], group[1]) not in existing and predicate(group)
            ),
            None,
        )
        if match is not None:
            selected.append(match)

    add_first(lambda group: {item.buy_type for item in group[2]} == {BuyType.A})
    add_first(lambda group: {item.buy_type for item in group[2]} == {BuyType.C})
    add_first(lambda group: any(item.buy_type is BuyType.B for item in group[2]))
    for buy_type in BuyType:
        while (
            sum(
                any(item.buy_type is buy_type for item in group[2])
                for group in selected
            )
            < MAX_SAMPLES_PER_TYPE
            and len(selected) < MAX_SAMPLE_WINDOWS
        ):
            before = len(selected)
            add_first(
                lambda group, target=buy_type: any(
                    item.buy_type is target for item in group[2]
                )
            )
            if len(selected) == before:
                break
    for bucket in SizeBucket:
        if not any(buckets.get(code) is bucket for code, _, _ in selected):
            add_first(lambda group, target=bucket: buckets.get(group[0]) is target)
    return tuple(sorted(selected, key=lambda item: (item[1], item[0])))


def _events_for_window(
    signals: Sequence[BuyCandidate], points: Mapping[date, DailyMaPoint]
) -> tuple[ReviewEvent, ...]:
    events: list[ReviewEvent] = []
    for signal in signals:
        if signal.ma5_inflection_date is not None:
            inflection = points[signal.ma5_inflection_date]
            events.append(
                ReviewEvent(
                    ReviewEventType.MA_INFLECTION,
                    signal.ma5_inflection_date,
                    "MA5 INFLECT",
                    adjusted_plot_price=inflection.ma5,
                )
            )
        if signal.ma10_inflection_date is not None:
            inflection = points[signal.ma10_inflection_date]
            events.append(
                ReviewEvent(
                    ReviewEventType.MA_INFLECTION,
                    signal.ma10_inflection_date,
                    "MA10 INFLECT",
                    adjusted_plot_price=inflection.ma10,
                )
            )
        if signal.parent_buy_a_date is not None:
            parent = points[signal.parent_buy_a_date]
            events.append(
                ReviewEvent(
                    ReviewEventType.DAILY_MA_BUY_SIGNAL,
                    signal.parent_buy_a_date,
                    "BUY A PARENT",
                    adjusted_plot_price=parent.close,
                )
            )
        if signal.breakdown_date is not None:
            breakdown = points[signal.breakdown_date]
            events.append(
                ReviewEvent(
                    ReviewEventType.MA_BREAKDOWN,
                    signal.breakdown_date,
                    "MA10 -1 BREAK",
                    adjusted_plot_price=breakdown.close,
                )
            )
        events.append(
            ReviewEvent(
                ReviewEventType.SMA10_BREAKOUT
                if signal.breakout_ma == 10
                else ReviewEventType.BOX_BREAKOUT,
                signal.signal_date,
                f"MA{signal.breakout_ma} +1 CROSS",
                adjusted_plot_price=signal.close,
            )
        )
        events.append(
            ReviewEvent(
                ReviewEventType.DAILY_MA_BUY_SIGNAL,
                signal.signal_date,
                f"BUY {signal.buy_type.name}",
                adjusted_plot_price=signal.close,
                details={"signal_id": signal.signal_id, "research_only": True},
            )
        )
    return tuple(events)


def _overview_events(signals: Sequence[BuyCandidate]) -> tuple[ReviewEvent, ...]:
    counters: Counter[BuyType] = Counter()
    events: list[ReviewEvent] = []
    for signal in sorted(signals, key=lambda item: (item.signal_date, item.signal_id)):
        counters[signal.buy_type] += 1
        events.append(
            ReviewEvent(
                ReviewEventType.DAILY_MA_BUY_SIGNAL,
                signal.signal_date,
                f"{signal.buy_type.name}{counters[signal.buy_type]:02d}",
                adjusted_plot_price=signal.close,
                details={"signal_id": signal.signal_id, "research_only": True},
            )
        )
    return tuple(events)


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def assert_no_future_outcome_fields(payload: object) -> None:
    if isinstance(payload, Mapping):
        forbidden = {
            str(key)
            for key in payload
            if str(key).casefold() in FORBIDDEN_OUTCOME_FIELDS
        }
        if forbidden:
            raise ValueError(
                f"future-outcome fields are forbidden: {sorted(forbidden)}"
            )
        for value in payload.values():
            assert_no_future_outcome_fields(value)
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        for value in payload:
            assert_no_future_outcome_fields(value)


REGISTRY_COLUMNS = (
    "stock_code",
    "signal_date",
    "signal_type",
    "signal_id",
    "review_universe",
    "size_bucket",
    "market_cap",
    "market_cap_asof",
    "close",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ma120",
    "ma5_inflection_date",
    "ma10_inflection_date",
    "below_ma10_run",
    "below_ma20_run",
    "ma10_breakout_pct",
    "ma20_breakout_pct",
    "ma10_breakdown_pct",
    "cross_threshold_pct",
    "ma10_slope10_pct",
    "ma20_slope10_pct",
    "ma60_slope10_pct",
    "slope_guard_pass",
    "distance_to_ma60_pct",
    "distance_to_ma120_pct",
    "parent_buy_a_date",
    "parent_buy_a_signal_id",
    "other_qualifying_parent_ids",
    "overlap_tags",
    "manual_review_status",
    "manual_review_note",
)


def _registry_rows(
    signals: Sequence[BuyCandidate],
    selected: Mapping[str, BucketedStock],
) -> tuple[dict[str, object], ...]:
    groups: dict[tuple[str, date], list[str]] = defaultdict(list)
    for signal in signals:
        groups[(signal.stock_code, signal.signal_date)].append(signal.buy_type.value)
    rows: list[dict[str, object]] = []
    for signal in sorted(
        signals,
        key=lambda item: (
            item.stock_code,
            item.signal_date,
            item.buy_type.value,
            item.signal_id,
        ),
    ):
        metadata = selected[signal.stock_code]
        values = asdict(signal)
        values.update(
            {
                "signal_type": signal.buy_type.value,
                "review_universe": "BALANCED_PRIMARY_LOCAL_ELIGIBLE",
                "size_bucket": metadata.bucket.value,
                "market_cap": metadata.market_cap,
                "market_cap_asof": MARKET_CAP_AS_OF,
                "overlap_tags": "+".join(
                    sorted(groups[(signal.stock_code, signal.signal_date)])
                ),
                "manual_review_status": "",
                "manual_review_note": "",
            }
        )
        rows.append(values)
    return tuple(rows)


def write_registry(
    signals: Sequence[BuyCandidate],
    selected: Mapping[str, BucketedStock],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REGISTRY_COLUMNS)
        writer.writeheader()
        for row in _registry_rows(signals, selected):
            writer.writerow(
                {
                    key: ""
                    if row.get(key) is None
                    else "|".join(row[key])
                    if isinstance(row.get(key), tuple)
                    else _json_default(row[key])
                    if isinstance(row.get(key), (date, Decimal, Enum))
                    else row.get(key, "")
                    for key in REGISTRY_COLUMNS
                }
            )


def _guard_report(candidates: Sequence[BuyCandidate]) -> dict[str, dict[str, int]]:
    return {
        buy_type.name: {
            "before_guard": sum(item.buy_type is buy_type for item in candidates),
            "rejected": sum(
                item.buy_type is buy_type and not item.slope_guard_pass
                for item in candidates
            ),
            "accepted": sum(
                item.buy_type is buy_type and item.slope_guard_pass
                for item in candidates
            ),
        }
        for buy_type in BuyType
    }


def generate_visual_proof(
    *,
    start_date: date = RESEARCH_START,
    end_date: date = RESEARCH_END,
    output_path: Path = OUTPUT_PATH,
    registry_path: Path = REGISTRY_PATH,
    comparison_path: Path = COMPARISON_PATH,
    chart_root: Path = CHART_ROOT,
) -> dict[str, Any]:
    """Generate V0.3 artifacts from existing local Daily and KRX data only."""

    if start_date > end_date:
        raise ValueError("start_date must not exceed end_date")
    local_codes = tuple(
        sorted(
            path.name
            for path in DEFAULT_RAW_ROOT.iterdir()
            if path.is_dir() and re.fullmatch(r"[0-9]{6}", path.name, flags=re.ASCII)
        )
    )
    market_caps = {row.stock_code: row for row in load_local_market_caps()}
    bars_by_code: dict[str, tuple[DailyBar, ...]] = {}
    eligible_caps: list[MarketCapRecord] = []
    excluded: list[dict[str, str]] = []
    for stock_code in local_codes:
        if stock_code not in market_caps:
            excluded.append({"stock_code": stock_code, "reason": "MISSING_MARKET_CAP"})
            continue
        bars = load_local_daily_bars(
            stock_code, LOCAL_DATA_START, LOCAL_ARTIFACT_BASE_DATE
        )
        if (
            not bars
            or bars[0].trade_date > start_date
            or bars[-1].trade_date < end_date
        ):
            excluded.append(
                {"stock_code": stock_code, "reason": "INSUFFICIENT_DAILY_COVERAGE"}
            )
            continue
        bars_by_code[stock_code] = bars
        eligible_caps.append(market_caps[stock_code])
    selected_rows = select_balanced_review_universe(eligible_caps)
    selected_by_code = {row.stock_code: row for row in selected_rows}
    primary_codes = tuple(sorted(selected_by_code))
    legacy_codes = tuple(code for code in LEGACY_STOCK_CODES if code in bars_by_code)
    points_by_code = {
        code: calculate_daily_ma_points(bars_by_code[code])
        for code in sorted(set(primary_codes) | set(legacy_codes))
    }

    candidates_by_code = {
        code: tuple(
            candidate
            for candidate in detect_buy_candidates(code, points_by_code[code])
            if start_date <= candidate.signal_date <= end_date
        )
        for code in points_by_code
    }
    signals_by_code = {
        code: accepted_signals(candidates)
        for code, candidates in candidates_by_code.items()
    }
    primary_candidates = tuple(
        candidate for code in primary_codes for candidate in candidates_by_code[code]
    )
    primary_signals = tuple(
        signal for code in primary_codes for signal in signals_by_code[code]
    )
    legacy_v0_3 = tuple(
        signal for code in legacy_codes for signal in signals_by_code[code]
    )
    legacy_v0_2_candidates = tuple(
        candidate
        for code in legacy_codes
        for candidate in detect_v0_2_candidates(code, points_by_code[code])
        if start_date <= candidate.signal_date <= end_date
    )
    legacy_v0_2 = accepted_v0_2_signals(legacy_v0_2_candidates)

    v0_2_counts = _counts(legacy_v0_2)
    v0_3_counts = _counts(legacy_v0_3)
    comparison = {
        "stock_codes": list(legacy_codes),
        "review_period": {"start": start_date, "end": end_date},
        "v0_2_counts": v0_2_counts,
        "v0_3_counts": v0_3_counts,
        "count_deltas_v0_3_minus_v0_2": {
            key: v0_3_counts[key] - v0_2_counts[key] for key in v0_3_counts
        },
        "v0_2_overlap_counts": _overlap_counts(legacy_v0_2),
        "v0_3_overlap_counts": _overlap_counts(legacy_v0_3),
        "v0_2_slope_rejected_counts": {
            buy_type.name: sum(
                candidate.buy_type.name == buy_type.name
                and not candidate.slope_guard_pass
                for candidate in legacy_v0_2_candidates
            )
            for buy_type in BuyType
        },
        "v0_3_guard_report": _guard_report(
            tuple(
                candidate
                for code in legacy_codes
                for candidate in candidates_by_code[code]
            )
        ),
    }
    comparison_path.parent.mkdir(parents=True, exist_ok=True)
    comparison_path.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )

    bucket_by_code = {code: row.bucket for code, row in selected_by_code.items()}
    sample_windows = select_sample_windows(primary_signals, bucket_by_code)
    sample_charts: list[dict[str, object]] = []
    for ordinal, (stock_code, signal_date, group) in enumerate(sample_windows, 1):
        bars = bars_by_code[stock_code]
        point_by_date = {
            point.trade_date: point for point in points_by_code[stock_code]
        }
        events = _events_for_window(group, point_by_date)
        date_index = {bar.trade_date: index for index, bar in enumerate(bars)}
        causal_start = min(event.event_date for event in events)
        causal_distance = date_index[signal_date] - date_index[causal_start]
        calendar = ExplicitTradingCalendar(bar.trade_date for bar in bars)
        for mode, base_pre, post_sessions in (
            ("FULL_CONTEXT", FULL_CONTEXT_PRE_SESSIONS, FULL_CONTEXT_POST_SESSIONS),
            ("SIGNAL_ZOOM", SIGNAL_ZOOM_PRE_SESSIONS, SIGNAL_ZOOM_POST_SESSIONS),
        ):
            prepared = prepare_review_chart(
                bars,
                chart_type=ChartType.EVENT_REVIEW,
                events=events,
                calendar=calendar,
                focus_date=signal_date,
                pre_sessions=max(base_pre, causal_distance),
                post_sessions=post_sessions,
                show_sma5=True,
                show_sma120=True,
                ma_color_scheme="DAILY_MA_RESEARCH",
            )
            filename = f"{ordinal:02d}_{stock_code}_{signal_date.isoformat()}_{mode.lower()}.png"
            artifact = render_review_chart(
                prepared,
                chart_root / "samples" / filename,
                strategy_policy="RESEARCH_ONLY_DAILY_MA_UNIFIED_BUY_V0_3",
                summary={
                    "signals": [asdict(signal) for signal in group],
                    "size_bucket": bucket_by_code[stock_code],
                    "network_calls": 0,
                    "orders_created": 0,
                },
            )
            sample_charts.append(
                {
                    "window_id": f"WINDOW_{ordinal:02d}",
                    "mode": mode,
                    "stock_code": stock_code,
                    "signal_date": signal_date,
                    "size_bucket": bucket_by_code[stock_code],
                    "signal_ids": [signal.signal_id for signal in group],
                    "png_path": artifact.png_path.as_posix(),
                    "chart_metadata_path": artifact.metadata_path.as_posix(),
                }
            )

    overview_charts: list[dict[str, object]] = []
    for stock_code in primary_codes:
        overview_bars = slice_daily_bars(bars_by_code[stock_code], start_date, end_date)
        stock_signals = signals_by_code[stock_code]
        prepared = prepare_review_chart(
            overview_bars,
            chart_type=ChartType.STOCK_OVERVIEW,
            events=_overview_events(stock_signals),
            show_sma5=True,
            show_sma120=True,
            ma_color_scheme="DAILY_MA_RESEARCH",
            overview_year_month_axis=True,
        )
        artifact = render_review_chart(
            prepared,
            chart_root / "overview" / f"{stock_code}_overview.png",
            strategy_policy="RESEARCH_ONLY_DAILY_MA_UNIFIED_BUY_V0_3",
            summary={
                "size_bucket": bucket_by_code[stock_code],
                "signal_count": len(stock_signals),
                "network_calls": 0,
                "orders_created": 0,
            },
        )
        overview_charts.append(
            {
                "stock_code": stock_code,
                "size_bucket": bucket_by_code[stock_code],
                "png_path": artifact.png_path.as_posix(),
                "chart_metadata_path": artifact.metadata_path.as_posix(),
                "signal_count": len(stock_signals),
            }
        )

    write_registry(primary_signals, selected_by_code, registry_path)
    counts_by_bucket = {
        bucket.value: _counts(
            tuple(
                signal
                for signal in primary_signals
                if bucket_by_code[signal.stock_code] is bucket
            )
        )
        for bucket in SizeBucket
    }
    groups = _candidate_groups(primary_signals)
    payload: dict[str, Any] = {
        "proof_version": PROOF_VERSION,
        "scope": "VISUAL_RULE_INTERPRETATION_AUDIT_ONLY",
        "daily_input_source": "LOCAL_KIWOOM_KA10081_RAW_AND_ADJUSTED_ARTIFACTS",
        "signal_basis": "ADJUSTED_DAILY_CLOSE",
        "review_period": {"start": start_date, "end": end_date},
        "market_cap": {
            "source": MARKET_CAP_SOURCE,
            "as_of": MARKET_CAP_AS_OF,
            "raw_paths": [path.as_posix() for path in MARKET_CAP_PATHS],
            "bucket_method": "DESCENDING_MARKET_CAP_EQUAL_COUNT_TERCILES_AMONG_LOCAL_DAILY_ELIGIBLE_STOCKS",
            "selection_method": "UP_TO_SIX_EVENLY_SPACED_MARKET_CAP_RANKS_PER_BUCKET",
            "eligible_count": len(eligible_caps),
            "target_count": 18,
            "selected_count": len(selected_rows),
            "shortage_reason": "ONLY_ELEVEN_LOCAL_STOCKS_HAVE_REQUIRED_KA10081_DAILY_COVERAGE"
            if len(selected_rows) < 18
            else None,
        },
        "selected_universe": [asdict(row) for row in selected_rows],
        "excluded_local_codes": excluded,
        "cross_contract": {
            "up": "Close[T] >= MA[T]*1.01 AND Close[T-1] < MA[T-1]*1.01",
            "down": "Close[T] <= MA[T]*0.99 AND Close[T-1] > MA[T-1]*0.99",
            "close_only": True,
        },
        "structural_inflection_contract": "UNCHANGED_FROM_V0_2",
        "buy_a_contract": "below MA10 >=10; MA5 structural event; MA10 +1% cross; slope guard",
        "buy_b_contract": "accepted A; first MA10 -1% breakdown and later first +1% reclaim both within T0+1..T0+10",
        "buy_c_contract": "below MA20 >=15; MA10 structural event; MA20 +1% cross; slope guard",
        "slope_guard": {
            "status": "PROVISIONAL_FALLING_KNIFE_GUARD",
            "ma10_slope10_pct_gte": SLOPE_RESTRICTION_PCT,
        },
        "research_observations_only": [
            "BUY-A may act as early tactical recovery in declining structures toward major resistance",
            "BUY-A may mark pullback-low recovery in rising structures",
        ],
        "guard_report": _guard_report(primary_candidates),
        "final_signal_counts": _counts(primary_signals),
        "counts_by_size_bucket": counts_by_bucket,
        "overlap_counts": _overlap_counts(primary_signals),
        "buy_b_parent_coverage": {
            "buy_b_count": sum(
                signal.buy_type is BuyType.B for signal in primary_signals
            ),
            "with_parent": sum(
                signal.buy_type is BuyType.B
                and signal.parent_buy_a_signal_id is not None
                for signal in primary_signals
            ),
        },
        "unique_signal_dates": len({signal.signal_date for signal in primary_signals}),
        "registry_rows": len(primary_signals),
        "registry_path": registry_path.as_posix(),
        "comparison_path": comparison_path.as_posix(),
        "a_only_available": any(
            {item.buy_type for item in group} == {BuyType.A} for _, _, group in groups
        ),
        "c_only_available": any(
            {item.buy_type for item in group} == {BuyType.C} for _, _, group in groups
        ),
        "sample_windows": [
            {
                "stock_code": code,
                "signal_date": day,
                "size_bucket": bucket_by_code[code],
                "signals": [asdict(signal) for signal in group],
            }
            for code, day, group in sample_windows
        ],
        "sample_charts": sample_charts,
        "overview_charts": overview_charts,
        "overview_axis": {
            "year_format": "YY",
            "month_labels": ["01", "04", "07", "10"],
            "year_separator": "FIRST_AVAILABLE_TRADING_SESSION_OF_NEW_YEAR",
        },
        "ma_colors": {
            "MA5": "BLACK",
            "MA10": "BLUE",
            "MA20": "RED",
            "MA60": "GREEN",
            "MA120": "ORANGE",
        },
        "network_calls": 0,
        "orders_created": 0,
        "strategy_profitability_evaluated": False,
        "future_outcome_evaluated": False,
        "forbidden_outcome_fields": sorted(FORBIDDEN_OUTCOME_FIELDS),
    }
    assert_no_future_outcome_fields(payload)
    assert_no_future_outcome_fields(comparison)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat, default=RESEARCH_START)
    parser.add_argument("--end-date", type=date.fromisoformat, default=RESEARCH_END)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--comparison", type=Path, default=COMPARISON_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    args = parser.parse_args()
    payload = generate_visual_proof(
        start_date=args.start_date,
        end_date=args.end_date,
        output_path=args.output,
        registry_path=args.registry,
        comparison_path=args.comparison,
        chart_root=args.chart_root,
    )
    print(
        json.dumps(
            {
                "selected": payload["market_cap"]["selected_count"],
                "final_signal_counts": payload["final_signal_counts"],
            },
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
