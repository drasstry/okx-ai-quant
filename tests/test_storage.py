import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from okx_ai_quant.models import (
    BalanceSnapshot,
    FillRecord,
    OrderRecord,
    OrderState,
    RiskStatus,
    Signal,
    SignalDirection,
    TradeAnalysis,
)
from okx_ai_quant.risk import RiskDecision
from okx_ai_quant.storage import SQLiteStorage


def test_database_initializes_tables(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()

        table_names = storage.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()

        assert {row["name"] for row in table_names} >= {
            "market_candles",
            "tickers",
            "signals",
            "risk_decisions",
            "orders",
            "fills",
            "positions",
            "position_exits",
            "balances",
            "trade_analyses",
            "daily_reports",
            "bot_state",
        }


def test_context_manager_closes_connection(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        connection = storage.connection
        storage.initialize()

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_upsert_candle_and_load_recent(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()
        base_time = datetime(2026, 4, 29, 8, 0, tzinfo=UTC)

        newer = storage.upsert_candle(
            symbol="BTC-USDT-SWAP",
            timeframe="1m",
            timestamp=base_time + timedelta(minutes=1),
            open=101.0,
            high=103.0,
            low=100.5,
            close=102.0,
            volume=12.0,
        )
        older = storage.upsert_candle(
            symbol="BTC-USDT-SWAP",
            timeframe="1m",
            timestamp=base_time,
            open=100.0,
            high=101.5,
            low=99.0,
            close=101.0,
            volume=10.0,
        )
        storage.upsert_candle(
            symbol="BTC-USDT-SWAP",
            timeframe="1m",
            timestamp=base_time,
            open=100.0,
            high=102.0,
            low=98.5,
            close=101.25,
            volume=11.0,
        )

        candles = storage.load_recent_candles("BTC-USDT-SWAP", "1m", limit=2)

        assert [candle.id for candle in candles] == [older.id, newer.id]
        assert candles[0].timestamp == base_time
        assert candles[0].timestamp.tzinfo is UTC
        assert candles[0].high == 102.0
        assert candles[0].volume == 11.0
        assert candles[1].timestamp == base_time + timedelta(minutes=1)


def test_upsert_candle_rejects_naive_datetime(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()

        with pytest.raises(ValueError, match="timezone-aware"):
            storage.upsert_candle(
                symbol="BTC-USDT-SWAP",
                timeframe="1m",
                timestamp=datetime(2026, 4, 29, 8, 0),
                open=100.0,
                high=101.5,
                low=99.0,
                close=101.0,
                volume=10.0,
            )


def test_insert_signal_round_trip(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()
        created_at = datetime(2026, 4, 29, 9, 15, tzinfo=UTC)
        signal = Signal(
            symbol="ETH-USDT-SWAP",
            timeframe="15m",
            direction=SignalDirection.LONG,
            confidence=0.72,
            reason="Momentum breakout with controlled downside.",
            created_at=created_at,
        )

        signal_id = storage.insert_signal(signal)
        loaded = storage.load_signal(signal_id)

        assert loaded == Signal(
            id=signal_id,
            symbol="ETH-USDT-SWAP",
            timeframe="15m",
            direction=SignalDirection.LONG,
            confidence=0.72,
            reason="Momentum breakout with controlled downside.",
            created_at=created_at,
        )
        assert loaded.created_at.tzinfo is UTC


def test_insert_risk_decision_persists_size_and_leverage(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()
        signal_id = storage.insert_signal(
            Signal(
                symbol="ETH-USDT-SWAP",
                timeframe="15m",
                direction=SignalDirection.LONG,
                confidence=0.72,
                reason="Momentum breakout with controlled downside.",
                created_at=datetime(2026, 4, 29, 9, 15, tzinfo=UTC),
            )
        )
        decision = RiskDecision(
            signal_id=signal_id,
            status=RiskStatus.APPROVED,
            reason="Risk checks passed.",
            position_size_usdt=120.5,
            leverage=2,
            created_at=datetime(2026, 4, 29, 9, 16, tzinfo=UTC),
        )

        decision_id = storage.insert_risk_decision(decision)
        row = storage.connection.execute(
            "SELECT * FROM risk_decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()

        assert row["signal_id"] == signal_id
        assert row["status"] == RiskStatus.APPROVED.value
        assert row["position_size_usdt"] == 120.5
        assert row["leverage"] == 2


def test_insert_order_persists_submitted_order(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()
        signal_id = storage.insert_signal(
            Signal(
                symbol="ETH-USDT-SWAP",
                timeframe="15m",
                direction=SignalDirection.SHORT,
                confidence=0.72,
                reason="Momentum breakdown with controlled downside.",
                created_at=datetime(2026, 4, 29, 9, 15, tzinfo=UTC),
            )
        )
        order = OrderRecord(
            signal_id=signal_id,
            order_id="okx-order-1",
            symbol="ETH-USDT-SWAP",
            side=SignalDirection.SHORT,
            quantity=55.0,
            state=OrderState.SUBMITTED,
            created_at=datetime(2026, 4, 29, 9, 17, tzinfo=UTC),
        )

        order_row_id = storage.insert_order(order)
        row = storage.connection.execute(
            "SELECT * FROM orders WHERE id = ?",
            (order_row_id,),
        ).fetchone()

        assert row["signal_id"] == signal_id
        assert row["order_id"] == "okx-order-1"
        assert row["side"] == SignalDirection.SHORT.value
        assert row["state"] == OrderState.SUBMITTED.value


def test_order_tracking_state_fill_balance_and_bot_state_round_trip(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()
        signal_id = storage.insert_signal(
            Signal(
                symbol="BTC-USDT-SWAP",
                timeframe="1H",
                direction=SignalDirection.LONG,
                confidence=0.8,
                reason="Breakout.",
                created_at=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),
            )
        )
        storage.insert_order(
            OrderRecord(
                signal_id=signal_id,
                order_id="okx-order-2",
                client_order_id="okxaiabc",
                symbol="BTC-USDT-SWAP",
                side=SignalDirection.LONG,
                quantity=1.5,
                state=OrderState.SUBMITTED,
                created_at=datetime(2026, 5, 7, 9, 1, tzinfo=UTC),
            )
        )

        open_orders = storage.load_open_orders()
        assert open_orders[0].client_order_id == "okxaiabc"

        assert storage.update_order_state(state=OrderState.FILLED, order_id="okx-order-2") == 1
        assert storage.load_open_orders() == []

        fill_id = storage.insert_fill(
            FillRecord(
                order_id="okx-order-2",
                symbol="BTC-USDT-SWAP",
                price=60000.0,
                quantity=0.01,
                fee=0.05,
                created_at=datetime(2026, 5, 7, 9, 2, tzinfo=UTC),
            )
        )
        assert fill_id == 1

        storage.upsert_balance(
            BalanceSnapshot(
                currency="USDT",
                available=900.0,
                equity=1000.0,
                updated_at=datetime(2026, 5, 7, 9, 3, tzinfo=UTC),
            )
        )
        assert storage.load_balance("USDT").equity == 1000.0

        storage.set_state("last", "value", datetime(2026, 5, 7, 9, 4, tzinfo=UTC))
        assert storage.get_state("last") == "value"


def test_insert_trade_analysis_round_trip(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()
        signal_id = storage.insert_signal(
            Signal(
                symbol="ETH-USDT-SWAP",
                timeframe="15m",
                direction=SignalDirection.LONG,
                confidence=0.72,
                reason="Momentum breakout with controlled downside.",
                created_at=datetime(2026, 4, 29, 9, 15, tzinfo=UTC),
            )
        )
        created_at = datetime(2026, 4, 29, 9, 20, tzinfo=UTC)
        analysis = TradeAnalysis(
            signal_id=signal_id,
            english="AI analysis: price action favors patience before adding risk.",
            chinese="AI分析：价格行为更适合耐心等待，再增加风险敞口。",
            created_at=created_at,
        )

        analysis_id = storage.insert_trade_analysis(analysis)
        loaded = storage.load_trade_analysis_by_signal_id(signal_id)

        assert loaded == TradeAnalysis(
            id=analysis_id,
            signal_id=signal_id,
            english="AI analysis: price action favors patience before adding risk.",
            chinese="AI分析：价格行为更适合耐心等待，再增加风险敞口。",
            created_at=created_at,
        )
        assert loaded.created_at.tzinfo is UTC


def test_insert_trade_analysis_rejects_missing_signal(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()

        with pytest.raises(sqlite3.IntegrityError):
            storage.insert_trade_analysis(
                TradeAnalysis(
                    signal_id=42,
                    english="AI analysis: missing signal should not be allowed.",
                    chinese="AI分析：不应允许缺失信号的分析记录。",
                    created_at=datetime(2026, 4, 29, 9, 20, tzinfo=UTC),
                )
            )


def test_load_trade_analyses_for_date(tmp_path):
    with SQLiteStorage(tmp_path / "quant.sqlite3") as storage:
        storage.initialize()
        signal_id = storage.insert_signal(
            Signal(
                symbol="ETH-USDT-SWAP",
                timeframe="15m",
                direction=SignalDirection.LONG,
                confidence=0.72,
                reason="Momentum breakout with controlled downside.",
                created_at=datetime(2026, 5, 7, 9, 15, tzinfo=UTC),
            )
        )
        storage.insert_trade_analysis(
            TradeAnalysis(
                signal_id=signal_id,
                english="AI analysis for today.",
                chinese="今日AI分析。",
                created_at=datetime(2026, 5, 7, 9, 20, tzinfo=UTC),
            )
        )

        analyses = storage.load_trade_analyses_for_date("2026-05-07")

        assert len(analyses) == 1
        assert analyses[0].chinese == "今日AI分析。"
