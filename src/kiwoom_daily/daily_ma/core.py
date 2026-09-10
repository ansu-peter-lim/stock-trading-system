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
    *,
    ratio: Decimal = Decimal("1.02"),
) -> bool:
    if ratio <= 0:
        raise ValueError("breakout ratio must be positive")
    return (
        ma is not None
        and previous_close is not None
        and previous_ma is not None
        and close >= ma * ratio
        and previous_close < previous_ma * ratio
    )


def confirmed_breakdown(
    close: Decimal,
    ma: Decimal | None,
    previous_close: Decimal | None,
    previous_ma: Decimal | None,
    *,
    ratio: Decimal = Decimal("0.98"),
) -> bool:
    if ratio <= 0:
        raise ValueError("breakdown ratio must be positive")
    return (
        ma is not None
        and previous_close is not None
        and previous_ma is not None
        and close <= ma * ratio
        and previous_close > previous_ma * ratio
    )


def structural_upward_inflection_state(
    values: Sequence[Decimal | None], index: int, *, lookback: int
) -> bool:
    """Return the structural upward-inflection state at ``index``.

    The reference low is the most recent minimum in the inclusive trailing
    ``lookback`` MA-value window.  A state requires the current value to be
    above that low, with no intervening value above the current one.  Missing
    values or incomplete windows are unavailable rather than inferred.
    """

    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if index < lookback - 1:
        return False
    window = values[index - lookback + 1 : index + 1]
    if len(window) != lookback or any(value is None for value in window):
        return False
    numeric = tuple(value for value in window if value is not None)
    minimum = min(numeric)
    local_low_index = max(
        offset for offset, value in enumerate(numeric) if value == minimum
    )
    current = numeric[-1]
    if current <= minimum:
        return False
    return all(value <= current for value in numeric[local_low_index + 1 : -1])


def structural_upward_inflection_events(
    values: Sequence[Decimal | None], *, lookback: int
) -> tuple[bool, ...]:
    """Emit only false-to-true transitions of the structural state."""

    states = tuple(
        structural_upward_inflection_state(values, index, lookback=lookback)
        for index in range(len(values))
    )
    return tuple(
        state and (index == 0 or not states[index - 1])
        for index, state in enumerate(states)
    )
