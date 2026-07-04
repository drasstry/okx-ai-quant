from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd

from okx_ai_quant.indicators import atr, ema, rsi
from okx_ai_quant.models import Candle, Signal, SignalDirection

AVAILABLE_STRATEGIES = (
    "ema-rsi-atr",
    "rsi-bollinger-reversion",
    "donchian-breakout",
    "ema-momentum",
    "multi-timeframe-trend",
    "volatility-adjusted-momentum",
    "cross-sectional-momentum-funding",
    "funding-carry",
    "daily-trend",
)


@dataclass(frozen=True, kw_only=True)
class StrategySignal(Signal):
    expected_move: float = 0.0
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    recommended_leverage: int | None = None


@dataclass(frozen=True, kw_only=True)
class EmaRsiAtrStrategy:
    min_expected_move: float
    fast_span: int = 12
    slow_span: int = 26
    rsi_period: int = 14
    atr_period: int = 14

    def generate(
        self,
        symbol: str,
        one_hour: list[Candle],
        four_hour: list[Candle],
    ) -> Signal:
        required_candles = max(self.slow_span, self.rsi_period + 1, self.atr_period)
        if len(one_hour) < required_candles or len(four_hour) < required_candles:
            return self._hold(
                symbol,
                "Not enough candles for EMA, RSI, and ATR calculations.",
            )

        one_hour_frame = self._frame(one_hour)
        four_hour_frame = self._frame(four_hour)

        one_hour_fast = ema(one_hour_frame["close"], self.fast_span)
        one_hour_slow = ema(one_hour_frame["close"], self.slow_span)
        four_hour_fast = ema(four_hour_frame["close"], self.fast_span)
        four_hour_slow = ema(four_hour_frame["close"], self.slow_span)
        one_hour_rsi = rsi(one_hour_frame["close"], self.rsi_period).iloc[-1]
        one_hour_atr = atr(one_hour_frame, self.atr_period).iloc[-1]
        latest_close = float(one_hour_frame["close"].iloc[-1])

        if pd.isna(one_hour_rsi) or pd.isna(one_hour_atr) or latest_close <= 0:
            return self._hold(symbol, "Not enough candles for stable indicator values.")

        one_hour_uptrend = one_hour_fast.iloc[-1] > one_hour_slow.iloc[-1]
        four_hour_uptrend = four_hour_fast.iloc[-1] > four_hour_slow.iloc[-1]
        one_hour_downtrend = one_hour_fast.iloc[-1] < one_hour_slow.iloc[-1]
        four_hour_downtrend = four_hour_fast.iloc[-1] < four_hour_slow.iloc[-1]

        expected_move = float((one_hour_atr * 1.5) / latest_close)
        if expected_move < self.min_expected_move:
            return self._hold(
                symbol,
                f"Expected move {expected_move:.4f} is below minimum {self.min_expected_move:.4f}.",
                expected_move=expected_move,
            )

        if four_hour_uptrend and one_hour_uptrend and one_hour_rsi >= 50.0:
            return self._signal(
                symbol=symbol,
                direction=SignalDirection.LONG,
                confidence=self._confidence(one_hour_rsi, expected_move),
                reason="4H and 1H EMA uptrend aligned with acceptable RSI.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close - float(one_hour_atr),
                target_price=latest_close + float(one_hour_atr * 1.5),
            )

        if four_hour_downtrend and one_hour_downtrend and one_hour_rsi <= 50.0:
            return self._signal(
                symbol=symbol,
                direction=SignalDirection.SHORT,
                confidence=self._confidence(100.0 - one_hour_rsi, expected_move),
                reason="4H and 1H EMA downtrend aligned with acceptable RSI.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close + float(one_hour_atr),
                target_price=latest_close - float(one_hour_atr * 1.5),
            )

        return self._hold(
            symbol,
            "4H trend filter and 1H signal conditions are not aligned.",
            expected_move=expected_move,
        )

    def _signal(
        self,
        *,
        symbol: str,
        direction: SignalDirection,
        confidence: float,
        reason: str,
        expected_move: float,
        entry_price: float,
        stop_price: float,
        target_price: float,
    ) -> StrategySignal:
        return StrategySignal(
            symbol=symbol,
            timeframe="1H",
            direction=direction,
            confidence=confidence,
            reason=reason,
            created_at=datetime.now(UTC),
            expected_move=expected_move,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
        )

    def _hold(
        self,
        symbol: str,
        reason: str,
        *,
        expected_move: float = 0.0,
    ) -> StrategySignal:
        return StrategySignal(
            symbol=symbol,
            timeframe="1H",
            direction=SignalDirection.HOLD,
            confidence=0.0,
            reason=reason,
            created_at=datetime.now(UTC),
            expected_move=expected_move,
            stop_price=None,
            target_price=None,
        )

    @staticmethod
    def _frame(candles: list[Candle]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "high": [candle.high for candle in candles],
                "low": [candle.low for candle in candles],
                "close": [candle.close for candle in candles],
            }
        )

    @staticmethod
    def _confidence(rsi_strength: float, expected_move: float) -> float:
        rsi_component = min(abs(rsi_strength - 50.0) / 50.0, 1.0)
        move_component = min(expected_move * 10.0, 1.0)
        return round(0.5 + (0.3 * rsi_component) + (0.2 * move_component), 4)


