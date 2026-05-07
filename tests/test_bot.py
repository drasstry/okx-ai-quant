from dataclasses import dataclass
from datetime import UTC, date, datetime

from okx_ai_quant.bot import TradingBot, _daily_report_key, _due_report_slots
from okx_ai_quant.config import Settings
from okx_ai_quant.execution import ExecutionEngine
from okx_ai_quant.models import (
    Candle,
    ExitReason,
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
        self.instruments = {"data": [{"instId": "BTC-USDT-SWAP", "state": "live"}]}
        self.ticker_last = "101"

    def sync_server_time(self) -> int:
        return 0

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

    def get_instruments(self, inst_type):
        return self.instruments["data"]

    def cancel_order(self, symbol, *, order_id=None, client_order_id=None):
        return {"code": "0", "data": [{"ordId": order_id, "clOrdId": client_order_id}]}

    def _raise_for_error(self, response):
        if response.get("code") not in {None, "0"}:
            raise RuntimeError(response.get("msg", "error"))


class FakeTradeApi:
    def __init__(self) -> None:
        self.calls = []

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
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
        execution_engine=ExecutionEngine(client),
        risk_state=RiskState(),
        candle_limit=2,
        enable_trading=False,
    )
    return TradingBot(settings=settings, runner=runner, notifier=NullNotifier()), client


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
    assert bot.runner.storage.load_open_orders()[0].order_id == "okx-order-1"


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
    bot.sync_tracked_orders()

    position = bot.runner.storage.load_position("BTC-USDT-SWAP")
    exits = bot.runner.storage.load_position_exits("BTC-USDT-SWAP")
    assert position.state == PositionState.CLOSED
    assert position.realized_pnl == -6.0
    assert exits[0].reason == ExitReason.STOP_LOSS
    assert exits[0].realized_pnl == -6.0


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
        execution_engine=ExecutionEngine(client),
        risk_state=RiskState(),
        candle_limit=2,
        enable_trading=False,
    )
    bot = TradingBot(settings=settings, runner=runner, notifier=NullNotifier())

    results = bot.run_once()

    assert [result.symbol for result in results] == ["BTC-USDT-SWAP"]


def test_due_report_slots_include_missed_times_without_future_times():
    now = datetime(2026, 5, 7, 8, 3, tzinfo=UTC)

    assert _due_report_slots(now, ["00:00", "08:00", "12:00", "bad"]) == ["00:00", "08:00"]


def test_scheduled_report_slot_does_not_conflict_with_manual_report(tmp_path):
    bot, _client = _bot(tmp_path, enable_trading=False)
    report_date = date(2026, 5, 7)

    assert bot.send_daily_report(report_date, force=True)
    assert bot.send_daily_report(report_date, report_slot="08:00")
    assert not bot.send_daily_report(report_date, report_slot="08:00")

    assert bot.runner.storage.daily_report_exists(_daily_report_key(report_date))
    assert bot.runner.storage.daily_report_exists(_daily_report_key(report_date, "08:00"))
