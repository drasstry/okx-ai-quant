from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, kw_only=True)
class CostModel:
    fee_rate_per_side: float
    slippage_rate: float
    min_expected_move: float = 0.006

    def __post_init__(self) -> None:
        if not isfinite(self.fee_rate_per_side) or self.fee_rate_per_side < 0:
            raise ValueError("fee_rate_per_side must be greater than or equal to 0")
        if not isfinite(self.slippage_rate) or self.slippage_rate < 0:
            raise ValueError("slippage_rate must be greater than or equal to 0")
        if not isfinite(self.min_expected_move) or self.min_expected_move <= 0:
            raise ValueError("min_expected_move must be greater than 0")

    def round_trip_cost_rate(self) -> float:
        return (self.fee_rate_per_side * 2) + self.slippage_rate

    def is_trade_worthwhile(self, expected_move: float) -> bool:
        if not isfinite(expected_move):
            return False
        return expected_move >= max(self.min_expected_move, self.round_trip_cost_rate())
