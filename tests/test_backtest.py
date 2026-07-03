from datetime import UTC, datetime, timedelta

import pytest

from okx_ai_quant.backtest import (
    BacktestConfig,
    BacktestEngine,
    SymbolHistory,
)
from okx_ai_quant.backtest_report import render_backtest_html, render_backtest_summary
from okx_ai_quant.models import Candle, SignalDirection
from okx_ai_quant.strategy import StrategySignal

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
SYMBOL = "BTC-USDT-SWAP"


def _candle(index: int, *, open_=100.0, high=100.5, low=99.5, close=100.0, symbol=SYMBOL) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe="1H",
        timestamp=T0 + timedelta(hours=index),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=10.0,
    )


class StubStrategy:
    """Emit one directional signal at a given hour index, HOLD otherwise."""

    def __init__(
        self,
        *,
        direction=SignalDirection.LONG,
        fire_at: set[int] | None = None,
        stop=95.0,
        target=105.0,
        entry=100.0,
    ) -> None:
        self.direction = direction
        self.fire_at = fire_at if fire_at is not None else {0}
        self.stop = stop
        self.target = target
        self.entry = entry
        self.calls: list[tuple[str, int]] = []

    def generate(self, symbol, one_hour, four_hour):
        index = int((one_hour[-1].timestamp - T0).total_seconds() // 3600)
        self.calls.append((symbol, index))
        if index in self.fire_at:
            return StrategySignal(
                symbol=symbol,
                timeframe="1H",
                direction=self.direction,
                confidence=0.8,
                reason="stub",
                created_at=one_hour[-1].timestamp,
                expected_move=0.05,
                entry_price=self.entry,
                stop_price=self.stop,
                target_price=self.target,
            )
        return StrategySignal(
            symbol=symbol,
            timeframe="1H",
            direction=SignalDirection.HOLD,
            confidence=0.0,
            reason="hold",
            created_at=one_hour[-1].timestamp,
            expected_move=0.0,
            stop_price=None,
            target_price=None,
        )


def _config(**overrides) -> BacktestConfig:
    values = {
        "symbols": [SYMBOL],
        "initial_capital_usdt": 1000.0,
        "fee_rate_per_side": 0.001,
        "slippage_rate": 0.0,
        "exit_on_reverse_signal": True,
    }
    values.update(overrides)
    return BacktestConfig(**values)


def _engine(candles, strategy, **config_overrides) -> BacktestEngine:
    config = _config(**config_overrides)
    return BacktestEngine(
        strategy_name="stub",
        config=config,
        data={SYMBOL: SymbolHistory(one_hour=candles, four_hour=[], funding=[])},
        strategy=strategy,
    )


def test_take_profit_fills_intrabar_at_target():
    candles = [
        _candle(0),
        _candle(1, high=106.0, close=104.0),
        _candle(2),
    ]
    engine = _engine(candles, StubStrategy())
    result = engine.run()

    trade = result.trades[0]
    assert trade.exit_reason == "TAKE_PROFIT"
    assert trade.exit_price == pytest.approx(105.0)
    # Sized at the 10% notional cap: 100 USDT -> qty 1.0 at entry 100.
    assert trade.quantity == pytest.approx(1.0)
    assert trade.price_pnl == pytest.approx(5.0)
    # Fees: 0.1% of 100 at entry + 0.1% of 105 at exit.
    assert trade.fees == pytest.approx(0.1 + 0.105)
    assert result.final_equity == pytest.approx(1000.0 + 5.0 - 0.205)


def test_stop_wins_when_stop_and_target_hit_in_same_candle():
    candles = [
        _candle(0),
        _candle(1, high=106.0, low=94.0, close=100.0),
    ]
    engine = _engine(candles, StubStrategy())
    result = engine.run()

    trade = result.trades[0]
    assert trade.exit_reason == "STOP_LOSS"
    assert trade.exit_price == pytest.approx(95.0)
    assert trade.price_pnl == pytest.approx(-5.0)


def test_short_stop_uses_candle_high():
    candles = [
        _candle(0),
        _candle(1, high=103.5, close=103.0),
    ]
    engine = _engine(candles, StubStrategy(direction=SignalDirection.SHORT, stop=103.0, target=95.0))
    result = engine.run()

    trade = result.trades[0]
    assert trade.direction == SignalDirection.SHORT
    assert trade.exit_reason == "STOP_LOSS"
    assert trade.price_pnl == pytest.approx(-3.0)


def test_timeout_exit_at_close():
    candles = [_candle(index) for index in range(5)]
    engine = _engine(
        candles,
        StubStrategy(stop=90.0, target=120.0),
        position_timeout_hours=2,
    )
    result = engine.run()

    trade = result.trades[0]
    assert trade.exit_reason == "TIMEOUT"
    assert trade.closed_at == T0 + timedelta(hours=3)


def test_no_stacking_single_position_per_symbol():
    candles = [_candle(index) for index in range(6)]
    strategy = StubStrategy(fire_at=set(range(6)), stop=90.0, target=120.0)
    engine = _engine(candles, strategy, position_timeout_hours=72)
    result = engine.run()

    assert len(result.trades) == 1  # signal fires every candle but never stacks


def test_daily_loss_breaker_blocks_new_entries_for_the_day():
    # Entry at hour0 close, stopped out in hour1 (-5 on 1000 = 0.5% loss).
    candles = [
        _candle(0),
        _candle(1, low=94.0, close=99.0),
        _candle(2),
        _candle(3),
    ]
    strategy = StubStrategy(fire_at={0, 2}, stop=95.0, target=200.0)
    engine = _engine(candles, strategy, max_daily_loss=0.004)
    result = engine.run()

    assert [trade.exit_reason for trade in result.trades] == ["STOP_LOSS"]
    assert len(result.trades) == 1  # the hour-2 signal is blocked by the breaker


def test_consecutive_loss_breaker_blocks_entries():
    candles = [
        _candle(0),
        _candle(1, low=94.0, close=99.0),
        _candle(2),
        _candle(3),
    ]
    strategy = StubStrategy(fire_at={0, 2}, stop=95.0, target=200.0)
    engine = _engine(candles, strategy, max_consecutive_losses=1)
    result = engine.run()

    assert len(result.trades) == 1


def test_reverse_signal_closes_without_same_bar_reentry():
    long_then_short = StubStrategy(fire_at={0, 2}, stop=90.0, target=120.0)

    def generate(symbol, one_hour, four_hour, _orig=long_then_short.generate):
        signal = _orig(symbol, one_hour, four_hour)
        index = int((one_hour[-1].timestamp - T0).total_seconds() // 3600)
        if index == 2:
            return StrategySignal(
                symbol=symbol,
                timeframe="1H",
                direction=SignalDirection.SHORT,
                confidence=0.8,
                reason="flip",
                created_at=one_hour[-1].timestamp,
                expected_move=0.05,
                entry_price=100.0,
                stop_price=105.0,
                target_price=90.0,
            )
        return signal

    long_then_short.generate = generate
    candles = [_candle(index) for index in range(4)]
    engine = _engine(candles, long_then_short)
    result = engine.run()

    assert result.trades[0].exit_reason == "REVERSE_SIGNAL"
    # The flip must not open a short on the very same candle.
    assert len(result.trades) == 1


def test_funding_charged_to_open_long_position():
    funding_time = T0 + timedelta(hours=2)
    candles = [_candle(index) for index in range(4)]
    config = _config()
    engine = BacktestEngine(
        strategy_name="stub",
        config=config,
        data={
            SYMBOL: SymbolHistory(
                one_hour=candles,
                four_hour=[],
                funding=[(funding_time, 0.001)],
            )
        },
        strategy=StubStrategy(stop=90.0, target=120.0),
    )
    result = engine.run()

    trade = result.trades[0]
    # Long pays positive funding: 0.1% of ~100 notional.
    assert trade.funding == pytest.approx(-0.1)


def test_warmup_window_skips_trading_before_trade_start():
    candles = [_candle(index) for index in range(6)]
    strategy = StubStrategy(fire_at=set(range(6)), stop=90.0, target=120.0)
    config = _config()
    engine = BacktestEngine(
        strategy_name="stub",
        config=config,
        data={SYMBOL: SymbolHistory(one_hour=candles, four_hour=[], funding=[])},
        strategy=strategy,
        trade_start=T0 + timedelta(hours=4),
    )
    result = engine.run()

    assert result.trades[0].opened_at >= T0 + timedelta(hours=4)
    assert result.equity_curve[0][0] >= T0 + timedelta(hours=4)


def test_drawdown_halt_flattens_and_stops_trading_for_good():
    # Entry at 100 with stop 80; price collapses to 85 (unrealized -15% on
    # 100% notional... here notional is 10% so equity dips 1.5%); use a tight
    # max_drawdown so the crash trips the kill switch while the position is open.
    candles = [
        _candle(0),
        _candle(1, high=100.5, low=86.0, close=86.0),
        _candle(2, close=86.0),
        _candle(3, close=86.0),
    ]
    # Stop 20% away -> stop-distance sizing gives 50 notional (qty 0.5), so
    # the crash to 86 dents equity by ~0.7%; a 0.5% limit must trip.
    strategy = StubStrategy(fire_at={0, 2, 3}, stop=80.0, target=150.0)
    engine = _engine(candles, strategy, max_drawdown=0.005)
    result = engine.run()

    assert result.halted_at is not None
    trade = result.trades[0]
    assert trade.exit_reason == "DRAWDOWN_HALT"
    # No re-entry after the halt even though signals keep firing.
    assert len(result.trades) == 1


def test_exposure_net_cap_limits_book_in_backtest():
    symbols = [f"C{i}-USDT-SWAP" for i in range(6)]
    data = {}
    for symbol in symbols:
        data[symbol] = SymbolHistory(
            one_hour=[_candle(index, symbol=symbol) for index in range(4)],
            four_hour=[],
            funding=[],
        )

    class MultiStub(StubStrategy):
        def generate(self, symbol, one_hour, four_hour):
            return StrategySignal(
                symbol=symbol,
                timeframe="1H",
                direction=SignalDirection.LONG,
                confidence=0.8,
                reason="stub",
                created_at=one_hour[-1].timestamp,
                expected_move=0.05,
                entry_price=100.0,
                stop_price=95.0,
                target_price=150.0,
            )

    config = BacktestConfig(
        symbols=symbols,
        initial_capital_usdt=1000.0,
        fee_rate_per_side=0.0,
        slippage_rate=0.0,
        max_net_exposure_rate=0.25,
        max_total_exposure_rate=1.0,
    )
    engine = BacktestEngine(
        strategy_name="stub",
        config=config,
        data=data,
        strategy=MultiStub(),
    )
    result = engine.run()

    # Each position is 10% notional; the 25% net cap admits at most 2-3 longs,
    # not the 5 allowed by max_open_positions.
    open_now = [trade for trade in result.trades if trade.closed_at is None]
    assert 0 < len(open_now) <= 3


def test_equity_based_sizing_shrinks_after_losses():
    candles = [
        _candle(0),
        _candle(1, low=94.0, close=99.0),  # stop-out: equity drops
        _candle(2, close=99.0),
        _candle(3, close=99.0),
        _candle(4, close=99.0),
    ]
    strategy = StubStrategy(fire_at={0, 3}, stop=95.0, target=200.0)
    # Loosen breakers so the second entry is allowed the same day.
    engine = _engine(
        candles,
        strategy,
        max_daily_loss=0.99,
        max_consecutive_losses=10,
    )
    result = engine.run()

    assert len(result.trades) == 2
    first, second = result.trades
    # Second entry is sized off the reduced equity, not initial capital.
    assert second.notional_usdt < first.notional_usdt


def test_engine_feeds_daily_channel_to_daily_trend_strategy():
    from okx_ai_quant.strategy import create_strategy

    # Build 70 days of steadily rising candles so the 50-day daily EMA has
    # enough history (the strategy needs >= slow_span + 1 daily bars).
    days = 70
    hours = 24 * days
    price = 100.0
    one_hour = []
    daily = []
    for h in range(hours):
        price *= 1.002
        one_hour.append(_candle(h, open_=price / 1.002, high=price * 1.003, low=price * 0.999, close=price))
    # Daily candles aligned to UTC midnight.
    for d in range(days):
        idx = d * 24
        day_candles = one_hour[idx : idx + 24]
        daily.append(
            Candle(
                symbol=SYMBOL,
                timeframe="1D",
                timestamp=T0 + timedelta(days=d),
                open=day_candles[0].open,
                high=max(c.high for c in day_candles),
                low=min(c.low for c in day_candles),
                close=day_candles[-1].close,
                volume=1000.0,
            )
        )

    config = BacktestConfig(symbols=[SYMBOL], initial_capital_usdt=10_000.0)
    engine = BacktestEngine(
        strategy_name="daily-trend",
        config=config,
        data={SYMBOL: SymbolHistory(one_hour=one_hour, four_hour=[], funding=[], daily=daily)},
        strategy=create_strategy("daily-trend", min_expected_move=0.006),
        trade_start=T0 + timedelta(days=55),
    )
    result = engine.run()

    assert result.trades, "daily-trend should trade the sustained uptrend"
    assert all(t.direction == SignalDirection.LONG for t in result.trades)


def test_window_stats_splits_in_and_out_of_sample():
    candles = [
        _candle(0),
        _candle(1, high=106.0, close=104.0),  # TP win early
        _candle(2),
    ]
    engine = _engine(candles, StubStrategy())
    result = engine.run()

    # The only trade closed at hour 2; a cutoff after it sees no OOS trades.
    after = T0 + timedelta(hours=10)
    oos = result.window_stats(since=after)
    assert oos["trades"] == 0
    ins = result.window_stats(since=T0)
    assert ins["trades"] == 1


def test_report_renders_stats_and_curve():
    candles = [
        _candle(0),
        _candle(1, high=106.0, close=104.0),
        _candle(2),
    ]
    engine = _engine(candles, StubStrategy())
    result = engine.run()

    summary = render_backtest_summary([result])
    html = render_backtest_html(
        [result],
        config=_config(),
        start=T0,
        end=T0 + timedelta(hours=3),
    )

    assert "stub" in summary
    assert "OKX AI Quant 回测报告" in html
    assert "polyline" in html
    assert "TAKE_PROFIT" in html


def test_engine_smoke_with_real_strategy_on_trending_data():
    from okx_ai_quant.strategy import create_strategy

    candles_a: list[Candle] = []
    candles_b: list[Candle] = []
    price_a, price_b = 100.0, 200.0
    for index in range(240):
        drift_a = 1.004 if index < 160 else 0.99
        drift_b = 0.997
        price_a *= drift_a
        price_b *= drift_b
        candles_a.append(
            _candle(index, open_=price_a / drift_a, high=price_a * 1.01, low=price_a * 0.985, close=price_a)
        )
        candles_b.append(
            _candle(
                index,
                symbol="ETH-USDT-SWAP",
                open_=price_b / drift_b,
                high=price_b * 1.012,
                low=price_b * 0.986,
                close=price_b,
            )
        )

    config = BacktestConfig(
        symbols=[SYMBOL, "ETH-USDT-SWAP"],
        initial_capital_usdt=10_000.0,
    )
    engine = BacktestEngine(
        strategy_name="ema-momentum",
        config=config,
        data={
            SYMBOL: SymbolHistory(one_hour=candles_a, four_hour=[], funding=[]),
            "ETH-USDT-SWAP": SymbolHistory(one_hour=candles_b, four_hour=[], funding=[]),
        },
        strategy=create_strategy("ema-momentum", min_expected_move=0.006),
        trade_start=T0 + timedelta(hours=40),
    )
    result = engine.run()

    assert result.trades, "trending synthetic data should produce trades"
    assert result.equity_curve
    closed = [trade for trade in result.trades if trade.closed_at is not None]
    assert all(trade.exit_reason for trade in closed)
