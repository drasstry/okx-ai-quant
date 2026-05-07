import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from okx_ai_quant.account import AccountService
from okx_ai_quant.config import Settings
from okx_ai_quant.execution import ExecutionDecision
from okx_ai_quant.market_data import normalize_ticker
from okx_ai_quant.models import (
    ExitReason,
    FillRecord,
    OrderRecord,
    OrderState,
    PositionRecord,
    RiskStatus,
    SignalDirection,
)
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
        self.publish_runtime_status()
        self._notify(
            "OKX AI Quant bot started "
            f"(mode={self.settings.TRADING_MODE}, "
            f"trading={self.settings.ENABLE_TRADING}, "
            f"strategy={self.settings.STRATEGY_NAME}, "
            f"poll={self.settings.POLL_INTERVAL_SECONDS}s)."
        )
        while True:
            started = time.time()
            self.publish_runtime_status()
            self.run_once()
            self.maybe_send_daily_report()
            elapsed = time.time() - started
            time.sleep(max(5, self.settings.POLL_INTERVAL_SECONDS - elapsed))

    def publish_runtime_status(self) -> None:
        now = datetime.now(UTC)
        runtime_values = {
            "runtime:mode": str(self.settings.TRADING_MODE),
            "runtime:trading_enabled": str(self.settings.ENABLE_TRADING),
            "runtime:strategy": self.settings.STRATEGY_NAME,
            "runtime:symbols": ", ".join(self.settings.symbols),
            "runtime:poll_interval_seconds": str(self.settings.POLL_INTERVAL_SECONDS),
            "runtime:updated_at": now.isoformat(),
        }
        for key, value in runtime_values.items():
            self.runner.storage.set_state(key, value, now)

    def run_once(self) -> list[BotCycleResult]:
        self._sync_server_time()
        try:
            self.sync_tracked_orders()
            self.sweep_stale_orders()
        except Exception:
            pass
        self.reconcile_exchange_state()
        if self.settings.MANAGE_EXISTING_POSITIONS:
            self.monitor_positions()

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
        now = datetime.now(self.tz)
        sent = False
        for report_slot in _due_report_slots(now, self.settings.report_times):
            state_key = _report_slot_state_key(now.date(), report_slot)
            if self.runner.storage.get_state(state_key):
                continue
            if self.send_daily_report(now.date(), report_slot=report_slot):
                self.runner.storage.set_state(state_key, "sent", datetime.now(UTC))
                sent = True
        return sent

    def send_daily_report(
        self,
        report_date: date | None = None,
        *,
        force: bool = False,
        report_slot: str | None = None,
    ) -> bool:
        report_date = report_date or datetime.now(self.tz).date()
        report_key = _daily_report_key(report_date, report_slot)
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
        self._save_entry_plan(result, submitted)
        self._notify(
            f"Submitted order: {submitted.side} {submitted.symbol} "
            f"size={submitted.quantity:.6g}, ordId={submitted.order_id or '-'}."
        )
        return BotCycleResult(symbol=symbol, result=result, submitted_order=submitted)

    def monitor_positions(self) -> list[OrderRecord]:
        submitted_orders: list[OrderRecord] = []
        for position in self.runner.storage.load_open_positions():
            decision = self._position_exit_decision(position)
            if decision is None:
                continue
            reason, exit_price, notes = decision
            if not self.settings.ENABLE_TRADING:
                self._notify(
                    f"Planned exit only: {reason.value} {position.symbol} "
                    f"at {exit_price:.6g}. Set ENABLE_TRADING=true to submit close orders."
                )
                continue

            close_order = self.runner.execution_engine.close_position(
                position,
                reason=reason.value,
            )
            self.runner.storage.insert_order(close_order)
            self.runner.storage.mark_position_closing(
                symbol=position.symbol,
                reason=reason,
                updated_at=datetime.now(UTC),
            )
            submitted_orders.append(close_order)
            self._notify(
                f"Submitted close order: {reason.value} {position.symbol} "
                f"qty={abs(position.quantity):.6g}, ordId={close_order.order_id or '-'}."
            )
            self.runner.storage.set_state(
                f"last_exit_reason:{position.symbol}",
                notes,
                datetime.now(UTC),
            )
        return submitted_orders

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

    def reconcile_exchange_state(self) -> None:
        self._snapshot_balances()
        try:
            exchange_positions = self.account.fetch_positions()
        except Exception as exc:
            self._notify(f"Could not reconcile OKX positions: {exc}")
            return

        allowed_symbols = set(self.settings.symbols)
        seen_symbols: set[str] = set()
        now = datetime.now(UTC)
        for exchange_position in exchange_positions:
            if exchange_position.symbol not in allowed_symbols:
                continue
            if exchange_position.average_entry <= 0:
                continue

            local = self.runner.storage.load_position(exchange_position.symbol)
            seen_symbols.add(exchange_position.symbol)
            self.runner.storage.upsert_position(
                symbol=exchange_position.symbol,
                side=exchange_position.side,
                quantity=exchange_position.quantity,
                average_entry=exchange_position.average_entry,
                opened_at=local.opened_at if local is not None else exchange_position.opened_at,
                updated_at=exchange_position.updated_at,
                entry_signal_id=local.entry_signal_id if local is not None else None,
                stop_loss=local.stop_loss if local is not None else None,
                take_profit=local.take_profit if local is not None else None,
                expires_at=local.expires_at if local is not None else None,
            )

        for local in self.runner.storage.load_open_positions():
            if local.symbol in allowed_symbols and local.symbol not in seen_symbols:
                self.runner.storage.close_position(
                    symbol=local.symbol,
                    reason=ExitReason.RISK_EXIT,
                    exit_price=local.average_entry,
                    closed_at=now,
                    notes="Local position closed because OKX reported no matching open SWAP position.",
                )

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

        if order.order_type.startswith("market_close"):
            reason = _exit_reason_from_order_type(order.order_type)
            exit_record = self.runner.storage.close_position(
                symbol=order.symbol,
                reason=reason,
                exit_price=avg_price,
                closed_at=datetime.now(UTC),
                notes=f"Closed by {reason.value} from OKX fill {order.order_id}.",
            )
            if exit_record is not None:
                self._notify(
                    f"Position closed: {exit_record.reason.value} {exit_record.symbol} "
                    f"PnL={exit_record.realized_pnl:.6g}."
                )
            return

        signed_quantity = fill_size if order.side == SignalDirection.LONG else -fill_size
        current = self.runner.storage.load_position_quantity(order.symbol)
        updated = current + signed_quantity
        plan = self._load_entry_plan(order)
        opened_at = _datetime_from_plan(plan.get("opened_at")) if plan else None
        expires_at = _datetime_from_plan(plan.get("expires_at")) if plan else None
        self.runner.storage.upsert_position(
            symbol=order.symbol,
            quantity=updated,
            average_entry=avg_price,
            side=order.side,
            entry_signal_id=order.signal_id,
            stop_loss=_float_or_none(plan.get("stop_loss")) if plan else None,
            take_profit=_float_or_none(plan.get("take_profit")) if plan else None,
            expires_at=expires_at,
            opened_at=opened_at,
            updated_at=datetime.now(UTC),
        )

    def _position_exit_decision(
        self,
        position: PositionRecord,
    ) -> tuple[ExitReason, float, str] | None:
        exit_price = self._latest_market_price(position.symbol)
        if exit_price <= 0:
            return None

        if position.side == SignalDirection.LONG:
            if position.stop_loss is not None and exit_price <= position.stop_loss:
                return (
                    ExitReason.STOP_LOSS,
                    exit_price,
                    f"Last price {exit_price:.6g} <= stop {position.stop_loss:.6g}.",
                )
            if position.take_profit is not None and exit_price >= position.take_profit:
                return (
                    ExitReason.TAKE_PROFIT,
                    exit_price,
                    f"Last price {exit_price:.6g} >= target {position.take_profit:.6g}.",
                )
        elif position.side == SignalDirection.SHORT:
            if position.stop_loss is not None and exit_price >= position.stop_loss:
                return (
                    ExitReason.STOP_LOSS,
                    exit_price,
                    f"Last price {exit_price:.6g} >= stop {position.stop_loss:.6g}.",
                )
            if position.take_profit is not None and exit_price <= position.take_profit:
                return (
                    ExitReason.TAKE_PROFIT,
                    exit_price,
                    f"Last price {exit_price:.6g} <= target {position.take_profit:.6g}.",
                )

        now = datetime.now(UTC)
        if position.expires_at is not None and now >= position.expires_at:
            return (
                ExitReason.TIMEOUT,
                exit_price,
                f"Position exceeded timeout at {position.expires_at.isoformat()}.",
            )

        risk_state = self.runner._resolve_risk_state()
        if risk_state.daily_loss_rate >= self.settings.MAX_DAILY_LOSS:
            return (
                ExitReason.RISK_EXIT,
                exit_price,
                f"Daily loss {risk_state.daily_loss_rate:.4f} reached limit.",
            )

        if self.settings.EXIT_ON_REVERSE_SIGNAL:
            reverse = self._reverse_signal_reason(position)
            if reverse is not None:
                return (ExitReason.REVERSE_SIGNAL, exit_price, reverse)

        return None

    def _latest_market_price(self, symbol: str) -> float:
        try:
            ticker = normalize_ticker(self.runner.client.get_ticker(symbol))
            return ticker.last
        except Exception:
            candles = self.runner.storage.load_recent_candles(symbol, "1H", 1)
            return candles[-1].close if candles else 0.0

    def _reverse_signal_reason(self, position: PositionRecord) -> str | None:
        try:
            one_hour = self.runner._fetch_store_candles(position.symbol, "1H")
            four_hour = self.runner._fetch_store_candles(position.symbol, "4H")
            signal = self.runner.strategy.generate(position.symbol, one_hour, four_hour)
        except Exception:
            return None

        if position.side == SignalDirection.LONG and signal.direction == SignalDirection.SHORT:
            return f"Reverse signal detected: {signal.reason}"
        if position.side == SignalDirection.SHORT and signal.direction == SignalDirection.LONG:
            return f"Reverse signal detected: {signal.reason}"
        return None

    def _save_entry_plan(self, result: RunOnceResult, order: OrderRecord) -> None:
        expires_at = datetime.now(UTC) + timedelta(hours=self.settings.POSITION_TIMEOUT_HOURS)
        plan = {
            "opened_at": datetime.now(UTC).isoformat(),
            "expires_at": expires_at.isoformat(),
            "entry_price": getattr(result.signal, "entry_price", None),
            "stop_loss": getattr(result.signal, "stop_price", None),
            "take_profit": getattr(result.signal, "target_price", None),
        }
        payload = json.dumps(plan, sort_keys=True)
        for key in self._entry_plan_keys(order):
            self.runner.storage.set_state(key, payload, datetime.now(UTC))

    def _load_entry_plan(self, order: OrderRecord) -> dict[str, object]:
        for key in self._entry_plan_keys(order):
            raw = self.runner.storage.get_state(key)
            if not raw:
                continue
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
        return {}

    def _entry_plan_keys(self, order: OrderRecord) -> list[str]:
        keys: list[str] = []
        if order.client_order_id:
            keys.append(f"entry_plan:client:{order.client_order_id}")
        if order.order_id:
            keys.append(f"entry_plan:order:{order.order_id}")
        return keys

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
        exits = self.runner.storage.load_position_exits()
        todays_exits = [exit for exit in exits if exit.closed_at.date().isoformat() == report_date]
        if todays_exits:
            metrics["closed_positions"] = len(todays_exits)
            metrics["realized_pnl"] = f"{sum(exit.realized_pnl for exit in todays_exits):.6g}"
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


