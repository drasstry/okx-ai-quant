from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from okx_ai_quant.execution import ExecutionEngine, to_okx_order_size
from okx_ai_quant.models import Candle, OrderState, RiskStatus, Signal, SignalDirection
from okx_ai_quant.risk import RiskDecision, RiskState
from okx_ai_quant.runner import Runner


def _okx_row(minute: int, close: str = "101") -> list[str]:
    timestamp_ms = str(int(datetime(2026, 5, 6, 8, minute, tzinfo=UTC).timestamp() * 1000))
    return [timestamp_ms, "100", "102", "99", close, "12.5"]


class FakeClient:
    def __init__(self):
        self.candle_calls = []
        self.trade_api = FakeTradeApi()
        self.raise_checked = []
        self.flag = "1"
        self.settings = None

    def get_candles(self, symbol, bar, limit):
        self.candle_calls.append((symbol, bar, limit))
        return [_okx_row(1), _okx_row(0)]

    def _raise_for_error(self, response):
        self.raise_checked.append(response)
        if response["code"] != "0":
            raise RuntimeError(response["msg"])


class FakeTradeApi:
    def __init__(self):
        self.calls = []
        self.response = {"code": "0", "data": [{"ordId": "okx-order-1"}]}

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeStorage:
    def __init__(self):
        self.candles = []
        self.signals = []
        self.risk_decisions = []
        self.trade_analyses = []
        self.orders = []
        self.next_signal_id = 100

    def upsert_candle(self, **kwargs):
        candle = Candle(id=len(self.candles) + 1, **kwargs)
        self.candles.append(candle)
        return candle

    def insert_signal(self, signal):
        signal_id = self.next_signal_id
        self.next_signal_id += 1
        self.signals.append(signal)
        return signal_id

    def insert_risk_decision(self, decision):
        self.risk_decisions.append(decision)
        return len(self.risk_decisions)

    def insert_trade_analysis(self, analysis):
        self.trade_analyses.append(analysis)
        return len(self.trade_analyses)

    def insert_order(self, order):
        self.orders.append(order)
        return len(self.orders)


class FakeStrategy:
    def __init__(self, direction):
        self.direction = direction
        self.calls = []

    def generate(self, symbol, one_hour, four_hour):
        self.calls.append((symbol, one_hour, four_hour))
        return Signal(
            symbol=symbol,
            timeframe="1H",
            direction=self.direction,
            confidence=0.8 if self.direction != SignalDirection.HOLD else 0.0,
            reason="Strategy fixture.",
            created_at=datetime(2026, 5, 6, 8, 2, tzinfo=UTC),
        )


class FakeRiskGuard:
    def __init__(self, status):
        self.status = status
        self.calls = []

    def evaluate(self, signal, state):
        self.calls.append((signal, state))
        return RiskDecision(
            signal_id=signal.id,
            status=self.status,
            reason="Risk fixture.",
            position_size_usdt=125.5 if self.status == RiskStatus.APPROVED else 0.0,
            leverage=3,
            created_at=datetime(2026, 5, 6, 8, 3, tzinfo=UTC),
        )


@dataclass(frozen=True, kw_only=True)
class ExecutionDecision:
    signal_id: int
    symbol: str
    direction: SignalDirection
    status: RiskStatus
    reason: str
    position_size_usdt: float
    leverage: int
    created_at: datetime


def test_execution_engine_maps_long_short_to_buy_sell():
    client = FakeClient()
    engine = ExecutionEngine(client)

    long_order = engine.submit(
        ExecutionDecision(
            signal_id=11,
            symbol="BTC-USDT-SWAP",
            direction=SignalDirection.LONG,
            status=RiskStatus.APPROVED,
            reason="approved",
            position_size_usdt=42.25,
            leverage=2,
            created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
        )
    )
    short_order = engine.submit(
        ExecutionDecision(
            signal_id=12,
            symbol="ETH-USDT-SWAP",
            direction=SignalDirection.SHORT,
            status=RiskStatus.APPROVED,
            reason="approved",
            position_size_usdt=55.0,
            leverage=2,
            created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
        )
    )

    assert client.trade_api.calls[0] == {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "isolated",
        "side": "buy",
        "ordType": "market",
        "sz": "42.25",
        "clOrdId": client.trade_api.calls[0]["clOrdId"],
    }
    assert client.trade_api.calls[0]["clOrdId"].startswith("okxai")
    assert client.trade_api.calls[0]["clOrdId"].isalnum()
    assert client.trade_api.calls[1]["side"] == "sell"
    assert client.trade_api.calls[1]["sz"] == "55.0"
    assert client.raise_checked == [
        {"code": "0", "data": [{"ordId": "okx-order-1"}]},
        {"code": "0", "data": [{"ordId": "okx-order-1"}]},
    ]
    assert long_order.state == OrderState.SUBMITTED
    assert long_order.order_id == "okx-order-1"
    assert long_order.created_at.tzinfo is UTC
    assert short_order.state == OrderState.SUBMITTED


