from datetime import UTC, datetime, timedelta

import pytest

from okx_ai_quant.models import SignalDirection
from okx_ai_quant.portfolio import (
    ExposureLimits,
    evaluate_drawdown,
    exposure_block_reason,
)
from okx_ai_quant.risk import RiskGuard, RiskState, RiskStatus
from okx_ai_quant.strategy import StrategySignal


def test_drawdown_updates_high_water_mark_and_trips_at_limit():
    status = evaluate_drawdown(equity=110.0, high_water_mark=100.0, max_drawdown=0.10)
    assert status.high_water_mark == 110.0
    assert not status.tripped

    status = evaluate_drawdown(equity=99.0, high_water_mark=110.0, max_drawdown=0.10)
    assert status.high_water_mark == 110.0
    assert status.drawdown == pytest.approx(0.10)
    assert status.tripped


def test_drawdown_ignores_unknown_equity():
    status = evaluate_drawdown(equity=0.0, high_water_mark=100.0, max_drawdown=0.10)
    assert not status.tripped
    assert status.high_water_mark == 100.0


def test_exposure_total_cap_blocks():
    reason = exposure_block_reason(
        equity=1000.0,
        long_notional=300.0,
        short_notional=50.0,
        candidate_notional=100.0,
        candidate_direction=SignalDirection.LONG,
        limits=ExposureLimits(max_total_rate=0.40, max_net_rate=1.0),
    )
    assert reason is not None and "Total exposure" in reason


def test_exposure_net_cap_blocks_correlated_stacking():
    # 200 long vs 0 short: adding 100 more long breaches a 25% net cap.
    reason = exposure_block_reason(
        equity=1000.0,
        long_notional=200.0,
        short_notional=0.0,
        candidate_notional=100.0,
        candidate_direction=SignalDirection.LONG,
        limits=ExposureLimits(max_total_rate=1.0, max_net_rate=0.25),
    )
    assert reason is not None and "Net directional" in reason

    # A short against the long book reduces net exposure: allowed.
    assert (
        exposure_block_reason(
            equity=1000.0,
            long_notional=200.0,
            short_notional=0.0,
            candidate_notional=100.0,
            candidate_direction=SignalDirection.SHORT,
            limits=ExposureLimits(max_total_rate=1.0, max_net_rate=0.25),
        )
        is None
    )


def _signal(*, entry=100.0, stop=95.0) -> StrategySignal:
    return StrategySignal(
        symbol="BTC-USDT-SWAP",
        timeframe="1H",
        direction=SignalDirection.LONG,
        confidence=0.8,
        reason="test",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        expected_move=0.05,
        entry_price=entry,
        stop_price=stop,
        target_price=110.0,
    )


def _guard(**overrides) -> RiskGuard:
    values = {
        "symbols": ["BTC-USDT-SWAP"],
        "reference_capital_usdt": 1000.0,
        "max_positions": 5,
    }
    values.update(overrides)
    return RiskGuard(**values)


def test_sizing_uses_live_equity_over_reference_capital():
    guard = _guard()
    decision = guard.evaluate(_signal(), RiskState(equity_usdt=500.0))
    # 10% notional cap of live equity 500, not of reference 1000.
    assert decision.status == RiskStatus.APPROVED
    assert decision.position_size_usdt == pytest.approx(50.0)

    fallback = guard.evaluate(_signal(), RiskState())
    assert fallback.position_size_usdt == pytest.approx(100.0)


def test_loss_streak_halves_risk_budget():
    guard = _guard(loss_streak_days=2, loss_streak_risk_multiplier=0.5)
    normal = guard.evaluate(_signal(), RiskState(equity_usdt=1000.0))
    reduced = guard.evaluate(
        _signal(),
        RiskState(equity_usdt=1000.0, consecutive_losing_days=2),
    )
    assert reduced.position_size_usdt == pytest.approx(normal.position_size_usdt / 2)


def test_storage_reports_equity_and_losing_day_streak(tmp_path):
    from okx_ai_quant.models import BalanceSnapshot, ExitReason, PositionExitRecord
    from okx_ai_quant.storage import SQLiteStorage

    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()
        now = datetime.now(UTC)
        storage.upsert_balance(
            BalanceSnapshot(currency="USDT", available=900.0, equity=950.0, updated_at=now)
        )
        for days_ago, pnl in ((2, -30.0), (1, -10.0)):
            closed = now - timedelta(days=days_ago)
            storage.insert_position_exit(
                PositionExitRecord(
                    position_id=None,
                    symbol="BTC-USDT-SWAP",
                    side=SignalDirection.LONG,
                    reason=ExitReason.STOP_LOSS,
                    entry_price=100.0,
                    exit_price=90.0,
                    quantity=1.0,
                    realized_pnl=pnl,
                    opened_at=closed,
                    closed_at=closed,
                    notes="loss",
                )
            )

        state = storage.load_risk_state()
        assert state.equity_usdt == pytest.approx(950.0)
        assert state.consecutive_losing_days == 2


def test_equity_snapshot_series_roundtrip(tmp_path):
    from okx_ai_quant.storage import SQLiteStorage

    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()
        base = datetime(2026, 7, 1, tzinfo=UTC)
        for hour, equity in enumerate((100.0, 105.0, 95.0)):
            storage.insert_equity_snapshot(
                currency="USDT",
                equity=equity,
                available=equity,
                created_at=base + timedelta(hours=hour),
            )

        series = storage.load_equity_series("USDT")
        assert [value for _, value in series] == [100.0, 105.0, 95.0]