@dataclass(frozen=True, kw_only=True)
class RsiBollingerReversionStrategy:
    min_expected_move: float
    period: int = 20
    std_multiplier: float = 2.0
    rsi_period: int = 14
    atr_period: int = 14
    oversold: float = 35.0
    overbought: float = 65.0

    def generate(
        self,
        symbol: str,
        one_hour: list[Candle],
        four_hour: list[Candle],
    ) -> Signal:
        del four_hour
        required_candles = max(self.period, self.rsi_period + 1, self.atr_period)
        if len(one_hour) < required_candles:
            return _hold(symbol, "Not enough candles for RSI/Bollinger mean reversion.")

        frame = _frame(one_hour)
        close = frame["close"]
        latest_close = float(close.iloc[-1])
        moving_average = close.rolling(window=self.period, min_periods=self.period).mean()
        moving_std = close.rolling(window=self.period, min_periods=self.period).std(ddof=0)
        latest_mean = float(moving_average.iloc[-1])
        latest_std = float(moving_std.iloc[-1])
        latest_rsi = rsi(close, self.rsi_period).iloc[-1]
        latest_atr = atr(frame, self.atr_period).iloc[-1]

        if (
            pd.isna(latest_mean)
            or pd.isna(latest_std)
            or pd.isna(latest_rsi)
            or pd.isna(latest_atr)
            or latest_close <= 0
        ):
            return _hold(symbol, "Not enough candles for stable mean reversion values.")

        upper_band = latest_mean + (self.std_multiplier * latest_std)
        lower_band = latest_mean - (self.std_multiplier * latest_std)
        expected_move = abs(latest_mean - latest_close) / latest_close
        if expected_move < self.min_expected_move:
            return _hold(
                symbol,
                f"Expected move {expected_move:.4f} is below minimum {self.min_expected_move:.4f}.",
                expected_move=expected_move,
            )

        if latest_close <= lower_band and latest_rsi <= self.oversold:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.LONG,
                confidence=_bounded_confidence(0.55 + expected_move),
                reason="RSI/Bollinger mean reversion long: price is near the lower band and RSI is oversold.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close - float(latest_atr),
                target_price=latest_mean,
            )

        if latest_close >= upper_band and latest_rsi >= self.overbought:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.SHORT,
                confidence=_bounded_confidence(0.55 + expected_move),
                reason="RSI/Bollinger mean reversion short: price is near the upper band and RSI is overbought.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close + float(latest_atr),
                target_price=latest_mean,
            )

        return _hold(
            symbol,
            "RSI/Bollinger mean reversion conditions are not aligned.",
            expected_move=expected_move,
        )


@dataclass(frozen=True, kw_only=True)
class DonchianBreakoutStrategy:
    min_expected_move: float
    lookback: int = 20
    atr_period: int = 14

    def generate(
        self,
        symbol: str,
        one_hour: list[Candle],
        four_hour: list[Candle],
    ) -> Signal:
        required_candles = max(self.lookback + 1, self.atr_period)
        if len(one_hour) < required_candles or len(four_hour) < 26:
            return _hold(symbol, "Not enough candles for Donchian breakout.")

        one = _frame(one_hour)
        four = _frame(four_hour)
        latest_close = float(one["close"].iloc[-1])
        latest_atr = atr(one, self.atr_period).iloc[-1]
        previous_window = one.iloc[-self.lookback - 1 : -1]
        channel_high = float(previous_window["high"].max())
        channel_low = float(previous_window["low"].min())
        four_fast = ema(four["close"], 12).iloc[-1]
        four_slow = ema(four["close"], 26).iloc[-1]

        if pd.isna(latest_atr) or latest_close <= 0:
            return _hold(symbol, "Not enough candles for stable Donchian values.")

        if latest_close > channel_high and four_fast > four_slow:
            expected_move = (latest_close - channel_high + float(latest_atr)) / latest_close
            if expected_move < self.min_expected_move:
                return _hold(symbol, "Donchian breakout move is below cost threshold.", expected_move=expected_move)
            return _signal(
                symbol=symbol,
                direction=SignalDirection.LONG,
                confidence=_bounded_confidence(0.55 + expected_move),
                reason="Donchian breakout long: price broke above the recent channel high.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=channel_high - float(latest_atr),
                target_price=latest_close + float(latest_atr * 1.5),
            )

        if latest_close < channel_low and four_fast < four_slow:
            expected_move = (channel_low - latest_close + float(latest_atr)) / latest_close
            if expected_move < self.min_expected_move:
                return _hold(symbol, "Donchian breakdown move is below cost threshold.", expected_move=expected_move)
            return _signal(
                symbol=symbol,
                direction=SignalDirection.SHORT,
                confidence=_bounded_confidence(0.55 + expected_move),
                reason="Donchian breakdown short: price broke below the recent channel low.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=channel_low + float(latest_atr),
                target_price=latest_close - float(latest_atr * 1.5),
            )

        return _hold(symbol, "Donchian channel breakout conditions are not aligned.")


