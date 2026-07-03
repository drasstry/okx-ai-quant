"""Portfolio-level risk controls shared by the live bot and the backtester.

These are the account-preservation rules that sit above per-trade risk:

- **Drawdown kill switch**: once equity falls a configured fraction below its
  high-water mark, flatten everything and halt new entries until a human
  resumes (which also re-bases the high-water mark to current equity).
- **Exposure caps**: total open notional and *net directional* notional are
  limited as a fraction of equity. Crypto majors are highly correlated, so
  five same-direction positions behave like one large one — the net cap is
  what actually bounds that.

Everything here is a pure function so the backtest engine and the live bot
cannot drift apart in semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from okx_ai_quant.models import SignalDirection


@dataclass(frozen=True, kw_only=True)
class ExposureLimits:
    max_total_rate: float = 0.40
    max_net_rate: float = 0.25


@dataclass(frozen=True, kw_only=True)
class DrawdownStatus:
    high_water_mark: float
    drawdown: float
    tripped: bool


def evaluate_drawdown(
    *,
    equity: float,
    high_water_mark: float,
    max_drawdown: float,
) -> DrawdownStatus:
    """Update the high-water mark and decide whether the kill switch trips."""
    if not isfinite(equity) or equity <= 0:
        return DrawdownStatus(high_water_mark=high_water_mark, drawdown=0.0, tripped=False)
    hwm = max(high_water_mark, equity)
    drawdown = 1.0 - (equity / hwm) if hwm > 0 else 0.0
    # Epsilon so an exact-boundary drawdown (e.g. 99/110 for a 10% limit)
    # is not saved by one ulp of floating point noise.
    return DrawdownStatus(
        high_water_mark=hwm,
        drawdown=drawdown,
        tripped=drawdown >= max_drawdown - 1e-12,
    )


def exposure_block_reason(
    *,
    equity: float,
    long_notional: float,
    short_notional: float,
    candidate_notional: float,
    candidate_direction: SignalDirection,
    limits: ExposureLimits,
) -> str | None:
    """Return a rejection reason when adding the candidate would breach a cap."""
    if not isfinite(equity) or equity <= 0:
        return None  # unknown equity: fall back to per-trade limits only

    new_long = long_notional + (
        candidate_notional if candidate_direction == SignalDirection.LONG else 0.0
    )
    new_short = short_notional + (
        candidate_notional if candidate_direction == SignalDirection.SHORT else 0.0
    )
    total_rate = (new_long + new_short) / equity
    net_rate = abs(new_long - new_short) / equity

    if total_rate > limits.max_total_rate:
        return (
            f"Total exposure {total_rate:.2%} would exceed cap "
            f"{limits.max_total_rate:.2%}."
        )
    if net_rate > limits.max_net_rate:
        return (
            f"Net directional exposure {net_rate:.2%} would exceed cap "
            f"{limits.max_net_rate:.2%} (correlated book counts as one bet)."
        )
    return None
