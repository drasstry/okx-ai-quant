"""Historical backtest that replays the live bot semantics.

The engine reuses the production strategy classes and ``RiskGuard`` so a
backtest exercises the same code path the bot trades with:

- signals are generated from confirmed candles only;
- stop-loss / take-profit fill intrabar (exchange-side attached orders),
  with stop priority when both trigger inside one candle;
- position timeout, reverse-signal exits and the daily-loss flatten mirror
  ``TradingBot`` behaviour;
- the daily-loss and consecutive-loss breakers reset per UTC day;
- one position per symbol, ``MAX_OPEN_POSITIONS`` across symbols;
- taker fees, slippage and funding are charged explicitly.

Known simplifications (stated in the report): fills assume the order
executes at the signal candle close (the live bot submits a market order
within one poll interval of the candle close); contract lot rounding is
ignored; margin is not modelled (1x notional accounting).
"""

from __future__ import annotations

import csv
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from okx_ai_quant.models import Candle, RiskStatus, SignalDirection
from okx_ai_quant.portfolio import ExposureLimits, evaluate_drawdown, exposure_block_reason
from okx_ai_quant.risk import RiskGuard, RiskState
from okx_ai_quant.strategy import create_strategy

OKX_BASE_URL = "https://www.okx.com"
CANDLE_LIMIT = 100  # live MARKET_CANDLE_LIMIT default: strategies see 100 candles.


@dataclass(frozen=True, kw_only=True)
class BacktestConfig:
    symbols: list[str]
    initial_capital_usdt: float = 80_000.0
    max_risk_per_trade: float = 0.01
    max_daily_loss: float = 0.02
    max_consecutive_losses: int = 3
    max_open_positions: int = 5
    fee_rate_per_side: float = 0.0005
    slippage_rate: float = 0.0005
    min_expected_move: float = 0.006
    position_timeout_hours: int = 72
    exit_on_reverse_signal: bool = True
    max_drawdown: float = 0.10
    max_total_exposure_rate: float = 0.40
    max_net_exposure_rate: float = 0.25
    loss_streak_days: int = 2
    loss_streak_risk_multiplier: float = 0.5

    def strategy_min_expected_move(self) -> float:
        # Mirrors cli.build_runner: max(MIN_EXPECTED_MOVE, round-trip cost).
        return max(self.min_expected_move, self.fee_rate_per_side * 2 + self.slippage_rate)


@dataclass(frozen=True, kw_only=True)
class SymbolHistory:
    one_hour: list[Candle]
    four_hour: list[Candle]
    funding: list[tuple[datetime, float]] = field(default_factory=list)
    daily: list[Candle] = field(default_factory=list)


@dataclass(kw_only=True)
class BacktestTrade:
    symbol: str
    direction: SignalDirection
    opened_at: datetime
    entry_price: float
    quantity: float
    notional_usdt: float
    stop_loss: float | None
    take_profit: float | None
    closed_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    price_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.price_pnl - self.fees + self.funding

    @property
    def signed(self) -> float:
        return 1.0 if self.direction == SignalDirection.LONG else -1.0


