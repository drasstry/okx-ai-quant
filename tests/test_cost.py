import math

import pytest

from okx_ai_quant.cost import CostModel


def test_round_trip_cost_includes_two_fee_sides_and_slippage():
    model = CostModel(fee_rate_per_side=0.001, slippage_rate=0.002)

    assert model.round_trip_cost_rate() == 0.004


def test_expected_move_gate_rejects_small_edge_and_accepts_sufficient_edge():
    model = CostModel(
        fee_rate_per_side=0.001,
        slippage_rate=0.002,
        min_expected_move=0.006,
    )

    assert model.is_trade_worthwhile(0.0059) is False
    assert model.is_trade_worthwhile(0.006) is True


def test_expected_move_gate_uses_cost_when_cost_exceeds_minimum():
    model = CostModel(
        fee_rate_per_side=0.003,
        slippage_rate=0.002,
        min_expected_move=0.006,
    )

    assert model.is_trade_worthwhile(0.0079) is False
    assert model.is_trade_worthwhile(0.008) is True


def test_expected_move_gate_rejects_non_finite_expected_move():
    model = CostModel(fee_rate_per_side=0.001, slippage_rate=0.002)

    assert model.is_trade_worthwhile(math.inf) is False
    assert model.is_trade_worthwhile(math.nan) is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"fee_rate_per_side": -0.001, "slippage_rate": 0.002}, "fee_rate_per_side"),
        ({"fee_rate_per_side": 0.001, "slippage_rate": -0.002}, "slippage_rate"),
        (
            {"fee_rate_per_side": 0.001, "slippage_rate": 0.002, "min_expected_move": 0.0},
            "min_expected_move",
        ),
        ({"fee_rate_per_side": math.nan, "slippage_rate": 0.002}, "fee_rate_per_side"),
        ({"fee_rate_per_side": 0.001, "slippage_rate": math.nan}, "slippage_rate"),
        (
            {"fee_rate_per_side": 0.001, "slippage_rate": 0.002, "min_expected_move": math.nan},
            "min_expected_move",
        ),
    ],
)
def test_cost_model_rejects_invalid_rates(kwargs, message):
    with pytest.raises(ValueError, match=message):
        CostModel(**kwargs)