@dataclass(frozen=True, kw_only=True)
class EmaMomentumStrategy:
    min_expected_move: float
    fast_span: int = 12
    slow_span: int = 26
    momentum_lookback: int = 6
    atr_period: int = 14

    def generate(
        self,
        symbol: str,
        one_hour: list[Candle],
        four_hour: list[Candle],
    ) -> Signal:
        del four_hour
        required_candles = max(self.slow_span, self.momentum_lookback + 1, self.atr_period)
        if len(one_hour) < required_candles:
            return _hold(symbol, "Not enough candles for EMA momentum.")

        frame = _frame(one_hour)
        close = frame["close"]
        latest_close = float(close.iloc[-1])
        lookback_close = float(close.iloc[-self.momentum_lookback - 1])
        fast = ema(close, self.fast_span).iloc[-1]
        slow = ema(close, self.slow_span).iloc[-1]
        latest_atr = atr(frame, self.atr_period).iloc[-1]

        if pd.isna(fast) or pd.isna(slow) or pd.isna(latest_atr) or latest_close <= 0 or lookback_close <= 0:
            return _hold(symbol, "Not enough candles for stable EMA momentum values.")

        momentum = (latest_close / lookback_close) - 1.0
        expected_move = max(abs(momentum), float(latest_atr / latest_close))
        if expected_move < self.min_expected_move:
            return _hold(
                symbol,
                f"Momentum move {expected_move:.4f} is below minimum {self.min_expected_move:.4f}.",
                expected_move=expected_move,
            )

        if fast > slow and momentum > 0:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.LONG,
                confidence=_bounded_confidence(0.55 + abs(momentum)),
                reason="EMA momentum long: EMA trend is positive and recent momentum is rising.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close - float(latest_atr),
                target_price=latest_close + float(latest_atr * 1.5),
            )

        if fast < slow and momentum < 0:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.SHORT,
                confidence=_bounded_confidence(0.55 + abs(momentum)),
                reason="EMA momentum short: EMA trend is negative and recent momentum is falling.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close + float(latest_atr),
                target_price=latest_close - float(latest_atr * 1.5),
            )

        return _hold(symbol, "EMA momentum conditions are not aligned.", expected_move=expected_move)