@dataclass(frozen=True, kw_only=True)
class StrategyResult:
    strategy: str
    trades: list[BacktestTrade]
    equity_curve: list[tuple[datetime, float]]
    initial_capital: float
    halted_at: datetime | None = None

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.initial_capital

    @property
    def total_return(self) -> float:
        return self.final_equity / self.initial_capital - 1.0

    @property
    def max_drawdown(self) -> float:
        peak = self.initial_capital
        worst = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                worst = min(worst, equity / peak - 1.0)
        return worst

    def window_stats(self, *, since: datetime) -> dict[str, object]:
        """Net/gross summary of trades that closed on or after ``since``.

        Used for the out-of-sample split: an edge that only exists before the
        cutoff is overfitting, not signal.
        """
        window = [
            trade
            for trade in self.trades
            if trade.closed_at is not None and trade.closed_at >= since
        ]
        wins = [trade for trade in window if trade.net_pnl > 0]
        net = sum(trade.net_pnl for trade in window)
        gross = sum(trade.price_pnl for trade in window)
        return {
            "trades": len(window),
            "net_pnl": net,
            "gross_pnl": gross,
            "net_return": net / self.initial_capital if self.initial_capital else 0.0,
            "gross_return": gross / self.initial_capital if self.initial_capital else 0.0,
            "win_rate": (len(wins) / len(window)) if window else 0.0,
        }

    def stats(self) -> dict[str, object]:
        closed = [trade for trade in self.trades if trade.closed_at is not None]
        wins = [trade for trade in closed if trade.net_pnl > 0]
        losses = [trade for trade in closed if trade.net_pnl <= 0]
        gross_win = sum(trade.net_pnl for trade in wins)
        gross_loss = -sum(trade.net_pnl for trade in losses)
        reasons: dict[str, int] = {}
        for trade in closed:
            reasons[trade.exit_reason or "-"] = reasons.get(trade.exit_reason or "-", 0) + 1
        total_fees = sum(trade.fees for trade in self.trades)
        total_funding = sum(trade.funding for trade in self.trades)
        net_pnl = self.final_equity - self.initial_capital
        # Gross = net stripped of costs, so a "profitable before fees but
        # killed by fees" strategy is visible at a glance.
        gross_pnl = net_pnl + total_fees - total_funding
        traded_notional = sum(trade.notional_usdt for trade in self.trades)
        return {
            "strategy": self.strategy,
            "final_equity": self.final_equity,
            "total_return": self.total_return,
            "gross_pnl": gross_pnl,
            "gross_return": gross_pnl / self.initial_capital if self.initial_capital else 0.0,
            "net_pnl": net_pnl,
            "cost_drag": total_fees / self.initial_capital if self.initial_capital else 0.0,
            "turnover": traded_notional / self.initial_capital if self.initial_capital else 0.0,
            "max_drawdown": self.max_drawdown,
            "trades": len(closed),
            "open_at_end": len(self.trades) - len(closed),
            "win_rate": (len(wins) / len(closed)) if closed else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
            "total_fees": total_fees,
            "total_funding": total_funding,
            "exit_reasons": reasons,
            "halted_at": self.halted_at,
        }


