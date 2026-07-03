from dataclasses import dataclass
from typing import Callable

from okx_ai_quant.analysis import TradeAnalysisGenerator
from okx_ai_quant.execution import ExecutionDecision, ExecutionEngine
from okx_ai_quant.journal import Journal
from okx_ai_quant.market_data import normalize_candles
from okx_ai_quant.models import OrderRecord, OrderState, RiskStatus, Signal, TradeAnalysis
from okx_ai_quant.risk import RiskDecision, RiskState


RiskStateBuilder = Callable[[object], RiskState]


@dataclass(frozen=True, kw_only=True)
class RunOnceResult:
    signal: Signal
    risk_decision: RiskDecision
    analysis: TradeAnalysis
    order: OrderRecord | None


class Runner:
    def __init__(
        self,
        *,
        client,
        storage,
        strategy,
        risk_guard,
        execution_engine: ExecutionEngine | None = None,
        analysis_generator: TradeAnalysisGenerator | None = None,
        risk_state: RiskState | None = None,
        risk_state_builder: RiskStateBuilder | None = None,
        candle_limit: int = 100,
        enable_trading: bool = True,
    ):
        self.client = client
        self.storage = storage
        self.strategy = strategy
        self.risk_guard = risk_guard
        self.execution_engine = execution_engine or ExecutionEngine(client)
        self.analysis_generator = analysis_generator or TradeAnalysisGenerator()
        self.risk_state = risk_state or RiskState()
        self.risk_state_builder = risk_state_builder
        self.candle_limit = candle_limit
        self.enable_trading = enable_trading
        self.journal = Journal(storage)
        self._cycle_candle_cache: dict[tuple[str, str], list] = {}

    def close(self) -> None:
        """Release resources held by the runner (currently the storage)."""
        close = getattr(self.storage, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "Runner":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def run_once(self, symbol: str) -> RunOnceResult:
        one_hour = self._fetch_store_candles(symbol, "1H")
        four_hour = self._fetch_store_candles(symbol, "4H")

        signal = self.strategy.generate(symbol, one_hour, four_hour)
        signal = self.journal.record_signal(signal)

        current_state = self._resolve_risk_state()
        decision = self.risk_guard.evaluate(signal, current_state)
        decision = self.journal.record_risk_decision(decision)

        analysis = self.analysis_generator.generate(signal, decision)
        insert_analysis = getattr(self.storage, "insert_trade_analysis", None)
        if insert_analysis is not None:
            insert_analysis(analysis)

        order = None
        if decision.status == RiskStatus.APPROVED:
            if not self.enable_trading:
                order = OrderRecord(
                    signal_id=decision.signal_id,
                    order_id=None,
                    symbol=signal.symbol,
                    side=signal.direction,
                    quantity=decision.position_size_usdt,
                    state=OrderState.CREATED,
                    created_at=decision.created_at,
                )
                return RunOnceResult(
                    signal=signal,
                    risk_decision=decision,
                    analysis=analysis,
                    order=order,
                )

            order = self.execution_engine.submit(
                ExecutionDecision(
                    signal_id=decision.signal_id,
                    symbol=signal.symbol,
                    direction=signal.direction,
                    status=decision.status,
                    reason=decision.reason,
                    position_size_usdt=decision.position_size_usdt,
                    leverage=decision.leverage,
                    created_at=decision.created_at,
                    stop_loss=getattr(signal, "stop_price", None),
                    take_profit=getattr(signal, "target_price", None),
                    id=decision.id,
                )
            )
            insert_order = getattr(self.storage, "insert_order", None)
            if insert_order is not None:
                insert_order(order)

        return RunOnceResult(
            signal=signal,
            risk_decision=decision,
            analysis=analysis,
            order=order,
        )

    def prepare_universe(self, symbols: list[str]) -> None:
        # Every bot cycle must start from fresh candles; without this reset
        # strategies keep seeing the first cycle's data forever.
        self._cycle_candle_cache = {}
        prepare = getattr(self.strategy, "prepare_universe", None)
        if not callable(prepare):
            return

        wants_daily = bool(getattr(self.strategy, "requires_daily", False))
        one_hour: dict[str, list] = {}
        four_hour: dict[str, list] = {}
        daily: dict[str, list] = {}
        funding_rates: dict[str, float] = {}
        for symbol in symbols:
            one_hour[symbol] = self._fetch_store_candles(symbol, "1H")
            four_hour[symbol] = self._fetch_store_candles(symbol, "4H")
            if wants_daily:
                daily[symbol] = self._fetch_store_candles(symbol, "1D")
            funding_rate = self._fetch_funding_rate(symbol)
            if funding_rate is not None:
                funding_rates[symbol] = funding_rate

        prepare(
            symbols=symbols,
            one_hour=one_hour,
            four_hour=four_hour,
            funding_rates=funding_rates,
            daily=daily,
        )

    def _fetch_store_candles(self, symbol: str, timeframe: str):
        cache_key = (symbol, timeframe)
        if cache_key in self._cycle_candle_cache:
            return self._cycle_candle_cache[cache_key]

        rows = self.client.get_candles(symbol, timeframe, self.candle_limit)
        candles = normalize_candles(symbol, timeframe, rows)
        stored = [
            self.storage.upsert_candle(
                symbol=candle.symbol,
                timeframe=candle.timeframe,
                timestamp=candle.timestamp,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
            for candle in candles
        ]
        self._cycle_candle_cache[cache_key] = stored
        return stored

    def _fetch_funding_rate(self, symbol: str) -> float | None:
        get_funding_rate = getattr(self.client, "get_funding_rate", None)
        if not callable(get_funding_rate):
            return None
        try:
            return get_funding_rate(symbol)
        except Exception:
            return None

    def _resolve_risk_state(self) -> RiskState:
        """Resolve the RiskState to pass into the RiskGuard.

        Resolution order:
        1. Explicit ``risk_state_builder(storage)`` callback, if provided.
        2. ``storage.load_risk_state()`` (duck-typed) if the storage exposes it.
        3. Fall back to the RiskState injected at construction time.

        This lets production code aggregate today's PnL / consecutive losses /
        open positions from the journal, while keeping the in-memory test
        fixtures untouched.
        """
        if self.risk_state_builder is not None:
            built = self.risk_state_builder(self.storage)
            if built is not None:
                return built

        loader = getattr(self.storage, "load_risk_state", None)
        if callable(loader):
            reference_capital = getattr(self.risk_guard, "reference_capital_usdt", None)
            try:
                loaded = loader(reference_capital_usdt=reference_capital)
            except TypeError:
                loaded = loader()
            if isinstance(loaded, RiskState):
                return loaded

        return self.risk_state
