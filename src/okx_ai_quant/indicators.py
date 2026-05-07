import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    if span <= 0:
        raise ValueError("span must be positive")

    return series.astype(float).ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    if period <= 0:
        raise ValueError("period must be positive")

    values = series.astype(float)
    delta = values.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)

    average_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    average_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    relative_strength = average_gain / average_loss
    result = 100.0 - (100.0 / (1.0 + relative_strength))
    result = result.mask((average_loss == 0.0) & (average_gain > 0.0), 100.0)
    result = result.mask((average_gain == 0.0) & (average_loss > 0.0), 0.0)
    result = result.mask((average_gain == 0.0) & (average_loss == 0.0), 50.0)
    return result.clip(lower=0.0, upper=100.0)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    if period <= 0:
        raise ValueError("period must be positive")

    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    close = frame["close"].astype(float)
    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window=period, min_periods=period).mean()
