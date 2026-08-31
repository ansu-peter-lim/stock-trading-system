"""Strategy V1 daily trend and the intentionally narrow UP-path Core MVP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum

from .events import stable_id
from .execution import OrderSide
from .indicators import DailyIndicatorPoint
from .models import DailyBar
from .trading_calendar import TradingCalendar
from .validation import KOREA_TZ


class CoreStrategyValidationError(ValueError):
    """Daily Core input or configuration violates the V1 contract."""


class DailyTrendState(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    MIXED = "MIXED"
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class DailyCoreSignalType(str, Enum):
    ENTER = "DAILY_ENTRY_SIGNAL"
    FULL_EXIT = "DAILY_FULL_EXIT_SIGNAL"


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    strategy_version: str = "V1"
    uptrend_ma20_band_pct: Decimal = Decimal(3)
    core_fraction_of_full: Decimal = Decimal("0.90")

    def __post_init__(self) -> None:
        if self.strategy_version != "V1":
            raise CoreStrategyValidationError("this MVP supports Strategy V1 only")
        if not isinstance(self.uptrend_ma20_band_pct, Decimal) or not Decimal(
            0
        ) <= self.uptrend_ma20_band_pct < Decimal(100):
            raise CoreStrategyValidationError(
                "uptrend_ma20_band_pct must be Decimal in [0, 100)"
            )
        if not isinstance(self.core_fraction_of_full, Decimal) or not Decimal(
            0
        ) < self.core_fraction_of_full <= Decimal(1):
            raise CoreStrategyValidationError(
                "core_fraction_of_full must be Decimal in (0, 1]"
            )


@dataclass(frozen=True, slots=True)
class DailyCoreSignal:
    signal_id: str
    stock_code: str
    signal_type: DailyCoreSignalType
    side: OrderSide
    signal_generated_at: datetime
    signal_available_at: datetime
    generated_trade_date: date
    activation_trade_date: date
    stock_full_weight: Decimal
    target_core_weight: Decimal
    trend_state: DailyTrendState
    reason: str
    signal_low: Decimal
    signal_close: Decimal
    signal_sma20: Decimal


class DailyTrendClassifier:
    """Classify only from already trailing, signal-price indicator values."""

    def classify(self, point: DailyIndicatorPoint) -> DailyTrendState:
        slope20 = point.ma20_slope_5
        slope60 = point.ma60_slope_5
        if slope20 is None or slope60 is None:
            return DailyTrendState.INSUFFICIENT_DATA
        if slope20 == 0 or slope60 == 0:
            return DailyTrendState.NEUTRAL
        if slope20 > 0 and slope60 > 0:
            return DailyTrendState.UP
        if slope20 < 0 and slope60 < 0:
            return DailyTrendState.DOWN
        return DailyTrendState.MIXED


def is_uptrend_ma20_near(
    *,
    signal_low: Decimal,
    signal_close: Decimal,
    signal_ma20: Decimal,
    band_pct: Decimal = Decimal(3),
) -> bool:
    """Apply the inclusive V1 low-or-close adjusted-price band contract."""

    if not all(
        isinstance(value, Decimal)
        for value in (signal_low, signal_close, signal_ma20, band_pct)
    ):
        raise CoreStrategyValidationError("MA20 band inputs must be Decimal")
    if signal_ma20 <= 0 or not Decimal(0) <= band_pct < Decimal(100):
        raise CoreStrategyValidationError("invalid MA20 band inputs")
    ratio = band_pct / Decimal(100)
    lower = signal_ma20 * (Decimal(1) - ratio)
    upper = signal_ma20 * (Decimal(1) + ratio)
    return lower <= signal_low <= upper or lower <= signal_close <= upper


def core_target_weight(
    stock_full_weight: Decimal,
    core_fraction_of_full: Decimal = Decimal("0.90"),
) -> Decimal:
    if not isinstance(stock_full_weight, Decimal) or not Decimal(
        0
    ) < stock_full_weight <= Decimal(1):
        raise CoreStrategyValidationError("stock_full_weight must be Decimal in (0, 1]")
    if not isinstance(core_fraction_of_full, Decimal) or not Decimal(
        0
    ) < core_fraction_of_full <= Decimal(1):
        raise CoreStrategyValidationError(
            "core_fraction_of_full must be Decimal in (0, 1]"
        )
    return stock_full_weight * core_fraction_of_full


class DailyCoreSignalGenerator:
    """Generate only the phase-3 UP-path entry and holding exit signals."""

    def __init__(
        self,
        calendar: TradingCalendar,
        config: StrategyConfig | None = None,
        classifier: DailyTrendClassifier | None = None,
    ) -> None:
        self._calendar = calendar
        self.config = config or StrategyConfig()
        self._classifier = classifier or DailyTrendClassifier()

    def evaluate(
        self,
        bar: DailyBar,
        point: DailyIndicatorPoint,
        *,
        holding_core: bool,
        stock_full_weight: Decimal,
    ) -> DailyCoreSignal | None:
        if bar.stock_code != point.stock_code or bar.trade_date != point.trade_date:
            raise CoreStrategyValidationError("daily bar and indicator are not aligned")
        target_weight = core_target_weight(
            stock_full_weight, self.config.core_fraction_of_full
        )
        trend = self._classifier.classify(point)
        if point.sma20 is None:
            return None

        signal_type: DailyCoreSignalType | None = None
        side: OrderSide | None = None
        reason = ""
        signal_target = target_weight
        if holding_core and bar.signal.close < point.sma20:
            signal_type = DailyCoreSignalType.FULL_EXIT
            side = OrderSide.SELL
            reason = "HOLDING_CORE_AND_SIGNAL_CLOSE_BELOW_DAILY_SMA20"
            signal_target = Decimal(0)
        elif (
            not holding_core
            and trend is DailyTrendState.UP
            and is_uptrend_ma20_near(
                signal_low=bar.signal.low,
                signal_close=bar.signal.close,
                signal_ma20=point.sma20,
                band_pct=self.config.uptrend_ma20_band_pct,
            )
        ):
            signal_type = DailyCoreSignalType.ENTER
            side = OrderSide.BUY
            reason = "UP_AND_SIGNAL_LOW_OR_CLOSE_WITHIN_DAILY_SMA20_BAND"
        if signal_type is None or side is None:
            return None

        generated_at = datetime.combine(bar.trade_date, time(15, 30), tzinfo=KOREA_TZ)
        activation_date = self._calendar.next_trading_day(bar.trade_date)
        signal_id = stable_id(
            "daily_core_signal",
            bar.stock_code,
            bar.trade_date.isoformat(),
            signal_type.value,
            stock_full_weight,
            signal_target,
            bar.signal.low,
            bar.signal.close,
            point.sma20,
        )
        return DailyCoreSignal(
            signal_id,
            bar.stock_code,
            signal_type,
            side,
            generated_at,
            generated_at,
            bar.trade_date,
            activation_date,
            stock_full_weight,
            signal_target,
            trend,
            reason,
            bar.signal.low,
            bar.signal.close,
            point.sma20,
        )
