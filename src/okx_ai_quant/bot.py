import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from okx_ai_quant.account import AccountService
from okx_ai_quant.config import Settings
from okx_ai_quant.execution import ExecutionDecision
from okx_ai_quant.models import FillRecord, OrderRecord, OrderState, RiskStatus, SignalDirection
from okx_ai_quant.notifier import NotificationError, Notifier
from okx_ai_quant.reports import render_daily_report
from okx_ai_quant.runner import RunOnceResult, Runner


@dataclass(frozen=True, kw_only=True)
class BotCycleResult:
    symbol: str
    result: RunOnceResult | None
    submitted_order: OrderRecord | None = None
    skipped_reason: str | None = None


class TradingBot:
    def __init__(self, *, settings: Settings, runner: Runner, notifier: Notifier) -> None:
        self.settings = settings
        self.runner = runner
        self.notifier = notifier
        self.tz = ZoneInfo(settings.APP_TIMEZONE)
        self.account = AccountService(runner.client)
        self._tradable_symbols_cache: list[str] | None = None

    def run_forever(self) -> None:
        self._notify(
            "OKX AI Quant bot started "
            f"(mode={self.settings.TRADING_MODE}, "
            f"trading={self.settings.ENABLE_TRADING}, "
            f"strategy={self.settings.STRATEGY_NAME}, "
            f"poll={self.settings.POLL_INTERVAL_SECONDS}s)."
        )
        while True:
            started = time.time()
            self.run_once()
            self.maybe_send_daily_report()
            elapsed = time.time() - started
            time.sleep(max(5, self.settings.POLL_INTERVAL_SECONDS - elapsed))

    def run_once(self) -> list[BotCycleResult]:
        self._sync_server_time()
        try:
            self.sync_tracked_orders()
            self.sweep_stale_orders()
        except Exception:
            pass
        self._snapshot_balances()

        results: list[BotCycleResult] = []
        for symbol in self._tradable_symbols():
            try:
                results.append(self._process_symbol(symbol))
            except Exception as exc:  # noqa: BLE001 - bot loop must isolate symbols.
                reason = f"{symbol} failed: {exc}"
                self._notify(reason)
                results.append(BotCycleResult(symbol=symbol, result=None, skipped_reason=reason))
        return results

    def _tradable_symbols(self) -> list[str]:
        if self._tradable_symbols_cache is not None:
            return self._tradable_symbols_cache

        get_instruments = getattr(self.runner.client, "get_instruments", None)
        if not callable(get_instruments):
            self._tradable_symbols_cache = self.settings.symbols
            return self._tradable_symbols_cache

        try:
            rows = get_instruments("SWAP")
        except Exception as exc:
            self._notify(f"Could not validate OKX SWAP instruments; using configured symbols: {exc}")
            self._tradable_symbols_cache = self.settings.symbols
            return self._tradable_symbols_cache

        available = {
            str(row.get("instId"))
            for row in rows
            if row.get("instId") and str(row.get("state", "live")).lower() == "live"
        }
        tradable = [symbol for symbol in self.settings.symbols if symbol in available]
        skipped = [symbol for symbol in self.settings.symbols if symbol not in available]
        if skipped:
            self._notify(
                "Skipped symbols not available in this OKX environment: " + ", ".join(skipped)
            )
        if not tradable:
            self._notify("No configured symbols are available in this OKX environment.")

        self._tradable_symbols_cache = tradable
        return self._tradable_symbols_cache

    def maybe_send_daily_report(self) -> bool:
        today = datetime.now(self.tz).date()
        if not _is_report_due(datetime.now(self.tz), self.settings.report_times):
            return False
        return self.send_daily_report(today)

    def send_daily_report(self, report_date: date | None = None, *, force: bool = False) -> bool:
        report_date = report_date or datetime.now(self.tz).date()
        report_key = report_date.isoformat()
        storage = self.runner.storage
        if storage.daily_report_exists(report_key) and not force:
            return False

        metrics = self._daily_metrics(report_key)
        analyses = storage.load_trade_analyses_for_date(report_key)
        report = render_daily_report(report_date, metrics, analyses=analyses)
        storage.insert_daily_report(report_key, report, datetime.now(UTC))
        self._notify(report)
        return True

    def sync_tracked_orders(self) -> None:
        for order in self.runner.storage.load_open_orders():
            if not order.order_id and not order.client_order_id:
                continue
            raw = self.runner.client.get_order(
                order.symbol,
                order_id=order.order_id,
                client_order_id=order.client_order_id,
            )
            rows = raw.get("data") or []
            if not rows:
                continue
            data = rows[0]
            state = _map_okx_order_state(data.get("state"))
            if state is None:
                continue
            self.runner.storage.update_order_state(
                state=state,
                order_id=order.order_id,
                client_order_id=order.client_order_id,
            )
            if state == OrderState.FILLED:
                self._record_filled_order(order, data)

    def sweep_stale_orders(self) -> None:
        raw = self.runner.client.get_pending_orders()
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        for order in raw.get("data") or []:
            client_order_id = str(order.get("clOrdId") or "")
            if not client_order_id.startswith("okxai"):
                continue
            created_ms = _int(order.get("cTime") or order.get("uTime") or now_ms)
            age_seconds = max(0.0, (now_ms - created_ms) / 1000)
            if age_seconds < self.settings.ORDER_STALE_SECONDS:
                continue
            symbol = str(order.get("instId") or "")
            order_id = order.get("ordId")
            self.runner.client.cancel_order(
                symbol,
                order_id=order_id,
                client_order_id=client_order_id,
            )
            self.runner.storage.update_order_state(
                state=OrderState.CANCELED,
                order_id=order_id,
                client_order_id=client_order_id,
            )
            self._notify(f"Canceled stale order {client_order_id} on {symbol} after {age_seconds:.0f}s.")

    def _process_symbol(self, symbol: str) -> BotCycleResult:
        pending_reason = self._pending_order_reason(symbol)
        if pending_reason:
            return BotCycleResult(symbol=symbol, result=None, skipped_reason=pending_reason)

        result = self.runner.run_once(symbol)
        if result.risk_decision.status != RiskStatus.APPROVED:
            return BotCycleResult(symbol=symbol, result=result)

        duplicate_reason = self._duplicate_signal_reason(result)
        if duplicate_reason:
            return BotCycleResult(symbol=symbol, result=result, skipped_reason=duplicate_reason)

        self._mark_signal_seen(result)
        if not self.settings.ENABLE_TRADING:
            self._notify(
                f"Planned order only: {result.signal.direction} {symbol} "
                f"{result.risk_decision.position_size_usdt:.2f} USDT. "
                "Set ENABLE_TRADING=true to submit demo/live orders."
            )
            return BotCycleResult(symbol=symbol, result=result, skipped_reason="trading disabled")

        submitted = self.runner.execution_engine.submit(
            ExecutionDecision(
                signal_id=result.risk_decision.signal_id,
                symbol=result.signal.symbol,
                direction=result.signal.direction,
                status=result.risk_decision.status,
                reason=result.risk_decision.reason,
                position_size_usdt=result.risk_decision.position_size_usdt,
                leverage=result.risk_decision.leverage,
                created_at=result.risk_decision.created_at,
                id=result.risk_decision.id,
            )
        )
        self.runner.storage.insert_order(submitted)
        self._notify(
            f"Submitted order: {submitted.side} {submitted.symbol} "
            f"size={submitted.quantity:.6g}, ordId={submitted.order_id or '-'}."
        )
        return BotCycleResult(symbol=symbol, result=result, submitted_order=submitted)

    def _pending_order_reason(self, symbol: str) -> str | None:
        try:
            raw = self.runner.client.get_pending_orders(symbol)
        except Exception as exc:
            if self.settings.ENABLE_TRADING:
                return f"Could not query pending orders for {symbol}: {exc}"
            return None
        bot_orders = [
            order for order in raw.get("data") or []
            if str(order.get("clOrdId") or "").startswith("okxai")
        ]
        if bot_orders:
            return f"{symbol} has {len(bot_orders)} pending bot order(s)."
        return None

    def _duplicate_signal_reason(self, result: RunOnceResult) -> str | None:
        candle_stamp = self._latest_candle_stamp(result.signal.symbol, result.signal.timeframe)
        if candle_stamp is None:
            return None
        key = self._last_signal_key(result.signal.symbol, result.signal.direction)
        if self.runner.storage.get_state(key) == candle_stamp:
            return f"{result.signal.symbol} already handled {result.signal.direction} for candle {candle_stamp}."
        return None

    def _mark_signal_seen(self, result: RunOnceResult) -> None:
        candle_stamp = self._latest_candle_stamp(result.signal.symbol, result.signal.timeframe)
        if candle_stamp is None:
            return
        key = self._last_signal_key(result.signal.symbol, result.signal.direction)
        self.runner.storage.set_state(key, candle_stamp, datetime.now(UTC))

    def _latest_candle_stamp(self, symbol: str, timeframe: str) -> str | None:
        candles = self.runner.storage.load_recent_candles(symbol, timeframe, 1)
        if not candles:
            return None
        return candles[-1].timestamp.isoformat()

    def _last_signal_key(self, symbol: str, direction: SignalDirection) -> str:
        mode = "submit" if self.settings.ENABLE_TRADING else "observe"
        return f"last_signal_candle:{mode}:{symbol}:{direction.value}"

    def _snapshot_balances(self) -> None:
        try:
            summary = self.account.fetch_summary()
        except Exception:
            return
        now = datetime.now(UTC)
        for snapshot in summary.balance_snapshots(now):
            self.runner.storage.upsert_balance(snapshot)

    def _record_filled_order(self, order: OrderRecord, data: dict[str, object]) -> None:
        fill_size = _float(data.get("accFillSz"))
        avg_price = _float(data.get("avgPx") or data.get("px"))
        fee = abs(_float(data.get("fee")))
        if fill_size <= 0 or avg_price <= 0 or not order.order_id:
            return

        self.runner.storage.insert_fill(
            FillRecord(
                order_id=order.order_id,
                symbol=order.symbol,
                price=avg_price,
                quantity=fill_size,
                fee=fee,
                created_at=datetime.now(UTC),
            )
        )

        signed_quantity = fill_size if order.side == SignalDirection.LONG else -fill_size
        current = self.runner.storage.load_position_quantity(order.symbol)
        updated = current + signed_quantity
        self.runner.storage.upsert_position(
            symbol=order.symbol,
            quantity=updated,
            average_entry=avg_price,
            updated_at=datetime.now(UTC),
        )

    def _daily_metrics(self, report_date: str) -> dict[str, object]:
        rows = self.runner.storage.connection.execute(
            """
            SELECT state, COUNT(*) AS n
            FROM orders
            WHERE substr(created_at, 1, 10) = ?
            GROUP BY state
            """,
            (report_date,),
        ).fetchall()
        metrics: dict[str, object] = {f"orders_{row['state'].lower()}": row["n"] for row in rows}
        balance = self.runner.storage.load_balance("USDT")
        if balance is not None:
            metrics["usdt_equity"] = f"{balance.equity:.2f}"
            metrics["usdt_available"] = f"{balance.available:.2f}"
        return metrics

    def _sync_server_time(self) -> None:
        sync = getattr(self.runner.client, "sync_server_time", None)
        if callable(sync):
            sync()

    def _notify(self, message: str) -> None:
        try:
            self.notifier.send(message)
        except NotificationError:
            return


def _map_okx_order_state(value: object) -> OrderState | None:
    raw = str(value or "").lower()
    if raw in {"live", "partially_filled"}:
        return OrderState.PARTIALLY_FILLED
    if raw == "filled":
        return OrderState.FILLED
    if raw in {"canceled", "cancelled"}:
        return OrderState.CANCELED
    return None


def _is_report_due(now: datetime, report_times: list[str]) -> bool:
    current = f"{now.hour:02d}:{now.minute:02d}"
    return current in set(report_times)


def _float(value: object) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
