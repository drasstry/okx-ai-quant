import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from okx_ai_quant.models import (
    BalanceSnapshot,
    Candle,
    FillRecord,
    OrderRecord,
    OrderState,
    ExitReason,
    PositionExitRecord,
    PositionRecord,
    PositionState,
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
                side TEXT NOT NULL DEFAULT 'LONG',
                quantity REAL NOT NULL,
                average_entry REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'OPEN',
                opened_at TEXT,
                updated_at TEXT NOT NULL,
                entry_signal_id INTEGER,
                stop_loss REAL,
                take_profit REAL,
                expires_at TEXT,
                exit_reason TEXT,
                closed_at TEXT,
                realized_pnl REAL
            );

            CREATE TABLE IF NOT EXISTS position_exits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                reason TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                quantity REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                notes TEXT NOT NULL,
                FOREIGN KEY (position_id) REFERENCES positions(id)
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

            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                currency TEXT NOT NULL,
                equity REAL NOT NULL,
                available REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_equity_snapshots_ccy_time
                ON equity_snapshots (currency, created_at);
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

        position_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(positions)").fetchall()
        }
        for column, definition in (
            ("side", "TEXT NOT NULL DEFAULT 'LONG'"),
            ("state", "TEXT NOT NULL DEFAULT 'OPEN'"),
            ("opened_at", "TEXT"),
            ("entry_signal_id", "INTEGER"),
            ("stop_loss", "REAL"),
            ("take_profit", "REAL"),
            ("expires_at", "TEXT"),
            ("margin_mode", "TEXT"),
            ("leverage", "INTEGER"),
            ("exit_reason", "TEXT"),
            ("closed_at", "TEXT"),
            ("realized_pnl", "REAL"),
        ):
            if column not in position_columns:
                self.connection.execute(f"ALTER TABLE positions ADD COLUMN {column} {definition}")
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

    def insert_equity_snapshot(
        self,
        *,
        currency: str,
        equity: float,
        available: float,
        created_at: datetime,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO equity_snapshots (currency, equity, available, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (currency, equity, available, _to_utc_text(created_at)),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def load_equity_series(self, currency: str, limit: int = 2000) -> list[tuple[datetime, float]]:
        rows = self.connection.execute(
            """
            SELECT created_at, equity FROM (
                SELECT created_at, equity FROM equity_snapshots
                WHERE currency = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY created_at ASC
            """,
            (currency, limit),
        ).fetchall()
        return [(_from_utc_text(row["created_at"]), float(row["equity"])) for row in rows]

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
        side: SignalDirection | None = None,
        state: PositionState = PositionState.OPEN,
        opened_at: datetime | None = None,
        entry_signal_id: int | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        expires_at: datetime | None = None,
        margin_mode: str | None = None,
        leverage: int | None = None,
    ) -> int:
        side = side or (SignalDirection.SHORT if quantity < 0 else SignalDirection.LONG)
        opened_at = opened_at or updated_at
        self.connection.execute(
            """
            INSERT INTO positions (
                symbol, side, quantity, average_entry, state, opened_at, updated_at,
                entry_signal_id, stop_loss, take_profit, expires_at, margin_mode, leverage
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                side = excluded.side,
                quantity = excluded.quantity,
                average_entry = excluded.average_entry,
                state = excluded.state,
                opened_at = CASE
                    WHEN positions.state = 'CLOSED' OR positions.quantity = 0
                    THEN excluded.opened_at
                    ELSE COALESCE(positions.opened_at, excluded.opened_at)
                END,
                updated_at = excluded.updated_at,
                entry_signal_id = COALESCE(excluded.entry_signal_id, positions.entry_signal_id),
                stop_loss = COALESCE(excluded.stop_loss, positions.stop_loss),
                take_profit = COALESCE(excluded.take_profit, positions.take_profit),
                expires_at = COALESCE(excluded.expires_at, positions.expires_at),
                margin_mode = COALESCE(excluded.margin_mode, positions.margin_mode),
                leverage = COALESCE(excluded.leverage, positions.leverage),
                exit_reason = NULL,
                closed_at = NULL,
                realized_pnl = NULL
            """,
            (
                symbol,
                side.value,
                quantity,
                average_entry,
                state.value,
                _to_utc_text(opened_at),
                _to_utc_text(updated_at),
                entry_signal_id,
                stop_loss,
                take_profit,
                _to_utc_text(expires_at) if expires_at is not None else None,
                margin_mode,
                leverage,
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM positions WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        self.connection.commit()
        return int(row["id"])

    def load_position(self, symbol: str) -> PositionRecord | None:
        row = self.connection.execute(
            "SELECT * FROM positions WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        return self._position_from_row(row) if row is not None else None

    def load_open_positions(self) -> list[PositionRecord]:
        rows = self.connection.execute(
            """
            SELECT * FROM positions
            WHERE state = 'OPEN' AND ABS(quantity) > 0
            ORDER BY updated_at ASC
            """
        ).fetchall()
        return [self._position_from_row(row) for row in rows]

    def mark_position_closing(
        self,
        *,
        symbol: str,
        reason: ExitReason,
        updated_at: datetime,
    ) -> int:
        cursor = self.connection.execute(
            """
            UPDATE positions
            SET state = ?, exit_reason = ?, updated_at = ?
            WHERE symbol = ? AND state = 'OPEN'
            """,
            (PositionState.CLOSING.value, reason.value, _to_utc_text(updated_at), symbol),
        )
        self.connection.commit()
        return int(cursor.rowcount)

    def close_position(
        self,
        *,
        symbol: str,
        reason: ExitReason,
        exit_price: float,
        closed_at: datetime,
        notes: str,
        pnl_multiplier: float = 1.0,
    ) -> PositionExitRecord | None:
        position = self.load_position(symbol)
        if position is None or position.quantity == 0:
            return None

        realized_pnl = (exit_price - position.average_entry) * position.quantity * pnl_multiplier
        abs_quantity = abs(position.quantity)
        self.connection.execute(
            """
            UPDATE positions
            SET quantity = 0,
                state = ?,
                exit_reason = ?,
                closed_at = ?,
                realized_pnl = ?,
                updated_at = ?
            WHERE symbol = ?
            """,
            (
                PositionState.CLOSED.value,
                reason.value,
                _to_utc_text(closed_at),
                realized_pnl,
                _to_utc_text(closed_at),
                symbol,
            ),
        )
        exit_record = PositionExitRecord(
            position_id=position.id,
            symbol=position.symbol,
            side=position.side,
            reason=reason,
            entry_price=position.average_entry,
            exit_price=exit_price,
            quantity=abs_quantity,
            realized_pnl=realized_pnl,
            opened_at=position.opened_at,
            closed_at=closed_at,
            notes=notes,
        )
        exit_id = self.insert_position_exit(exit_record, commit=False)
        self.connection.commit()
        return PositionExitRecord(
            id=exit_id,
            position_id=exit_record.position_id,
            symbol=exit_record.symbol,
            side=exit_record.side,
            reason=exit_record.reason,
            entry_price=exit_record.entry_price,
            exit_price=exit_record.exit_price,
            quantity=exit_record.quantity,
            realized_pnl=exit_record.realized_pnl,
            opened_at=exit_record.opened_at,
            closed_at=exit_record.closed_at,
            notes=exit_record.notes,
        )

    def insert_position_exit(self, exit_record: PositionExitRecord, *, commit: bool = True) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO position_exits (
                position_id, symbol, side, reason, entry_price, exit_price, quantity,
                realized_pnl, opened_at, closed_at, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exit_record.position_id,
                exit_record.symbol,
                exit_record.side.value,
                exit_record.reason.value,
                exit_record.entry_price,
                exit_record.exit_price,
                exit_record.quantity,
                exit_record.realized_pnl,
                _to_utc_text(exit_record.opened_at),
                _to_utc_text(exit_record.closed_at),
                exit_record.notes,
            ),
        )
        if commit:
            self.connection.commit()
        return int(cursor.lastrowid)

    def load_position_exits(self, symbol: str | None = None) -> list[PositionExitRecord]:
        if symbol:
            rows = self.connection.execute(
                "SELECT * FROM position_exits WHERE symbol = ? ORDER BY closed_at ASC",
                (symbol,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM position_exits ORDER BY closed_at ASC"
            ).fetchall()
        return [self._position_exit_from_row(row) for row in rows]

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

    def load_risk_state(self, *, reference_capital_usdt: float | None = None):
        """Aggregate the current RiskState from persisted trading state.

        ``open_positions`` counts real tracked positions plus non-terminal
        orders, so a filled entry keeps blocking additional new entries until
        that position is closed. ``daily_loss_rate`` and
        ``consecutive_losses`` come from today's (UTC) realized position
        exits, so the MAX_DAILY_LOSS / MAX_CONSECUTIVE_LOSSES breakers act on
        actual trading PnL and reset each day instead of latching forever.
        """
        from okx_ai_quant.risk import RiskState

        position_rows = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM positions
            WHERE state IN ('OPEN', 'CLOSING') AND ABS(quantity) > 0
            """
        ).fetchone()
        pending_rows = self.connection.execute(
            """
            SELECT COUNT(*) AS n FROM orders
            WHERE state IN ('CREATED', 'SUBMITTED', 'PARTIALLY_FILLED')
            """
        ).fetchone()
        open_positions = int(position_rows["n"] if position_rows is not None else 0) + int(
            pending_rows["n"] if pending_rows is not None else 0
        )

        today = datetime.now(UTC).date().isoformat()
        recent_exits = self.connection.execute(
            """
            SELECT realized_pnl FROM position_exits
            WHERE substr(closed_at, 1, 10) = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (today,),
        ).fetchall()
        consecutive_losses = 0
        for row in recent_exits:
            if float(row["realized_pnl"]) >= 0:
                break
            consecutive_losses += 1

        balance = self.load_balance("USDT")
        equity_usdt = float(balance.equity) if balance is not None and balance.equity > 0 else 0.0

        capital = equity_usdt if equity_usdt > 0 else (reference_capital_usdt or 0.0)
        daily_loss_rate = 0.0
        if capital > 0:
            pnl_row = self.connection.execute(
                """
                SELECT COALESCE(SUM(realized_pnl), 0.0) AS pnl FROM position_exits
                WHERE substr(closed_at, 1, 10) = ?
                """,
                (today,),
            ).fetchone()
            realized_today = float(pnl_row["pnl"] if pnl_row is not None else 0.0)
            daily_loss_rate = max(0.0, -realized_today) / capital

        return RiskState(
            daily_loss_rate=daily_loss_rate,
            consecutive_losses=consecutive_losses,
            open_positions=open_positions,
            equity_usdt=equity_usdt,
            consecutive_losing_days=self._consecutive_losing_days(today),
        )

    def _consecutive_losing_days(self, today: str) -> int:
        """Count consecutive negative-PnL days immediately preceding today.

        A day without any exits breaks the streak: the deleveraging rule is
        about an active losing run, not stale history.
        """
        rows = self.connection.execute(
            """
            SELECT substr(closed_at, 1, 10) AS day, SUM(realized_pnl) AS pnl
            FROM position_exits
            WHERE substr(closed_at, 1, 10) < ?
            GROUP BY day
            ORDER BY day DESC
            LIMIT 10
            """,
            (today,),
        ).fetchall()
        streak = 0
        expected = datetime.fromisoformat(today).date()
        for row in rows:
            day = datetime.fromisoformat(str(row["day"])).date()
            expected = expected - timedelta(days=1)
            if day != expected or float(row["pnl"]) >= 0:
                break
            streak += 1
        return streak

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

    def _position_from_row(self, row: sqlite3.Row) -> PositionRecord:
        opened_at = row["opened_at"] or row["updated_at"]
        return PositionRecord(
            id=row["id"],
            symbol=row["symbol"],
            side=SignalDirection(row["side"]),
            quantity=row["quantity"],
            average_entry=row["average_entry"],
            state=PositionState(row["state"]),
            opened_at=_from_utc_text(opened_at),
            updated_at=_from_utc_text(row["updated_at"]),
            entry_signal_id=row["entry_signal_id"],
            stop_loss=row["stop_loss"],
            take_profit=row["take_profit"],
            expires_at=_from_utc_text(row["expires_at"]) if row["expires_at"] else None,
            margin_mode=row["margin_mode"],
            leverage=row["leverage"],
            exit_reason=ExitReason(row["exit_reason"]) if row["exit_reason"] else None,
            closed_at=_from_utc_text(row["closed_at"]) if row["closed_at"] else None,
            realized_pnl=row["realized_pnl"],
        )

    def _position_exit_from_row(self, row: sqlite3.Row) -> PositionExitRecord:
        return PositionExitRecord(
            id=row["id"],
            position_id=row["position_id"],
            symbol=row["symbol"],
            side=SignalDirection(row["side"]),
            reason=ExitReason(row["reason"]),
            entry_price=row["entry_price"],
            exit_price=row["exit_price"],
            quantity=row["quantity"],
            realized_pnl=row["realized_pnl"],
            opened_at=_from_utc_text(row["opened_at"]),
            closed_at=_from_utc_text(row["closed_at"]),
            notes=row["notes"],
        )
