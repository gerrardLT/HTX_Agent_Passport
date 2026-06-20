"""任务 14 恢复管理器 + 循环检测器 + 检查点 单元测试。

覆盖维度（对应 Req 14 AC1-7 / Req 18 AC6）
--------------------------------------------
1. **工具失败重试**：
   - 幂等 + retryable → retry=True, max_attempts=1
   - 非幂等 → retry=False + state=EXECUTION_FAILED + 审计
   - 幂等 + non-retryable → retry=False + state=EXECUTION_FAILED + 审计

2. **幻觉处理**：
   - state → PLAN_INVALID + PLAN_HALLUCINATION 审计

3. **循环检测**：
   - 同参数 3 次 → True
   - 2 次 → False
   - 不同参数 3 次 → False
   - reset 清空历史
   - handle_loop_detected → state=FAILED + LOOP_DETECTED 审计

4. **模型不可用**：
   - 仅写审计，不改状态

5. **检查点**：
   - save → restore 数据一致
   - to_json → from_json → to_json 等价
   - 无检查点 → None
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentAction, AgentPassport, AuditEvent, User
from app.models.enums import ActionState, AuditEventType
from app.services.recovery_manager import (
    Checkpoint,
    LoopDetector,
    RecoveryManager,
    RetryDecision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_user(db_session: Session) -> User:
    """创建测试用户。"""
    user = User(primary_wallet=f"0xREC{uuid.uuid4().hex[:36]}")
    db_session.add(user)
    db_session.flush()
    return user


def _make_passport(db_session: Session, user: User) -> AgentPassport:
    """创建测试 Passport。"""
    passport = AgentPassport(
        user_id=user.id,
        name="test-passport",
        agent_type="trader",
        state="ACTIVE",
        policy_json={"version": "0.1", "capabilities": {}, "limits": {}, "approval": {}, "blocked_actions": []},
    )
    db_session.add(passport)
    db_session.flush()
    return passport


def _make_action(
    db_session: Session, user: User, passport: AgentPassport, state: str = ActionState.EXECUTING
) -> AgentAction:
    """创建测试 Action。"""
    action = AgentAction(
        passport_id=passport.id,
        user_id=user.id,
        trace_id=uuid.uuid4(),
        natural_language_request="test task",
        state=state,
        execution_mode="simulation",
    )
    db_session.add(action)
    db_session.flush()
    return action


def _get_audit_events(db_session: Session, action_id: uuid.UUID) -> list[AuditEvent]:
    """获取指定 action 的审计事件。"""
    stmt = select(AuditEvent).where(AuditEvent.action_id == action_id)
    return list(db_session.execute(stmt).scalars().all())


# ===========================================================================
# 1. 工具失败重试
# ===========================================================================
class TestToolFailure:
    """Req 14 AC1: 工具执行失败恢复策略。"""

    def test_idempotent_retryable_tool_returns_retry_true(self, db_session: Session) -> None:
        """getTicker + retryable=True → retry=True, max_attempts=1。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)

        mgr = RecoveryManager(db_session)
        decision = mgr.handle_tool_failure(
            action_id=action.id,
            user_id=user.id,
            tool="getTicker",
            error=TimeoutError("connection timeout"),
            retryable=True,
        )

        assert decision.retry is True
        assert decision.max_attempts == 1
        # 不应改变 action 状态
        db_session.refresh(action)
        assert action.state == ActionState.EXECUTING
        # 不应写入审计事件
        events = _get_audit_events(db_session, action.id)
        assert len(events) == 0

    def test_non_idempotent_tool_returns_retry_false(self, db_session: Session) -> None:
        """placeSpotOrder → retry=False + state=EXECUTION_FAILED + 审计。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)

        mgr = RecoveryManager(db_session)
        decision = mgr.handle_tool_failure(
            action_id=action.id,
            user_id=user.id,
            tool="placeSpotOrder",
            error=RuntimeError("order failed"),
            retryable=True,
        )

        assert decision.retry is False
        # 状态应变为 EXECUTION_FAILED
        db_session.refresh(action)
        assert action.state == ActionState.EXECUTION_FAILED
        # 应写入 EXECUTION_FAILED 审计事件
        events = _get_audit_events(db_session, action.id)
        assert len(events) == 1
        assert events[0].event_type == AuditEventType.EXECUTION_FAILED
        assert events[0].event_json["data"]["tool"] == "placeSpotOrder"
        assert "order failed" in events[0].event_json["data"]["error"]

    def test_idempotent_non_retryable_returns_retry_false(self, db_session: Session) -> None:
        """getTicker + retryable=False → retry=False + state=EXECUTION_FAILED + 审计。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)

        mgr = RecoveryManager(db_session)
        decision = mgr.handle_tool_failure(
            action_id=action.id,
            user_id=user.id,
            tool="getTicker",
            error=RuntimeError("permanent failure"),
            retryable=False,
        )

        assert decision.retry is False
        db_session.refresh(action)
        assert action.state == ActionState.EXECUTION_FAILED
        events = _get_audit_events(db_session, action.id)
        assert len(events) == 1
        assert events[0].event_type == AuditEventType.EXECUTION_FAILED


