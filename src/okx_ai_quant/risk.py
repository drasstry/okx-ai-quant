from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Iterable

from okx_ai_quant.models import RiskStatus, Signal, SignalDirection


def _validate_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not isfinite(value) or value < 1:
        raise ValueError(f"{name} must be an integer greater than or equal to 1")


@dataclass(frozen=True, kw_only=True)
class RiskState:
    daily_loss_rate: float = 0.0
    consecutive_losses: int = 0
    open_positions: int = 0
    equity_usdt: float = 0.0  # live account equity; 0 = unknown, fall back to reference capital
    consecutive_losing_days: int = 0


@dataclass(frozen=True, kw_only=True)
class RiskDecision:
    signal_id: int | None
    status: RiskStatus
    reason: str
    position_size_usdt: float
    leverage: int
    created_at: datetime
    id: int | None = None


class RiskGuard:
    def __init__(
        self,
        *,
        symbols: Iterable[str],
        reference_capital_usdt: float,
        max_risk_per_trade: float = 0.01,
        max_daily_loss: float = 0.02,
        max_consecutive_losses: int = 3,
        max_positions: int = 1,
        leverage: int = 1,
        loss_streak_days: int = 2,
        loss_streak_risk_multiplier: float = 0.5,
    ) -> None:
        if not isfinite(reference_capital_usdt) or reference_capital_usdt <= 0:
            raise ValueError("reference_capital_usdt must be greater than 0")
        if not isfinite(max_risk_per_trade) or max_risk_per_trade <= 0:
            raise ValueError("max_risk_per_trade must be greater than 0")
        if not isfinite(max_daily_loss) or max_daily_loss <= 0:
            raise ValueError("max_daily_loss must be greater than 0")
        _validate_positive_int(max_consecutive_losses, "max_consecutive_losses")
        _validate_positive_int(max_positions, "max_positions")
        _validate_positive_int(leverage, "leverage")
        _validate_positive_int(loss_streak_days, "loss_streak_days")
        if (
            not isfinite(loss_streak_risk_multiplier)
            or not 0 < loss_streak_risk_multiplier <= 1
        ):
            raise ValueError("loss_streak_risk_multiplier must be in (0, 1]")

        self.symbols = frozenset(symbols)
        self.reference_capital_usdt = reference_capital_usdt
        self.max_risk_per_trade = max_risk_per_trade
        self.max_daily_loss = max_daily_loss
        self.max_consecutive_losses = max_consecutive_losses
        self.max_positions = max_positions
        self.leverage = leverage
        self.loss_streak_days = loss_streak_days
        self.loss_streak_risk_multiplier = loss_streak_risk_multiplier

    def evaluate(self, signal: Signal, state: RiskState) -> RiskDecision:
        if signal.symbol not in self.symbols:
            return self._reject(signal, f"Symbol {signal.symbol} is not in the whitelist.")

        if signal.direction not in {SignalDirection.LONG, SignalDirection.SHORT}:
            return self._reject(signal, "Only LONG/SHORT signals can be approved.")

        if not isfinite(state.daily_loss_rate) or state.daily_loss_rate >= self.max_daily_loss:
            return self._reject(signal, "Daily loss limit reached.")

        if state.consecutive_losses >= self.max_consecutive_losses:
            return self._reject(signal, "Maximum consecutive losses reached.")

        if state.open_positions >= self.max_positions:
            return self._reject(signal, "Maximum open position limit reached.")

        position_size_usdt, reason = self._position_size(signal, state)
        leverage = self._leverage_for_signal(signal)
        return RiskDecision(
            signal_id=signal.id,
            status=RiskStatus.APPROVED,
            reason=reason,
            position_size_usdt=position_size_usdt,
            leverage=leverage,
            created_at=datetime.now(UTC),
        )

    def _risk_capital(self, state: RiskState) -> float:
        """Live equity when known, otherwise the configured reference capital.

        Sizing off live equity makes risk shrink in a drawdown and compound
        in a run-up; a static reference kept sizing full-size while the
        account bled.
        """
        if isfinite(state.equity_usdt) and state.equity_usdt > 0:
            return state.equity_usdt
        return self.reference_capital_usdt

    def _risk_multiplier(self, state: RiskState) -> float:
        if state.consecutive_losing_days >= self.loss_streak_days:
            return self.loss_streak_risk_multiplier
        return 1.0

    def _position_size(self, signal: Signal, state: RiskState) -> tuple[float, str]:
        """Compute notional USDT exposure for an approved signal.

        - Capital base: live equity (fallback: reference capital), scaled by
          the loss-streak multiplier after consecutive losing days.
        - Default: ``capital * min(10 * max_risk_per_trade, 0.10)`` when no
          stop is provided.
        - When the signal carries a ``stop_price``, scale the notional so an
          adverse move to the stop consumes at most ``max_risk_per_trade`` of
          capital, capped by the default to avoid accidental up-sizing.
        """
        capital = self._risk_capital(state) * self._risk_multiplier(state)
        default_size = capital * min(0.10, self.max_risk_per_trade * 10)
        default_reason = "Risk checks passed."

        stop_price = getattr(signal, "stop_price", None)
        entry_price = getattr(signal, "entry_price", None)

        if (
            entry_price is None
            or stop_price is None
            or not isfinite(stop_price)
            or not isfinite(entry_price)
        ):
            return default_size, default_reason

        risk_distance = abs(entry_price - stop_price)
        if risk_distance <= 0 or not isfinite(risk_distance) or entry_price <= 0:
            return default_size, default_reason

        loss_fraction_at_stop = risk_distance / entry_price
        if loss_fraction_at_stop <= 0:
            return default_size, default_reason

        risk_budget = capital * self.max_risk_per_trade
        sized = risk_budget / loss_fraction_at_stop
        if not isfinite(sized) or sized <= 0:
            return default_size, default_reason

        capped = min(sized, default_size)
        return capped, (
            "Risk checks passed; position sized by stop distance."
            if capped < default_size
            else default_reason
        )

    def _reject(self, signal: Signal, reason: str) -> RiskDecision:
        return RiskDecision(
            signal_id=signal.id,
            status=RiskStatus.REJECTED,
            reason=reason,
            position_size_usdt=0.0,
            leverage=self._leverage_for_signal(signal),
            created_at=datetime.now(UTC),
        )

    def _leverage_for_signal(self, signal: Signal) -> int:
        recommended = getattr(signal, "recommended_leverage", None)
        if recommended is None:
            return self.leverage
        if (
            isinstance(recommended, bool)
            or not isinstance(recommended, int)
            or not isfinite(recommended)
            or recommended < 1
        ):
            return self.leverage
        return min(self.leverage, recommended)
