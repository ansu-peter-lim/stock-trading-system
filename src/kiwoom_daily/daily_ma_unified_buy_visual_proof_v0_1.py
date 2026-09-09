"""Outcome-free visual proof for provisional Daily MA unified BUY ideas.

This research module detects and draws BUY-A/B/C candidates from Kiwoom
adjusted Daily bars.  It never creates an order, reads a future outcome, or
uses RAW execution prices.  The provisional definitions are deliberately kept
here rather than connected to a production strategy engine.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from itertools import pairwise
from pathlib import Path
from typing import Any

from src.backtest_engine.indicators import simple_moving_average
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

from .down_box_daily_execution_proof import _load_stock

PROOF_VERSION = "DAILY_MA_UNIFIED_BUY_VISUAL_PROOF_V0_1"
OUTPUT_PATH = Path(
    "data/processed/strategy_review/daily_ma_unified_buy_visual_proof_v0_1.json"
)
CHART_ROOT = Path(
    "data/processed/strategy_charts/daily_ma_unified_buy_visual_proof_v0_1"
)
RESEARCH_START = date(2023, 9, 1)
RESEARCH_END = date(2026, 8, 28)
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
BREAKOUT_RATIO = Decimal("1.02")
BREAKDOWN_RATIO = Decimal("0.98")
BUY_A_MIN_BELOW_RUN = 10
BUY_C_MIN_BELOW_RUN = 15
BUY_B_MIN_BARS_AFTER_BREAKDOWN = 1
BUY_B_MAX_BARS_AFTER_BREAKDOWN = 10
FULL_CONTEXT_PRE_SESSIONS = 120
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
    """Provisional visual-audit candidates, not production strategy signals."""

    A = "BUY_A_LONG_RECOVERY_10"
    B = "BUY_B_QUICK_RECLAIM_10_PROVISIONAL"
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
    ma5_upward_inflection: bool
    ma10_upward_inflection: bool
    ma10_breakout: bool
    ma20_breakout: bool
    ma10_breakdown: bool


@dataclass(frozen=True, slots=True)
class BuyCandidate:
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
    previous_ma10_below_run: int
    previous_ma20_below_run: int
    ma5_inflection_date: date | None
    ma10_inflection_date: date | None
    breakout_ma: int
    breakout_ratio: Decimal
    elapsed_bars_inflection_to_breakout: int | None
    breakdown_date: date | None
    bars_since_breakdown: int | None
    restriction_pass: bool
    review_status: str | None = None


def _slope(values: Sequence[Decimal | None], index: int) -> Decimal | None:
    if index < SLOPE_PERIOD:
        return None
    current, previous = values[index], values[index - SLOPE_PERIOD]
    if current is None or previous is None or previous == 0:
        return None
    return (current / previous - 1) * 100


def slope_state(value: Decimal | None) -> str | None:
    """Classify the frozen visual slope description without filtering on it."""

    if value is None:
        return None
    if value > 3:
        return "UP"
    if value < -3:
        return "DOWN"
    return "FLAT"


def confirmed_breakout(
    close: Decimal,
    ma: Decimal | None,
    previous_close: Decimal | None,
    previous_ma: Decimal | None,
) -> bool:
    return (
        ma is not None
        and previous_close is not None
        and previous_ma is not None
        and close >= ma * BREAKOUT_RATIO
        and previous_close < previous_ma * BREAKOUT_RATIO
    )


def confirmed_breakdown(
    close: Decimal,
    ma: Decimal | None,
    previous_close: Decimal | None,
    previous_ma: Decimal | None,
) -> bool:
    return (
        ma is not None
        and previous_close is not None
        and previous_ma is not None
        and close <= ma * BREAKDOWN_RATIO
        and previous_close > previous_ma * BREAKDOWN_RATIO
    )


def upward_inflection(values: Sequence[Decimal | None], index: int) -> bool:
    if index < 2:
        return False
    current, previous, two_back = values[index], values[index - 1], values[index - 2]
    return (
        current is not None
        and previous is not None
        and two_back is not None
        and current > previous
        and previous <= two_back
    )


def consecutive_below_runs(
    closes: Sequence[Decimal], ma: Sequence[Decimal | None]
) -> tuple[int, ...]:
    if len(closes) != len(ma):
        raise ValueError("close and MA sequences must have equal length")
    count = 0
    output: list[int] = []
    for close, current_ma in zip(closes, ma, strict=True):
        if current_ma is not None and close < current_ma:
            count += 1
        else:
            count = 0
        output.append(count)
    return tuple(output)


def calculate_daily_ma_points(bars: Sequence[DailyBar]) -> tuple[DailyMaPoint, ...]:
    """Calculate only adjusted-price primitives for the visual proof."""

    canonical = tuple(sorted(bars, key=lambda bar: (bar.stock_code, bar.trade_date)))
    validate_daily_bars(canonical)
    if not canonical:
        return ()
    if len({bar.stock_code for bar in canonical}) != 1:
        raise ValueError("Daily MA proof accepts one stock at a time")
    closes = tuple(bar.signal.close for bar in canonical)
    ma5 = simple_moving_average(closes, 5)
    ma10 = simple_moving_average(closes, 10)
    ma20 = simple_moving_average(closes, 20)
    ma60 = simple_moving_average(closes, 60)
    ma120 = simple_moving_average(closes, 120)
    run10 = consecutive_below_runs(closes, ma10)
    run20 = consecutive_below_runs(closes, ma20)
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
                ma10_slope10_pct=_slope(ma10, index),
                ma20_slope10_pct=_slope(ma20, index),
                ma60_slope10_pct=_slope(ma60, index),
                ma10_below_run=run10[index],
                ma20_below_run=run20[index],
                ma5_upward_inflection=upward_inflection(ma5, index),
                ma10_upward_inflection=upward_inflection(ma10, index),
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


def _last_inflection_within_run(
    points: Sequence[DailyMaPoint],
    *,
    signal_index: int,
    previous_run: int,
    field: str,
) -> int | None:
    if previous_run <= 0:
        return None
    run_start = signal_index - previous_run
    indexes = range(signal_index - 1, run_start - 1, -1)
    return next((index for index in indexes if getattr(points[index], field)), None)


def detect_buy_candidates(
    stock_code: str, points: Sequence[DailyMaPoint]
) -> tuple[BuyCandidate, ...]:
    """Detect all provisional candidates without resolving overlaps or rejects."""

    canonical = tuple(sorted(points, key=lambda point: point.trade_date))
    if any(
        point.trade_date == next_point.trade_date
        for point, next_point in pairwise(canonical)
    ):
        raise ValueError("Daily MA points must not contain duplicate dates")
    candidates: list[BuyCandidate] = []
    last_breakdown: int | None = None
    for index, point in enumerate(canonical):
        if point.ma10_breakdown:
            last_breakdown = index
        if index == 0:
            continue
        previous = canonical[index - 1]
        restriction_pass = (
            point.ma10_slope10_pct is not None
            and point.ma10_slope10_pct >= SLOPE_RESTRICTION_PCT
        )
        if point.ma10_breakout:
            inflection = _last_inflection_within_run(
                canonical,
                signal_index=index,
                previous_run=previous.ma10_below_run,
                field="ma5_upward_inflection",
            )
            if (
                previous.ma10_below_run >= BUY_A_MIN_BELOW_RUN
                and inflection is not None
            ):
                candidates.append(
                    _candidate(
                        stock_code,
                        point,
                        BuyType.A,
                        previous_ma10_run=previous.ma10_below_run,
                        previous_ma20_run=previous.ma20_below_run,
                        ma5_inflection_date=canonical[inflection].trade_date,
                        elapsed=index - inflection,
                        restriction_pass=restriction_pass,
                    )
                )
            if last_breakdown is not None:
                elapsed = index - last_breakdown
                if (
                    BUY_B_MIN_BARS_AFTER_BREAKDOWN
                    <= elapsed
                    <= BUY_B_MAX_BARS_AFTER_BREAKDOWN
                ):
                    candidates.append(
                        _candidate(
                            stock_code,
                            point,
                            BuyType.B,
                            previous_ma10_run=previous.ma10_below_run,
                            previous_ma20_run=previous.ma20_below_run,
                            breakdown_date=canonical[last_breakdown].trade_date,
                            bars_since_breakdown=elapsed,
                            restriction_pass=restriction_pass,
                        )
                    )
        if point.ma20_breakout:
            inflection = _last_inflection_within_run(
                canonical,
                signal_index=index,
                previous_run=previous.ma20_below_run,
                field="ma10_upward_inflection",
            )
            if (
                previous.ma20_below_run >= BUY_C_MIN_BELOW_RUN
                and inflection is not None
            ):
                candidates.append(
                    _candidate(
                        stock_code,
                        point,
                        BuyType.C,
                        previous_ma10_run=previous.ma10_below_run,
                        previous_ma20_run=previous.ma20_below_run,
                        ma10_inflection_date=canonical[inflection].trade_date,
                        elapsed=index - inflection,
                        restriction_pass=restriction_pass,
                    )
                )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (item.signal_date, item.stock_code, item.buy_type.value),
        )
    )


def _candidate(
    stock_code: str,
    point: DailyMaPoint,
    buy_type: BuyType,
    *,
    previous_ma10_run: int,
    previous_ma20_run: int,
    ma5_inflection_date: date | None = None,
    ma10_inflection_date: date | None = None,
    elapsed: int | None = None,
    breakdown_date: date | None = None,
    bars_since_breakdown: int | None = None,
    restriction_pass: bool,
) -> BuyCandidate:
    breakout_ma = 20 if buy_type is BuyType.C else 10
    return BuyCandidate(
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
        previous_ma10_below_run=previous_ma10_run,
        previous_ma20_below_run=previous_ma20_run,
        ma5_inflection_date=ma5_inflection_date,
        ma10_inflection_date=ma10_inflection_date,
        breakout_ma=breakout_ma,
        breakout_ratio=BREAKOUT_RATIO,
        elapsed_bars_inflection_to_breakout=elapsed,
        breakdown_date=breakdown_date,
        bars_since_breakdown=bars_since_breakdown,
        restriction_pass=restriction_pass,
    )


def select_signal_windows(
    candidates: Sequence[BuyCandidate],
) -> tuple[tuple[str, date, tuple[BuyCandidate, ...]], ...]:
    """Select at most two earliest candidates per type, preserving overlap."""

    selected: list[BuyCandidate] = []
    for buy_type in BuyType:
        selected.extend(
            sorted(
                (
                    candidate
                    for candidate in candidates
                    if candidate.buy_type is buy_type
                ),
                key=lambda item: (item.signal_date, item.stock_code),
            )[:MAX_SAMPLES_PER_TYPE]
        )
    grouped: dict[tuple[str, date], list[BuyCandidate]] = defaultdict(list)
    for candidate in selected:
        grouped[(candidate.stock_code, candidate.signal_date)].append(candidate)
    return tuple(
        (
            stock_code,
            signal_date,
            tuple(sorted(group, key=lambda item: item.buy_type.value)),
        )
        for (stock_code, signal_date), group in sorted(
            grouped.items(), key=lambda item: (item[0][1], item[0][0])
        )
    )


def _candidate_label(candidate: BuyCandidate) -> str:
    base = {BuyType.A: "A", BuyType.B: "B?", BuyType.C: "C"}[candidate.buy_type]
    return base if candidate.restriction_pass else f"X-{base}"


def _events_for_window(
    candidates: Sequence[BuyCandidate],
    points: Mapping[date, DailyMaPoint],
) -> tuple[ReviewEvent, ...]:
    signal_date = candidates[0].signal_date
    point = points[signal_date]
    labels = "+".join(_candidate_label(candidate) for candidate in candidates)
    events: list[ReviewEvent] = [
        ReviewEvent(
            ReviewEventType.DAILY_BUY_CANDIDATE,
            signal_date,
            labels,
            adjusted_plot_price=point.close,
            details={"research_only": True, "candidate_confirmed_at": "DAILY_T_CLOSE"},
        )
    ]
    for candidate in candidates:
        if candidate.ma5_inflection_date is not None:
            inflection = points[candidate.ma5_inflection_date]
            events.append(
                ReviewEvent(
                    ReviewEventType.MA5_TURN,
                    candidate.ma5_inflection_date,
                    "MA5 INFLECT",
                    adjusted_plot_price=inflection.ma5,
                )
            )
        if candidate.ma10_inflection_date is not None:
            inflection = points[candidate.ma10_inflection_date]
            events.append(
                ReviewEvent(
                    ReviewEventType.SMA10_REBREAK,
                    candidate.ma10_inflection_date,
                    "MA10 INFLECT",
                    adjusted_plot_price=inflection.ma10,
                )
            )
        if candidate.breakdown_date is not None:
            breakdown = points[candidate.breakdown_date]
            events.append(
                ReviewEvent(
                    ReviewEventType.SMA10_BREAKOUT,
                    candidate.breakdown_date,
                    "MA10 -2% BREAK",
                    adjusted_plot_price=breakdown.close,
                )
            )
        events.append(
            ReviewEvent(
                ReviewEventType.BOX_BREAKOUT
                if candidate.breakout_ma == 20
                else ReviewEventType.SMA10_BREAKOUT,
                signal_date,
                f"MA{candidate.breakout_ma} +2%",
                adjusted_plot_price=point.close,
            )
        )
    return tuple(events)


def _summary(candidates: Sequence[BuyCandidate]) -> dict[str, Any]:
    return {
        "proof_version": PROOF_VERSION,
        "signal_decision_at": "DAILY_T_CLOSE",
        "execution_reference_only": "T_PLUS_1_MARKET_OPEN",
        "execution_created": False,
        "orders_created": False,
        "network_calls": 0,
        "review_status": None,
        "candidates": [asdict(candidate) for candidate in candidates],
    }


def _json_default(value: object) -> str:
    if isinstance(value, (date, Decimal, Enum)):
        return (
            value.isoformat()
            if isinstance(value, date)
            else str(getattr(value, "value", value))
        )
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def assert_no_future_outcome_fields(payload: object) -> None:
    """Guard the research artifact against accidental outcome-based fields."""

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


def _in_research_period(candidates: Iterable[BuyCandidate]) -> tuple[BuyCandidate, ...]:
    return tuple(
        candidate
        for candidate in candidates
        if RESEARCH_START <= candidate.signal_date <= RESEARCH_END
    )


def generate_visual_proof(
    *,
    stock_codes: Sequence[str] = DEFAULT_STOCK_CODES,
    output_path: Path = OUTPUT_PATH,
    chart_root: Path = CHART_ROOT,
) -> dict[str, Any]:
    """Generate deterministic charts and metadata from local adjusted Daily cache."""

    selected_codes = tuple(sorted(set(stock_codes)))
    all_candidates: list[BuyCandidate] = []
    bars_by_code: dict[str, tuple[DailyBar, ...]] = {}
    points_by_code: dict[str, tuple[DailyMaPoint, ...]] = {}
    for stock_code in selected_codes:
        bars, _ = _load_stock(stock_code)
        canonical = tuple(sorted(bars, key=lambda bar: bar.trade_date))
        points = calculate_daily_ma_points(canonical)
        bars_by_code[stock_code] = canonical
        points_by_code[stock_code] = points
        all_candidates.extend(
            _in_research_period(detect_buy_candidates(stock_code, points))
        )
    candidates = tuple(
        sorted(
            all_candidates,
            key=lambda item: (item.signal_date, item.stock_code, item.buy_type.value),
        )
    )
    windows = select_signal_windows(candidates)
    chart_records: list[dict[str, Any]] = []
    for ordinal, (stock_code, signal_date, window_candidates) in enumerate(windows, 1):
        point_by_date = {
            point.trade_date: point for point in points_by_code[stock_code]
        }
        events = _events_for_window(window_candidates, point_by_date)
        calendar = ExplicitTradingCalendar(
            bar.trade_date for bar in bars_by_code[stock_code]
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
            )
            filename = f"{ordinal:02d}_{stock_code}_{signal_date.isoformat()}_{mode.lower()}.png"
            artifact = render_review_chart(
                prepared,
                chart_root / filename,
                strategy_policy="RESEARCH_ONLY_DAILY_MA_UNIFIED_BUY_V0_1",
                summary=_summary(window_candidates),
            )
            chart_records.append(
                {
                    "window_id": f"WINDOW_{ordinal:02d}",
                    "mode": mode,
                    "stock_code": stock_code,
                    "signal_date": signal_date,
                    "signal_labels": [
                        _candidate_label(candidate) for candidate in window_candidates
                    ],
                    "png_path": artifact.png_path.as_posix(),
                    "chart_metadata_path": artifact.metadata_path.as_posix(),
                    "render_backend": artifact.backend,
                }
            )
    counts = {
        buy_type.value: sum(candidate.buy_type is buy_type for candidate in candidates)
        for buy_type in BuyType
    }
    rejected = {
        buy_type.value: sum(
            candidate.buy_type is buy_type and not candidate.restriction_pass
            for candidate in candidates
        )
        for buy_type in BuyType
    }
    all_groups: dict[tuple[str, date], list[BuyCandidate]] = defaultdict(list)
    for candidate in candidates:
        all_groups[(candidate.stock_code, candidate.signal_date)].append(candidate)
    overlaps = Counter(
        "+".join(candidate.buy_type.value for candidate in group)
        for group in all_groups.values()
        if len(group) > 1
    )
    payload = {
        "proof_version": PROOF_VERSION,
        "scope": "VISUAL_RULE_INTERPRETATION_AUDIT_ONLY",
        "daily_input_source": "KIWOOM_ADJUSTED_DAILY_BARS",
        "moving_average_basis": "SIGNAL_ADJUSTED_DAILY_CLOSE",
        "research_period": {"start": RESEARCH_START, "end": RESEARCH_END},
        "stock_codes": list(selected_codes),
        "sample_selection_rule": "per BUY type: earliest candidate, then next earliest; group same stock/date overlaps without priority",
        "provisional_inflection_definition": "MA[T] > MA[T-1] AND MA[T-1] <= MA[T-2]",
        "buy_b_provisional_boundary": "confirmed MA10 breakdown then MA10 +2% breakout after 1 through 10 completed Daily bars inclusive",
        "slope_restriction": {
            "ma10_slope10_pct_gte": SLOPE_RESTRICTION_PCT,
            "rejected_not_deleted": True,
        },
        "candidate_counts": counts,
        "slope_rejected_counts": rejected,
        "selected_signal_window_count": len(windows),
        "overlap_counts": dict(sorted(overlaps.items())),
        "selected_windows": [
            {
                "stock_code": stock_code,
                "signal_date": signal_date,
                "candidates": [asdict(candidate) for candidate in group],
            }
            for stock_code, signal_date, group in windows
        ],
        "charts": chart_records,
        "review_status_allowed_values": [
            "VISUALLY_MATCHES_INTENT",
            "VISUALLY_TOO_EARLY",
            "VISUALLY_TOO_LATE",
            "INFLECTION_MISMATCH",
            "BREAKOUT_MISMATCH",
            "AMBIGUOUS",
        ],
        "network_calls": 0,
        "orders_created": 0,
        "future_outcome_evaluated": False,
        "forbidden_outcome_fields": sorted(FORBIDDEN_OUTCOME_FIELDS),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    assert_no_future_outcome_fields(payload)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-code", action="append", dest="stock_codes")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--chart-root", type=Path, default=CHART_ROOT)
    args = parser.parse_args()
    payload = generate_visual_proof(
        stock_codes=tuple(args.stock_codes)
        if args.stock_codes
        else DEFAULT_STOCK_CODES,
        output_path=args.output,
        chart_root=args.chart_root,
    )
    print(
        json.dumps(
            {
                "candidate_counts": payload["candidate_counts"],
                "charts": len(payload["charts"]),
            }
        )
    )


if __name__ == "__main__":
    main()
