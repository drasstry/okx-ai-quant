from datetime import UTC, datetime, timedelta

from okx_ai_quant.models import Candle, Signal, SignalDirection
from okx_ai_quant.strategy import (
    AVAILABLE_STRATEGIES,
    CrossSectionalMomentumFundingStrategy,
    DonchianBreakoutStrategy,
    EmaMomentumStrategy,
    EmaRsiAtrStrategy,
    MultiTimeframeTrendStrategy,
    RsiBollingerReversionStrategy,
    VolatilityAdjustedMomentumStrategy,
    create_strategy,
)


def _candles(symbol: str, timeframe: str, closes: list[float]) -> list[Candle]:
    start = datetime(2026, 4, 29, tzinfo=UTC)
    return [
        Candle(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=start + timedelta(hours=index),
            open=close - 0.2,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=100.0 + index,
        )
        for index, close in enumerate(closes)
    ]


def test_strategy_generates_long_for_aligned_uptrend():
    symbol = "BTC-USDT-SWAP"
    one_hour = _candles(symbol, "1H", [100.0 + index for index in range(40)])
    four_hour = _candles(symbol, "4H", [90.0 + index * 1.5 for index in range(40)])
    strategy = EmaRsiAtrStrategy(min_expected_move=0.005)

    signal = strategy.generate(symbol, one_hour=one_hour, four_hour=four_hour)

    assert isinstance(signal, Signal)
    assert signal.direction == SignalDirection.LONG
    assert signal.timeframe == "1H"
    assert signal.confidence > 0.5
    assert signal.expected_move >= strategy.min_expected_move
    assert signal.stop_price < one_hour[-1].close < signal.target_price
    assert signal.created_at.tzinfo is UTC
    assert "uptrend" in signal.reason.lower()


def test_strategy_generates_short_for_aligned_downtrend():
    symbol = "BTC-USDT-SWAP"
    one_hour = _candles(symbol, "1H", [140.0 - index for index in range(40)])
    four_hour = _candles(symbol, "4H", [160.0 - index * 1.5 for index in range(40)])
    strategy = EmaRsiAtrStrategy(min_expected_move=0.005)

    signal = strategy.generate(symbol, one_hour=one_hour, four_hour=four_hour)

    assert signal.direction == SignalDirection.SHORT
    assert signal.confidence > 0.5
    assert signal.expected_move >= strategy.min_expected_move
    assert signal.target_price < one_hour[-1].close < signal.stop_price
    assert signal.created_at.tzinfo is UTC
    assert "downtrend" in signal.reason.lower()


def test_strategy_holds_when_expected_move_is_too_small():
    symbol = "BTC-USDT-SWAP"
    one_hour = _candles(symbol, "1H", [100.0 + index for index in range(40)])
    four_hour = _candles(symbol, "4H", [90.0 + index * 1.5 for index in range(40)])
    strategy = EmaRsiAtrStrategy(min_expected_move=0.5)

    signal = strategy.generate(symbol, one_hour=one_hour, four_hour=four_hour)

    assert signal.direction == SignalDirection.HOLD
    assert signal.expected_move < strategy.min_expected_move
    assert "expected move" in signal.reason.lower()


def test_strategy_holds_when_candle_history_is_too_short():
    symbol = "BTC-USDT-SWAP"
    strategy = EmaRsiAtrStrategy(min_expected_move=0.005)

    signal = strategy.generate(
        symbol,
        one_hour=_candles(symbol, "1H", [100.0, 101.0, 102.0]),
        four_hour=_candles(symbol, "4H", [100.0, 101.0, 102.0]),
    )

    assert signal.direction == SignalDirection.HOLD
    assert signal.confidence == 0.0
    assert signal.expected_move == 0.0
    assert "not enough candles" in signal.reason.lower()


