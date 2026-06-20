"""Demo 场景编排（任务 19 / Req 25）。

提供 3 个预设场景函数，全程无外部依赖（B.AI + HTX 全 mock）：
- happy: 合法任务 → EXECUTED
- reject: 提现任务 → AUTO_REJECTED（规则路由拦截）
- over_limit: 超限任务 → AUTO_REJECTED（LIMIT_MAX_NOTIONAL_EXCEEDED）
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AgentAction
from app.models.enums import ActionState, AuditEventType
from app.schemas.action_plan import validate_action_plan_schema
from app.services.audit_writer import ACTOR_TYPE_SYSTEM, AuditWriter
from app.services.input_normalizer import build_blocked_action_plan, normalize_and_route
from app.services.seed_data import SEED_MARKET_DATA, SEED_TASKS


def _create_action(
    session: Session,
    passport_id: uuid.UUID,
    user_id: uuid.UUID,
    task: str,
    execution_mode: str = "simulation",
) -> AgentAction:
    """创建一个 action 记录。"""
    action = AgentAction(
        passport_id=passport_id,
        user_id=user_id,
        trace_id=uuid.uuid4(),
        natural_language_request=task,
        state=ActionState.REQUESTED,
        execution_mode=execution_mode,
    )
    session.add(action)
    session.flush()
    return action


def run_happy_scenario(
    session: Session, passport_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    """Happy path: 合法 10 USDT 买入 → EXECUTED。

    流程：REQUESTED → PLANNING → PLAN_VALIDATED → RISK_CHECKING →
    REQUIRE_APPROVAL → APPROVED → EXECUTING → EXECUTED
    """
    task = SEED_TASKS["happy"]
    action = _create_action(session, passport_id, user_id, task)
    audit = AuditWriter(session)

    # 1. 规则路由：不拦截
    normalized = normalize_and_route(task)
    assert normalized.mode == "task"

    # 2. 模拟 planner 返回合法 ActionPlan
    action.state = ActionState.PLANNING
    session.flush()

    mock_plan = {
        "version": "0.1",
        "intent_summary": "查看 BTC/USDT 行情并准备 10 USDT 限价买入",
        "actions": [
            {
                "type": "place_order",
                "symbol": "btcusdt",
                "side": "buy",
                "order_type": "limit",
                "amount": 0.000147,
                "amount_unit": "base",
                "max_notional_usdt": 10.0,
                "limit_price": 68000.0,
                "requires_user_approval": True,
                "rationale": "在策略范围内的 10 USDT 限价买入",
            }
        ],
        "assumptions": ["BTC/USDT 当前价格约 68000"],
        "risk_notes": ["限价单可能不会立即成交"],
    }
    plan = validate_action_plan_schema(mock_plan)
    assert plan is not None

    action.normalized_action_json = mock_plan
    action.state = ActionState.PLAN_VALIDATED
    session.flush()

    # 3. Policy Engine（简化：直接标记 REQUIRE_APPROVAL）
    action.state = ActionState.APPROVAL_REQUIRED
    action.risk_verdict = "REQUIRE_APPROVAL"
    action.risk_score = 40
    action.reason_codes = []
    session.flush()

    # 4. 自动审批
    action.state = ActionState.APPROVED
    session.flush()

    # 5. 模拟执行
    action.state = ActionState.EXECUTING
    session.flush()

    action.state = ActionState.EXECUTED
    session.flush()

    # 写审计事件
    audit.write(
        event_type=AuditEventType.ACTION_REQUESTED,
        user_id=user_id,
        passport_id=passport_id,
        action_id=action.id,
        trace_id=action.trace_id,
        actor_type=ACTOR_TYPE_SYSTEM,
        event_data={"task": task, "scenario": "happy"},
    )

    session.commit()
    return {"action_id": action.id, "final_state": ActionState.EXECUTED}


def run_reject_scenario(
    session: Session, passport_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    """Reject path: 提现任务 → 规则路由拦截 → AUTO_REJECTED。"""
    task = SEED_TASKS["reject"]
    action = _create_action(session, passport_id, user_id, task)
    audit = AuditWriter(session)

    # 1. 规则路由拦截
    normalized = normalize_and_route(task)
    assert normalized.mode == "blocked_shortcut"

    # 2. 生成 no_op ActionPlan
    blocked_plan = build_blocked_action_plan(task, normalized.blocked_reason or "BLOCKED")
    action.normalized_action_json = blocked_plan
    action.state = ActionState.AUTO_REJECTED
    action.risk_verdict = "REJECT"
    action.risk_score = 100
    action.reason_codes = ["BLOCKED_ACTION_WITHDRAW"]
    session.flush()

    audit.write(
        event_type=AuditEventType.ACTION_REQUESTED,
        user_id=user_id,
        passport_id=passport_id,
        action_id=action.id,
        trace_id=action.trace_id,
        actor_type=ACTOR_TYPE_SYSTEM,
        event_data={"task": task, "scenario": "reject"},
    )

    session.commit()
    return {
        "action_id": action.id,
        "final_state": ActionState.AUTO_REJECTED,
        "reason_codes": ["BLOCKED_ACTION_WITHDRAW"],
    }


def run_over_limit_scenario(
    session: Session, passport_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    """Over-limit path: 500 USDT 买入 → LIMIT_MAX_NOTIONAL_EXCEEDED → AUTO_REJECTED。"""
    task = SEED_TASKS["over_limit"]
    action = _create_action(session, passport_id, user_id, task)
    audit = AuditWriter(session)

    # 1. 规则路由：不拦截（不含提现关键字）
    normalized = normalize_and_route(task)
    assert normalized.mode == "task"

    # 2. 模拟 planner 返回超限 ActionPlan
    action.state = ActionState.PLANNING
    session.flush()

    mock_plan = {
        "version": "0.1",
        "intent_summary": "买入 500 USDT 的 BTC",
        "actions": [
            {
                "type": "place_order",
                "symbol": "btcusdt",
                "side": "buy",
                "order_type": "market",
                "amount": 0.00735,
                "amount_unit": "base",
                "max_notional_usdt": 500.0,
                "rationale": "用户要求买入 500 USDT 的 BTC",
            }
        ],
        "assumptions": [],
        "risk_notes": ["金额超过策略限制"],
    }
    action.normalized_action_json = mock_plan
    action.state = ActionState.PLAN_VALIDATED
    session.flush()

    # 3. Policy Engine 拒绝（超限）
    action.state = ActionState.AUTO_REJECTED
    action.risk_verdict = "REJECT"
    action.risk_score = 85
    action.reason_codes = ["LIMIT_MAX_NOTIONAL_EXCEEDED"]
    session.flush()

    audit.write(
        event_type=AuditEventType.ACTION_REQUESTED,
        user_id=user_id,
        passport_id=passport_id,
        action_id=action.id,
        trace_id=action.trace_id,
        actor_type=ACTOR_TYPE_SYSTEM,
        event_data={"task": task, "scenario": "over_limit"},
    )

    session.commit()
    return {
        "action_id": action.id,
        "final_state": ActionState.AUTO_REJECTED,
        "reason_codes": ["LIMIT_MAX_NOTIONAL_EXCEEDED"],
    }


__all__ = [
    "run_happy_scenario",
    "run_over_limit_scenario",
    "run_reject_scenario",
]
