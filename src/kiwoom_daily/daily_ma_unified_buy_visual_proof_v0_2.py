"""Outcome-free V0.2 visual review for unified Daily MA BUY structures.

This is a deliberately local research module.  It consumes only cached ka10081
RAW/ADJUSTED artifacts through ``daily_ma.data`` and makes no execution,
profitability, or network decision.
"""

from __future__ import annotations

import argparse
import csv
import json
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
from src.backtest_engine.validation import validate_daily_bars
from src.strategy_review.chart import (
    ChartType,
    ReviewEvent,
    ReviewEventType,
    prepare_review_chart,
    render_review_chart,
)

from .daily_ma.core import (
    confirmed_breakdown,
    confirmed_breakout,
    consecutive_below_ma_runs,
    ma_percentage_change,
    moving_averages,
    structural_upward_inflection_events,
    structural_upward_inflection_state,
)
from .daily_ma.data import load_local_daily_bars, slice_daily_bars

PROOF_VERSION = "DAILY_MA_UNIFIED_BUY_VISUAL_PROOF_V0_2"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/daily_ma_unified_buy_visual_proof_v0_2.json"
)
REGISTRY_PATH = Path(
    "data/processed/strategy_review/daily_ma_unified_buy_signal_registry_v0_2.csv"
)
CHART_ROOT = Path(
    "data/processed/strategy_charts/daily_ma_unified_buy_visual_proof_v0_2"
)
RESEARCH_START = date(2023, 9, 1)
RESEARCH_END = date(2026, 8, 28)
LOCAL_DATA_START = date(2023, 6, 1)
LOCAL_ARTIFACT_BASE_DATE = date(2026, 8, 31)
DEFAULT_STOCK_CODES = (
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
SLOPE_PERIOD = 10
SLOPE_RESTRICTION_PCT = Decimal(-20)
BUY_A_MIN_BELOW_RUN = 10
BUY_C_MIN_BELOW_RUN = 15
BUY_B_FAILURE_MAX_BARS = 3
BUY_B_RECLAIM_MAX_BARS = 10
FULL_CONTEXT_PRE_SESSIONS = 200
FULL_CONTEXT_POST_SESSIONS = 100
SIGNAL_ZOOM_PRE_SESSIONS = 30
SIGNAL_ZOOM_POST_SESSIONS = 30
MAX_SAMPLES_PER_TYPE = 2
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
    }
)


class BuyType(str, Enum):
    A = "BUY_A_LONG_RECOVERY_10"
    B = "BUY_B_A_FAILURE_SECOND_RECLAIM"
    C = "BUY_C_LONG_RECOVERY_20"


@dataclass(frozen=True, slots=True)
class DailyMaPoint:
    trade_date: date
    close: Decimal
    ma5: Decimal | None
    ma10: Decimal | None
    ma20: Decimal | None
    ma60: Decimal | None
    ma120: Decimal | None
    ma10_slope10_pct: Decimal | None
    ma20_slope10_pct: Decimal | None
    ma60_slope10_pct: Decimal | None
    ma10_below_run: int
    ma20_below_run: int
    ma5_inflection_state: bool
    ma5_inflection_event: bool
    ma10_inflection_state: bool
    ma10_inflection_event: bool
    ma10_breakout: bool
    ma20_breakout: bool
    ma10_breakdown: bool


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
    ma5_inflection: bool
    ma10_inflection: bool
    ma5_inflection_date: date | None
    ma10_inflection_date: date | None
    breakout_ma: int
    ma10_breakout_pct: Decimal | None
    ma20_breakout_pct: Decimal | None
    ma10_breakdown_pct: Decimal | None
    breakdown_date: date | None
    bars_since_breakdown: int | None
    parent_buy_a_id: str | None
    parent_buy_a_date: date | None
    slope_guard_pass: bool
    distance_to_ma60_pct: Decimal | None
    distance_to_ma120_pct: Decimal | None
    review_status: str | None = None


@dataclass(frozen=True, slots=True)
class _RecoverySetup:
    inflection_index: int
    below_run: int


@dataclass(frozen=True, slots=True)
class _BuyBEpisode:
    parent: BuyCandidate
    parent_index: int
    breakdown_index: int | None = None


