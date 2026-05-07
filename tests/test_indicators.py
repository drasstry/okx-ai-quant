import math

import pandas as pd

from okx_ai_quant.indicators import atr, ema, rsi


def test_ema_preserves_length_and_uses_recursive_final_value():
    values = pd.Series([10.0, 11.0, 12.0, 13.0])

    result = ema(values, span=3)

    assert len(result) == len(values)
    assert result.iloc[-1] == 12.125


def test_rsi_stays_within_zero_to_one_hundred():
    values = pd.Series(
        [44.0, 45.0, 46.0, 45.5, 47.0, 48.0, 47.5, 49.0, 50.0, 51.0, 50.5, 52.0]
    )

    result = rsi(values, period=5).dropna()

    assert not result.empty
    assert result.between(0.0, 100.0).all()


def test_atr_uses_true_range_with_previous_close_gaps():
    frame = pd.DataFrame(
        [
            {"high": 10.0, "low": 8.0, "close": 9.0},
            {"high": 13.0, "low": 9.0, "close": 12.0},
            {"high": 14.0, "low": 11.0, "close": 13.0},
        ]
    )

    result = atr(frame, period=2)

    assert math.isnan(result.iloc[0])
    assert result.iloc[1] == 3.0
    assert result.iloc[2] == 3.5