def test_strategy_factory_lists_supported_strategies():
    assert AVAILABLE_STRATEGIES == (
        "ema-rsi-atr",
        "rsi-bollinger-reversion",
        "donchian-breakout",
        "ema-momentum",
        "multi-timeframe-trend",
        "volatility-adjusted-momentum",
        "cross-sectional-momentum-funding",
    )
    assert isinstance(create_strategy("ema-rsi-atr", min_expected_move=0.006), EmaRsiAtrStrategy)
    assert isinstance(
        create_strategy("rsi-bollinger-reversion", min_expected_move=0.006),
        RsiBollingerReversionStrategy,
    )
    assert isinstance(
        create_strategy("donchian-breakout", min_expected_move=0.006),
        DonchianBreakoutStrategy,
    )
    assert isinstance(create_strategy("ema-momentum", min_expected_move=0.006), EmaMomentumStrategy)
    assert isinstance(
        create_strategy("multi-timeframe-trend", min_expected_move=0.006),
        MultiTimeframeTrendStrategy,
    )
    assert isinstance(
        create_strategy("volatility-adjusted-momentum", min_expected_move=0.006),
        VolatilityAdjustedMomentumStrategy,
    )
    assert isinstance(
        create_strategy("cross-sectional-momentum-funding", min_expected_move=0.006),
        CrossSectionalMomentumFundingStrategy,
    )


def test_strategy_factory_rejects_unknown_strategy():
    try:
        create_strategy("martingale", min_expected_move=0.006)
    except ValueError as exc:
        assert "Unknown strategy" in str(exc)
    else:
        raise AssertionError("create_strategy should reject unknown names")


def test_cross_sectional_momentum_funding_selects_tails_and_recommends_leverage():
    symbols = [
        "BTC-USDT-SWAP",
        "ETH-USDT-SWAP",
        "SOL-USDT-SWAP",
        "XRP-USDT-SWAP",
        "ADA-USDT-SWAP",
    ]
    closes = {
        "BTC-USDT-SWAP": [100, 102, 104, 107, 111, 116, 122, 129, 137, 146],
        "ETH-USDT-SWAP": [100, 101, 102, 103, 104, 105, 106, 107, 108, 109],
        "SOL-USDT-SWAP": [100, 100, 101, 100, 101, 100, 101, 100, 101, 100],
        "XRP-USDT-SWAP": [160, 153, 146, 139, 132, 125, 118, 111, 104, 97],
        "ADA-USDT-SWAP": [110, 108, 106, 104, 102, 100, 98, 96, 94, 92],
    }
    one_hour = {
        symbol: _candles(symbol, "1H", values)
        for symbol, values in closes.items()
    }
    strategy = CrossSectionalMomentumFundingStrategy(
        min_expected_move=0.005,
        momentum_lookback=3,
        volatility_lookback=3,
        atr_period=3,
        min_universe_size=5,
    )

    strategy.prepare_universe(
        symbols=symbols,
        one_hour=one_hour,
        four_hour={symbol: _candles(symbol, "4H", values) for symbol, values in closes.items()},
        funding_rates={
            "BTC-USDT-SWAP": 0.0001,
            "XRP-USDT-SWAP": -0.0001,
        },
    )

    long_signal = strategy.generate("BTC-USDT-SWAP", one_hour["BTC-USDT-SWAP"], [])
    short_signal = strategy.generate("XRP-USDT-SWAP", one_hour["XRP-USDT-SWAP"], [])
    middle_signal = strategy.generate("SOL-USDT-SWAP", one_hour["SOL-USDT-SWAP"], [])

    assert long_signal.direction == SignalDirection.LONG
    assert long_signal.recommended_leverage >= 1
    assert "cross-sectional momentum" in long_signal.reason.lower()
    assert short_signal.direction == SignalDirection.SHORT
    assert short_signal.recommended_leverage >= 1
    assert middle_signal.direction == SignalDirection.HOLD


