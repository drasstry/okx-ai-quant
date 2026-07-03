from dataclasses import dataclass
from datetime import UTC, date, datetime

from okx_ai_quant.bot import TradingBot, _daily_report_key, _due_report_slots
from okx_ai_quant.config import Settings
from okx_ai_quant.execution import ExecutionEngine
from okx_ai_quant.models import (
    Candle,
    ExitReason,
    OrderRecord,
    OrderState,
    PositionExitRecord,
    PositionRecord,
    PositionState,
    RiskStatus,
    Signal,
    SignalDirection,
)
from okx_ai_quant.notifier import NullNotifier
from okx_ai_quant.risk import RiskDecision, RiskState
from okx_ai_quant.runner import Runner
from okx_ai_quant.storage import SQLiteStorage
from okx_ai_quant.strategy import StrategySignal


def _okx_row(minute: int, close: str = "101") -> list[str]:
    timestamp_ms = str(int(datetime(2026, 5, 7, 8, minute, tzinfo=UTC).timestamp() * 1000))
    return [timestamp_ms, "100", "102", "99", close, "12.5"]


class FakeClient:
    def __init__(self) -> None:
        self.trade_api = FakeTradeApi()
        self.flag = "1"
        self.settings = None
        self.pending = {"data": []}
        self.order_response = {"data": []}
        self.instruments = {
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "state": "live",
                    "instType": "SWAP",
                    "ctType": "linear",
                    "ctVal": "0.01",
                    "ctValCcy": "BTC",
                    "lotSz": "0.01",
                    "minSz": "0.01",
                }
            ]
        }
        self.ticker_last = "101"
        self.positions_response = {"data": []}
        self.leverage_calls = []
        self.connectivity_ok = True
        self.connectivity_detail = "OKX connectivity healthy (5/5)"
        self.connectivity_checks = []

    def sync_server_time(self) -> int:
        return 0

    def check_connectivity(self, *, attempts=5, required_successes=5, symbol="BTC-USDT-SWAP"):
        self.connectivity_checks.append(
            {"attempts": attempts, "required_successes": required_successes, "symbol": symbol}
        )
        return self.connectivity_ok, self.connectivity_detail

    def get_candles(self, symbol, bar, limit):
        return [_okx_row(0), _okx_row(1)]

    def get_pending_orders(self, symbol=None):
        return self.pending

    def get_order(self, symbol, *, order_id=None, client_order_id=None):
        return self.order_response

    def get_ticker(self, symbol):
        return {
            "instId": symbol,
            "ts": str(int(datetime(2026, 5, 7, 8, 5, tzinfo=UTC).timestamp() * 1000)),
            "bidPx": self.ticker_last,
            "askPx": self.ticker_last,
            "last": self.ticker_last,
            "vol24h": "1000",
        }

    def get_balance(self):
        return {
            "data": [
                {
                    "totalEq": "1000",
                    "details": [{"ccy": "USDT", "eq": "1000", "availEq": "900"}],
                }
            ]
        }

    def get_positions(self, *, inst_type=None, symbol=None):
        return self.positions_response

    def get_instruments(self, inst_type):
        return self.instruments["data"]

    def set_leverage(self, symbol, *, leverage, margin_mode, position_side=None):
        self.leverage_calls.append(
            {
                "symbol": symbol,
                "leverage": leverage,
                "margin_mode": margin_mode,
                "position_side": position_side,
            }
        )
        response = {"code": "0", "data": [{"instId": symbol, "lever": str(leverage)}]}
        self._raise_for_error(response)
        return response

    def cancel_order(self, symbol, *, order_id=None, client_order_id=None):
        return {"code": "0", "data": [{"ordId": order_id, "clOrdId": client_order_id}]}

    def _raise_for_error(self, response):
        if response.get("code") not in {None, "0"}:
            raise RuntimeError(response.get("msg", "error"))