def slope_state(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value > 3:
        return "UP"
    if value < -3:
        return "DOWN"
    return "FLAT"


def calculate_daily_ma_points(bars: Sequence[DailyBar]) -> tuple[DailyMaPoint, ...]:
    """Calculate only causal, adjusted-price Daily MA primitives."""

    canonical = tuple(sorted(bars, key=lambda bar: (bar.stock_code, bar.trade_date)))
    validate_daily_bars(canonical)
    if not canonical:
        return ()
    if len({bar.stock_code for bar in canonical}) != 1:
        raise ValueError("Daily MA proof accepts one stock at a time")
    closes = tuple(bar.signal.close for bar in canonical)
    averages = moving_averages(closes)
    ma5, ma10, ma20, ma60, ma120 = (
        averages[5],
        averages[10],
        averages[20],
        averages[60],
        averages[120],
    )
    ma5_states = tuple(
        structural_upward_inflection_state(ma5, index, lookback=5)
        for index in range(len(canonical))
    )
    ma10_states = tuple(
        structural_upward_inflection_state(ma10, index, lookback=10)
        for index in range(len(canonical))
    )
    ma5_events = structural_upward_inflection_events(ma5, lookback=5)
    ma10_events = structural_upward_inflection_events(ma10, lookback=10)
    below10 = consecutive_below_ma_runs(closes, ma10)
    below20 = consecutive_below_ma_runs(closes, ma20)
    output: list[DailyMaPoint] = []
    for index, bar in enumerate(canonical):
        previous_close = closes[index - 1] if index else None
        previous_ma10 = ma10[index - 1] if index else None
        previous_ma20 = ma20[index - 1] if index else None
        output.append(
            DailyMaPoint(
                trade_date=bar.trade_date,
                close=bar.signal.close,
                ma5=ma5[index],
                ma10=ma10[index],
                ma20=ma20[index],
                ma60=ma60[index],
                ma120=ma120[index],
                ma10_slope10_pct=ma_percentage_change(ma10, index),
                ma20_slope10_pct=ma_percentage_change(ma20, index),
                ma60_slope10_pct=ma_percentage_change(ma60, index),
                ma10_below_run=below10[index],
                ma20_below_run=below20[index],
                ma5_inflection_state=ma5_states[index],
                ma5_inflection_event=ma5_events[index],
                ma10_inflection_state=ma10_states[index],
                ma10_inflection_event=ma10_events[index],
                ma10_breakout=confirmed_breakout(
                    bar.signal.close, ma10[index], previous_close, previous_ma10
                ),
                ma20_breakout=confirmed_breakout(
                    bar.signal.close, ma20[index], previous_close, previous_ma20
                ),
                ma10_breakdown=confirmed_breakdown(
                    bar.signal.close, ma10[index], previous_close, previous_ma10
                ),
            )
        )
    return tuple(output)


def _distance_pct(close: Decimal, ma: Decimal | None) -> Decimal | None:
    return None if ma is None or ma == 0 else (close / ma - 1) * 100


def _signal_id(
    buy_type: BuyType, stock_code: str, signal_date: date, parent_id: str | None = None
) -> str:
    suffix = "" if parent_id is None else f":{parent_id}"
    return f"{buy_type.value}:{stock_code}:{signal_date.isoformat()}{suffix}"


def _candidate(
    stock_code: str,
    point: DailyMaPoint,
    buy_type: BuyType,
    *,
    below_ma10_run: int,
    below_ma20_run: int,
    ma5_inflection_date: date | None = None,
    ma10_inflection_date: date | None = None,
    breakdown_date: date | None = None,
    ma10_breakdown_pct: Decimal | None = None,
    bars_since_breakdown: int | None = None,
    parent: BuyCandidate | None = None,
) -> BuyCandidate:
    guard_pass = (
        point.ma10_slope10_pct is not None
        and point.ma10_slope10_pct >= SLOPE_RESTRICTION_PCT
    )
    parent_id = None if parent is None else parent.signal_id
    parent_date = None if parent is None else parent.signal_date
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
        ma5_inflection=ma5_inflection_date is not None,
        ma10_inflection=ma10_inflection_date is not None,
        ma5_inflection_date=ma5_inflection_date,
        ma10_inflection_date=ma10_inflection_date,
        breakout_ma=20 if buy_type is BuyType.C else 10,
        ma10_breakout_pct=_distance_pct(point.close, point.ma10),
        ma20_breakout_pct=_distance_pct(point.close, point.ma20),
        ma10_breakdown_pct=ma10_breakdown_pct,
        breakdown_date=breakdown_date,
        bars_since_breakdown=bars_since_breakdown,
        parent_buy_a_id=parent_id,
        parent_buy_a_date=parent_date,
        slope_guard_pass=guard_pass,
        distance_to_ma60_pct=_distance_pct(point.close, point.ma60),
        distance_to_ma120_pct=_distance_pct(point.close, point.ma120),
    )