def test_execution_engine_rejects_non_approved_decision_without_placing_order():
    client = FakeClient()
    engine = ExecutionEngine(client)

    with pytest.raises(ValueError, match="APPROVED"):
        engine.submit(
            ExecutionDecision(
                signal_id=13,
                symbol="BTC-USDT-SWAP",
                direction=SignalDirection.LONG,
                status=RiskStatus.REJECTED,
                reason="rejected",
                position_size_usdt=42.25,
                leverage=2,
                created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
            )
        )

    assert client.trade_api.calls == []


def test_to_okx_order_size_returns_mvp_size_string_and_rejects_non_positive_size():
    decision = ExecutionDecision(
        signal_id=14,
        symbol="BTC-USDT-SWAP",
        direction=SignalDirection.LONG,
        status=RiskStatus.APPROVED,
        reason="approved",
        position_size_usdt=42.25,
        leverage=2,
        created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
    )

    assert to_okx_order_size(decision) == "42.25"
    assert "USDT notional" in (to_okx_order_size.__doc__ or "")
    assert "contract-size conversion" in (to_okx_order_size.__doc__ or "")

    with pytest.raises(ValueError, match="positive"):
        to_okx_order_size(
            ExecutionDecision(
                signal_id=15,
                symbol="BTC-USDT-SWAP",
                direction=SignalDirection.LONG,
                status=RiskStatus.APPROVED,
                reason="approved",
                position_size_usdt=0.0,
                leverage=2,
                created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
            )
        )


def test_execution_engine_rejects_live_mode_without_explicit_allow_live():
    client = FakeClient()
    client.flag = "0"
    engine = ExecutionEngine(client)

    with pytest.raises(RuntimeError, match="ALLOW_LIVE_TRADING=true"):
        engine.submit(
            ExecutionDecision(
                signal_id=16,
                symbol="BTC-USDT-SWAP",
                direction=SignalDirection.LONG,
                status=RiskStatus.APPROVED,
                reason="approved",
                position_size_usdt=42.25,
                leverage=2,
                created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
            )
        )

    assert client.trade_api.calls == []


def test_execution_engine_allows_live_mode_when_explicitly_enabled():
    class FakeSettings:
        trading_mode = "live"
        ALLOW_LIVE_TRADING = True

    client = FakeClient()
    client.settings = FakeSettings()
    engine = ExecutionEngine(client)

    order = engine.submit(
        ExecutionDecision(
            signal_id=17,
            symbol="BTC-USDT-SWAP",
            direction=SignalDirection.LONG,
            status=RiskStatus.APPROVED,
            reason="approved",
            position_size_usdt=42.25,
            leverage=2,
            created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
        )
    )

    assert client.trade_api.calls[0]["instId"] == "BTC-USDT-SWAP"
    assert client.trade_api.calls[0]["side"] == "buy"
    assert order.order_id == "okx-order-1"


def test_runner_persists_signal_analysis_and_submits_approved_order():
    client = FakeClient()
    storage = FakeStorage()
    runner = Runner(
        client=client,
        storage=storage,
        strategy=FakeStrategy(SignalDirection.LONG),
        risk_guard=FakeRiskGuard(RiskStatus.APPROVED),
        execution_engine=ExecutionEngine(client),
        risk_state=RiskState(open_positions=0),
        candle_limit=2,
    )

    result = runner.run_once("BTC-USDT-SWAP")

    assert client.candle_calls == [
        ("BTC-USDT-SWAP", "1H", 2),
        ("BTC-USDT-SWAP", "4H", 2),
    ]
    assert len(storage.candles) == 4
    assert storage.signals[0].id is None
    assert storage.risk_decisions[0].signal_id == 100
    assert storage.trade_analyses[0].signal_id == 100
    assert storage.trade_analyses[0].english.strip()
    assert storage.trade_analyses[0].chinese.strip()
    assert len(client.trade_api.calls) == 1
    assert storage.orders[0].state == OrderState.SUBMITTED
    assert result.order is storage.orders[0]


def test_runner_records_analysis_but_does_not_submit_rejected_order():
    client = FakeClient()
    storage = FakeStorage()
    runner = Runner(
        client=client,
        storage=storage,
        strategy=FakeStrategy(SignalDirection.HOLD),
        risk_guard=FakeRiskGuard(RiskStatus.REJECTED),
        execution_engine=ExecutionEngine(client),
        risk_state=RiskState(open_positions=1),
        candle_limit=2,
    )

    result = runner.run_once("BTC-USDT-SWAP")

    assert len(storage.signals) == 1
    assert storage.risk_decisions[0].status == RiskStatus.REJECTED
    assert storage.trade_analyses[0].signal_id == 100
    assert storage.trade_analyses[0].english.strip()
    assert storage.trade_analyses[0].chinese.strip()
    assert client.trade_api.calls == []
    assert storage.orders == []
    assert result.order is None