class FakeTradeApi:
    def __init__(self) -> None:
        self.calls = []
        self.failures = []

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        return {"code": "0", "data": [{"ordId": "okx-order-1"}]}


@dataclass(frozen=True)
class FakeStrategy:
    direction: SignalDirection = SignalDirection.LONG

    def generate(self, symbol, one_hour: list[Candle], four_hour: list[Candle]) -> Signal:
        return StrategySignal(
            symbol=symbol,
            timeframe="1H",
            direction=self.direction,
            confidence=0.8,
            reason="fixture",
            entry_price=101.0,
            stop_price=99.0 if self.direction == SignalDirection.LONG else 103.0,
            target_price=105.0 if self.direction == SignalDirection.LONG else 97.0,
            created_at=datetime(2026, 5, 7, 8, 2, tzinfo=UTC),
        )


class FakeRiskGuard:
    def evaluate(self, signal, state):
        return RiskDecision(
            signal_id=signal.id,
            status=RiskStatus.APPROVED,
            reason="approved",
            position_size_usdt=25.0,
            leverage=1,
            created_at=datetime(2026, 5, 7, 8, 3, tzinfo=UTC),
        )


def _bot(tmp_path, *, enable_trading: bool) -> tuple[TradingBot, FakeClient]:
    settings = Settings(
        _env_file=None,
        ENABLE_TRADING=enable_trading,
        SYMBOLS="BTC-USDT-SWAP",
        DB_PATH=tmp_path / "bot.sqlite3",
    )
    storage = SQLiteStorage(settings.DB_PATH)
    storage.initialize()
    client = FakeClient()
    client.settings = settings
    runner = Runner(
        client=client,
        storage=storage,
        strategy=FakeStrategy(),
        risk_guard=FakeRiskGuard(),
        execution_engine=ExecutionEngine(client, retry_delay_seconds=0),
        risk_state=RiskState(),
        candle_limit=2,
        enable_trading=False,
    )
    return TradingBot(settings=settings, runner=runner, notifier=NullNotifier()), client


def _set_exchange_position(
    client: FakeClient,
    *,
    symbol: str = "BTC-USDT-SWAP",
    pos: str = "0.5",
    pos_side: str = "long",
    avg_px: str = "110",
    margin_mode: str = "isolated",
    lever: str = "1",
) -> None:
    client.positions_response = {
        "data": [
            {
                "instId": symbol,
                "mgnMode": margin_mode,
                "lever": lever,
                "posSide": pos_side,
                "pos": pos,
                "avgPx": avg_px,
            }
        ]
    }


def test_bot_observe_mode_plans_without_submitting(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=False)

    results = bot.run_once()

    assert results[0].skipped_reason == "trading disabled"
    assert client.trade_api.calls == []
    assert bot.runner.storage.load_balance("USDT").available == 900.0


def test_bot_submit_mode_places_order_after_dry_run(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)

    results = bot.run_once()

    assert results[0].submitted_order.order_id == "okx-order-1"
    assert client.trade_api.calls[0]["instId"] == "BTC-USDT-SWAP"
    # SL/TP must be attached on the exchange, not just tracked locally.
    assert client.trade_api.calls[0]["attachAlgoOrds"] == [
        {"slTriggerPx": "99", "slOrdPx": "-1", "tpTriggerPx": "105", "tpOrdPx": "-1"}
    ]
    assert bot.runner.storage.load_open_orders()[0].order_id == "okx-order-1"
    assert client.connectivity_checks == [
        {"attempts": 5, "required_successes": 5, "symbol": "BTC-USDT-SWAP"}
    ]


