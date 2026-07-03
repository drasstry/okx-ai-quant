from datetime import UTC, datetime
from typing import Sequence

from okx_ai_quant.models import Candle, Ticker


def _utc_from_ms(value: str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def normalize_candles(symbol: str, timeframe: str, rows: Sequence[Sequence[str]]) -> list[Candle]:
    candles = [
        Candle(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=_utc_from_ms(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in rows
        if _is_confirmed(row)
    ]
    return sorted(candles, key=lambda candle: candle.timestamp)


def _is_confirmed(row: Sequence[str]) -> bool:
    # OKX candle rows carry a trailing confirm flag ("0" while the candle is
    # still forming). Signals must only see completed candles, otherwise every
    # strategy repaints intra-candle.
    if len(row) < 9:
        return True
    return str(row[-1]) != "0"


def _optional_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def normalize_ticker(row: dict[str, str | None]) -> Ticker:
    return Ticker(
        symbol=row["instId"],
        timestamp=_utc_from_ms(row["ts"]),
        bid=_optional_float(row.get("bidPx")),
        ask=_optional_float(row.get("askPx")),
        last=float(row["last"]),
        volume_24h=float(row.get("vol24h", "0")),
    )