@dataclass(frozen=True, kw_only=True)
class MultiTimeframeTrendStrategy:
    min_expected_move: float
    fast_span: int = 12
    medium_span: int = 26
    slow_span: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    min_trend_gap: float = 0.002

    def generate(
        self,
        symbol: str,
        one_hour: list[Candle],
        four_hour: list[Candle],
    ) -> Signal:
        required_candles = max(self.slow_span + 1, self.rsi_period + 1, self.atr_period)
        if len(one_hour) < required_candles or len(four_hour) < required_candles:
            return _hold(symbol, "Not enough candles for multi-timeframe trend.")

        one = _frame(one_hour)
        four = _frame(four_hour)
        close = one["close"]
        latest_close = float(close.iloc[-1])
        latest_atr = atr(one, self.atr_period).iloc[-1]
        latest_rsi = rsi(close, self.rsi_period).iloc[-1]
        if pd.isna(latest_atr) or pd.isna(latest_rsi) or latest_close <= 0:
            return _hold(symbol, "Not enough candles for stable multi-timeframe trend values.")

        one_fast = ema(close, self.fast_span).iloc[-1]
        one_medium = ema(close, self.medium_span).iloc[-1]
        one_slow = ema(close, self.slow_span).iloc[-1]
        four_fast_series = ema(four["close"], self.fast_span)
        four_medium = ema(four["close"], self.medium_span).iloc[-1]
        four_slow = ema(four["close"], self.slow_span).iloc[-1]
        four_fast = four_fast_series.iloc[-1]
        previous_four_fast = four_fast_series.iloc[-2]

        expected_move = float(max((latest_atr * 1.8) / latest_close, abs(one_fast - one_slow) / latest_close))
        if expected_move < self.min_expected_move:
            return _hold(
                symbol,
                f"Trend expected move {expected_move:.4f} is below minimum {self.min_expected_move:.4f}.",
                expected_move=expected_move,
            )

        one_up = one_fast > one_medium > one_slow and latest_close > one_medium
        four_up = four_fast > four_medium > four_slow and four_fast > previous_four_fast
        one_down = one_fast < one_medium < one_slow and latest_close < one_medium
        four_down = four_fast < four_medium < four_slow and four_fast < previous_four_fast
        trend_gap = abs(one_fast - one_slow) / latest_close

        if trend_gap < self.min_trend_gap:
            return _hold(
                symbol,
                f"Trend gap {trend_gap:.4f} is below minimum {self.min_trend_gap:.4f}.",
                expected_move=expected_move,
            )

        if four_up and one_up and 45.0 <= latest_rsi <= 72.0:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.LONG,
                confidence=_bounded_confidence(0.58 + trend_gap * 8 + expected_move),
                reason="Multi-timeframe trend long: 4H and 1H EMA stacks are aligned upward with healthy RSI.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close - float(latest_atr * 1.2),
                target_price=latest_close + float(latest_atr * 2.0),
            )

        if four_down and one_down and 28.0 <= latest_rsi <= 55.0:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.SHORT,
                confidence=_bounded_confidence(0.58 + trend_gap * 8 + expected_move),
                reason="Multi-timeframe trend short: 4H and 1H EMA stacks are aligned downward with healthy RSI.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close + float(latest_atr * 1.2),
                target_price=latest_close - float(latest_atr * 2.0),
            )

        return _hold(
            symbol,
            "Multi-timeframe trend conditions are not aligned.",
            expected_move=expected_move,
        )


@dataclass(frozen=True, kw_only=True)
class VolatilityAdjustedMomentumStrategy:
    min_expected_move: float
    fast_span: int = 8
    slow_span: int = 21
    momentum_lookback: int = 12
    atr_period: int = 14
    min_momentum_to_atr: float = 1.25

    def generate(
        self,
        symbol: str,
        one_hour: list[Candle],
        four_hour: list[Candle],
    ) -> Signal:
        del four_hour
        required_candles = max(self.slow_span + 1, self.momentum_lookback + 1, self.atr_period)
        if len(one_hour) < required_candles:
            return _hold(symbol, "Not enough candles for volatility-adjusted momentum.")

        frame = _frame(one_hour)
        close = frame["close"]
        latest_close = float(close.iloc[-1])
        lookback_close = float(close.iloc[-self.momentum_lookback - 1])
        latest_atr = atr(frame, self.atr_period).iloc[-1]
        fast = ema(close, self.fast_span).iloc[-1]
        slow = ema(close, self.slow_span).iloc[-1]
        previous_fast = ema(close.iloc[:-1], self.fast_span).iloc[-1]

        if (
            pd.isna(latest_atr)
            or pd.isna(fast)
            or pd.isna(slow)
            or latest_close <= 0
            or lookback_close <= 0
        ):
            return _hold(symbol, "Not enough candles for stable volatility-adjusted momentum values.")

        momentum = (latest_close / lookback_close) - 1.0
        atr_rate = float(latest_atr / latest_close)
        momentum_to_atr = abs(momentum) / atr_rate if atr_rate > 0 else 0.0
        expected_move = max(abs(momentum), atr_rate)

        if expected_move < self.min_expected_move:
            return _hold(
                symbol,
                f"Volatility-adjusted momentum {expected_move:.4f} is below minimum {self.min_expected_move:.4f}.",
                expected_move=expected_move,
            )
        if momentum_to_atr < self.min_momentum_to_atr:
            return _hold(
                symbol,
                f"Momentum/ATR ratio {momentum_to_atr:.2f} is below minimum {self.min_momentum_to_atr:.2f}.",
                expected_move=expected_move,
            )

        fast_rising = fast > previous_fast
        fast_falling = fast < previous_fast
        if fast > slow and fast_rising and momentum > 0:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.LONG,
                confidence=_bounded_confidence(0.55 + min(momentum_to_atr, 3.0) * 0.08),
                reason="Volatility-adjusted momentum long: positive momentum clears ATR noise and EMA slope is rising.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close - float(latest_atr * 1.1),
                target_price=latest_close + float(latest_atr * 1.8),
            )

        if fast < slow and fast_falling and momentum < 0:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.SHORT,
                confidence=_bounded_confidence(0.55 + min(momentum_to_atr, 3.0) * 0.08),
                reason="Volatility-adjusted momentum short: negative momentum clears ATR noise and EMA slope is falling.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close + float(latest_atr * 1.1),
                target_price=latest_close - float(latest_atr * 1.8),
            )

        return _hold(
            symbol,
            "Volatility-adjusted momentum conditions are not aligned.",
            expected_move=expected_move,
        )


