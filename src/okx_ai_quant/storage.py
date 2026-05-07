import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from okx_ai_quant.models import (
    BalanceSnapshot,
    Candle,
    FillRecord,
    OrderRecord,
    OrderState,
    Signal,
    SignalDirection,
    TradeAnalysis,
)
from okx_ai_quant.risk import RiskDecision


def _to_utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime values must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _from_utc_text(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class SQLiteStorage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                UNIQUE (symbol, timeframe, timestamp)
            );

            CREATE TABLE IF NOT EXISTS tickers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                last REAL NOT NULL,
                volume_24h REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                direction TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS risk_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                position_size_usdt REAL NOT NULL DEFAULT 0,
                leverage INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                order_id TEXT,
                client_order_id TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                order_type TEXT NOT NULL DEFAULT 'market',
                price REAL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (order_id),
                UNIQUE (client_order_id),
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            );

            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                fee REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                quantity REAL NOT NULL,
                average_entry REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                currency TEXT NOT NULL UNIQUE,
                available REAL NOT NULL,
                equity REAL NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trade_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL UNIQUE,
                english TEXT NOT NULL,
                chinese TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            );

            CREATE TABLE IF NOT EXISTS daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

        risk_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(risk_decisions)").fetchall()
        }
        if "position_size_usdt" not in risk_columns:
            self.connection.execute(
                "ALTER TABLE risk_decisions ADD COLUMN position_size_usdt REAL NOT NULL DEFAULT 0"
            )
        if "leverage" not in risk_columns:
            self.connection.execute(
                "ALTER TABLE risk_decisions ADD COLUMN leverage INTEGER NOT NULL DEFAULT 1"
            )

        order_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(orders)").fetchall()
        }
        if "client_order_id" not in order_columns:
            self.connection.execute("ALTER TABLE orders ADD COLUMN client_order_id TEXT")
        if "order_type" not in order_columns:
            self.connection.execute(
                "ALTER TABLE orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'market'"
            )
        if "price" not in order_columns:
            self.connection.execute("ALTER TABLE orders ADD COLUMN price REAL")
        self.connection.commit()

    def upsert_candle(
        self,
        *,
        symbol: str,
        timeframe: str,
        timestamp: datetime,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> Candle:
        timestamp_text = _to_utc_text(timestamp)
        self.connection.execute(
            """
            INSERT INTO market_candles
                (symbol, timeframe, timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, timeframe, timestamp) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume
            """,
            (symbol, timeframe, timestamp_text, open, high, low, close, volume),
        )
        row = self.connection.execute(
            """
            SELECT * FROM market_candles
            WHERE symbol = ? AND timeframe = ? AND timestamp = ?
            """,
            (symbol, timeframe, timestamp_text),
        ).fetchone()
        self.connection.commit()
        return self._candle_from_row(row)

    def load_recent_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        rows = self.connection.execute(
            """
            SELECT * FROM (
                SELECT * FROM market_candles
                WHERE symbol = ? AND timeframe = ?
                ORDER BY timestamp DESC
                LIMIT ?
            )
            ORDER BY timestamp ASC
            """,
            (symbol, timeframe, limit),
        ).fetchall()
        return [self._candle_from_row(row) for row in rows]

    def insert_signal(self, signal: Signal) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO signals (symbol, timeframe, direction, confidence, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                signal.symbol,
                signal.timeframe,
                signal.direction.value,
                signal.confidence,
                signal.reason,
                _to_utc_text(signal.created_at),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def load_signal(self, signal_id: int) -> Signal:
        row = self.connection.execute(
            "SELECT * FROM signals WHERE id = ?",
            (signal_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Signal {signal_id} not found")
        return Signal(
            id=row["id"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            direction=SignalDirection(row["direction"]),
            confidence=row["confidence"],
            reason=row["reason"],
            created_at=_from_utc_text(row["created_at"]),
        )

    def insert_risk_decision(self, decision: RiskDecision) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO risk_decisions
                (signal_id, status, reason, position_size_usdt, leverage, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                decision.signal_id,
                decision.status.value,
                decision.reason,
                decision.position_size_usdt,
                decision.leverage,
                _to_utc_text(decision.created_at),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def insert_order(self, order: OrderRecord) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO orders
                (signal_id, order_id, client_order_id, symbol, side, quantity, order_type, price, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.signal_id,
                order.order_id,
                order.client_order_id,
                order.symbol,
                order.side.value,
                order.quantity,
                order.order_type,
                order.price,
                order.state.value,
                _to_utc_text(order.created_at),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def load_open_orders(self) -> list[OrderRecord]:
        rows = self.connection.execute(
            """
            SELECT * FROM orders
            WHERE state IN ('SUBMITTED', 'PARTIALLY_FILLED')
            ORDER BY id ASC
            """
        ).fetchall()
        return [self._order_from_row(row) for row in rows]

    def update_order_state(
        self,
        *,
        state: OrderState,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> int:
        if not order_id and not client_order_id:
            raise ValueError("order_id or client_order_id is required")

        if order_id:
            cursor = self.connection.execute(
                "UPDATE orders SET state = ? WHERE order_id = ?",
                (state.value, order_id),
            )
        else:
            cursor = self.connection.execute(
                "UPDATE orders SET state = ? WHERE client_order_id = ?",
                (state.value, client_order_id),
            )
        self.connection.commit()
        return int(cursor.rowcount)

    def insert_fill(self, fill: FillRecord) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO fills (order_id, symbol, price, quantity, fee, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fill.order_id,
                fill.symbol,
                fill.price,
                fill.quantity,
                fill.fee,
                _to_utc_text(fill.created_at),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def upsert_balance(self, balance: BalanceSnapshot) -> int:
        self.connection.execute(
            """
            INSERT INTO balances (currency, available, equity, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(currency) DO UPDATE SET
                available = excluded.available,
                equity = excluded.equity,
                updated_at = excluded.updated_at
            """,
            (
                balance.currency,
                balance.available,
                balance.equity,
                _to_utc_text(balance.updated_at),
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM balances WHERE currency = ?",
            (balance.currency,),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def load_balance(self, currency: str) -> BalanceSnapshot | None:
        row = self.connection.execute(
            "SELECT * FROM balances WHERE currency = ?",
            (currency,),
        ).fetchone()
        if row is None:
            return None
        return BalanceSnapshot(
            id=row["id"],
            currency=row["currency"],
            available=row["available"],
            equity=row["equity"],
            updated_at=_from_utc_text(row["updated_at"]),
        )

    def upsert_position(
        self,
        *,
        symbol: str,
        quantity: float,
        average_entry: float,
        updated_at: datetime,
    ) -> int:
        self.connection.execute(
            """
            INSERT INTO positions (symbol, quantity, average_entry, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                quantity = excluded.quantity,
                average_entry = excluded.average_entry,
                updated_at = excluded.updated_at
            """,
            (symbol, quantity, average_entry, _to_utc_text(updated_at)),
        )
        row = self.connection.execute(
            "SELECT id FROM positions WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def load_position_quantity(self, symbol: str) -> float:
        row = self.connection.execute(
            "SELECT quantity FROM positions WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        return float(row["quantity"]) if row is not None else 0.0

    def insert_daily_report(self, report_date: str, content: str, created_at: datetime) -> int:
        self.connection.execute(
            """
            INSERT INTO daily_reports (report_date, content, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(report_date) DO UPDATE SET
                content = excluded.content,
                created_at = excluded.created_at
            """,
            (report_date, content, _to_utc_text(created_at)),
        )
        row = self.connection.execute(
            "SELECT id FROM daily_reports WHERE report_date = ?",
            (report_date,),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def daily_report_exists(self, report_date: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM daily_reports WHERE report_date = ?",
            (report_date,),
        ).fetchone()
        return row is not None

    def get_state(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM bot_state WHERE key = ?",
            (key,),
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_state(self, key: str, value: str, updated_at: datetime) -> None:
        self.connection.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, _to_utc_text(updated_at)),
        )
        self.connection.commit()

    def insert_trade_analysis(self, analysis: TradeAnalysis) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO trade_analyses (signal_id, english, chinese, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                analysis.signal_id,
                analysis.english,
                analysis.chinese,
                _to_utc_text(analysis.created_at),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def load_trade_analysis_by_signal_id(self, signal_id: int) -> TradeAnalysis:
        row = self.connection.execute(
            "SELECT * FROM trade_analyses WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Trade analysis for signal {signal_id} not found")
        return TradeAnalysis(
            id=row["id"],
            signal_id=row["signal_id"],
            english=row["english"],
            chinese=row["chinese"],
            created_at=_from_utc_text(row["created_at"]),
        )

    def load_trade_analyses_for_date(self, report_date: str) -> list[TradeAnalysis]:
        rows = self.connection.execute(
            """
            SELECT * FROM trade_analyses
            WHERE substr(created_at, 1, 10) = ?
            ORDER BY created_at ASC
            """,
            (report_date,),
        ).fetchall()
        return [
            TradeAnalysis(
                id=row["id"],
                signal_id=row["signal_id"],
                english=row["english"],
                chinese=row["chinese"],
                created_at=_from_utc_text(row["created_at"]),
            )
            for row in rows
        ]

    def load_risk_state(self):
        """Aggregate a lightweight RiskState from persisted orders.

        This is deliberately conservative for the MVP: it counts
        non-terminal orders as "open positions" and treats the number of
        FAILED/CANCELED orders since the last FILLED order as a proxy for
        ``consecutive_losses``. Daily PnL aggregation requires fills data
        and is left at 0.0 until the fills pipeline is wired in.
        """
        from okx_ai_quant.risk import RiskState

        open_rows = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM orders
            WHERE state IN ('CREATED', 'SUBMITTED', 'PARTIALLY_FILLED')
            """
        ).fetchone()
        open_positions = int(open_rows["n"]) if open_rows is not None else 0

        recent_rows = self.connection.execute(
            """
            SELECT state FROM orders
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()
        consecutive_losses = 0
        for row in recent_rows:
            state = row["state"]
            if state == "FILLED":
                break
            if state in ("FAILED", "CANCELED"):
                consecutive_losses += 1

        return RiskState(
            daily_loss_rate=0.0,
            consecutive_losses=consecutive_losses,
            open_positions=open_positions,
        )

    def _candle_from_row(self, row: sqlite3.Row) -> Candle:
        return Candle(
            id=row["id"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            timestamp=_from_utc_text(row["timestamp"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
        )

    def _order_from_row(self, row: sqlite3.Row) -> OrderRecord:
        return OrderRecord(
            id=row["id"],
            signal_id=row["signal_id"],
            order_id=row["order_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=SignalDirection(row["side"]),
            quantity=row["quantity"],
            order_type=row["order_type"],
            price=row["price"],
            state=OrderState(row["state"]),
            created_at=_from_utc_text(row["created_at"]),
        )