# ===========================================================================
# 2. 幻觉处理
# ===========================================================================
class TestHallucination:
    """Req 14 AC2: 模型幻觉恢复。"""

    def test_handle_hallucination_sets_plan_invalid(self, db_session: Session) -> None:
        """state → PLAN_INVALID + PLAN_HALLUCINATION 审计。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport, state=ActionState.PLANNING)

        mgr = RecoveryManager(db_session)
        mgr.handle_hallucination(
            action_id=action.id,
            user_id=user.id,
            hallucinated_fields=["symbol:FAKEUSDT", "price:99999"],
        )

        db_session.refresh(action)
        assert action.state == ActionState.PLAN_INVALID
        events = _get_audit_events(db_session, action.id)
        assert len(events) == 1
        assert events[0].event_type == AuditEventType.PLAN_HALLUCINATION
        assert events[0].event_json["data"]["hallucinated_fields"] == [
            "symbol:FAKEUSDT",
            "price:99999",
        ]


# ===========================================================================
# 3. 循环检测
# ===========================================================================
class TestLoopDetector:
    """Req 14 AC4 / Req 18 AC6: 循环检测逻辑。"""

    def test_loop_detector_triggers_at_threshold(self) -> None:
        """同参数 3 次 → True。"""
        detector = LoopDetector(max_repeat=3)
        params = {"symbol": "btcusdt"}

        assert detector.record_and_check("getTicker", params) is False
        assert detector.record_and_check("getTicker", params) is False
        assert detector.record_and_check("getTicker", params) is True

    def test_loop_detector_no_trigger_below_threshold(self) -> None:
        """2 次 → False。"""
        detector = LoopDetector(max_repeat=3)
        params = {"symbol": "btcusdt"}

        assert detector.record_and_check("getTicker", params) is False
        assert detector.record_and_check("getTicker", params) is False
        # 只有 2 次，不触发

    def test_loop_detector_different_params_no_trigger(self) -> None:
        """不同参数 3 次 → False。"""
        detector = LoopDetector(max_repeat=3)

        assert detector.record_and_check("getTicker", {"symbol": "btcusdt"}) is False
        assert detector.record_and_check("getTicker", {"symbol": "ethusdt"}) is False
        assert detector.record_and_check("getTicker", {"symbol": "dogeusdt"}) is False

    def test_loop_detector_different_tools_no_trigger(self) -> None:
        """不同工具同参数 3 次 → False。"""
        detector = LoopDetector(max_repeat=3)
        params = {"symbol": "btcusdt"}

        assert detector.record_and_check("getTicker", params) is False
        assert detector.record_and_check("getAccountBalance", params) is False
        assert detector.record_and_check("getTicker", params) is False

    def test_loop_detector_reset_clears_history(self) -> None:
        """reset 后历史清空，不再触发。"""
        detector = LoopDetector(max_repeat=3)
        params = {"symbol": "btcusdt"}

        detector.record_and_check("getTicker", params)
        detector.record_and_check("getTicker", params)
        detector.reset()
        # reset 后重新计数
        assert detector.record_and_check("getTicker", params) is False
        assert detector.record_and_check("getTicker", params) is False
        assert len(detector.history) == 2

    def test_handle_loop_detected_sets_failed(self, db_session: Session) -> None:
        """state → FAILED + LOOP_DETECTED 审计。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)

        mgr = RecoveryManager(db_session)
        mgr.handle_loop_detected(
            action_id=action.id,
            user_id=user.id,
            tool="getTicker",
            params={"symbol": "btcusdt"},
            count=3,
        )

        db_session.refresh(action)
        assert action.state == ActionState.FAILED
        events = _get_audit_events(db_session, action.id)
        assert len(events) == 1
        assert events[0].event_type == AuditEventType.LOOP_DETECTED
        assert events[0].event_json["data"]["tool"] == "getTicker"
        assert events[0].event_json["data"]["count"] == 3


