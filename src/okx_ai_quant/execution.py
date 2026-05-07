from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import Callable, Protocol
from uuid import uuid4

from okx_ai_quant.models import OrderRecord, OrderState, PositionRecord, RiskStatus, SignalDirection


class ExecutableRiskDecision(Protocol):
    signal_id: int | None
    symbol: str
    direction: SignalDirection
    status: RiskStatus
    position_size_usdt: float


OrderSizer = Callable[[ExecutableRiskDecision, bool], str]


@dataclass(frozen=True, kw_only=True)
class ExecutionDecision:
    signal_id: int | None
    symbol: str
    direction: SignalDirection
    status: RiskStatus
    reason: str
    position_size_usdt: float
    leverage: int
    created_at: datetime
    id: int | None = None


class ExecutionEngine:
    def __init__(self, client, *, order_sizer: OrderSizer | None = None):
        self.client = client
        self._order_sizer = order_sizer

    def submit(self, decision: ExecutableRiskDecision) -> OrderRecord:
        if decision.status != RiskStatus.APPROVED:
            raise ValueError("Only APPROVED risk decisions can be submitted")
        is_live = _is_live_client(self.client)
        if is_live and not _live_execution_allowed(self.client):
            raise RuntimeError("Live execution requires ALLOW_LIVE_TRADING=true")

        if decision.direction == SignalDirection.LONG:
            side = "buy"
        elif decision.direction == SignalDirection.SHORT:
            side = "sell"
        else:
            raise ValueError("Only LONG/SHORT decisions can be submitted")

        sizer = self._order_sizer or to_okx_order_size
        order_size = sizer(decision, is_live) if self._order_sizer else sizer(decision)
        client_order_id = f"okxai{uuid4().hex[:20]}"
        response = self.client.trade_api.place_order(
            instId=decision.symbol,
            tdMode="isolated",
            side=side,
            ordType="market",
            sz=order_size,
            clOrdId=client_order_id,
        )
        self.client._raise_for_error(response)
        order_id = None
        data = response.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            order_id = data[0].get("ordId")

        return OrderRecord(
            signal_id=decision.signal_id,
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=decision.symbol,
            side=decision.direction,
            quantity=float(decision.position_size_usdt),
            state=OrderState.SUBMITTED,
            created_at=datetime.now(UTC),
        )

    def close_position(self, position: PositionRecord, *, reason: str) -> OrderRecord:
        is_live = _is_live_client(self.client)
        if is_live and not _live_execution_allowed(self.client):
            raise RuntimeError("Live execution requires ALLOW_LIVE_TRADING=true")

        if position.side == SignalDirection.LONG:
            side = "sell"
            close_direction = SignalDirection.SHORT
        elif position.side == SignalDirection.SHORT:
            side = "buy"
            close_direction = SignalDirection.LONG
        else:
            raise ValueError("Only LONG/SHORT positions can be closed")

        quantity = abs(position.quantity)
        if not isfinite(quantity) or quantity <= 0:
            raise ValueError("position quantity must be positive")

        client_order_id = f"okxaiclose{uuid4().hex[:15]}"
        response = self.client.trade_api.place_order(
            instId=position.symbol,
            tdMode="isolated",
            side=side,
            ordType="market",
            sz=str(quantity),
            reduceOnly="true",
            clOrdId=client_order_id,
        )
        self.client._raise_for_error(response)
        order_id = None
        data = response.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            order_id = data[0].get("ordId")

        return OrderRecord(
            signal_id=position.entry_signal_id,
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=position.symbol,
            side=close_direction,
            quantity=quantity,
            state=OrderState.SUBMITTED,
            order_type=f"market_close:{reason}",
            created_at=datetime.now(UTC),
        )


def to_okx_order_size(decision: ExecutableRiskDecision) -> str:
    """Return OKX ``sz`` for the demo MVP.

    The MVP uses the risk decision's USDT notional as a placeholder size.
    For OKX perpetual swaps ``sz`` is the number of contracts, which depends
    on each instrument's ``ctVal``/``lotSz``/``minSz``. This demo sizer does
    NOT perform that contract-size conversion and must be replaced with an
    instrument-aware ``OrderSizer`` (via ``ExecutionEngine(order_sizer=...)``)
    before running against real capital.
    """
    if not isfinite(decision.position_size_usdt) or decision.position_size_usdt <= 0:
        raise ValueError("position_size_usdt must be positive")
    return str(decision.position_size_usdt)


def _is_live_client(client) -> bool:
    """Return True when the client targets OKX live trading.

    Priority:
    1. Use ``settings.trading_mode`` when available (single source of truth).
    2. Fall back to the legacy OKX flag string, where ``"0"`` denotes live
       (``"1"`` denotes the demo/paper environment).
    """
    settings = getattr(client, "settings", None)
    trading_mode = getattr(settings, "trading_mode", None)
    if trading_mode is not None:
        return str(trading_mode).lower() == "live"

    return getattr(client, "flag", None) == "0"


def _live_execution_allowed(client) -> bool:
    settings = getattr(client, "settings", None)
    return bool(getattr(settings, "ALLOW_LIVE_TRADING", False))
