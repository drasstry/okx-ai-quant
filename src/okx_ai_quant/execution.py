from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from math import isfinite
from time import sleep
from typing import Any, Callable, Protocol
from uuid import uuid4

from okx_ai_quant.models import OrderRecord, OrderState, PositionRecord, RiskStatus, SignalDirection


class ExecutableRiskDecision(Protocol):
    signal_id: int | None
    symbol: str
    direction: SignalDirection
    status: RiskStatus
    position_size_usdt: float
    leverage: int


OrderSizer = Callable[[ExecutableRiskDecision], str]


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
    stop_loss: float | None = None
    take_profit: float | None = None
    id: int | None = None


class ExecutionEngine:
    def __init__(
        self,
        client,
        *,
        order_sizer: OrderSizer | None = None,
        margin_mode: str = "cross",
        max_order_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
    ):
        margin_mode = str(margin_mode).lower()
        if margin_mode not in {"cross", "isolated"}:
            raise ValueError("margin_mode must be either 'cross' or 'isolated'")
        self.client = client
        self._order_sizer = order_sizer or InstrumentAwareOrderSizer(client)
        self.margin_mode = margin_mode
        self.max_order_attempts = max(1, max_order_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._configured_leverage: set[tuple[str, str, int]] = set()

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

        order_size = self._order_sizer(decision)
        self._ensure_exchange_leverage(decision)
        client_order_id = f"okxai{uuid4().hex[:20]}"
        params = {
            "instId": decision.symbol,
            "tdMode": self.margin_mode,
            "side": side,
            "ordType": "market",
            "sz": order_size,
            "clOrdId": client_order_id,
        }
        attached_algo = _attached_stop_orders(decision)
        if attached_algo:
            params["attachAlgoOrds"] = attached_algo
        response = self._place_order_with_retries(**params)
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
            quantity=float(order_size),
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
        response = self._submit_close_order(
            position=position,
            side=side,
            quantity=quantity,
            client_order_id=client_order_id,
        )
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

    def _ensure_exchange_leverage(self, decision: ExecutableRiskDecision) -> None:
        key = (decision.symbol, self.margin_mode, int(decision.leverage))
        if key in self._configured_leverage:
            return

        set_leverage = getattr(self.client, "set_leverage", None)
        if not callable(set_leverage):
            raise RuntimeError("OKX leverage configuration is required before submitting orders")

        self._set_leverage_with_retries(
            decision.symbol,
            leverage=int(decision.leverage),
            margin_mode=self.margin_mode,
        )
        self._configured_leverage.add(key)

    def _set_leverage_with_retries(self, symbol: str, *, leverage: int, margin_mode: str):
        attempts = self.max_order_attempts
        delay_seconds = self.retry_delay_seconds
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                return self.client.set_leverage(
                    symbol,
                    leverage=leverage,
                    margin_mode=margin_mode,
                )
            except Exception as exc:  # noqa: BLE001 - same transient exchange boundary as orders.
                last_error = exc
                if attempt >= attempts or not _is_transient_execution_error(exc):
                    raise
                sleep(delay_seconds)
                delay_seconds *= 2
        raise RuntimeError(f"Leverage setting failed after retries: {last_error}")

    def _submit_close_order(
        self,
        *,
        position: PositionRecord,
        side: str,
        quantity: float,
        client_order_id: str,
    ):
        last_error = None
        primary_margin_mode = position.margin_mode or self.margin_mode
        for td_mode in _close_td_modes(primary_margin_mode):
            try:
                return self._place_order_with_retries(
                    instId=position.symbol,
                    tdMode=td_mode,
                    side=side,
                    ordType="market",
                    sz=str(quantity),
                    reduceOnly="true",
                    clOrdId=client_order_id,
                )
            except Exception as exc:  # noqa: BLE001 - allow isolated/cross fallback for legacy positions.
                last_error = exc
                if _is_transient_execution_error(exc):
                    raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("No tdMode configured for close order")

    def _place_order_with_retries(self, **params):
        attempts = self.max_order_attempts
        delay_seconds = self.retry_delay_seconds
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                place_order = getattr(self.client, "place_order", None)
                if callable(place_order):
                    return place_order(**params)
                response = self.client.trade_api.place_order(**params)
                self.client._raise_for_error(response)
                return response
            except Exception as exc:  # noqa: BLE001 - classify below, then retry only transients.
                last_error = exc
                if attempt >= attempts or not _is_transient_execution_error(exc):
                    raise
                sleep(delay_seconds)
                delay_seconds *= 2
        raise RuntimeError(f"Order placement failed after retries: {last_error}")



@dataclass(frozen=True, kw_only=True)
class InstrumentSpec:
    symbol: str
    inst_type: str
    base_ccy: str
    quote_ccy: str
    contract_type: str
    contract_value: Decimal
    contract_value_ccy: str
    lot_size: Decimal
    min_size: Decimal


class InstrumentAwareOrderSizer:
    """Convert a USDT notional risk decision into OKX contract ``sz``."""

    def __init__(self, client, *, inst_type: str = "SWAP") -> None:
        self.client = client
        self.inst_type = inst_type
        self._specs: dict[str, InstrumentSpec] = {}

    def __call__(self, decision: ExecutableRiskDecision) -> str:
        if not isfinite(decision.position_size_usdt) or decision.position_size_usdt <= 0:
            raise ValueError("position_size_usdt must be positive")

        spec = self._spec_for(decision.symbol)
        notional = _decimal_from_float(decision.position_size_usdt, "position_size_usdt")
        contract_notional = self._contract_notional_usdt(spec)
        raw_contracts = notional / contract_notional
        contracts = _floor_to_lot(raw_contracts, spec.lot_size)
        if contracts < spec.min_size:
            raise ValueError(
                f"Order size {contracts} contracts is below {decision.symbol} minSz "
                f"{spec.min_size}; increase position size or reduce the symbol list."
            )
        return _format_decimal(contracts)

    def _spec_for(self, symbol: str) -> InstrumentSpec:
        if symbol in self._specs:
            return self._specs[symbol]

        get_instruments = getattr(self.client, "get_instruments", None)
        if not callable(get_instruments):
            raise RuntimeError("OKX instrument metadata is required before submitting orders")

        rows = get_instruments(self.inst_type)
        for row in rows:
            if isinstance(row, dict) and row.get("instId") == symbol:
                spec = _instrument_spec_from_row(row)
                self._specs[symbol] = spec
                return spec
        raise RuntimeError(f"Could not find OKX {self.inst_type} instrument metadata for {symbol}")

    def _contract_notional_usdt(self, spec: InstrumentSpec) -> Decimal:
        if spec.contract_value <= 0:
            raise ValueError(f"{spec.symbol} ctVal must be positive")

        contract_value_ccy = spec.contract_value_ccy.upper()
        quote_ccy = spec.quote_ccy.upper()
        base_ccy = spec.base_ccy.upper()

        if spec.contract_type == "inverse" or contract_value_ccy in {quote_ccy, "USD", "USDT", "USDC"}:
            return spec.contract_value

        if contract_value_ccy == base_ccy:
            return spec.contract_value * self._last_price(spec.symbol)

        raise RuntimeError(
            f"Unsupported contract value currency for {spec.symbol}: {spec.contract_value_ccy}"
        )

    def _last_price(self, symbol: str) -> Decimal:
        get_ticker = getattr(self.client, "get_ticker", None)
        if not callable(get_ticker):
            raise RuntimeError("OKX ticker price is required to size base-denominated contracts")

        ticker = get_ticker(symbol)
        price = _first_positive_decimal(ticker, ("last", "lastPx", "markPx"))
        if price is not None:
            return price

        bid = _first_positive_decimal(ticker, ("bidPx",))
        ask = _first_positive_decimal(ticker, ("askPx",))
        if bid is not None and ask is not None:
            return (bid + ask) / Decimal("2")

        raise RuntimeError(f"Could not resolve a positive ticker price for {symbol}")


def _attached_stop_orders(decision: ExecutableRiskDecision) -> list[dict[str, str]]:
    """Build OKX ``attachAlgoOrds`` so SL/TP live on the exchange.

    Without this the stop only exists in the local bot loop: it fires with up
    to one poll interval of delay and not at all when the bot process is down.
    ``-1`` order prices request market execution on trigger.
    """
    stop_loss = getattr(decision, "stop_loss", None)
    take_profit = getattr(decision, "take_profit", None)
    algo: dict[str, str] = {}
    if stop_loss is not None and isfinite(stop_loss) and stop_loss > 0:
        algo["slTriggerPx"] = f"{stop_loss:.10g}"
        algo["slOrdPx"] = "-1"
    if take_profit is not None and isfinite(take_profit) and take_profit > 0:
        algo["tpTriggerPx"] = f"{take_profit:.10g}"
        algo["tpOrdPx"] = "-1"
    return [algo] if algo else []


def to_okx_order_size(decision: ExecutableRiskDecision) -> str:
    """Return the raw decision size as ``sz``.

    This legacy helper is kept for tests and explicitly injected fixtures only.
    Production order submission uses ``InstrumentAwareOrderSizer`` so OKX SWAP
    orders are sized in contracts using ``ctVal``/``lotSz``/``minSz``.
    """
    if not isfinite(decision.position_size_usdt) or decision.position_size_usdt <= 0:
        raise ValueError("position_size_usdt must be positive")
    return str(decision.position_size_usdt)


def _instrument_spec_from_row(row: dict[str, Any]) -> InstrumentSpec:
    symbol = str(row.get("instId") or "")
    parts = symbol.split("-")
    base_ccy = str(row.get("baseCcy") or (parts[0] if parts else "")).upper()
    quote_ccy = str(row.get("quoteCcy") or (parts[1] if len(parts) > 1 else "")).upper()
    return InstrumentSpec(
        symbol=symbol,
        inst_type=str(row.get("instType") or ""),
        base_ccy=base_ccy,
        quote_ccy=quote_ccy,
        contract_type=str(row.get("ctType") or "linear").lower(),
        contract_value=_decimal_from_text(row.get("ctVal"), "ctVal"),
        contract_value_ccy=str(row.get("ctValCcy") or base_ccy).upper(),
        lot_size=_decimal_from_text(row.get("lotSz"), "lotSz"),
        min_size=_decimal_from_text(row.get("minSz"), "minSz"),
    )


def _decimal_from_float(value: float, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a valid decimal") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _decimal_from_text(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value or ""))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a valid decimal") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _first_positive_decimal(row: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = Decimal(str(value))
        except InvalidOperation:
            continue
        if parsed > 0:
            return parsed
    return None


def _floor_to_lot(value: Decimal, lot_size: Decimal) -> Decimal:
    lots = (value / lot_size).to_integral_value(rounding=ROUND_FLOOR)
    return lots * lot_size


def _format_decimal(value: Decimal) -> str:
    formatted = format(value.normalize(), "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def _close_td_modes(primary: str) -> tuple[str, ...]:
    """Try the configured mode first, then the other mode for older positions."""
    primary = primary.lower()
    if primary == "cross":
        return ("cross", "isolated")
    if primary == "isolated":
        return ("isolated", "cross")
    return (primary,)


def _is_transient_execution_error(exc: Exception) -> bool:
    message = str(exc).lower()
    transient_markers = (
        "all operations failed",
        "connection",
        "connecterror",
        "eof occurred",
        "ssl",
        "timed out",
        "timeout",
        "temporarily",
        "network",
        "remote protocol error",
    )
    return isinstance(exc, (TimeoutError, OSError)) or any(
        marker in message for marker in transient_markers
    )


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