def test_cross_sectional_momentum_funding_blocks_crowded_long_funding():
    symbols = [
        "BTC-USDT-SWAP",
        "ETH-USDT-SWAP",
        "SOL-USDT-SWAP",
        "XRP-USDT-SWAP",
        "ADA-USDT-SWAP",
    ]
    closes = {
        "BTC-USDT-SWAP": [100, 102, 104, 107, 111, 116, 122, 129, 137, 146],
        "ETH-USDT-SWAP": [100, 101, 102, 103, 104, 105, 106, 107, 108, 109],
        "SOL-USDT-SWAP": [100, 100, 101, 100, 101, 100, 101, 100, 101, 100],
        "XRP-USDT-SWAP": [160, 153, 146, 139, 132, 125, 118, 111, 104, 97],
        "ADA-USDT-SWAP": [110, 108, 106, 104, 102, 100, 98, 96, 94, 92],
    }
    one_hour = {
        symbol: _candles(symbol, "1H", values)
        for symbol, values in closes.items()
    }
    strategy = CrossSectionalMomentumFundingStrategy(
        min_expected_move=0.005,
        momentum_lookback=3,
        volatility_lookback=3,
        atr_period=3,
        min_universe_size=5,
        max_long_funding_rate=0.0008,
    )

    strategy.prepare_universe(
        symbols=symbols,
        one_hour=one_hour,
        four_hour={symbol: _candles(symbol, "4H", values) for symbol, values in closes.items()},
        funding_rates={"BTC-USDT-SWAP": 0.0020},
    )

    signal = strategy.generate("BTC-USDT-SWAP", one_hour["BTC-USDT-SWAP"], [])

    assert signal.direction == SignalDirection.HOLD
    assert "funding filter" in signal.reason.lower()


def test_rsi_bollinger_reversion_generates_long_on_oversold_lower_band():
    symbol = "BTC-USDT-SWAP"
    closes = [100.0] * 35 + [98.0, 96.0, 94.0, 92.0, 88.0]
    strategy = RsiBollingerReversionStrategy(min_expected_move=0.005)

    signal = strategy.generate(
        symbol,
        one_hour=_candles(symbol, "1H", closes),
        four_hour=_candles(symbol, "4H", [100.0] * 40),
    )

    assert signal.direction == SignalDirection.LONG
    assert "mean reversion" in signal.reason.lower()
    assert signal.stop_price < closes[-1] < signal.target_price


def test_rsi_bollinger_reversion_generates_short_on_overbought_upper_band():
    symbol = "BTC-USDT-SWAP"
    closes = [100.0] * 35 + [102.0, 104.0, 106.0, 108.0, 112.0]
    strategy = RsiBollingerReversionStrategy(min_expected_move=0.005)

    signal = strategy.generate(
        symbol,
        one_hour=_candles(symbol, "1H", closes),
        four_hour=_candles(symbol, "4H", [100.0] * 40),
    )

    assert signal.direction == SignalDirection.SHORT
    assert "mean reversion" in signal.reason.lower()
    assert signal.target_price < closes[-1] < signal.stop_price


def test_donchian_breakout_generates_long_on_channel_breakout():
    symbol = "BTC-USDT-SWAP"
    strategy = DonchianBreakoutStrategy(min_expected_move=0.005, lookback=20)
    closes = [100.0] * 39 + [106.0]

    signal = strategy.generate(
        symbol,
        one_hour=_candles(symbol, "1H", closes),
        four_hour=_candles(symbol, "4H", [100.0 + index * 0.2 for index in range(40)]),
    )

    assert signal.direction == SignalDirection.LONG
    assert "breakout" in signal.reason.lower()


def test_donchian_breakout_generates_short_on_channel_breakdown():
    symbol = "BTC-USDT-SWAP"
    strategy = DonchianBreakoutStrategy(min_expected_move=0.005, lookback=20)
    closes = [100.0] * 39 + [94.0]

    signal = strategy.generate(
        symbol,
        one_hour=_candles(symbol, "1H", closes),
        four_hour=_candles(symbol, "4H", [110.0 - index * 0.2 for index in range(40)]),
    )

    assert signal.direction == SignalDirection.SHORT
    assert "breakdown" in signal.reason.lower()