class BacktestEngine:
    """Replay one strategy over shared symbol history."""

    def __init__(
        self,
        *,
        strategy_name: str,
        config: BacktestConfig,
        data: dict[str, SymbolHistory],
        strategy: object | None = None,
        trade_start: datetime | None = None,
    ) -> None:
        self.config = config
        self.strategy_name = strategy_name
        self.trade_start = trade_start
        self.strategy = strategy or create_strategy(
            strategy_name,
            min_expected_move=config.strategy_min_expected_move(),
        )
        self.data = {symbol: history for symbol, history in data.items() if history.one_hour}
        self.risk_guard = RiskGuard(
            symbols=list(self.data),
            reference_capital_usdt=config.initial_capital_usdt,
            max_risk_per_trade=config.max_risk_per_trade,
            max_daily_loss=config.max_daily_loss,
            max_consecutive_losses=config.max_consecutive_losses,
            max_positions=config.max_open_positions,
            leverage=1,
            loss_streak_days=config.loss_streak_days,
            loss_streak_risk_multiplier=config.loss_streak_risk_multiplier,
        )
        self.exposure_limits = ExposureLimits(
            max_total_rate=config.max_total_exposure_rate,
            max_net_rate=config.max_net_exposure_rate,
        )
        self.high_water_mark = config.initial_capital_usdt
        self.halted_at: datetime | None = None

        self.open_positions: dict[str, BacktestTrade] = {}
        self._closed_this_bar: set[str] = set()
        self.trades: list[BacktestTrade] = []
        self.realized_cash = 0.0  # net of fees and funding
        self.daily_price_pnl: dict[str, float] = {}  # mirrors storage realized_pnl
        self.daily_exit_streak: dict[str, list[float]] = {}
        self.equity_curve: list[tuple[datetime, float]] = []

    # ------------------------------------------------------------------ run
    def run(self) -> StrategyResult:
        timeline = sorted(
            {candle.timestamp for history in self.data.values() for candle in history.one_hour}
        )
        cursors: dict[str, int] = {symbol: -1 for symbol in self.data}
        four_cursors: dict[str, int] = {symbol: 0 for symbol in self.data}
        daily_cursors: dict[str, int] = {symbol: 0 for symbol in self.data}
        funding_cursors: dict[str, int] = {symbol: 0 for symbol in self.data}
        self._wants_daily = bool(getattr(self.strategy, "requires_daily", False))
        previous_close_time: datetime | None = None

        for ts in timeline:
            close_time = ts + timedelta(hours=1)
            current: dict[str, Candle] = {}
            for symbol, history in self.data.items():
                index = cursors[symbol] + 1
                candles = history.one_hour
                while index < len(candles) and candles[index].timestamp <= ts:
                    cursors[symbol] = index
                    index += 1
                pointer = cursors[symbol]
                if pointer >= 0 and candles[pointer].timestamp == ts:
                    current[symbol] = candles[pointer]

            if self.trade_start is not None and close_time < self.trade_start:
                previous_close_time = close_time
                continue  # warmup: indicators only, no trading

            self._closed_this_bar.clear()
            self._apply_funding(current, previous_close_time, close_time, funding_cursors)
            self._intrabar_exits(current, close_time)
            self._timeout_exits(current, close_time)
            self._daily_loss_flatten(current, close_time)
            self._drawdown_check(current, close_time)

            signals = self._generate_signals(
                current, cursors, four_cursors, daily_cursors, close_time
            )
            if self.config.exit_on_reverse_signal:
                self._reverse_signal_exits(current, signals, close_time)
            self._entries(current, signals, close_time)

            self._mark_equity(current, close_time)
            previous_close_time = close_time

        return StrategyResult(
            strategy=self.strategy_name,
            trades=self.trades,
            equity_curve=self.equity_curve,
            initial_capital=self.config.initial_capital_usdt,
            halted_at=self.halted_at,
        )

    # ------------------------------------------------------------- mechanics
    def _apply_funding(
        self,
        current: dict[str, Candle],
        previous_close: datetime | None,
        close_time: datetime,
        funding_cursors: dict[str, int],
    ) -> None:
        for symbol, trade in self.open_positions.items():
            history = self.data[symbol]
            cursor = funding_cursors[symbol]
            events = history.funding
            while cursor < len(events) and events[cursor][0] <= close_time:
                event_time, rate = events[cursor]
                applies = (previous_close is None or event_time > previous_close) and (
                    trade.opened_at < event_time
                )
                if applies:
                    mark = current[symbol].close if symbol in current else trade.entry_price
                    # Positive funding: longs pay shorts.
                    delta = -rate * trade.signed * trade.quantity * mark
                    trade.funding += delta
                    self.realized_cash += delta
                cursor += 1
            funding_cursors[symbol] = cursor

    def _intrabar_exits(self, current: dict[str, Candle], close_time: datetime) -> None:
        for symbol in list(self.open_positions):
            candle = current.get(symbol)
            if candle is None:
                continue
            trade = self.open_positions[symbol]
            stop = trade.stop_loss
            target = trade.take_profit
            if trade.direction == SignalDirection.LONG:
                stop_hit = stop is not None and candle.low <= stop
                target_hit = target is not None and candle.high >= target
            else:
                stop_hit = stop is not None and candle.high >= stop
                target_hit = target is not None and candle.low <= target
            if stop_hit:  # conservative: stop wins when both trigger in one candle
                self._close(trade, price=float(stop), at=close_time, reason="STOP_LOSS")
            elif target_hit:
                self._close(trade, price=float(target), at=close_time, reason="TAKE_PROFIT")

    def _timeout_exits(self, current: dict[str, Candle], close_time: datetime) -> None:
        timeout = timedelta(hours=self.config.position_timeout_hours)
        for symbol in list(self.open_positions):
            candle = current.get(symbol)
            trade = self.open_positions[symbol]
            if candle is None or close_time < trade.opened_at + timeout:
                continue
            self._close(trade, price=candle.close, at=close_time, reason="TIMEOUT")

    def _daily_loss_flatten(self, current: dict[str, Candle], close_time: datetime) -> None:
        if not self._daily_loss_tripped(close_time):
            return
        for symbol in list(self.open_positions):
            candle = current.get(symbol)
            if candle is None:
                continue
            self._close(
                self.open_positions[symbol],
                price=candle.close,
                at=close_time,
                reason="RISK_EXIT",
            )

    def _drawdown_check(self, current: dict[str, Candle], close_time: datetime) -> None:
        """Mirror the live drawdown kill switch: flatten and halt for good.

        The live bot requires a human /resume; within a backtest run that
        means trading never restarts after the trip.
        """
        if self.halted_at is not None:
            return
        equity = self._current_equity(current)
        status = evaluate_drawdown(
            equity=equity,
            high_water_mark=self.high_water_mark,
            max_drawdown=self.config.max_drawdown,
        )
        self.high_water_mark = status.high_water_mark
        if not status.tripped:
            return
        self.halted_at = close_time
        for symbol in list(self.open_positions):
            candle = current.get(symbol)
            if candle is None:
                continue
            self._close(
                self.open_positions[symbol],
                price=candle.close,
                at=close_time,
                reason="DRAWDOWN_HALT",
            )

    def _current_equity(self, current: dict[str, Candle]) -> float:
        unrealized = 0.0
        for symbol, trade in self.open_positions.items():
            mark = current[symbol].close if symbol in current else trade.entry_price
            unrealized += (mark - trade.entry_price) * trade.quantity * trade.signed
        return self.config.initial_capital_usdt + self.realized_cash + unrealized

    def _consecutive_losing_days(self, close_time: datetime) -> int:
        streak = 0
        expected = close_time.date()
        while True:
            expected = expected - timedelta(days=1)
            pnl = self.daily_price_pnl.get(expected.isoformat())
            if pnl is None or pnl >= 0:
                break
            streak += 1
        return streak

    def _generate_signals(
        self,
        current: dict[str, Candle],
        cursors: dict[str, int],
        four_cursors: dict[str, int],
        daily_cursors: dict[str, int],
        close_time: datetime,
    ) -> dict[str, object]:
        one_hour_windows: dict[str, list[Candle]] = {}
        four_hour_windows: dict[str, list[Candle]] = {}
        daily_windows: dict[str, list[Candle]] = {}
        for symbol in current:
            history = self.data[symbol]
            pointer = cursors[symbol]
            one_hour_windows[symbol] = history.one_hour[max(0, pointer + 1 - CANDLE_LIMIT) : pointer + 1]
            cursor = four_cursors[symbol]
            candles = history.four_hour
            while (
                cursor < len(candles)
                and candles[cursor].timestamp + timedelta(hours=4) <= close_time
            ):
                cursor += 1
            four_cursors[symbol] = cursor
            four_hour_windows[symbol] = candles[max(0, cursor - CANDLE_LIMIT) : cursor]

            if self._wants_daily:
                dcursor = daily_cursors[symbol]
                dcandles = history.daily
                # Only expose daily bars that have fully closed by close_time.
                while (
                    dcursor < len(dcandles)
                    and dcandles[dcursor].timestamp + timedelta(days=1) <= close_time
                ):
                    dcursor += 1
                daily_cursors[symbol] = dcursor
                daily_windows[symbol] = dcandles[max(0, dcursor - CANDLE_LIMIT) : dcursor]

        prepare = getattr(self.strategy, "prepare_universe", None)
        if callable(prepare):
            funding_rates = {
                symbol: self._latest_funding_rate(symbol, close_time) for symbol in current
            }
            prepare(
                symbols=list(current),
                one_hour=one_hour_windows,
                four_hour=four_hour_windows,
                funding_rates=funding_rates,
                daily=daily_windows,
            )

        signals: dict[str, object] = {}
        entries_possible = not self._entries_blocked(close_time)
        for symbol in current:
            has_position = symbol in self.open_positions
            needs_signal = (has_position and self.config.exit_on_reverse_signal) or (
                not has_position and entries_possible
            )
            if not needs_signal:
                continue
            signals[symbol] = self.strategy.generate(
                symbol,
                one_hour_windows[symbol],
                four_hour_windows[symbol],
            )
        return signals

    def _reverse_signal_exits(
        self,
        current: dict[str, Candle],
        signals: dict[str, object],
        close_time: datetime,
    ) -> None:
        for symbol in list(self.open_positions):
            signal = signals.get(symbol)
            candle = current.get(symbol)
            if signal is None or candle is None:
                continue
            trade = self.open_positions[symbol]
            opposite = (
                trade.direction == SignalDirection.LONG
                and signal.direction == SignalDirection.SHORT
            ) or (
                trade.direction == SignalDirection.SHORT
                and signal.direction == SignalDirection.LONG
            )
            if opposite:
                self._close(trade, price=candle.close, at=close_time, reason="REVERSE_SIGNAL")

    def _entries(
        self,
        current: dict[str, Candle],
        signals: dict[str, object],
        close_time: datetime,
    ) -> None:
        if self._entries_blocked(close_time):
            return
        for symbol, signal in signals.items():
            if symbol in self.open_positions or symbol in self._closed_this_bar:
                # Mirrors the live bot: a close submitted this cycle keeps the
                # position row open until the fill is recorded, so re-entry
                # happens no earlier than the next candle.
                continue
            if signal.direction not in {SignalDirection.LONG, SignalDirection.SHORT}:
                continue
            equity = self._current_equity(current)
            state = RiskState(
                daily_loss_rate=self._daily_loss_rate(close_time),
                consecutive_losses=self._consecutive_losses(close_time),
                open_positions=len(self.open_positions),
                equity_usdt=equity,
                consecutive_losing_days=self._consecutive_losing_days(close_time),
            )
            decision = self.risk_guard.evaluate(signal, state)
            if decision.status != RiskStatus.APPROVED:
                continue
            long_notional, short_notional = self._open_notional_by_side(current)
            if exposure_block_reason(
                equity=equity,
                long_notional=long_notional,
                short_notional=short_notional,
                candidate_notional=decision.position_size_usdt,
                candidate_direction=signal.direction,
                limits=self.exposure_limits,
            ):
                continue
            candle = current[symbol]
            slip = self.config.slippage_rate
            direction = signal.direction
            fill = candle.close * (1 + slip) if direction == SignalDirection.LONG else candle.close * (1 - slip)
            notional = decision.position_size_usdt
            quantity = notional / fill
            fee = notional * self.config.fee_rate_per_side
            trade = BacktestTrade(
                symbol=symbol,
                direction=direction,
                opened_at=close_time,
                entry_price=fill,
                quantity=quantity,
                notional_usdt=notional,
                stop_loss=getattr(signal, "stop_price", None),
                take_profit=getattr(signal, "target_price", None),
                fees=fee,
            )
            self.realized_cash -= fee
            self.open_positions[symbol] = trade
            self.trades.append(trade)

    def _close(self, trade: BacktestTrade, *, price: float, at: datetime, reason: str) -> None:
        slip = self.config.slippage_rate
        fill = price * (1 - slip) if trade.direction == SignalDirection.LONG else price * (1 + slip)
        price_pnl = (fill - trade.entry_price) * trade.quantity * trade.signed
        exit_fee = abs(fill * trade.quantity) * self.config.fee_rate_per_side
        trade.closed_at = at
        trade.exit_price = fill
        trade.exit_reason = reason
        trade.price_pnl = price_pnl
        trade.fees += exit_fee
        self.realized_cash += price_pnl - exit_fee
        day = at.date().isoformat()
        self.daily_price_pnl[day] = self.daily_price_pnl.get(day, 0.0) + price_pnl
        self.daily_exit_streak.setdefault(day, []).append(trade.net_pnl)
        self._closed_this_bar.add(trade.symbol)
        del self.open_positions[trade.symbol]

    # -------------------------------------------------------------- breakers
    def _daily_loss_rate(self, close_time: datetime) -> float:
        day = close_time.date().isoformat()
        pnl = self.daily_price_pnl.get(day, 0.0)
        return max(0.0, -pnl) / self.config.initial_capital_usdt

    def _daily_loss_tripped(self, close_time: datetime) -> bool:
        return self._daily_loss_rate(close_time) >= self.config.max_daily_loss

    def _consecutive_losses(self, close_time: datetime) -> int:
        day = close_time.date().isoformat()
        streak = 0
        for pnl in reversed(self.daily_exit_streak.get(day, [])):
            if pnl >= 0:
                break
            streak += 1
        return streak

    def _entries_blocked(self, close_time: datetime) -> bool:
        return (
            self.halted_at is not None
            or self._daily_loss_tripped(close_time)
            or self._consecutive_losses(close_time) >= self.config.max_consecutive_losses
            or len(self.open_positions) >= self.config.max_open_positions
        )

    def _open_notional_by_side(self, current: dict[str, Candle]) -> tuple[float, float]:
        long_notional = 0.0
        short_notional = 0.0
        for symbol, trade in self.open_positions.items():
            mark = current[symbol].close if symbol in current else trade.entry_price
            notional = trade.quantity * mark
            if trade.direction == SignalDirection.LONG:
                long_notional += notional
            else:
                short_notional += notional
        return long_notional, short_notional

    def _latest_funding_rate(self, symbol: str, close_time: datetime) -> float:
        rate = 0.0
        for event_time, event_rate in self.data[symbol].funding:
            if event_time > close_time:
                break
            rate = event_rate
        return rate

    def _mark_equity(self, current: dict[str, Candle], close_time: datetime) -> None:
        unrealized = 0.0
        for symbol, trade in self.open_positions.items():
            mark = current[symbol].close if symbol in current else trade.entry_price
            unrealized += (mark - trade.entry_price) * trade.quantity * trade.signed
        self.equity_curve.append(
            (close_time, self.config.initial_capital_usdt + self.realized_cash + unrealized)
        )