def _due_report_slots(now: datetime, report_times: list[str]) -> list[str]:
    due_slots: list[str] = []
    for report_time in report_times:
        scheduled = _parse_report_time(report_time)
        if scheduled is None:
            continue
        if now.timetz().replace(tzinfo=None) >= scheduled:
            due_slots.append(f"{scheduled.hour:02d}:{scheduled.minute:02d}")
    return due_slots


def _parse_report_time(value: str) -> dt_time | None:
    try:
        hour_text, minute_text = value.strip().split(":", maxsplit=1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt_time(hour=hour, minute=minute)


def _daily_report_key(report_date: date, report_slot: str | None = None) -> str:
    if report_slot is None:
        return report_date.isoformat()
    return f"{report_date.isoformat()}:{report_slot}"


def _report_slot_state_key(report_date: date, report_slot: str) -> str:
    return f"daily_report_sent:{report_date.isoformat()}:{report_slot}"


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


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    parsed = _float(value)
    return parsed if parsed > 0 else None


def _datetime_from_plan(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except ValueError:
        return None


def _exit_reason_from_order_type(order_type: str) -> ExitReason:
    _, _, raw_reason = order_type.partition(":")
    try:
        return ExitReason(raw_reason)
    except ValueError:
        return ExitReason.RISK_EXIT
