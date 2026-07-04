import json
import time
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from okx_ai_quant.account import AccountService
from okx_ai_quant.config import Settings
from okx_ai_quant.execution import ExecutionDecision
from okx_ai_quant.log_review import summarize_recent_failures
from okx_ai_quant.market_data import normalize_ticker
from okx_ai_quant.models import (
    ExitReason,
    FillRecord,
    OrderRecord,
    OrderState,
    PositionRecord,
    PositionState,
    RiskStatus,
    SignalDirection,
)
from okx_ai_quant.notifier import NotificationError, Notifier
from okx_ai_quant.portfolio import ExposureLimits, evaluate_drawdown, exposure_block_reason
from okx_ai_quant.reports import build_trade_overview_fallback, render_trade_overview
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
        self._blocked_leverage_updates: set[tuple[str, str, int]] = set()
        self._entry_block_reason: str | None = None

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
            try:
                self.publish_runtime_status()
                self.run_once()
                self.maybe_send_daily_report()
            except Exception as exc:  # noqa: BLE001 - a dead bot cannot manage stops; log and keep looping.
                self._notify(f"Bot cycle failed, retrying next cycle: {exc}")
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
        except Exception as exc:  # noqa: BLE001 - order sync must not block position management.
            self._notify(f"Order sync failed this cycle: {exc}")
        self.reconcile_exchange_state()
        if self.settings.MANAGE_EXISTING_POSITIONS:
            self.monitor_positions()

        tradable_symbols = self._tradable_symbols()
        self._entry_block_reason = self._portfolio_block_reason() or self._okx_entry_health_block_reason()
        if self._entry_block_reason:
            self._notify(f"Blocking new entries this cycle: {self._entry_block_reason}")
            return [
                BotCycleResult(symbol=symbol, result=None, skipped_reason=self._entry_block_reason)
                for symbol in tradable_symbols
            ]

        try:
            self.runner.prepare_universe(tradable_symbols)
        except Exception as exc:  # noqa: BLE001 - a data hiccup must not kill the bot.
            reason = f"Universe preparation failed: {exc}"
            self._notify(reason)
            return [
                BotCycleResult(symbol=symbol, result=None, skipped_reason=reason)
                for symbol in tradable_symbols
            ]
        results: list[BotCycleResult] = []
        for symbol in tradable_symbols:
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

        context = self._trade_overview_context(report_date)
        fallback = build_trade_overview_fallback(context)
        summary = self._generate_trade_overview_summary(context, fallback)
        report = render_trade_overview(
            report_date=report_date,
            report_slot=report_slot,
            context=context,
            llm_summary=summary,
        )
        storage.insert_daily_report(report_key, report, datetime.now(UTC))
        self._send_report(report)
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

        held_reason = self._held_position_reason(symbol)
        if held_reason:
            return BotCycleResult(symbol=symbol, result=None, skipped_reason=held_reason)

        result = self.runner.run_once(symbol)
        if result.risk_decision.status != RiskStatus.APPROVED:
            return BotCycleResult(symbol=symbol, result=result)

        duplicate_reason = self._duplicate_signal_reason(result)
        if duplicate_reason:
            return BotCycleResult(symbol=symbol, result=result, skipped_reason=duplicate_reason)

        exposure_reason = self._exposure_block_reason(result)
        if exposure_reason:
            self._notify(f"Exposure cap blocked {symbol}: {exposure_reason}")
            return BotCycleResult(symbol=symbol, result=result, skipped_reason=exposure_reason)

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
                stop_loss=getattr(result.signal, "stop_price", None),
                take_profit=getattr(result.signal, "target_price", None),
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
            exchange_position = self._refresh_position_from_exchange(position.symbol)
            if exchange_position is None:
                self._notify(
                    f"Skip exit for {position.symbol}: OKX reports no open position."
                )
                continue
            position = self._merge_position_plan(exchange_position, position)
            try:
                decision = self._position_exit_decision(position)
            except Exception as exc:  # noqa: BLE001 - one position must not stop the bot loop.
                self._notify(f"Could not evaluate exit for {position.symbol}: {exc}")
                continue
            if decision is None:
                continue
            reason, exit_price, notes = decision
            existing_close = self._open_close_order(position.symbol)
            if existing_close is not None:
                self.runner.storage.mark_position_closing(
                    symbol=position.symbol,
                    reason=reason,
                    updated_at=datetime.now(UTC),
                )
                self._notify(
                    f"Close order already pending: {reason.value} {position.symbol} "
                    f"ordId={existing_close.order_id or '-'}, clOrdId={existing_close.client_order_id or '-'}."
                )
                continue
            if not self.settings.ENABLE_TRADING:
                self._notify(
                    f"Planned exit only: {reason.value} {position.symbol} "
                    f"at {exit_price:.6g}. Set ENABLE_TRADING=true to submit close orders."
                )
                continue

            try:
                close_order = self.runner.execution_engine.close_position(
                    position,
                    reason=reason.value,
                )
            except Exception as exc:  # noqa: BLE001 - transient exchange failures are retried next cycle.
                self._notify(
                    f"Close order failed: {reason.value} {position.symbol} "
                    f"qty={abs(position.quantity):.6g}: {exc}"
                )
                continue

            self.runner.storage.insert_order(close_order)
            self.runner.storage.mark_position_closing(
                symbol=position.symbol,
                reason=reason,
                updated_at=datetime.now(UTC),
            )
            submitted_orders.append(close_order)
            self._notify(
                f"Submitted close order: {reason.value} {position.symbol} "
                f"qty={abs(position.quantity):.6g}, ordId={close_order.order_id or '-'}.",
            )
            self.runner.storage.set_state(
                f"last_exit_reason:{position.symbol}",
                notes,
                datetime.now(UTC),
            )
        return submitted_orders

    def _open_close_order(self, symbol: str) -> OrderRecord | None:
        for order in self.runner.storage.load_open_orders():
            if order.symbol == symbol and order.order_type.startswith("market_close"):
                return order
        return None

    # ------------------------------------------------------- portfolio risk
    def _portfolio_block_reason(self) -> str | None:
        """Run the drawdown kill switch; return a reason when entries are halted."""
        halted = self.runner.storage.get_state("portfolio:halted")
        equity = self._usdt_equity()
        if equity <= 0:
            if halted:
                return f"Trading halted: {halted} (send /resume to restart)."
            return None

        stored_hwm = _float(self.runner.storage.get_state("portfolio:hwm"))
        status = evaluate_drawdown(
            equity=equity,
            high_water_mark=stored_hwm if stored_hwm > 0 else equity,
            max_drawdown=self.settings.MAX_DRAWDOWN,
        )
        now = datetime.now(UTC)
        self.runner.storage.set_state("portfolio:hwm", f"{status.high_water_mark:.8g}", now)

        if halted:
            return f"Trading halted: {halted} (send /resume to restart)."
        if not status.tripped:
            return None

        reason = (
            f"Drawdown {status.drawdown:.2%} breached MAX_DRAWDOWN "
            f"{self.settings.MAX_DRAWDOWN:.2%} (equity {equity:.2f}, "
            f"HWM {status.high_water_mark:.2f})"
        )
        self.halt(reason)
        self._flatten_all_positions(reason)
        self._send_report(
            f"⛔ 回撤熔断触发：{reason}。已提交全部平仓并停止开新仓，"
            "确认后发送 /resume 恢复交易（恢复时高水位将重置为当前权益）。"
        )
        return f"Trading halted: {reason} (send /resume to restart)."

    def halt(self, reason: str) -> None:
        now = datetime.now(UTC)
        self.runner.storage.set_state("portfolio:halted", reason, now)
        self.runner.storage.set_state("portfolio:halted_at", now.isoformat(), now)

    def resume(self) -> float:
        """Clear the halt and re-base the high-water mark to current equity."""
        now = datetime.now(UTC)
        equity = self._usdt_equity()
        if equity > 0:
            self.runner.storage.set_state("portfolio:hwm", f"{equity:.8g}", now)
        self.runner.storage.set_state("portfolio:halted", "", now)
        return equity

    def is_halted(self) -> str | None:
        return self.runner.storage.get_state("portfolio:halted") or None

    def _flatten_all_positions(self, reason: str) -> None:
        for position in self.runner.storage.load_open_positions():
            if not self.settings.ENABLE_TRADING:
                self._notify(
                    f"Planned flatten only: {position.symbol} qty={abs(position.quantity):.6g} "
                    f"({reason}). Set ENABLE_TRADING=true to submit close orders."
                )
                continue
            try:
                close_order = self.runner.execution_engine.close_position(
                    position,
                    reason=ExitReason.RISK_EXIT.value,
                )
            except Exception as exc:  # noqa: BLE001 - keep flattening the rest of the book.
                self._notify(f"Flatten failed for {position.symbol}: {exc}")
                continue
            self.runner.storage.insert_order(close_order)
            self.runner.storage.mark_position_closing(
                symbol=position.symbol,
                reason=ExitReason.RISK_EXIT,
                updated_at=datetime.now(UTC),
            )
            self._notify(f"Flatten submitted: {position.symbol} ordId={close_order.order_id or '-'}.")

    def _usdt_equity(self) -> float:
        balance = self.runner.storage.load_balance("USDT")
        return float(balance.equity) if balance is not None and balance.equity > 0 else 0.0

    def _open_notional_by_side(self) -> tuple[float, float]:
        long_notional = 0.0
        short_notional = 0.0
        for position in self.runner.storage.load_open_positions():
            mark = self._latest_market_price(position.symbol)
            if mark <= 0:
                mark = position.average_entry
            notional = abs(position.quantity) * mark * self._contract_pnl_multiplier(
                position.symbol, mark
            )
            if position.side == SignalDirection.LONG:
                long_notional += notional
            else:
                short_notional += notional
        return long_notional, short_notional

    def _exposure_block_reason(self, result: RunOnceResult) -> str | None:
        long_notional, short_notional = self._open_notional_by_side()
        return exposure_block_reason(
            equity=self._usdt_equity(),
            long_notional=long_notional,
            short_notional=short_notional,
            candidate_notional=result.risk_decision.position_size_usdt,
            candidate_direction=result.signal.direction,
            limits=ExposureLimits(
                max_total_rate=self.settings.MAX_TOTAL_EXPOSURE_RATE,
                max_net_rate=self.settings.MAX_NET_EXPOSURE_RATE,
            ),
        )

    def _held_position_reason(self, symbol: str) -> str | None:
        """Block new entries while the symbol already has a position.

        Without this, a sustained trend adds another full-size entry every
        candle (each add also overwriting the previous stop plan), so the
        per-symbol exposure grows far beyond what the risk model sized.
        Reverse-signal/stop exits still manage the open position.
        """
        position = self.runner.storage.load_position(symbol)
        if (
            position is not None
            and position.state in {PositionState.OPEN, PositionState.CLOSING}
            and abs(position.quantity) > 0
        ):
            return f"{symbol} already has an open position; not stacking entries."
        return None

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
            if snapshot.currency == "USDT":
                self.runner.storage.insert_equity_snapshot(
                    currency="USDT",
                    equity=snapshot.equity,
                    available=snapshot.available,
                    created_at=now,
                )

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
                margin_mode=exchange_position.margin_mode,
                leverage=exchange_position.leverage,
            )
            self._enforce_position_leverage(exchange_position)

        for local in self.runner.storage.load_open_positions():
            if local.symbol in allowed_symbols and local.symbol not in seen_symbols:
                # The exchange already closed this position (for example an
                # attached SL/TP fired). Recording the exit at entry price
                # would book PnL=0 and blind the daily-loss breaker, so use
                # the latest market price as the best available exit price.
                exit_price = self._latest_market_price(local.symbol)
                if exit_price <= 0:
                    exit_price = local.average_entry
                exit_record = self.runner.storage.close_position(
                    symbol=local.symbol,
                    reason=ExitReason.RISK_EXIT,
                    exit_price=exit_price,
                    closed_at=now,
                    notes="Local position closed because OKX reported no matching open SWAP position.",
                    pnl_multiplier=self._contract_pnl_multiplier(local.symbol, exit_price),
                )
                if exit_record is not None:
                    self._notify(
                        f"Position closed on exchange: {exit_record.symbol} "
                        f"estimated PnL={exit_record.realized_pnl:.6g}."
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
            self._record_close_fill(order, avg_price)
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
            margin_mode=getattr(self.runner.execution_engine, "margin_mode", None),
            leverage=getattr(self.runner.risk_guard, "leverage", None),
            opened_at=opened_at,
            updated_at=datetime.now(UTC),
        )

    def _record_close_fill(self, order: OrderRecord, avg_price: float) -> None:
        reason = _exit_reason_from_order_type(order.order_type)
        exchange_position = self._refresh_position_from_exchange(order.symbol)
        if exchange_position is not None and abs(exchange_position.quantity) > 0:
            self._notify(
                f"Close fill received for {order.symbol}, but OKX still reports "
                f"{exchange_position.margin_mode or '-'} position qty={exchange_position.quantity:.6g}. "
                "Keeping local position OPEN."
            )
            return

        exit_record = self.runner.storage.close_position(
            symbol=order.symbol,
            reason=reason,
            exit_price=avg_price,
            closed_at=datetime.now(UTC),
            notes=f"Closed by {reason.value} from OKX fill {order.order_id}.",
            pnl_multiplier=self._contract_pnl_multiplier(order.symbol, avg_price),
        )
        if exit_record is not None:
            self._notify(
                f"Position closed: {exit_record.reason.value} {exit_record.symbol} "
                f"PnL={exit_record.realized_pnl:.6g}."
            )

    def _refresh_position_from_exchange(self, symbol: str) -> PositionRecord | None:
        get_positions = getattr(self.runner.client, "get_positions", None)
        if not callable(get_positions):
            return self.runner.storage.load_position(symbol)
        try:
            from okx_ai_quant.account import parse_positions_response

            positions = parse_positions_response(get_positions(inst_type="SWAP", symbol=symbol))
        except Exception as exc:  # noqa: BLE001 - caller decides whether local fallback is acceptable.
            self._notify(f"Could not refresh OKX position for {symbol}: {exc}")
            return self.runner.storage.load_position(symbol)

        exchange_position = next((item for item in positions if item.symbol == symbol), None)
        if exchange_position is None:
            return None

        local = self.runner.storage.load_position(symbol)
        merged = self._merge_position_plan(exchange_position, local)
        self.runner.storage.upsert_position(
            symbol=merged.symbol,
            side=merged.side,
            quantity=merged.quantity,
            average_entry=merged.average_entry,
            opened_at=merged.opened_at,
            updated_at=merged.updated_at,
            entry_signal_id=merged.entry_signal_id,
            stop_loss=merged.stop_loss,
            take_profit=merged.take_profit,
            expires_at=merged.expires_at,
            margin_mode=merged.margin_mode,
            leverage=merged.leverage,
        )
        self._enforce_position_leverage(merged)
        return merged

    def _merge_position_plan(
        self,
        exchange_position: PositionRecord,
        local: PositionRecord | None,
    ) -> PositionRecord:
        if local is None:
            return exchange_position
        return replace(
            exchange_position,
            opened_at=local.opened_at,
            entry_signal_id=local.entry_signal_id,
            stop_loss=local.stop_loss,
            take_profit=local.take_profit,
            expires_at=local.expires_at,
        )

    def _enforce_position_leverage(self, position: PositionRecord) -> None:
        if position.leverage is None or position.leverage <= self.settings.MAX_LEVERAGE:
            return
        margin_mode = position.margin_mode
        if not margin_mode:
            return
        key = (position.symbol, margin_mode, self.settings.MAX_LEVERAGE)
        if key in self._blocked_leverage_updates:
            return
        set_leverage = getattr(self.runner.client, "set_leverage", None)
        if not callable(set_leverage):
            return
        try:
            set_leverage(
                position.symbol,
                leverage=self.settings.MAX_LEVERAGE,
                margin_mode=margin_mode,
            )
            self._notify(
                f"Reduced OKX leverage above cap: {position.symbol} {margin_mode} "
                f"{position.leverage or '-'}x -> {self.settings.MAX_LEVERAGE}x."
            )
        except Exception as exc:  # noqa: BLE001 - leverage correction should not stop exits.
            if "59103" in str(exc):
                self._blocked_leverage_updates.add(key)
            self._notify(
                f"Could not update OKX leverage for {position.symbol} "
                f"{margin_mode}: {exc}"
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
        timeout_hours = (
            getattr(self.runner.strategy, "recommended_timeout_hours", None)
            or self.settings.POSITION_TIMEOUT_HOURS
        )
        expires_at = datetime.now(UTC) + timedelta(hours=timeout_hours)
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

    def _trade_overview_context(self, report_date: date) -> dict[str, object]:
        report_day = report_date.isoformat()
        balance = self.runner.storage.load_balance("USDT")
        risk_state = self.runner._resolve_risk_state()
        open_positions = [
            self._position_overview(position)
            for position in self.runner.storage.load_open_positions()
        ]
        total_unrealized = sum(float(item["unrealized_pnl_usdt"]) for item in open_positions)
        closed_positions = [
            self._exit_overview(exit_record)
            for exit_record in self.runner.storage.load_position_exits()
            if exit_record.closed_at.date().isoformat() == report_day
        ]
        realized = sum(float(item["realized_pnl_usdt"]) for item in closed_positions)
        orders = self._order_counts(report_day)
        risk_points = self._risk_points(
            open_positions=open_positions,
            risk_state=risk_state,
            total_unrealized=total_unrealized,
        )
        return {
            "generated_at": datetime.now(self.tz).isoformat(timespec="seconds"),
            "report_date": report_day,
            "mode": str(self.settings.TRADING_MODE),
            "trading_enabled": self.settings.ENABLE_TRADING,
            "strategy": self.settings.STRATEGY_NAME,
            "account": {
                "equity_usdt": _fmt(balance.equity) if balance is not None else "-",
                "available_usdt": _fmt(balance.available) if balance is not None else "-",
            },
            "totals": {
                "open_position_count": len(open_positions),
                "unrealized_pnl_usdt": _fmt(total_unrealized),
                "realized_pnl_usdt": _fmt(realized),
                "drawdown": self._current_drawdown_text(),
                "halted": self.is_halted() or "",
                "risk_state_open_positions": risk_state.open_positions,
                "consecutive_losses": risk_state.consecutive_losses,
                "max_daily_loss": self.settings.MAX_DAILY_LOSS,
                "max_risk_per_trade": self.settings.MAX_RISK_PER_TRADE,
                "max_open_positions": self.settings.MAX_OPEN_POSITIONS,
                "max_leverage": self.settings.MAX_LEVERAGE,
                "margin_mode": str(self.settings.MARGIN_MODE),
            },
            "orders": orders,
            "open_positions": open_positions,
            "closed_positions": closed_positions,
            "risk_points": risk_points,
            "failure_review": self._failure_review(),
        }

    def _current_drawdown_text(self) -> str:
        equity = self._usdt_equity()
        hwm = _float(self.runner.storage.get_state("portfolio:hwm"))
        if equity <= 0 or hwm <= 0:
            return "-"
        drawdown = max(0.0, 1.0 - equity / max(hwm, equity))
        return f"{drawdown:.2%}（高水位 {hwm:.2f}，上限 {self.settings.MAX_DRAWDOWN:.0%}）"

    def _position_overview(self, position: PositionRecord) -> dict[str, object]:
        mark_price = self._latest_market_price(position.symbol)
        multiplier = self._contract_pnl_multiplier(position.symbol, mark_price)
        unrealized = 0.0
        if mark_price > 0:
            unrealized = (mark_price - position.average_entry) * position.quantity * multiplier
        return {
            "symbol": position.symbol,
            "side": position.side.value,
            "quantity": _fmt(position.quantity),
            "entry_price": _fmt(position.average_entry),
            "mark_price": _fmt(mark_price) if mark_price > 0 else "-",
            "unrealized_pnl_usdt": _fmt(unrealized),
            "stop_loss": _fmt(position.stop_loss) if position.stop_loss is not None else "-",
            "take_profit": _fmt(position.take_profit) if position.take_profit is not None else "-",
            "margin_mode": position.margin_mode or "-",
            "leverage": position.leverage if position.leverage is not None else "-",
            "state": position.state.value,
            "updated_at": position.updated_at.isoformat(),
        }

    def _exit_overview(self, exit_record) -> dict[str, object]:
        return {
            "symbol": exit_record.symbol,
            "side": exit_record.side.value,
            "reason": exit_record.reason.value,
            "quantity": _fmt(exit_record.quantity),
            "entry_price": _fmt(exit_record.entry_price),
            "exit_price": _fmt(exit_record.exit_price),
            "realized_pnl_usdt": _fmt(exit_record.realized_pnl),
            "closed_at": exit_record.closed_at.isoformat(),
        }

    def _order_counts(self, report_day: str) -> dict[str, int]:
        rows = self.runner.storage.connection.execute(
            """
            SELECT lower(state) AS state, COUNT(*) AS n
            FROM orders
            WHERE substr(created_at, 1, 10) = ?
            GROUP BY lower(state)
            """,
            (report_day,),
        ).fetchall()
        counts = {str(row["state"]): int(row["n"]) for row in rows}
        for state in ("filled", "canceled", "submitted", "created", "partially_filled", "failed"):
            counts.setdefault(state, 0)
        return counts

    def _risk_points(
        self,
        *,
        open_positions: list[dict[str, object]],
        risk_state,
        total_unrealized: float,
    ) -> list[str]:
        points: list[str] = []
        if risk_state.open_positions >= self.settings.MAX_OPEN_POSITIONS:
            points.append(
                f"当前风控 open_positions={risk_state.open_positions}，"
                f"已达到 MAX_OPEN_POSITIONS={self.settings.MAX_OPEN_POSITIONS}，会阻止继续新增仓位。"
            )
        if risk_state.consecutive_losses >= self.settings.MAX_CONSECUTIVE_LOSSES:
            points.append(
                f"连续失败/亏损计数 {risk_state.consecutive_losses} 已达到限制。"
            )
        if total_unrealized < 0:
            points.append(f"当前持仓估算浮亏 {abs(total_unrealized):.2f} USDT。")
        for item in open_positions:
            side = str(item["side"])
            mark = _float(item["mark_price"])
            stop = _float(item["stop_loss"])
            symbol = str(item["symbol"])
            if mark <= 0 or stop <= 0:
                continue
            if side == "LONG" and mark <= stop * 1.01:
                points.append(f"{symbol} LONG 接近/触及止损：mark={mark:g}, stop={stop:g}。")
            if side == "SHORT" and mark >= stop * 0.99:
                points.append(f"{symbol} SHORT 接近/触及止损：mark={mark:g}, stop={stop:g}。")
        return points

    def _failure_review(self) -> dict[str, object]:
        log_dir = self.settings.DB_PATH.parent
        return summarize_recent_failures(
            [
                log_dir / "okxbot.log",
                log_dir / "okxbot-telegram.log",
            ]
        )

    def _generate_trade_overview_summary(
        self,
        context: dict[str, object],
        fallback: str,
    ) -> str:
        llm_client = getattr(self.runner.analysis_generator, "llm_client", None)
        generator = getattr(llm_client, "generate_trade_overview", None)
        if not callable(generator):
            return fallback
        try:
            return generator(context=context, fallback_chinese=fallback)
        except Exception as exc:  # noqa: BLE001 - reports must still be delivered without LLM.
            return f"{fallback}\n\n大模型总结暂时不可用：{exc}"

    def _contract_pnl_multiplier(self, symbol: str, mark_price: float) -> float:
        get_instruments = getattr(self.runner.client, "get_instruments", None)
        if not callable(get_instruments):
            return 1.0
        try:
            rows = get_instruments("SWAP")
        except Exception:
            return 1.0
        base_ccy = symbol.split("-", maxsplit=1)[0].upper()
        for row in rows:
            if not isinstance(row, dict) or row.get("instId") != symbol:
                continue
            ct_val = _float(row.get("ctVal"))
            ct_val_ccy = str(row.get("ctValCcy") or "").upper()
            if ct_val <= 0:
                return 1.0
            if ct_val_ccy == base_ccy:
                return ct_val
            if ct_val_ccy in {"USDT", "USD", "USDC"} and mark_price > 0:
                return ct_val / mark_price
            return 1.0
        return 1.0

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

    def _okx_entry_health_block_reason(self) -> str | None:
        if not self.settings.ENABLE_TRADING or not self.settings.OKX_ENTRY_HEALTHCHECK_ENABLED:
            return None
        check_connectivity = getattr(self.runner.client, "check_connectivity", None)
        if not callable(check_connectivity):
            return None
        ok, details = check_connectivity(
            attempts=self.settings.OKX_ENTRY_HEALTHCHECK_ATTEMPTS,
            required_successes=self.settings.OKX_ENTRY_HEALTHCHECK_MIN_SUCCESSES,
            symbol=self.settings.symbols[0] if self.settings.symbols else "BTC-USDT-SWAP",
        )
        if ok:
            return None
        return details

    def _send_report(self, message: str) -> None:
        try:
            self.notifier.send(message)
        except NotificationError as exc:
            print(f"Report notification failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - report delivery must not stop the bot.
            print(f"Report notification failed unexpectedly: {exc}")

    def _notify(self, message: str) -> None:
        if self.settings.NOTIFIER.strip().lower() == "telegram":
            print(_timestamped_log(message))
            return
        try:
            self.notifier.send(message)
        except NotificationError as exc:
            print(_timestamped_log(f"Notification failed: {exc}"))
        except Exception as exc:  # noqa: BLE001 - notifications must never stop trading logic.
            print(_timestamped_log(f"Notification failed unexpectedly: {exc}"))


def _fmt(value: object, digits: int = 2) -> str:
    number = _float(value)
    if abs(number) >= 1000:
        return f"{number:.2f}"
    if abs(number) >= 1:
        return f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{number:.6f}".rstrip("0").rstrip(".") or "0"


def _timestamped_log(message: str) -> str:
    return f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}"


def _map_okx_order_state(value: object) -> OrderState | None:
    raw = str(value or "").lower()
    if raw in {"live", "partially_filled"}:
        return OrderState.PARTIALLY_FILLED
    if raw == "filled":
        return OrderState.FILLED
    if raw in {"canceled", "cancelled"}:
        return OrderState.CANCELED
    return None


_REPORT_SLOT_GRACE = timedelta(minutes=45)


def _due_report_slots(now: datetime, report_times: list[str]) -> list[str]:
    """Return slots due right now, within a grace window after their time.

    The grace window keeps a freshly (re)started bot from blasting every
    already-passed slot of the day in one go; a slot missed by more than the
    grace period is skipped instead of delivered hours late.
    """
    due_slots: list[str] = []
    local_time = now.replace(tzinfo=None)
    for report_time in report_times:
        scheduled = _parse_report_time(report_time)
        if scheduled is None:
            continue
        scheduled_at = local_time.replace(
            hour=scheduled.hour, minute=scheduled.minute, second=0, microsecond=0
        )
        if scheduled_at <= local_time < scheduled_at + _REPORT_SLOT_GRACE:
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