# --------------------------------------------------------------- data access
def fetch_okx_candles(
    symbol: str,
    bar: str,
    *,
    start: datetime,
    end: datetime,
    cache_dir: Path,
    request_pause_seconds: float = 0.12,
    offline: bool = False,
) -> list[Candle]:
    """Fetch confirmed OKX candles for [start, end], caching to CSV."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{symbol}_{bar}.csv"
    cached = _read_candle_cache(cache_path)

    if not offline:
        have_oldest = min(cached) if cached else None
        have_newest = max(cached) if cached else None
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        if have_oldest is None or have_oldest > start_ms:
            _fetch_okx_range(
                symbol,
                bar,
                start_ms=start_ms,
                end_ms=have_oldest if have_oldest is not None else end_ms,
                sink=cached,
                pause=request_pause_seconds,
            )
        if have_newest is not None and have_newest < end_ms:
            _fetch_okx_range(
                symbol,
                bar,
                start_ms=have_newest,
                end_ms=end_ms,
                sink=cached,
                pause=request_pause_seconds,
            )
        _write_candle_cache(cache_path, cached)

    candles = [
        Candle(
            symbol=symbol,
            timeframe=bar,
            timestamp=datetime.fromtimestamp(ts / 1000, tz=UTC),
            open=row[0],
            high=row[1],
            low=row[2],
            close=row[3],
            volume=row[4],
        )
        for ts, row in sorted(cached.items())
        if start <= datetime.fromtimestamp(ts / 1000, tz=UTC) <= end
    ]
    return candles


def _fetch_okx_range(
    symbol: str,
    bar: str,
    *,
    start_ms: int,
    end_ms: int,
    sink: dict[int, tuple[float, float, float, float, float]],
    pause: float,
) -> None:
    after = end_ms + 1
    while after > start_ms:
        rows = _okx_get(
            "/api/v5/market/history-candles",
            {"instId": symbol, "bar": bar, "limit": "100", "after": str(after)},
        )
        if not rows:
            break
        oldest = after
        for row in rows:
            ts = int(row[0])
            oldest = min(oldest, ts)
            if len(row) >= 9 and str(row[8]) == "0":
                continue  # skip the unconfirmed candle, same as live
            if ts < start_ms or ts > end_ms:
                continue
            sink[ts] = (
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
            )
        if oldest >= after:
            break
        after = oldest
        time.sleep(pause)


def fetch_okx_funding(
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    cache_dir: Path,
    request_pause_seconds: float = 0.25,
    offline: bool = False,
) -> list[tuple[datetime, float]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{symbol}_funding.csv"
    cached: dict[int, float] = {}
    if cache_path.exists():
        with cache_path.open() as handle:
            for row in csv.reader(handle):
                cached[int(row[0])] = float(row[1])

    if not offline:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        have_oldest = min(cached) if cached else None
        after = end_ms + 1 if have_oldest is None or have_oldest > start_ms else start_ms
        while after > start_ms:
            rows = _okx_get(
                "/api/v5/public/funding-rate-history",
                {"instId": symbol, "limit": "100", "after": str(after)},
            )
            if not rows:
                break
            oldest = after
            for row in rows:
                ts = int(row["fundingTime"])
                oldest = min(oldest, ts)
                if start_ms <= ts <= end_ms:
                    cached[ts] = float(row["fundingRate"])
            if oldest >= after:
                break
            after = oldest
            time.sleep(request_pause_seconds)
        with cache_path.open("w", newline="") as handle:
            writer = csv.writer(handle)
            for ts in sorted(cached):
                writer.writerow([ts, cached[ts]])

    return [
        (datetime.fromtimestamp(ts / 1000, tz=UTC), rate)
        for ts, rate in sorted(cached.items())
        if start <= datetime.fromtimestamp(ts / 1000, tz=UTC) <= end
    ]


def _okx_get(path: str, params: dict[str, str]) -> list:
    url = f"{OKX_BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "okx-ai-quant-backtest"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("code") != "0":
                raise RuntimeError(f"OKX API error {payload.get('code')}: {payload.get('msg')}")
            return payload.get("data") or []
        except Exception as exc:  # noqa: BLE001 - network boundary with retries.
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(
        f"OKX request failed after retries ({url}): {last_error}. "
        "If this host blocks okx.com, run the backtest from a machine that can "
        "reach the OKX public API, or pass --offline with a pre-filled cache."
    )


def _read_candle_cache(path: Path) -> dict[int, tuple[float, float, float, float, float]]:
    cached: dict[int, tuple[float, float, float, float, float]] = {}
    if path.exists():
        with path.open() as handle:
            for row in csv.reader(handle):
                cached[int(row[0])] = (
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                    float(row[5]),
                )
    return cached


def _write_candle_cache(path: Path, cached: dict[int, tuple[float, float, float, float, float]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        for ts in sorted(cached):
            writer.writerow([ts, *cached[ts]])


def run_backtest_command(args) -> int:
    """Entry point for the ``backtest`` CLI subcommand."""
    from okx_ai_quant.backtest_report import render_backtest_html, render_backtest_summary
    from okx_ai_quant.config import Settings
    from okx_ai_quant.strategy import AVAILABLE_STRATEGIES

    settings = Settings()
    symbols = (
        [item.strip() for item in args.symbols.split(",") if item.strip()]
        if args.symbols
        else settings.symbols
    )
    if args.strategies.strip().lower() in {"", "all"}:
        strategy_names = list(AVAILABLE_STRATEGIES)
    else:
        strategy_names = [item.strip() for item in args.strategies.split(",") if item.strip()]

    wants_daily = any(
        getattr(create_strategy(name, min_expected_move=0.006), "requires_daily", False)
        for name in strategy_names
    )

    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    config = BacktestConfig(
        symbols=symbols,
        initial_capital_usdt=args.capital,
        fee_rate_per_side=args.fee,
        slippage_rate=args.slippage,
        max_risk_per_trade=settings.MAX_RISK_PER_TRADE,
        max_daily_loss=settings.MAX_DAILY_LOSS,
        max_consecutive_losses=settings.MAX_CONSECUTIVE_LOSSES,
        max_open_positions=settings.MAX_OPEN_POSITIONS,
        min_expected_move=settings.MIN_EXPECTED_MOVE,
        position_timeout_hours=settings.POSITION_TIMEOUT_HOURS,
        exit_on_reverse_signal=settings.EXIT_ON_REVERSE_SIGNAL,
        max_drawdown=settings.MAX_DRAWDOWN,
        max_total_exposure_rate=settings.MAX_TOTAL_EXPOSURE_RATE,
        max_net_exposure_rate=settings.MAX_NET_EXPOSURE_RATE,
        loss_streak_days=settings.LOSS_STREAK_DAYS,
        loss_streak_risk_multiplier=settings.LOSS_STREAK_RISK_MULTIPLIER,
    )

    print(
        f"Backtest window: {start.date().isoformat()} -> {end.date().isoformat()} | "
        f"{len(symbols)} symbols | capital {config.initial_capital_usdt:,.0f} USDT"
    )
    data = load_history(
        symbols,
        start=start,
        end=end,
        cache_dir=Path(args.data_dir),
        offline=args.offline,
        with_daily=wants_daily,
    )
    if not data:
        print("No historical data available; nothing to backtest.")
        return 1

    results: list[StrategyResult] = []
    for name in strategy_names:
        print(f"running {name} ...", flush=True)
        engine = BacktestEngine(
            strategy_name=name,
            config=config,
            data=data,
            trade_start=start,
        )
        results.append(engine.run())

    oos_start = end - timedelta(days=args.oos_days) if args.oos_days > 0 else None
    print()
    print(render_backtest_summary(results))
    if oos_start is not None:
        from okx_ai_quant.backtest_report import render_oos_summary

        print()
        print(render_oos_summary(results, oos_start=oos_start))
    out_path = Path(args.out)
    out_path.write_text(
        render_backtest_html(results, config=config, start=start, end=end, oos_start=oos_start),
        encoding="utf-8",
    )
    print(f"\nHTML report written to {out_path}")
    return 0


def load_history(
    symbols: Iterable[str],
    *,
    start: datetime,
    end: datetime,
    cache_dir: Path,
    offline: bool = False,
    with_funding: bool = True,
    with_daily: bool = False,
    progress: bool = True,
) -> dict[str, SymbolHistory]:
    warmup = timedelta(hours=CANDLE_LIMIT * 4 + 8)
    # Daily strategies need many daily bars; warm up far enough back for a
    # 50-day EMA plus margin.
    daily_warmup = timedelta(days=CANDLE_LIMIT + 60)
    data: dict[str, SymbolHistory] = {}
    for symbol in symbols:
        if progress:
            print(f"loading {symbol} ...", flush=True)
        one_hour = fetch_okx_candles(
            symbol, "1H", start=start - warmup, end=end, cache_dir=cache_dir, offline=offline
        )
        four_hour = fetch_okx_candles(
            symbol, "4H", start=start - warmup, end=end, cache_dir=cache_dir, offline=offline
        )
        daily: list[Candle] = []
        if with_daily:
            daily = fetch_okx_candles(
                symbol, "1D", start=start - daily_warmup, end=end, cache_dir=cache_dir, offline=offline
            )
        funding: list[tuple[datetime, float]] = []
        if with_funding:
            try:
                funding = fetch_okx_funding(
                    symbol, start=start - warmup, end=end, cache_dir=cache_dir, offline=offline
                )
            except RuntimeError as exc:
                print(f"warning: funding history unavailable for {symbol}: {exc}")
        if one_hour:
            data[symbol] = SymbolHistory(
                one_hour=one_hour, four_hour=four_hour, funding=funding, daily=daily
            )
        elif progress:
            print(f"warning: no candles for {symbol}; skipping")
    return data