@dataclass(frozen=True)
class CrossSectionalScore:
    symbol: str
    direction: SignalDirection
    rank: int
    universe_size: int
    momentum: float
    realized_volatility: float
    atr_rate: float
    funding_rate: float
    recommended_leverage: int


@dataclass(kw_only=True)
class CrossSectionalMomentumFundingStrategy:
    """Rank the universe by medium-term momentum, then filter by funding and volatility."""

    min_expected_move: float
    momentum_lookback: int = 72
    volatility_lookback: int = 24
    selection_quantile: float = 0.20
    min_universe_size: int = 5
    atr_period: int = 14
    max_long_funding_rate: float = 0.0008
    max_short_funding_rate: float = 0.0008
    target_hourly_volatility: float = 0.015
    _scores: dict[str, CrossSectionalScore] = field(default_factory=dict, init=False)

    def prepare_universe(
        self,
        *,
        symbols: list[str],
        one_hour: dict[str, list[Candle]],
        four_hour: dict[str, list[Candle]],
        funding_rates: dict[str, float] | None = None,
        daily: dict[str, list[Candle]] | None = None,
    ) -> None:
        del four_hour, daily
        funding_rates = funding_rates or {}
        metrics: list[tuple[str, float, float, float, float]] = []
        required = max(self.momentum_lookback + 1, self.volatility_lookback + 1, self.atr_period)
        for symbol in symbols:
            candles = one_hour.get(symbol, [])
            if len(candles) < required:
                continue
            frame = _frame(candles)
            close = frame["close"]
            latest_close = float(close.iloc[-1])
            lookback_close = float(close.iloc[-self.momentum_lookback - 1])
            if latest_close <= 0 or lookback_close <= 0:
                continue

            returns = close.pct_change().dropna()
            realized_volatility = float(
                returns.tail(self.volatility_lookback).std(ddof=0)
            )
            latest_atr = atr(frame, self.atr_period).iloc[-1]
            if pd.isna(latest_atr) or not pd.notna(realized_volatility):
                continue

            momentum = (latest_close / lookback_close) - 1.0
            atr_rate = float(latest_atr / latest_close)
            funding_rate = funding_rates.get(symbol, 0.0)
            metrics.append(
                (
                    symbol,
                    momentum,
                    max(realized_volatility, 0.0001),
                    max(atr_rate, 0.0001),
                    funding_rate,
                )
            )

        self._scores = {}
        if len(metrics) < self.min_universe_size:
            return

        ranked = sorted(metrics, key=lambda row: row[1], reverse=True)
        tail_count = max(1, int(len(ranked) * self.selection_quantile))
        tail_count = min(tail_count, len(ranked) // 2)
        long_symbols = {symbol for symbol, *_ in ranked[:tail_count]}
        short_symbols = {symbol for symbol, *_ in ranked[-tail_count:]}
        bottom_ranks = {
            symbol: index + 1
            for index, (symbol, *_rest) in enumerate(reversed(ranked[-tail_count:]))
        }

        for rank, (symbol, momentum, realized_volatility, atr_rate, funding_rate) in enumerate(
            ranked,
            start=1,
        ):
            direction = SignalDirection.HOLD
            display_rank = rank
            if symbol in long_symbols:
                direction = SignalDirection.LONG
            elif symbol in short_symbols:
                direction = SignalDirection.SHORT
                display_rank = bottom_ranks[symbol]
            else:
                continue

            recommended_leverage = max(
                1,
                int(self.target_hourly_volatility / realized_volatility),
            )
            self._scores[symbol] = CrossSectionalScore(
                symbol=symbol,
                direction=direction,
                rank=display_rank,
                universe_size=len(ranked),
                momentum=momentum,
                realized_volatility=realized_volatility,
                atr_rate=atr_rate,
                funding_rate=funding_rate,
                recommended_leverage=recommended_leverage,
            )

    def generate(
        self,
        symbol: str,
        one_hour: list[Candle],
        four_hour: list[Candle],
    ) -> Signal:
        del four_hour
        score = self._scores.get(symbol)
        if score is None:
            return _hold(
                symbol,
                "Symbol is not in the selected cross-sectional momentum tails.",
            )

        expected_move = abs(score.momentum)
        if expected_move < self.min_expected_move:
            return _hold(
                symbol,
                f"Cross-sectional momentum {expected_move:.4f} is below minimum "
                f"{self.min_expected_move:.4f}.",
                expected_move=expected_move,
            )

        if (
            score.direction == SignalDirection.LONG
            and score.funding_rate > self.max_long_funding_rate
        ):
            return _hold(
                symbol,
                f"Long funding filter blocked entry: funding {score.funding_rate:.5f} "
                f"> {self.max_long_funding_rate:.5f}.",
                expected_move=expected_move,
            )

        if (
            score.direction == SignalDirection.SHORT
            and score.funding_rate < -self.max_short_funding_rate
        ):
            return _hold(
                symbol,
                f"Short funding filter blocked entry: funding {score.funding_rate:.5f} "
                f"< {-self.max_short_funding_rate:.5f}.",
                expected_move=expected_move,
            )

        if not one_hour:
            return _hold(symbol, "No recent candle is available for entry price.")

        latest_close = float(one_hour[-1].close)
        if latest_close <= 0:
            return _hold(symbol, "Latest close is not positive.")

        stop_rate = max(score.atr_rate, score.realized_volatility * 2.0, self.min_expected_move)
        target_rate = max(stop_rate * 1.6, expected_move * 0.5)
        percentile_strength = 1.0 - ((score.rank - 1) / max(1, score.universe_size - 1))
        confidence = _bounded_confidence(
            0.58 + (0.22 * percentile_strength) + min(expected_move, 0.12)
        )
        reason = (
            "Cross-sectional momentum with funding filter: "
            f"rank={score.rank}/{score.universe_size}, "
            f"momentum={score.momentum:.4f}, "
            f"funding={score.funding_rate:.5f}, "
            f"hourly_vol={score.realized_volatility:.4f}, "
            f"recommended_leverage={score.recommended_leverage}x."
        )

        if score.direction == SignalDirection.LONG:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.LONG,
                confidence=confidence,
                reason=reason,
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close * (1.0 - stop_rate),
                target_price=latest_close * (1.0 + target_rate),
                recommended_leverage=score.recommended_leverage,
            )

        return _signal(
            symbol=symbol,
            direction=SignalDirection.SHORT,
            confidence=confidence,
            reason=reason,
            expected_move=expected_move,
            entry_price=latest_close,
            stop_price=latest_close * (1.0 + stop_rate),
            target_price=latest_close * (1.0 - target_rate),
            recommended_leverage=score.recommended_leverage,
        )


