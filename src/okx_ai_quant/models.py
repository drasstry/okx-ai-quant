from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class SignalDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"
    HOLD = "HOLD"


class RiskStatus(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class OrderState(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"


class PositionState(StrEnum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    REVERSE_SIGNAL = "REVERSE_SIGNAL"
    TIMEOUT = "TIMEOUT"
    RISK_EXIT = "RISK_EXIT"


@dataclass(frozen=True, kw_only=True)
class Candle:
    symbol: str
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    id: int | None = None


@dataclass(frozen=True, kw_only=True)
class Ticker:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    last: float
    volume_24h: float
    id: int | None = None


@dataclass(frozen=True, kw_only=True)
class Signal:
    symbol: str
    timeframe: str
    direction: SignalDirection
    confidence: float
    reason: str
    created_at: datetime
    id: int | None = None


@dataclass(frozen=True, kw_only=True)
class OrderRequest:
    symbol: str
    side: SignalDirection
    quantity: float
    order_type: str
    created_at: datetime
    signal_id: int | None = None
    id: int | None = None


@dataclass(frozen=True, kw_only=True)
class OrderRecord:
    symbol: str
    side: SignalDirection
    quantity: float
    state: OrderState
    created_at: datetime
    signal_id: int | None = None
    order_id: str | None = None
    client_order_id: str | None = None
    order_type: str = "market"
    price: float | None = None
    id: int | None = None


@dataclass(frozen=True, kw_only=True)
class PositionRecord:
    symbol: str
    side: SignalDirection
    quantity: float
    average_entry: float
    state: PositionState
    opened_at: datetime
    updated_at: datetime
    entry_signal_id: int | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    expires_at: datetime | None = None
    margin_mode: str | None = None
    leverage: int | None = None
    exit_reason: ExitReason | None = None
    closed_at: datetime | None = None
    realized_pnl: float | None = None
    id: int | None = None


@dataclass(frozen=True, kw_only=True)
class PositionExitRecord:
    position_id: int | None
    symbol: str
    side: SignalDirection
    reason: ExitReason
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl: float
    opened_at: datetime
    closed_at: datetime
    notes: str
    id: int | None = None


@dataclass(frozen=True, kw_only=True)
class TradeAnalysis:
    signal_id: int
    english: str
    chinese: str
    created_at: datetime
    id: int | None = None


@dataclass(frozen=True, kw_only=True)
class FillRecord:
    order_id: str
    symbol: str
    price: float
    quantity: float
    fee: float
    created_at: datetime
    id: int | None = None


@dataclass(frozen=True, kw_only=True)
class BalanceSnapshot:
    currency: str
    available: float
    equity: float
    updated_at: datetime
    id: int | None = None
