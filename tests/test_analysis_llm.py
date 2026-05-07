from datetime import UTC, datetime

from okx_ai_quant.analysis import TradeAnalysisGenerator
from okx_ai_quant.models import RiskStatus, Signal, SignalDirection
from okx_ai_quant.risk import RiskDecision


class FakeLLMClient:
    def __init__(self):
        self.calls = []

    def generate_trade_analysis(self, *, signal, decision, fallback_english, fallback_chinese):
        self.calls.append(
            {
                "signal": signal,
                "decision": decision,
                "fallback_english": fallback_english,
                "fallback_chinese": fallback_chinese,
            }
        )
        return "AI English analysis", "AI Chinese analysis"


def test_trade_analysis_generator_uses_llm_client_when_provided():
    signal = Signal(
        id=7,
        symbol="BTC-USDT-SWAP",
        timeframe="1H",
        direction=SignalDirection.LONG,
        confidence=0.82,
        reason="Momentum confirmed.",
        created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
    )
    decision = RiskDecision(
        signal_id=7,
        status=RiskStatus.APPROVED,
        reason="Risk accepted.",
        position_size_usdt=100,
        leverage=1,
        created_at=datetime(2026, 5, 6, 8, 1, tzinfo=UTC),
    )
    llm_client = FakeLLMClient()

    analysis = TradeAnalysisGenerator(llm_client=llm_client).generate(signal, decision)

    assert analysis.english == "AI English analysis"
    assert analysis.chinese == "AI Chinese analysis"
    assert len(llm_client.calls) == 1
    assert "Momentum confirmed" in llm_client.calls[0]["fallback_english"]


def test_trade_analysis_generator_falls_back_when_llm_client_fails():
    class FailingLLMClient:
        def generate_trade_analysis(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    signal = Signal(
        id=8,
        symbol="ETH-USDT-SWAP",
        timeframe="1H",
        direction=SignalDirection.SHORT,
        confidence=0.7,
        reason="Breakdown confirmed.",
        created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
    )
    decision = RiskDecision(
        signal_id=8,
        status=RiskStatus.APPROVED,
        reason="Risk accepted.",
        position_size_usdt=50,
        leverage=1,
        created_at=datetime(2026, 5, 6, 8, 1, tzinfo=UTC),
    )

    analysis = TradeAnalysisGenerator(llm_client=FailingLLMClient()).generate(signal, decision)

    assert "provider unavailable" in analysis.english
    assert "LLM unavailable" in analysis.english
    assert "大模型不可用" in analysis.chinese


def test_openai_compatible_llm_client_serializes_dataclass_payloads(monkeypatch):
    from okx_ai_quant.llm import OpenAICompatibleLLMClient

    captured = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return {"output_text": '{"english":"LLM ok","chinese":"大模型正常"}'}

    class FakeOpenAI:
        def __init__(self, *, base_url, api_key):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            self.responses = FakeResponses()

    monkeypatch.setitem(__import__("sys").modules, "openai", type("FakeOpenAIModule", (), {"OpenAI": FakeOpenAI}))

    signal = Signal(
        id=9,
        symbol="BTC-USDT-SWAP",
        timeframe="1H",
        direction=SignalDirection.LONG,
        confidence=0.7,
        reason="Test signal.",
        created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
    )
    decision = RiskDecision(
        signal_id=9,
        status=RiskStatus.APPROVED,
        reason="Test risk.",
        position_size_usdt=10,
        leverage=1,
        created_at=datetime(2026, 5, 6, 8, 1, tzinfo=UTC),
    )

    english, chinese = OpenAICompatibleLLMClient(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
    ).generate_trade_analysis(
        signal=signal,
        decision=decision,
        fallback_english="fallback en",
        fallback_chinese="fallback zh",
    )

    assert english == "LLM ok"
    assert chinese == "大模型正常"
    assert captured["base_url"] == "https://example.test/v1"
    assert captured["api_key"] == "test-key"
    prompt = captured["input"][0]["content"][0]["text"]
    assert "BTC-USDT-SWAP" in prompt
    assert "APPROVED" in prompt