@dataclass(kw_only=True)
class FundingCarryStrategy:
    """Trade the funding rate as the primary signal, not just a filter.

    Persistently high positive funding means crowded longs paying shorts, so
    shorting both collects funding and fades a crowded book that often
    mean-reverts down (and symmetrically for deeply negative funding). The
    edge is thin per funding period, so entries need wide stops and a long
    hold (the framework timeout is ~72h ≈ 9 funding periods) for the
    collected carry to clear round-trip costs. A trend filter blocks fading a
    strong move purely for carry.
    """

    min_expected_move: float
    funding_entry: float = 0.0004  # per-8h funding magnitude to act on
    atr_period: int = 14
    mean_period: int = 24
    trend_fast: int = 12
    trend_slow: int = 48
    max_trend_gap: float = 0.02  # block fading a trend stronger than this
    stop_atr_multiple: float = 2.5
    _scores: dict[str, StrategySignal] = field(default_factory=dict, init=False)

    def prepare_universe(
        self,
        *,
        symbols: list[str],
        one_hour: dict[str, list[Candle]],
        four_hour: dict[str, list[Candle]],
        funding_rates: dict[str, float] | None = None,
        daily: dict[str, list[Candle]] | None = None,
    ) -> None:
        del four_hour, daily
        funding_rates = funding_rates or {}
        self._scores = {}
        required = max(self.atr_period + 1, self.mean_period, self.trend_slow)
        for symbol in symbols:
            candles = one_hour.get(symbol, [])
            if len(candles) < required:
                continue
            funding = funding_rates.get(symbol, 0.0)
            if abs(funding) < self.funding_entry:
                continue
            self._scores[symbol] = self._build_signal(symbol, candles, funding)

    def generate(self, symbol: str, one_hour: list[Candle], four_hour: list[Candle]) -> Signal:
        del one_hour, four_hour
        cached = self._scores.get(symbol)
        if cached is None:
            return _hold(symbol, "Funding is inside the neutral band or data is insufficient.")
        return cached

    def _build_signal(self, symbol: str, candles: list[Candle], funding: float) -> StrategySignal:
        frame = _frame(candles)
        close = frame["close"]
        latest_close = float(close.iloc[-1])
        latest_atr = atr(frame, self.atr_period).iloc[-1]
        mean = float(close.tail(self.mean_period).mean())
        fast = ema(close, self.trend_fast).iloc[-1]
        slow = ema(close, self.trend_slow).iloc[-1]
        if pd.isna(latest_atr) or latest_close <= 0 or slow <= 0:
            return _hold(symbol, "Not enough candles for stable funding-carry values.")

        atr_rate = float(latest_atr) / latest_close
        expected_move = max(atr_rate, abs(funding) * 9.0)  # ~9 funding periods over the hold
        if expected_move < self.min_expected_move:
            return _hold(symbol, "Funding-carry expected move below cost threshold.", expected_move=expected_move)

        trend_gap = float(fast - slow) / latest_close
        stop_distance = max(float(latest_atr) * self.stop_atr_multiple, latest_close * self.min_expected_move)

        # Positive funding: longs pay -> collect by shorting (fade crowded longs).
        if funding >= self.funding_entry:
            if trend_gap > self.max_trend_gap:
                return _hold(symbol, "Skip short: uptrend too strong to fade for carry.", expected_move=expected_move)
            target = min(mean, latest_close - stop_distance * 0.8)
            return _signal(
                symbol=symbol,
                direction=SignalDirection.SHORT,
                confidence=_bounded_confidence(0.55 + min(abs(funding) * 100, 0.3)),
                reason=(
                    f"Funding carry short: funding {funding:.5f}/8h (crowded longs), "
                    f"collect carry + fade toward mean {mean:.6g}."
                ),
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close + stop_distance,
                target_price=target,
            )

        # Deeply negative funding: shorts pay -> collect by going long.
        if -trend_gap > self.max_trend_gap:
            return _hold(symbol, "Skip long: downtrend too strong to fade for carry.", expected_move=expected_move)
        target = max(mean, latest_close + stop_distance * 0.8)
        return _signal(
            symbol=symbol,
            direction=SignalDirection.LONG,
            confidence=_bounded_confidence(0.55 + min(abs(funding) * 100, 0.3)),
            reason=(
                f"Funding carry long: funding {funding:.5f}/8h (crowded shorts), "
                f"collect carry + fade toward mean {mean:.6g}."
            ),
            expected_move=expected_move,
            entry_price=latest_close,
            stop_price=latest_close - stop_distance,
            target_price=target,
        )


