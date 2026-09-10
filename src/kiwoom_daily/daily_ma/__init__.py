"""Small, stable primitives for new Daily MA research only."""

from .core import (
    structural_upward_inflection_events,
    structural_upward_inflection_state,
)
from .data import load_local_daily_bars, slice_daily_bars

__all__ = (
    "load_local_daily_bars",
    "slice_daily_bars",
    "structural_upward_inflection_events",
    "structural_upward_inflection_state",
)
