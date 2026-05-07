from dataclasses import replace

from okx_ai_quant.models import Signal
from okx_ai_quant.risk import RiskDecision


class Journal:
    def __init__(self, storage):
        self.storage = storage

    def record_signal(self, signal: Signal) -> Signal:
        signal_id = self.storage.insert_signal(signal)
        return replace(signal, id=signal_id)

    def record_risk_decision(self, decision: RiskDecision) -> RiskDecision:
        insert = getattr(self.storage, "insert_risk_decision", None)
        if insert is None:
            return decision
        decision_id = insert(decision)
        return replace(decision, id=decision_id)