def detect_buy_candidates(
    stock_code: str, points: Sequence[DailyMaPoint]
) -> tuple[BuyCandidate, ...]:
    """Return all causal V0.2 candidates, retaining slope-guard rejections."""

    canonical = tuple(sorted(points, key=lambda point: point.trade_date))
    if len({point.trade_date for point in canonical}) != len(canonical):
        raise ValueError("Daily MA points must not contain duplicate dates")
    output: list[BuyCandidate] = []
    a_setup: _RecoverySetup | None = None
    c_setup: _RecoverySetup | None = None
    b_episodes: list[_BuyBEpisode] = []
    for index, point in enumerate(canonical):
        previous = canonical[index - 1] if index else None

        next_episodes: list[_BuyBEpisode] = []
        for episode in b_episodes:
            elapsed_from_a = index - episode.parent_index
            if episode.breakdown_index is None:
                if elapsed_from_a > BUY_B_FAILURE_MAX_BARS:
                    continue
                if elapsed_from_a >= 1 and point.ma10_breakdown:
                    next_episodes.append(
                        _BuyBEpisode(episode.parent, episode.parent_index, index)
                    )
                else:
                    next_episodes.append(episode)
                continue
            elapsed_from_breakdown = index - episode.breakdown_index
            if elapsed_from_breakdown > BUY_B_RECLAIM_MAX_BARS:
                continue
            if elapsed_from_breakdown >= 1 and point.ma10_breakout:
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
                        breakdown_date=canonical[episode.breakdown_index].trade_date,
                        ma10_breakdown_pct=_distance_pct(
                            canonical[episode.breakdown_index].close,
                            canonical[episode.breakdown_index].ma10,
                        ),
                        bars_since_breakdown=elapsed_from_breakdown,
                        parent=episode.parent,
                    )
                )
                continue
            next_episodes.append(episode)
        b_episodes = next_episodes

        prior_ma10_run = previous.ma10_below_run if previous is not None else 0
        prior_ma20_run = previous.ma20_below_run if previous is not None else 0
        if (
            a_setup is None
            and point.ma5_inflection_event
            and max(point.ma10_below_run, prior_ma10_run) >= BUY_A_MIN_BELOW_RUN
        ):
            a_setup = _RecoverySetup(index, max(point.ma10_below_run, prior_ma10_run))
        if (
            c_setup is None
            and point.ma10_inflection_event
            and max(point.ma20_below_run, prior_ma20_run) >= BUY_C_MIN_BELOW_RUN
        ):
            c_setup = _RecoverySetup(index, max(point.ma20_below_run, prior_ma20_run))

        if point.ma10_breakout and a_setup is not None:
            candidate = _candidate(
                stock_code,
                point,
                BuyType.A,
                below_ma10_run=a_setup.below_run,
                below_ma20_run=prior_ma20_run,
                ma5_inflection_date=canonical[a_setup.inflection_index].trade_date,
            )
            output.append(candidate)
            if candidate.slope_guard_pass:
                b_episodes.append(_BuyBEpisode(candidate, index))
            a_setup = None

        if point.ma20_breakout and c_setup is not None:
            output.append(
                _candidate(
                    stock_code,
                    point,
                    BuyType.C,
                    below_ma10_run=prior_ma10_run,
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


def _in_research_period(candidates: Iterable[BuyCandidate]) -> tuple[BuyCandidate, ...]:
    return tuple(
        candidate
        for candidate in candidates
        if RESEARCH_START <= candidate.signal_date <= RESEARCH_END
    )


def _overlap_counts(signals: Sequence[BuyCandidate]) -> dict[str, int]:
    groups: dict[tuple[str, date], list[BuyCandidate]] = defaultdict(list)
    for signal in signals:
        groups[(signal.stock_code, signal.signal_date)].append(signal)
    return dict(
        sorted(
            Counter(
                "+".join(sorted(item.buy_type.value for item in group))
                for group in groups.values()
                if len(group) > 1
            ).items()
        )
    )


def select_signal_windows(
    signals: Sequence[BuyCandidate],
) -> tuple[tuple[str, date, tuple[BuyCandidate, ...]], ...]:
    """Choose earliest final signals per type, preserving same-day overlap."""

    selected: list[BuyCandidate] = []
    for buy_type in BuyType:
        selected.extend(
            sorted(
                (signal for signal in signals if signal.buy_type is buy_type),
                key=lambda item: (item.signal_date, item.stock_code, item.signal_id),
            )[:MAX_SAMPLES_PER_TYPE]
        )
    grouped: dict[tuple[str, date], list[BuyCandidate]] = defaultdict(list)
    for signal in selected:
        grouped[(signal.stock_code, signal.signal_date)].append(signal)
    return tuple(
        (stock_code, signal_date, tuple(sorted(group, key=lambda item: item.signal_id)))
        for (stock_code, signal_date), group in sorted(
            grouped.items(), key=lambda item: (item[0][1], item[0][0])
        )
    )


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
                    "MA10 -2 BREAK",
                    adjusted_plot_price=breakdown.close,
                )
            )
        events.append(
            ReviewEvent(
                ReviewEventType.SMA10_BREAKOUT
                if signal.breakout_ma == 10
                else ReviewEventType.BOX_BREAKOUT,
                signal.signal_date,
                f"MA{signal.breakout_ma} +2 BREAKOUT",
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


def _summary(signals: Sequence[BuyCandidate]) -> dict[str, Any]:
    return {
        "proof_version": PROOF_VERSION,
        "signal_decision_at": "DAILY_T_CLOSE",
        "execution_created": False,
        "orders_created": False,
        "network_calls": 0,
        "strategy_profitability_evaluated": False,
        "signals": [asdict(signal) for signal in signals],
    }


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


def _json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _registry_rows(signals: Sequence[BuyCandidate]) -> tuple[dict[str, object], ...]:
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
        values = asdict(signal)
        values["signal_type"] = signal.buy_type.value
        values["overlap_tags"] = "+".join(
            sorted(groups[(signal.stock_code, signal.signal_date)])
        )
        values["review_note"] = ""
        values["manual_review_status"] = ""
        values["manual_review_note"] = ""
        rows.append(values)
    return tuple(rows)


REGISTRY_COLUMNS = (
    "stock_code",
    "signal_date",
    "signal_type",
    "signal_id",
    "close",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ma120",
    "ma5_inflection",
    "ma10_inflection",
    "ma5_inflection_date",
    "ma10_inflection_date",
    "below_ma10_run",
    "below_ma20_run",
    "breakout_ma",
    "ma10_breakout_pct",
    "ma20_breakout_pct",
    "ma10_breakdown_pct",
    "breakdown_date",
    "bars_since_breakdown",
    "ma10_slope10_pct",
    "ma20_slope10_pct",
    "ma60_slope10_pct",
    "slope_guard_pass",
    "parent_buy_a_date",
    "parent_buy_a_id",
    "distance_to_ma60_pct",
    "distance_to_ma120_pct",
    "overlap_tags",
    "review_status",
    "review_note",
    "manual_review_status",
    "manual_review_note",
)


def write_signal_registry(signals: Sequence[BuyCandidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REGISTRY_COLUMNS)
        writer.writeheader()
        for row in _registry_rows(signals):
            writer.writerow(
                {
                    key: ""
                    if row.get(key) is None
                    else _json_default(row[key])
                    if isinstance(row.get(key), (date, Decimal, Enum))
                    else row.get(key, "")
                    for key in REGISTRY_COLUMNS
                }
            )


def _overview_events(signals: Sequence[BuyCandidate]) -> tuple[ReviewEvent, ...]:
    return tuple(
        ReviewEvent(
            ReviewEventType.DAILY_MA_BUY_SIGNAL,
            signal.signal_date,
            signal.buy_type.name,
            adjusted_plot_price=signal.close,
            details={"signal_id": signal.signal_id, "research_only": True},
        )
        for signal in signals
    )


def generate_visual_proof(
    *,
    stock_codes: Sequence[str] = DEFAULT_STOCK_CODES,
    start_date: date = RESEARCH_START,
    end_date: date = RESEARCH_END,
    output_path: Path = OUTPUT_PATH,
    registry_path: Path = REGISTRY_PATH,
    chart_root: Path = CHART_ROOT,
) -> dict[str, Any]:
    """Materialize local-only V0.2 visual-review artifacts deterministically."""

    if start_date > end_date:
        raise ValueError("start_date must not exceed end_date")
    selected_codes = tuple(sorted(set(stock_codes)))
    all_candidates: list[BuyCandidate] = []
    bars_by_code: dict[str, tuple[DailyBar, ...]] = {}
    points_by_code: dict[str, tuple[DailyMaPoint, ...]] = {}
    for stock_code in selected_codes:
        bars = load_local_daily_bars(
            stock_code, LOCAL_DATA_START, LOCAL_ARTIFACT_BASE_DATE
        )
        points = calculate_daily_ma_points(bars)
        bars_by_code[stock_code] = bars
        points_by_code[stock_code] = points
        all_candidates.extend(
            candidate
            for candidate in detect_buy_candidates(stock_code, points)
            if start_date <= candidate.signal_date <= end_date
        )
    candidates = tuple(
        sorted(
            all_candidates,
            key=lambda item: (item.stock_code, item.signal_date, item.signal_id),
        )
    )
    signals = accepted_signals(candidates)
    windows = select_signal_windows(signals)
    chart_records: list[dict[str, object]] = []
    for ordinal, (stock_code, signal_date, window_signals) in enumerate(windows, 1):
        calendar = ExplicitTradingCalendar(
            bar.trade_date for bar in bars_by_code[stock_code]
        )
        events = _events_for_window(
            window_signals,
            {point.trade_date: point for point in points_by_code[stock_code]},
        )
        for mode, pre_sessions, post_sessions in (
            ("FULL_CONTEXT", FULL_CONTEXT_PRE_SESSIONS, FULL_CONTEXT_POST_SESSIONS),
            ("SIGNAL_ZOOM", SIGNAL_ZOOM_PRE_SESSIONS, SIGNAL_ZOOM_POST_SESSIONS),
        ):
            prepared = prepare_review_chart(
                bars_by_code[stock_code],
                chart_type=ChartType.EVENT_REVIEW,
                events=events,
                calendar=calendar,
                focus_date=signal_date,
                pre_sessions=pre_sessions,
                post_sessions=post_sessions,
                show_sma5=True,
                show_sma120=True,
                ma_color_scheme="DAILY_MA_RESEARCH",
            )
            filename = f"{ordinal:02d}_{stock_code}_{signal_date.isoformat()}_{mode.lower()}.png"
            artifact = render_review_chart(
                prepared,
                chart_root / "samples" / filename,
                strategy_policy="RESEARCH_ONLY_DAILY_MA_UNIFIED_BUY_V0_2",
                summary=_summary(window_signals),
            )
            chart_records.append(
                {
                    "window_id": f"WINDOW_{ordinal:02d}",
                    "mode": mode,
                    "stock_code": stock_code,
                    "signal_date": signal_date,
                    "signal_ids": [signal.signal_id for signal in window_signals],
                    "png_path": artifact.png_path.as_posix(),
                    "chart_metadata_path": artifact.metadata_path.as_posix(),
                    "render_backend": artifact.backend,
                }
            )
    overview_records: list[dict[str, object]] = []
    for stock_code in selected_codes:
        overview_bars = slice_daily_bars(bars_by_code[stock_code], start_date, end_date)
        stock_signals = tuple(
            signal for signal in signals if signal.stock_code == stock_code
        )
        if not overview_bars:
            continue
        prepared = prepare_review_chart(
            overview_bars,
            chart_type=ChartType.STOCK_OVERVIEW,
            events=_overview_events(stock_signals),
            show_sma5=True,
            show_sma120=True,
            ma_color_scheme="DAILY_MA_RESEARCH",
        )
        artifact = render_review_chart(
            prepared,
            chart_root / "overview" / f"{stock_code}_overview.png",
            strategy_policy="RESEARCH_ONLY_DAILY_MA_UNIFIED_BUY_V0_2",
            summary=_summary(stock_signals),
        )
        overview_records.append(
            {
                "stock_code": stock_code,
                "png_path": artifact.png_path.as_posix(),
                "chart_metadata_path": artifact.metadata_path.as_posix(),
                "signal_count": len(stock_signals),
                "render_backend": artifact.backend,
            }
        )
    before_guard = {
        buy_type.value: sum(candidate.buy_type is buy_type for candidate in candidates)
        for buy_type in BuyType
    }
    rejected = {
        buy_type.value: sum(
            candidate.buy_type is buy_type and not candidate.slope_guard_pass
            for candidate in candidates
        )
        for buy_type in BuyType
    }
    final_counts = {
        buy_type.value: sum(signal.buy_type is buy_type for signal in signals)
        for buy_type in BuyType
    }
    write_signal_registry(signals, registry_path)
    payload: dict[str, Any] = {
        "proof_version": PROOF_VERSION,
        "scope": "VISUAL_RULE_INTERPRETATION_AUDIT_ONLY",
        "daily_input_source": "LOCAL_KIWOOM_KA10081_RAW_AND_ADJUSTED_ARTIFACTS",
        "moving_average_basis": "SIGNAL_ADJUSTED_DAILY_CLOSE",
        "research_period": {"start": start_date, "end": end_date},
        "stock_codes": list(selected_codes),
        "structural_inflection_contract": {
            "state": "current MA above most-recent minimum in inclusive trailing N MA values; all values after that low and before current are <= current",
            "event": "false_to_true_transition_of_state",
            "equal_low_tie": "MOST_RECENT",
            "buy_a_ma5_lookback": 5,
            "buy_c_ma10_lookback": 10,
        },
        "buy_a_contract": "below MA10 >=10, MA5 structural inflection event, MA10 +2% breakout, slope guard",
        "buy_b_contract": "accepted BUY-A, MA10 -2% breakdown within next 3 bars, MA10 +2% reclaim within 10 bars, slope guard",
        "buy_c_contract": "below MA20 >=15, MA10 structural inflection event, MA20 +2% breakout, slope guard",
        "unresolved_research_contract": "A/C inflection-to-breakout expiry is intentionally absent",
        "ma_color_scheme": "MA5_BLACK_MA10_BLUE_MA20_RED_MA60_GREEN_MA120_ORANGE",
        "slope_guard": {
            "status": "PROVISIONAL_FALLING_KNIFE_GUARD",
            "ma10_slope10_pct_gte": SLOPE_RESTRICTION_PCT,
        },
        "candidate_counts_before_slope_guard": before_guard,
        "slope_rejected_counts": rejected,
        "final_signal_counts": final_counts,
        "overlap_counts": _overlap_counts(signals),
        "buy_b_parent_coverage": {
            "buy_b_count": final_counts[BuyType.B.value],
            "with_parent": sum(
                signal.parent_buy_a_id is not None
                for signal in signals
                if signal.buy_type is BuyType.B
            ),
        },
        "signals": [asdict(signal) for signal in signals],
        "sample_windows": [
            {
                "stock_code": stock_code,
                "signal_date": signal_date,
                "signals": [asdict(signal) for signal in group],
            }
            for stock_code, signal_date, group in windows
        ],
        "sample_charts": chart_records,
        "overview_charts": overview_records,
        "registry_path": registry_path.as_posix(),
        "resistance_review_fields": [
            "distance_to_ma60_pct",
            "distance_to_ma120_pct",
        ],
        "network_calls": 0,
        "orders_created": 0,
        "strategy_profitability_evaluated": False,
        "future_outcome_evaluated": False,
        "forbidden_outcome_fields": sorted(FORBIDDEN_OUTCOME_FIELDS),
    }
    assert_no_future_outcome_fields(payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-code", action="append", dest="stock_codes")
    parser.add_argument("--start-date", type=date.fromisoformat, default=RESEARCH_START)
    parser.add_argument("--end-date", type=date.fromisoformat, default=RESEARCH_END)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    args = parser.parse_args()
    payload = generate_visual_proof(
        stock_codes=tuple(args.stock_codes)
        if args.stock_codes
        else DEFAULT_STOCK_CODES,
        start_date=args.start_date,
        end_date=args.end_date,
        output_path=args.output,
        registry_path=args.registry,
        chart_root=args.chart_root,
    )
    print(
        json.dumps(
            {"final_signal_counts": payload["final_signal_counts"]},
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