def test_bot_drawdown_kill_switch_flattens_halts_and_resumes(tmp_path):
    import pytest

    bot, client = _bot(tmp_path, enable_trading=True)
    # Pretend the account once had 2000 USDT; FakeClient reports 1000 now (-50%).
    bot.runner.storage.set_state("portfolio:hwm", "2000", datetime.now(UTC))
    bot.runner.storage.upsert_position(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.LONG,
        quantity=0.5,
        average_entry=110.0,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )
    _set_exchange_position(client)

    results = bot.run_once()

    assert bot.is_halted() is not None
    assert all("halted" in (item.skipped_reason or "").lower() for item in results)
    # The open position was flattened with a reduce-only close order.
    assert any(call.get("reduceOnly") == "true" for call in client.trade_api.calls)

    equity = bot.resume()
    assert bot.is_halted() is None
    assert equity == pytest.approx(1000.0)
    assert float(bot.runner.storage.get_state("portfolio:hwm")) == pytest.approx(1000.0)


def test_bot_exposure_cap_blocks_new_entry(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    bot.settings = bot.settings.model_copy(
        update={"MAX_TOTAL_EXPOSURE_RATE": 0.0001, "MAX_NET_EXPOSURE_RATE": 0.0001}
    )

    results = bot.run_once()

    assert results[0].skipped_reason is not None
    assert "exposure" in results[0].skipped_reason.lower()
    assert all("attachAlgoOrds" not in call for call in client.trade_api.calls)


def test_bot_does_not_stack_entries_on_symbol_with_open_position(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    bot.runner.storage.upsert_position(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.LONG,
        quantity=0.5,
        average_entry=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )
    _set_exchange_position(client, avg_px="100")

    results = bot.run_once()

    assert results[0].skipped_reason == (
        "BTC-USDT-SWAP already has an open position; not stacking entries."
    )
    assert all("attachAlgoOrds" not in call for call in client.trade_api.calls)


def test_bot_reduces_exchange_leverage_when_above_env_cap(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    position = PositionRecord(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.SHORT,
        quantity=-0.5,
        average_entry=100.0,
        state=PositionState.OPEN,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        margin_mode="cross",
        leverage=3,
    )

    bot._enforce_position_leverage(position)

    assert client.leverage_calls == [
        {
            "symbol": "BTC-USDT-SWAP",
            "leverage": 1,
            "margin_mode": "cross",
            "position_side": None,
        }
    ]


def test_bot_does_not_raise_position_leverage_to_env_cap(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    bot.settings = Settings(
        _env_file=None,
        ENABLE_TRADING=True,
        SYMBOLS="BTC-USDT-SWAP",
        DB_PATH=tmp_path / "bot.sqlite3",
        MAX_LEVERAGE=5,
    )
    position = PositionRecord(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.LONG,
        quantity=0.5,
        average_entry=100.0,
        state=PositionState.OPEN,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        margin_mode="cross",
        leverage=2,
    )

    bot._enforce_position_leverage(position)

    assert client.leverage_calls == []


def test_bot_blocks_new_entries_when_okx_connectivity_is_unhealthy(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    client.connectivity_ok = False
    client.connectivity_detail = "OKX connectivity unhealthy (1/3): SSL timeout"

    results = bot.run_once()

    assert results[0].submitted_order is None
    assert "connectivity unhealthy" in results[0].skipped_reason
    assert client.trade_api.calls == []
    assert bot.runner.storage.load_open_orders() == []


def test_bot_still_allows_close_orders_when_entry_connectivity_check_fails(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    client.connectivity_ok = False
    client.connectivity_detail = "OKX connectivity unhealthy (0/3): SSL timeout"
    bot.runner.storage.upsert_position(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.LONG,
        quantity=0.5,
        average_entry=110.0,
        stop_loss=100.0,
        take_profit=130.0,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )
    _set_exchange_position(client)
    client.ticker_last = "99"

    results = bot.run_once()

    assert results[0].submitted_order is None
    assert "connectivity unhealthy" in results[0].skipped_reason
    assert client.trade_api.calls[0]["reduceOnly"] == "true"
    assert bot.runner.storage.load_open_orders()[0].order_type == "market_close:STOP_LOSS"


def test_bot_publishes_runtime_status_for_telegram_status(tmp_path):
    bot, _client = _bot(tmp_path, enable_trading=True)

    bot.publish_runtime_status()

    assert bot.runner.storage.get_state("runtime:mode") == "demo"
    assert bot.runner.storage.get_state("runtime:trading_enabled") == "True"
    assert bot.runner.storage.get_state("runtime:strategy") == "ema-rsi-atr"
    assert bot.runner.storage.get_state("runtime:symbols") == "BTC-USDT-SWAP"


def test_bot_records_open_position_plan_after_entry_fill(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    bot.run_once()
    client.order_response = {
        "data": [
            {
                "state": "filled",
                "accFillSz": "0.5",
                "avgPx": "101",
                "fee": "-0.01",
            }
        ]
    }

    bot.sync_tracked_orders()

    position = bot.runner.storage.load_position("BTC-USDT-SWAP")
    assert position is not None
    assert position.state == PositionState.OPEN
    assert position.side == SignalDirection.LONG
    assert position.quantity == 0.5
    assert position.stop_loss == 99.0
    assert position.take_profit == 105.0
    assert position.expires_at is not None


def test_bot_reconciles_exchange_position_and_balance(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    updated_ms = str(int(datetime(2026, 5, 7, 8, 4, tzinfo=UTC).timestamp() * 1000))
    client.positions_response = {
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "pos": "2",
                "posSide": "long",
                "avgPx": "99.5",
                "uTime": updated_ms,
            },
            {
                "instId": "ETH-USDT-SWAP",
                "pos": "1",
                "posSide": "long",
                "avgPx": "3000",
                "uTime": updated_ms,
            },
        ]
    }

    bot.reconcile_exchange_state()

    position = bot.runner.storage.load_position("BTC-USDT-SWAP")
    assert position is not None
    assert position.side == SignalDirection.LONG
    assert position.quantity == 2.0
    assert position.average_entry == 99.5
    assert bot.runner.storage.load_position("ETH-USDT-SWAP") is None
    assert bot.runner.storage.load_balance("USDT").equity == 1000.0


def test_bot_marks_local_position_closed_when_missing_on_exchange(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    bot.runner.storage.upsert_position(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.LONG,
        quantity=0.5,
        average_entry=110.0,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )
    client.positions_response = {"data": []}

    bot.reconcile_exchange_state()

    position = bot.runner.storage.load_position("BTC-USDT-SWAP")
    exits = bot.runner.storage.load_position_exits("BTC-USDT-SWAP")
    assert position.state == PositionState.CLOSED
    assert exits[0].reason == ExitReason.RISK_EXIT


def test_bot_still_submits_close_order_when_new_trade_loss_limit_is_reached(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    now = datetime.now(UTC)
    for index in range(3):
        bot.runner.storage.insert_position_exit(
            PositionExitRecord(
                position_id=None,
                symbol="ETH-USDT-SWAP",
                side=SignalDirection.LONG,
                reason=ExitReason.STOP_LOSS,
                entry_price=110.0,
                exit_price=100.0,
                quantity=1.0,
                realized_pnl=-10.0,
                opened_at=now,
                closed_at=now,
                notes="losing trade",
            )
        )
    assert bot.runner.storage.load_risk_state().consecutive_losses == 3
    bot.runner.storage.upsert_position(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.LONG,
        quantity=0.5,
        average_entry=110.0,
        stop_loss=100.0,
        take_profit=130.0,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )
    _set_exchange_position(client)
    client.ticker_last = "99"

    close_orders = bot.monitor_positions()

    assert close_orders[0].order_type == "market_close:STOP_LOSS"
    assert client.trade_api.calls[0]["reduceOnly"] == "true"
    assert client.trade_api.calls[0]["side"] == "sell"


def test_bot_close_order_failure_is_logged_without_crashing_or_marking_closing(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    client.trade_api.failures = [
        TimeoutError("read timed out"),
        TimeoutError("read timed out"),
        TimeoutError("read timed out"),
    ]
    bot.runner.storage.upsert_position(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.LONG,
        quantity=0.5,
        average_entry=110.0,
        stop_loss=100.0,
        take_profit=130.0,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )
    _set_exchange_position(client)
    client.ticker_last = "99"

    close_orders = bot.monitor_positions()

    assert close_orders == []
    assert len(client.trade_api.calls) == 3
    assert bot.runner.storage.load_position("BTC-USDT-SWAP").state == PositionState.OPEN


def test_bot_submits_close_order_and_records_exit_on_stop_loss(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    bot.runner.storage.upsert_position(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.LONG,
        quantity=0.5,
        average_entry=110.0,
        stop_loss=100.0,
        take_profit=130.0,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )
    _set_exchange_position(client)
    client.ticker_last = "99"

    close_orders = bot.monitor_positions()

    assert close_orders[0].order_type == "market_close:STOP_LOSS"
    assert client.trade_api.calls[0]["reduceOnly"] == "true"
    assert client.trade_api.calls[0]["side"] == "sell"
    assert bot.runner.storage.load_position("BTC-USDT-SWAP").state == PositionState.CLOSING

    client.order_response = {
        "data": [
            {
                "state": "filled",
                "accFillSz": "0.5",
                "avgPx": "98",
                "fee": "-0.01",
            }
        ]
    }
    client.positions_response = {"data": []}
    bot.sync_tracked_orders()

    position = bot.runner.storage.load_position("BTC-USDT-SWAP")
    exits = bot.runner.storage.load_position_exits("BTC-USDT-SWAP")
    assert position.state == PositionState.CLOSED
    assert position.realized_pnl == -0.06
    assert exits[0].reason == ExitReason.STOP_LOSS
    assert exits[0].realized_pnl == -0.06


def test_bot_does_not_submit_duplicate_close_order_when_one_is_pending(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    bot.runner.storage.upsert_position(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.LONG,
        quantity=0.5,
        average_entry=110.0,
        stop_loss=100.0,
        take_profit=130.0,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )
    _set_exchange_position(client)
    bot.runner.storage.insert_order(
        OrderRecord(
            symbol="BTC-USDT-SWAP",
            side=SignalDirection.SHORT,
            quantity=0.5,
            state=OrderState.SUBMITTED,
            created_at=datetime(2026, 5, 7, 8, 1, tzinfo=UTC),
            order_id="existing-close",
            client_order_id="okxaicloseexisting",
            order_type="market_close:STOP_LOSS",
        )
    )
    client.ticker_last = "99"

    close_orders = bot.monitor_positions()

    assert close_orders == []
    assert client.trade_api.calls == []
    assert bot.runner.storage.load_position("BTC-USDT-SWAP").state == PositionState.CLOSING


def test_bot_keeps_position_open_when_close_fill_does_not_flatten_okx_position(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=True)
    bot.runner.storage.upsert_position(
        symbol="BTC-USDT-SWAP",
        side=SignalDirection.SHORT,
        quantity=-10.0,
        average_entry=110.0,
        margin_mode="isolated",
        leverage=3,
        opened_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 7, 8, 0, tzinfo=UTC),
    )
    bot.runner.storage.insert_order(
        OrderRecord(
            symbol="BTC-USDT-SWAP",
            side=SignalDirection.LONG,
            quantity=10.0,
            state=OrderState.SUBMITTED,
            created_at=datetime(2026, 5, 7, 8, 1, tzinfo=UTC),
            order_id="close-cross-only",
            client_order_id="okxaicloseexisting",
            order_type="market_close:TAKE_PROFIT",
        )
    )
    client.order_response = {
        "data": [
            {
                "state": "filled",
                "accFillSz": "10",
                "avgPx": "100",
                "fee": "-0.01",
            }
        ]
    }
    client.positions_response = {
        "data": [
            {
                "instId": "BTC-USDT-SWAP",
                "mgnMode": "isolated",
                "lever": "3",
                "posSide": "net",
                "pos": "-10",
                "avgPx": "110",
            }
        ]
    }

    bot.sync_tracked_orders()

    position = bot.runner.storage.load_position("BTC-USDT-SWAP")
    assert position.state == PositionState.OPEN
    assert position.quantity == -10.0
    assert position.margin_mode == "isolated"
    assert bot.runner.storage.load_position_exits("BTC-USDT-SWAP") == []


def test_bot_skips_duplicate_signal_for_same_candle(tmp_path):
    bot, client = _bot(tmp_path, enable_trading=False)

    first = bot.run_once()
    second = bot.run_once()

    assert first[0].skipped_reason == "trading disabled"
    assert "already handled" in second[0].skipped_reason
    assert client.trade_api.calls == []


def test_bot_filters_symbols_not_available_in_okx_environment(tmp_path):
    settings = Settings(
        _env_file=None,
        ENABLE_TRADING=False,
        SYMBOLS="BTC-USDT-SWAP,SOL-USDT-SWAP",
        DB_PATH=tmp_path / "bot.sqlite3",
    )
    storage = SQLiteStorage(settings.DB_PATH)
    storage.initialize()
    client = FakeClient()
    client.settings = settings
    client.instruments = {"data": [{"instId": "BTC-USDT-SWAP", "state": "live"}]}
    runner = Runner(
        client=client,
        storage=storage,
        strategy=FakeStrategy(),
        risk_guard=FakeRiskGuard(),
        execution_engine=ExecutionEngine(client, retry_delay_seconds=0),
        risk_state=RiskState(),
        candle_limit=2,
        enable_trading=False,
    )
    bot = TradingBot(settings=settings, runner=runner, notifier=NullNotifier())

    results = bot.run_once()

    assert [result.symbol for result in results] == ["BTC-USDT-SWAP"]


def test_telegram_notifier_suppresses_operational_noise_but_sends_reports(tmp_path):
    class FakeNotifier:
        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append(message)

    bot, _client = _bot(tmp_path, enable_trading=False)
    bot.settings = Settings(
        _env_file=None,
        ENABLE_TRADING=False,
        NOTIFIER="telegram",
        SYMBOLS="BTC-USDT-SWAP",
        DB_PATH=tmp_path / "bot.sqlite3",
    )
    fake_notifier = FakeNotifier()
    bot.notifier = fake_notifier

    bot._notify("Submitted order: noisy detail")
    assert fake_notifier.messages == []

    assert bot.send_daily_report(date(2026, 5, 7), force=True)
    assert len(fake_notifier.messages) == 1
    assert "OKX 交易日报" in fake_notifier.messages[0]


def test_due_report_slots_only_fire_within_grace_window():
    now = datetime(2026, 5, 7, 8, 3, tzinfo=UTC)

    # 00:00 was missed hours ago; a restart must not blast stale reports.
    assert _due_report_slots(now, ["00:00", "08:00", "12:00", "bad"]) == ["08:00"]
    assert _due_report_slots(
        datetime(2026, 5, 7, 8, 50, tzinfo=UTC), ["08:00", "12:00"]
    ) == []


def test_scheduled_report_slot_does_not_conflict_with_manual_report(tmp_path):
    bot, _client = _bot(tmp_path, enable_trading=False)
    report_date = date(2026, 5, 7)

    assert bot.send_daily_report(report_date, force=True)
    assert bot.send_daily_report(report_date, report_slot="08:00")
    assert not bot.send_daily_report(report_date, report_slot="08:00")

    assert bot.runner.storage.daily_report_exists(_daily_report_key(report_date))
    assert bot.runner.storage.daily_report_exists(_daily_report_key(report_date, "08:00"))
