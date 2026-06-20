"""任务 19 Demo 场景集成测试。

验证 3 个预设场景的最终状态：
- happy → EXECUTED
- reject → AUTO_REJECTED (BLOCKED_ACTION_WITHDRAW)
- over_limit → AUTO_REJECTED (LIMIT_MAX_NOTIONAL_EXCEEDED)
- seed_data 加载幂等
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.enums import ActionState
from app.services.demo_scenarios import (
    run_happy_scenario,
    run_over_limit_scenario,
    run_reject_scenario,
)
from app.services.seed_data import SEED_USER_WALLET, load_seed_data

pytestmark = pytest.mark.integration


class TestSeedData:
    """种子数据加载测试。"""

    def test_seed_data_loads_without_error(self, db_session: Session) -> None:
        """load_seed_data 不抛异常，返回有效 ID。"""
        result = load_seed_data(db_session)
        assert "user_id" in result
        assert "credential_id" in result
        assert "passport_id" in result
        assert result["user_id"] is not None

    def test_seed_data_idempotent(self, db_session: Session) -> None:
        """多次加载不报错，返回相同 ID。"""
        result1 = load_seed_data(db_session)
        result2 = load_seed_data(db_session)
        assert result1["user_id"] == result2["user_id"]
        assert result1["credential_id"] == result2["credential_id"]
        assert result1["passport_id"] == result2["passport_id"]


class TestDemoScenarios:
    """3 个预设场景的最终状态验证。"""

    @pytest.fixture(autouse=True)
    def _seed(self, db_session: Session):
        """每个测试前加载种子数据。"""
        self.ids = load_seed_data(db_session)
        self.session = db_session

    def test_happy_scenario_ends_in_executed(self) -> None:
        """Happy path → EXECUTED。"""
        result = run_happy_scenario(
            self.session, self.ids["passport_id"], self.ids["user_id"]
        )
        assert result["final_state"] == ActionState.EXECUTED
        assert result["action_id"] is not None

    def test_reject_scenario_ends_in_auto_rejected(self) -> None:
        """Reject path → AUTO_REJECTED。"""
        result = run_reject_scenario(
            self.session, self.ids["passport_id"], self.ids["user_id"]
        )
        assert result["final_state"] == ActionState.AUTO_REJECTED
        assert "BLOCKED_ACTION_WITHDRAW" in result["reason_codes"]

    def test_over_limit_scenario_ends_in_auto_rejected(self) -> None:
        """Over-limit path → AUTO_REJECTED with LIMIT_MAX_NOTIONAL_EXCEEDED。"""
        result = run_over_limit_scenario(
            self.session, self.ids["passport_id"], self.ids["user_id"]
        )
        assert result["final_state"] == ActionState.AUTO_REJECTED
        assert "LIMIT_MAX_NOTIONAL_EXCEEDED" in result["reason_codes"]
