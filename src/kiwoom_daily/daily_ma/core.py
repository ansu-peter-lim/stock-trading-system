"""Stable, outcome-free Daily MA primitives for future research runs."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from src.backtest_engine.indicators import simple_moving_average


def moving_averages(
    closes: Sequence[Decimal], periods: Sequence[int] = (5, 10, 20, 60, 120)
) -> dict[int, tuple[Decimal | None, ...]]:
    return {period: tuple(simple_moving_average(closes, period)) for period in periods}


def ma_percentage_change(
    values: Sequence[Decimal | None], index: int, *, lookback: int = 10
) -> Decimal | None:
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if index < lookback:
        return None
    current, previous = values[index], values[index - lookback]
    if current is None or previous is None or previous == 0:
        return None
    return (current / previous - 1) * 100


def consecutive_below_ma_runs(
    closes: Sequence[Decimal], ma: Sequence[Decimal | None]
) -> tuple[int, ...]:
    if len(closes) != len(ma):
        raise ValueError("close and MA sequences must have equal length")
    count, output = 0, []
    for close, current_ma in zip(closes, ma, strict=True):
        count = count + 1 if current_ma is not None and close < current_ma else 0
        output.append(count)
    return tuple(output)


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
        and close >= ma * Decimal("1.02")
        and previous_close < previous_ma * Decimal("1.02")
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
        and close <= ma * Decimal("0.98")
        and previous_close > previous_ma * Decimal("0.98")
    )
