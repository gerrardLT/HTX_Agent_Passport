"""模拟引擎（任务 12.2 / Req 9 AC3）。

在 simulation 模式下基于种子行情数据返回确定性 fake order_id 与撮合结果。

设计要点：
- fake order_id = "sim-" + sha256(action_str)[:12]（确定性，相同输入恒产生相同 ID）
- 价格用 SEED_MARKET_DATA 的 last price（market order）或指定 limit_price（limit order）
- 全量成交（FILLED）
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.services.htx_adapter import SEED_MARKET_DATA


@dataclass
class SimulationResult:
    """模拟执行结果。"""

    order_id: str  # 确定性 fake order_id
    symbol: str
    side: str
    order_type: str
    amount: float
    price: float
    status: str  # "FILLED" / "PARTIALLY_FILLED"
    filled_amount: float
    filled_price: float


class SimulationEngine:
    """模拟引擎（Req 9 AC3）：simulation 模式下返回确定性 fake 结果。"""

    def execute(self, action: dict[str, Any]) -> SimulationResult:
        """基于 action（normalized_action_json）模拟执行。

        - fake order_id = "sim-" + sha256(action_str)[:12]（确定性）
        - 价格用 SEED_MARKET_DATA 的 last price（market order）或 limit_price（limit order）
        - 全量成交（FILLED）
        """
        symbol = action.get("symbol", "btcusdt").lower()
        side = action.get("side", "buy")
        order_type = action.get("order_type", "limit")
        amount = float(action.get("amount", 0))
        limit_price = action.get("limit_price")

        # 确定性 fake order_id
        action_str = f"{symbol}:{side}:{order_type}:{amount}:{limit_price}"
        order_id = "sim-" + hashlib.sha256(action_str.encode()).hexdigest()[:12]

        # 模拟价格：limit 单用指定价格，market 单用 SEED_MARKET_DATA last price
        market = SEED_MARKET_DATA.get(symbol, {"last": 0.0})
        if limit_price is not None and order_type == "limit":
            fill_price = float(limit_price)
        else:
            fill_price = market["last"]

        return SimulationResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            amount=amount,
            price=fill_price,
            status="FILLED",
            filled_amount=amount,
            filled_price=fill_price,
        )