# ===========================================================================
# 4. 模型不可用
# ===========================================================================
class TestModelUnavailable:
    """Req 14 AC5: 模型不可用恢复。"""

    def test_handle_model_unavailable_writes_audit(self, db_session: Session) -> None:
        """仅写审计，不改状态。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport, state=ActionState.PLANNING)

        original_state = action.state

        mgr = RecoveryManager(db_session)
        mgr.handle_model_unavailable(
            action_id=action.id,
            user_id=user.id,
        )

        # 状态不应改变
        db_session.refresh(action)
        assert action.state == original_state
        # 应写入 MODEL_UNAVAILABLE 审计事件
        events = _get_audit_events(db_session, action.id)
        assert len(events) == 1
        assert events[0].event_type == AuditEventType.MODEL_UNAVAILABLE
        assert "B.AI service unavailable" in events[0].event_json["data"]["message"]


# ===========================================================================
# 5. 检查点
# ===========================================================================
class TestCheckpoint:
    """Req 14 AC3, AC7: 检查点保存与恢复。"""

    def test_checkpoint_save_and_restore_roundtrip(self, db_session: Session) -> None:
        """save → restore 数据一致。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)

        checkpoint = Checkpoint(
            action_id=action.id,
            completed_steps=["PLANNING", "PLAN_VALIDATED"],
            pending_steps=["RISK_CHECKING", "EXECUTING"],
            last_tool_results=[{"tool": "getTicker", "result": {"last": 68000}}],
            timestamp=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
        )

        mgr = RecoveryManager(db_session)
        mgr.save_checkpoint(action.id, checkpoint)

        restored = mgr.restore_checkpoint(action.id)
        assert restored is not None
        assert restored.action_id == checkpoint.action_id
        assert restored.completed_steps == checkpoint.completed_steps
        assert restored.pending_steps == checkpoint.pending_steps
        assert restored.last_tool_results == checkpoint.last_tool_results
        assert restored.timestamp == checkpoint.timestamp

    def test_checkpoint_from_json_to_json_idempotent(self) -> None:
        """to_json → from_json → to_json 等价。"""
        action_id = uuid.uuid4()
        checkpoint = Checkpoint(
            action_id=action_id,
            completed_steps=["step_a", "step_b"],
            pending_steps=["step_c"],
            last_tool_results=[{"key": "value"}],
            timestamp=datetime(2026, 6, 15, 12, 30, 45, tzinfo=UTC),
        )

        json_1 = checkpoint.to_json()
        restored = Checkpoint.from_json(json_1)
        json_2 = restored.to_json()

        assert json_1 == json_2

    def test_restore_returns_none_when_no_checkpoint(self, db_session: Session) -> None:
        """无检查点 → None。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)

        mgr = RecoveryManager(db_session)
        result = mgr.restore_checkpoint(action.id)
        assert result is None
