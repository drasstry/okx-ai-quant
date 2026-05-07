from datetime import UTC, datetime
from typing import Protocol

from okx_ai_quant.models import RiskStatus, Signal, TradeAnalysis
from okx_ai_quant.risk import RiskDecision


class TradeAnalysisLLMClient(Protocol):
    def generate_trade_analysis(
        self,
        *,
        signal: Signal,
        decision: RiskDecision,
        fallback_english: str,
        fallback_chinese: str,
    ) -> tuple[str, str]: ...


class TradeAnalysisGenerator:
    def __init__(self, llm_client: TradeAnalysisLLMClient | None = None):
        self.llm_client = llm_client

    def generate(self, signal: Signal, decision: RiskDecision) -> TradeAnalysis:
        english, chinese = self._fallback_text(signal, decision)
        if self.llm_client is not None:
            try:
                english, chinese = self.llm_client.generate_trade_analysis(
                    signal=signal,
                    decision=decision,
                    fallback_english=english,
                    fallback_chinese=chinese,
                )
            except Exception as exc:
                english = f"{english} LLM unavailable: {exc}"
                chinese = f"{chinese} 大模型不可用：{exc}"

        return TradeAnalysis(
            signal_id=signal.id or decision.signal_id or 0,
            english=english,
            chinese=chinese,
            created_at=datetime.now(UTC),
        )

    def _fallback_text(self, signal: Signal, decision: RiskDecision) -> tuple[str, str]:
        status = decision.status.value
        direction = signal.direction.value
        size = decision.position_size_usdt
        leverage = decision.leverage
        action = "eligible for execution" if decision.status == RiskStatus.APPROVED else "not executable"
        chinese_action = "可以进入执行" if decision.status == RiskStatus.APPROVED else "不进入执行"

        return (
            (
                f"{signal.symbol} {signal.timeframe} signal is {direction} with "
                f"{signal.confidence:.2f} confidence. Risk status is {status}; "
                f"position size is {size:g} USDT at {leverage}x leverage, so it is {action}. "
                f"Signal reason: {signal.reason} Risk reason: {decision.reason}"
            ),
            (
                f"{signal.symbol} {signal.timeframe} 信号方向为 {direction}，"
                f"置信度 {signal.confidence:.2f}。风控状态为 {status}；"
                f"仓位规模 {size:g} USDT，杠杆 {leverage}x，因此{chinese_action}。"
                f"信号原因：{signal.reason} 风控原因：{decision.reason}"
            ),
        )