@dataclass(kw_only=True)
class DailyTrendStrategy:
    """Longer-horizon trend on daily candles to cut turnover and fee drag.

    Uses real 1D bars (supplied via ``prepare_universe(daily=...)``) with slow
    EMAs, so signals flip a handful of times over months rather than hourly.
    Wide daily-ATR stops give trends room to breathe, and the recommended
    holding period is weeks: the framework's 72h default timeout would force
    a close-and-reenter churn every 3 days that pays round-trip costs for
    nothing (in backtests it produced 158 timeout exits out of 190 trades).
    """

    requires_daily: bool = True
    recommended_timeout_hours: int = 504  # 21 days; daily trends need weeks
    min_expected_move: float
    fast_span: int = 20
    slow_span: int = 50
    atr_period: int = 14
    stop_atr_multiple: float = 2.0
    target_atr_multiple: float = 3.0
    _scores: dict[str, StrategySignal] = field(default_factory=dict, init=False)

    def prepare_universe(
        self,
        *,
        symbols: list[str],
        one_hour: dict[str, list[Candle]],
        four_hour: dict[str, list[Candle]],
        funding_rates: dict[str, float] | None = None,
        daily: dict[str, list[Candle]] | None = None,
    ) -> None:
        del one_hour, four_hour, funding_rates
        daily = daily or {}
        self._scores = {}
        required = max(self.slow_span + 1, self.atr_period + 1)
        for symbol in symbols:
            candles = daily.get(symbol, [])
            if len(candles) < required:
                continue
            self._scores[symbol] = self._build_signal(symbol, candles)

    def generate(self, symbol: str, one_hour: list[Candle], four_hour: list[Candle]) -> Signal:
        del four_hour
        cached = self._scores.get(symbol)
        if cached is None:
            return _hold(symbol, "Not enough daily candles for daily-trend, or trend not aligned.")
        # Enter at the latest intraday price, but keep the daily-derived plan.
        if not one_hour:
            return cached
        latest_close = float(one_hour[-1].close)
        if latest_close <= 0 or cached.direction == SignalDirection.HOLD:
            return cached
        return cached

    def _build_signal(self, symbol: str, candles: list[Candle]) -> StrategySignal:
        frame = _frame(candles)
        close = frame["close"]
        latest_close = float(close.iloc[-1])
        fast = ema(close, self.fast_span).iloc[-1]
        slow = ema(close, self.slow_span).iloc[-1]
        latest_atr = atr(frame, self.atr_period).iloc[-1]
        if pd.isna(fast) or pd.isna(slow) or pd.isna(latest_atr) or latest_close <= 0:
            return _hold(symbol, "Not enough daily candles for stable daily-trend values.")

        atr_rate = float(latest_atr) / latest_close
        expected_move = max(atr_rate, abs(float(fast - slow)) / latest_close)
        if expected_move < self.min_expected_move:
            return _hold(symbol, "Daily-trend expected move below cost threshold.", expected_move=expected_move)

        stop = float(latest_atr) * self.stop_atr_multiple
        target = float(latest_atr) * self.target_atr_multiple
        if fast > slow and latest_close > slow:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.LONG,
                confidence=_bounded_confidence(0.55 + min(expected_move * 4, 0.3)),
                reason="Daily-trend long: fast daily EMA above slow with price above the slow EMA.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close - stop,
                target_price=latest_close + target,
            )
        if fast < slow and latest_close < slow:
            return _signal(
                symbol=symbol,
                direction=SignalDirection.SHORT,
                confidence=_bounded_confidence(0.55 + min(expected_move * 4, 0.3)),
                reason="Daily-trend short: fast daily EMA below slow with price below the slow EMA.",
                expected_move=expected_move,
                entry_price=latest_close,
                stop_price=latest_close + stop,
                target_price=latest_close - target,
            )
        return _hold(symbol, "Daily-trend EMAs not aligned.", expected_move=expected_move)


