from datetime import UTC, datetime

from okx_ai_quant.analysis import TradeAnalysisGenerator
from okx_ai_quant.models import RiskStatus, Signal, SignalDirection
from okx_ai_quant.risk import RiskDecision


def test_analysis_generator_returns_bilingual_text():
    signal = Signal(
        id=7,
        symbol="BTC-USDT-SWAP",
        timeframe="1H",
        direction=SignalDirection.LONG,
        confidence=0.81,
        reason="4H and 1H EMA uptrend aligned.",
        created_at=datetime(2026, 5, 6, 8, 0, tzinfo=UTC),
    )
    decision = RiskDecision(
        signal_id=7,
        status=RiskStatus.APPROVED,
        reason="Risk checks passed.",
        position_size_usdt=100.0,
        leverage=2,
        created_at=datetime(2026, 5, 6, 8, 1, tzinfo=UTC),
    )

    analysis = TradeAnalysisGenerator().generate(signal, decision)

    assert analysis.signal_id == 7
    assert analysis.english.strip()
    assert analysis.chinese.strip()
    assert "BTC-USDT-SWAP" in analysis.english
    assert "BTC-USDT-SWAP" in analysis.chinese
    assert analysis.created_at.tzinfo is UTC
