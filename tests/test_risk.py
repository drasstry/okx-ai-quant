import math
from datetime import UTC, datetime

import pytest

from okx_ai_quant.models import RiskStatus, Signal, SignalDirection
from okx_ai_quant.risk import RiskGuard, RiskState


def _signal(
    *,
    symbol: str = "BTC-USDT-SWAP",
    direction: SignalDirection = SignalDirection.LONG,
) -> Signal:
    return Signal(
        symbol=symbol,
        timeframe="1H",
        direction=direction,
        confidence=0.75,
        reason="Test signal.",
        created_at=datetime(2026, 4, 29, 8, 0, tzinfo=UTC),
        id=42,
    )


def test_rejects_non_whitelisted_symbol():
    guard = RiskGuard(symbols={"BTC-USDT-SWAP"}, reference_capital_usdt=1000.0)

    decision = guard.evaluate(_signal(symbol="ETH-USDT-SWAP"), RiskState())

    assert decision.status == RiskStatus.REJECTED
    assert decision.position_size_usdt == 0.0
    assert "whitelist" in decision.reason.lower()


def test_rejects_loss_streak():
    guard = RiskGuard(symbols={"BTC-USDT-SWAP"}, reference_capital_usdt=1000.0)

    decision = guard.evaluate(_signal(), RiskState(consecutive_losses=3))

    assert decision.status == RiskStatus.REJECTED
    assert "consecutive" in decision.reason.lower()


def test_approves_valid_long_signal_with_position_size_and_leverage():
    guard = RiskGuard(
        symbols={"BTC-USDT-SWAP"},
        reference_capital_usdt=1000.0,
        leverage=3,
    )

    decision = guard.evaluate(_signal(direction=SignalDirection.LONG), RiskState())

    assert decision.status == RiskStatus.APPROVED
    assert decision.position_size_usdt == 100.0
    assert decision.leverage == 3
    assert decision.signal_id == 42


def test_approved_position_size_shrinks_with_stricter_max_risk_per_trade():
    guard = RiskGuard(
        symbols={"BTC-USDT-SWAP"},
        reference_capital_usdt=1000.0,
        max_risk_per_trade=0.005,
    )

    decision = guard.evaluate(_signal(direction=SignalDirection.LONG), RiskState())

    assert decision.status == RiskStatus.APPROVED
    assert decision.position_size_usdt == 50.0


def test_rejects_hold_signal():
    guard = RiskGuard(symbols={"BTC-USDT-SWAP"}, reference_capital_usdt=1000.0)

    decision = guard.evaluate(_signal(direction=SignalDirection.HOLD), RiskState())

    assert decision.status == RiskStatus.REJECTED
    assert "long/short" in decision.reason.lower()


def test_rejects_daily_loss_limit():
    guard = RiskGuard(symbols={"BTC-USDT-SWAP"}, reference_capital_usdt=1000.0)

    decision = guard.evaluate(_signal(), RiskState(daily_loss_rate=0.02))

    assert decision.status == RiskStatus.REJECTED
    assert "daily loss" in decision.reason.lower()


def test_rejects_non_finite_daily_loss_rate():
    guard = RiskGuard(symbols={"BTC-USDT-SWAP"}, reference_capital_usdt=1000.0)

    decision = guard.evaluate(_signal(), RiskState(daily_loss_rate=math.nan))

    assert decision.status == RiskStatus.REJECTED
    assert "daily loss" in decision.reason.lower()


def test_rejects_max_positions():
    guard = RiskGuard(symbols={"BTC-USDT-SWAP"}, reference_capital_usdt=1000.0)

    decision = guard.evaluate(_signal(), RiskState(open_positions=1))

    assert decision.status == RiskStatus.REJECTED
    assert "position" in decision.reason.lower()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_risk_per_trade": 0.0}, "max_risk_per_trade"),
        ({"reference_capital_usdt": 0.0}, "reference_capital_usdt"),
        ({"max_daily_loss": 0.0}, "max_daily_loss"),
        ({"max_consecutive_losses": 0}, "max_consecutive_losses"),
        ({"max_positions": 0}, "max_positions"),
        ({"leverage": 0}, "leverage"),
        ({"reference_capital_usdt": math.nan}, "reference_capital_usdt"),
        ({"max_risk_per_trade": math.nan}, "max_risk_per_trade"),
        ({"max_daily_loss": math.nan}, "max_daily_loss"),
        ({"max_consecutive_losses": math.nan}, "max_consecutive_losses"),
        ({"max_positions": math.nan}, "max_positions"),
        ({"leverage": math.nan}, "leverage"),
        ({"max_consecutive_losses": True}, "max_consecutive_losses"),
        ({"max_positions": True}, "max_positions"),
        ({"leverage": True}, "leverage"),
        ({"max_consecutive_losses": 1.5}, "max_consecutive_losses"),
        ({"max_positions": 1.5}, "max_positions"),
        ({"leverage": 1.5}, "leverage"),
    ],
)
def test_risk_guard_rejects_invalid_constructor_parameters(kwargs, message):
    params = {
        "symbols": {"BTC-USDT-SWAP"},
        "reference_capital_usdt": 1000.0,
    }
    params.update(kwargs)

    with pytest.raises(ValueError, match=message):
        RiskGuard(**params)