def create_strategy(name: str, *, min_expected_move: float):
    match name:
        case "ema-rsi-atr":
            return EmaRsiAtrStrategy(min_expected_move=min_expected_move)
        case "rsi-bollinger-reversion":
            return RsiBollingerReversionStrategy(min_expected_move=min_expected_move)
        case "donchian-breakout":
            return DonchianBreakoutStrategy(min_expected_move=min_expected_move)
        case "ema-momentum":
            return EmaMomentumStrategy(min_expected_move=min_expected_move)
        case "multi-timeframe-trend":
            return MultiTimeframeTrendStrategy(min_expected_move=min_expected_move)
        case "volatility-adjusted-momentum":
            return VolatilityAdjustedMomentumStrategy(min_expected_move=min_expected_move)
        case "cross-sectional-momentum-funding":
            return CrossSectionalMomentumFundingStrategy(min_expected_move=min_expected_move)
        case "funding-carry":
            return FundingCarryStrategy(min_expected_move=min_expected_move)
        case "daily-trend":
            return DailyTrendStrategy(min_expected_move=min_expected_move)
        case _:
            choices = ", ".join(AVAILABLE_STRATEGIES)
            raise ValueError(f"Unknown strategy {name!r}. Choose one of: {choices}")


def _frame(candles: list[Candle]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "high": [candle.high for candle in candles],
            "low": [candle.low for candle in candles],
            "close": [candle.close for candle in candles],
        }
    )


def _signal(
    *,
    symbol: str,
    direction: SignalDirection,
    confidence: float,
    reason: str,
    expected_move: float,
    entry_price: float,
    stop_price: float,
    target_price: float,
    recommended_leverage: int | None = None,
) -> StrategySignal:
    return StrategySignal(
        symbol=symbol,
        timeframe="1H",
        direction=direction,
        confidence=confidence,
        reason=reason,
        created_at=datetime.now(UTC),
        expected_move=expected_move,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        recommended_leverage=recommended_leverage,
    )


def _hold(symbol: str, reason: str, *, expected_move: float = 0.0) -> StrategySignal:
    return StrategySignal(
        symbol=symbol,
        timeframe="1H",
        direction=SignalDirection.HOLD,
        confidence=0.0,
        reason=reason,
        created_at=datetime.now(UTC),
        expected_move=expected_move,
        stop_price=None,
        target_price=None,
    )


def _bounded_confidence(value: float) -> float:
    return round(max(0.0, min(value, 0.95)), 4)
