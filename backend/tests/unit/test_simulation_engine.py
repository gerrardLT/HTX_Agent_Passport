"""模拟引擎单元测试（任务 12.2 / Req 9 AC3）。

覆盖：
1. test_execute_returns_filled_result：基本执行返回 FILLED
2. test_order_id_is_deterministic：相同输入产生相同 order_id
3. test_order_id_differs_for_different_inputs：不同输入产生不同 order_id
4. test_uses_seed_price_for_market_order：market 单用 SEED_MARKET_DATA last price
5. test_uses_limit_price_for_limit_order：limit 单用指定价格
6. test_unknown_symbol_uses_zero_price：未知 symbol 用 0 作为 fallback
"""

from __future__ import annotations

import pytest

from app.services.simulation_engine import SimulationEngine, SimulationResult


@pytest.fixture
def engine() -> SimulationEngine:
    return SimulationEngine()


class TestSimulationEngine:
    """模拟引擎核心功能测试。"""

    def test_execute_returns_filled_result(self, engine: SimulationEngine) -> None:
        """基本执行返回 FILLED 状态的 SimulationResult。"""
        action = {
            "symbol": "btcusdt",
            "side": "buy",
            "order_type": "limit",
            "amount": 0.001,
            "limit_price": 67000.0,
        }
        result = engine.execute(action)

        assert isinstance(result, SimulationResult)
        assert result.status == "FILLED"
        assert result.symbol == "btcusdt"
        assert result.side == "buy"
        assert result.order_type == "limit"
        assert result.amount == 0.001
        assert result.filled_amount == 0.001
        assert result.order_id.startswith("sim-")

    def test_order_id_is_deterministic(self, engine: SimulationEngine) -> None:
        """相同输入产生相同 order_id（确定性）。"""
        action = {
            "symbol": "ethusdt",
            "side": "sell",
            "order_type": "market",
            "amount": 1.5,
            "limit_price": None,
        }
        result1 = engine.execute(action)
        result2 = engine.execute(action)

        assert result1.order_id == result2.order_id

    def test_order_id_differs_for_different_inputs(self, engine: SimulationEngine) -> None:
        """不同输入产生不同 order_id。"""
        action1 = {
            "symbol": "btcusdt",
            "side": "buy",
            "order_type": "limit",
            "amount": 0.001,
            "limit_price": 67000.0,
        }
        action2 = {
            "symbol": "btcusdt",
            "side": "sell",
            "order_type": "limit",
            "amount": 0.001,
            "limit_price": 67000.0,
        }
        result1 = engine.execute(action1)
        result2 = engine.execute(action2)

        assert result1.order_id != result2.order_id

    def test_uses_seed_price_for_market_order(self, engine: SimulationEngine) -> None:
        """market 单用 SEED_MARKET_DATA last price。"""
        action = {
            "symbol": "btcusdt",
            "side": "buy",
            "order_type": "market",
            "amount": 0.01,
            "limit_price": None,
        }
        result = engine.execute(action)

        # btcusdt seed last = 68000.0
        assert result.filled_price == 68000.0
        assert result.price == 68000.0

    def test_uses_limit_price_for_limit_order(self, engine: SimulationEngine) -> None:
        """limit 单用指定价格。"""
        action = {
            "symbol": "ethusdt",
            "side": "buy",
            "order_type": "limit",
            "amount": 2.0,
            "limit_price": 3500.0,
        }
        result = engine.execute(action)

        assert result.filled_price == 3500.0
        assert result.price == 3500.0

    def test_unknown_symbol_uses_zero_price(self, engine: SimulationEngine) -> None:
        """未知 symbol 用 0 作为 fallback（market order）。"""
        action = {
            "symbol": "xyzusdt",
            "side": "buy",
            "order_type": "market",
            "amount": 100.0,
            "limit_price": None,
        }
        result = engine.execute(action)

        assert result.filled_price == 0.0
        assert result.price == 0.0
        assert result.symbol == "xyzusdt"
        assert result.status == "FILLED"