def test_ema_momentum_generates_long_on_positive_momentum():
    symbol = "BTC-USDT-SWAP"
    strategy = EmaMomentumStrategy(min_expected_move=0.005, momentum_lookback=6)
    closes = [100.0 + index * 0.8 for index in range(40)]

    signal = strategy.generate(
        symbol,
        one_hour=_candles(symbol, "1H", closes),
        four_hour=_candles(symbol, "4H", closes),
    )

    assert signal.direction == SignalDirection.LONG
    assert "momentum" in signal.reason.lower()


def test_ema_momentum_generates_short_on_negative_momentum():
    symbol = "BTC-USDT-SWAP"
    strategy = EmaMomentumStrategy(min_expected_move=0.005, momentum_lookback=6)
    closes = [140.0 - index * 0.8 for index in range(40)]

    signal = strategy.generate(
        symbol,
        one_hour=_candles(symbol, "1H", closes),
        four_hour=_candles(symbol, "4H", closes),
    )

    assert signal.direction == SignalDirection.SHORT
    assert "momentum" in signal.reason.lower()


def test_multi_timeframe_trend_generates_long_on_aligned_ema_stacks():
    symbol = "BTC-USDT-SWAP"
    strategy = MultiTimeframeTrendStrategy(min_expected_move=0.005)
    closes = [100.0 + index * 0.7 for index in range(60)] + [
        142.0 + ((index % 3) - 1) * 0.3 for index in range(20)
    ]
    one_hour = _candles(symbol, "1H", closes)
    four_hour = _candles(symbol, "4H", closes)

    signal = strategy.generate(symbol, one_hour=one_hour, four_hour=four_hour)

    assert signal.direction == SignalDirection.LONG
    assert "multi-timeframe trend" in signal.reason.lower()
    assert signal.stop_price < one_hour[-1].close < signal.target_price


def test_multi_timeframe_trend_generates_short_on_aligned_ema_stacks():
    symbol = "BTC-USDT-SWAP"
    strategy = MultiTimeframeTrendStrategy(min_expected_move=0.005)
    closes = [160.0 - index * 0.7 for index in range(60)] + [
        118.0 + ((index % 3) - 1) * 0.3 for index in range(20)
    ]
    one_hour = _candles(symbol, "1H", closes)
    four_hour = _candles(symbol, "4H", closes)

    signal = strategy.generate(symbol, one_hour=one_hour, four_hour=four_hour)

    assert signal.direction == SignalDirection.SHORT
    assert "multi-timeframe trend" in signal.reason.lower()
    assert signal.target_price < one_hour[-1].close < signal.stop_price


def test_volatility_adjusted_momentum_generates_long_when_momentum_clears_atr_noise():
    symbol = "BTC-USDT-SWAP"
    strategy = VolatilityAdjustedMomentumStrategy(min_expected_move=0.005, momentum_lookback=12)
    closes = [100.0 + index * 0.45 for index in range(45)] + [123.0, 125.0, 127.5]

    signal = strategy.generate(
        symbol,
        one_hour=_candles(symbol, "1H", closes),
        four_hour=_candles(symbol, "4H", closes),
    )

    assert signal.direction == SignalDirection.LONG
    assert "volatility-adjusted momentum" in signal.reason.lower()


def test_volatility_adjusted_momentum_holds_when_momentum_is_noise():
    symbol = "BTC-USDT-SWAP"
    strategy = VolatilityAdjustedMomentumStrategy(
        min_expected_move=0.005,
        momentum_lookback=12,
        min_momentum_to_atr=10.0,
    )
    closes = [100.0 + index * 0.2 for index in range(48)]

    signal = strategy.generate(
        symbol,
        one_hour=_candles(symbol, "1H", closes),
        four_hour=_candles(symbol, "4H", closes),
    )

    assert signal.direction == SignalDirection.HOLD
    assert "momentum/atr" in signal.reason.lower()
