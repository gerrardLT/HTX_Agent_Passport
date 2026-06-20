"""Action 详情与审计路由（Phase 1 / G10-G11 跟进）。

挂载到 ``/api/actions/*``——补全前端 ``useActionPolling`` 与审计重放页面
所需的端点：

```
GET /api/actions/{action_id}         → 200 action 详情（轮询用）
GET /api/actions/{action_id}/audit   → 200 该 action 的审计事件列表（前端时间线）
```

设计要点
--------
1. **鉴权隔离**：跨用户访问统一 404（避免存在性侧信道，与 passports / credentials
   路由风格一致）。
2. **审计响应形状**与 :data:`AuditEventResponse` 对齐——前端 ``AuditTimeline``
   组件直接消费。返回 ``{events, action_id, trace_id}`` 三字段，``trace_id``
   取该 action 的 trace_id（用于跨链路追踪）。
3. **不写**端点：本路由只暴露 GET——任何状态变更走对应专用路由
   （``/approve`` / ``/passports/{id}/...``）。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, get_db
from app.models import AgentAction, AuditEvent, ExecutionResult, User
from app.schemas.audit import AuditEventResponse

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /actions/{action_id}
# ---------------------------------------------------------------------------
@router.get(
    "/actions/{action_id}",
    status_code=status.HTTP_200_OK,
    summary="按 ID 获取 action 详情（轮询用；跨用户访问统一 404）",
)
def get_action_by_id(
    action_id: UUID,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """``GET /api/actions/{action_id}``

    前端 ``useActionPolling`` 每 2 秒轮询此端点。仅返回当前用户拥有的
    action（跨用户 404）。响应字段刻意宽松（dict 而非 Pydantic）——
    前端 ``ActionDetail`` 类型已对齐这套字段，引入 schema 反而增加耦合。
    """
    action = db.execute(
        select(AgentAction)
        .where(AgentAction.id == action_id)
        .where(AgentAction.user_id == current_user.id)
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "action not found"},
        )

    # 拉关联的 execution_result（如有）
    exec_result = db.execute(
        select(ExecutionResult)
        .where(ExecutionResult.action_id == action_id)
        .order_by(ExecutionResult.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    return {
        "id": str(action.id),
        "user_id": str(action.user_id),
        "passport_id": str(action.passport_id) if action.passport_id else None,
        "trace_id": str(action.trace_id) if action.trace_id else None,
        "natural_language_request": action.natural_language_request,
        "normalized_action_json": action.normalized_action_json,
        "state": action.state,
        "execution_mode": action.execution_mode,
        "risk_verdict": action.risk_verdict,
        "risk_score": action.risk_score,
        "reason_codes": list(action.reason_codes) if action.reason_codes else [],
        "policy_version_at_planning": action.policy_version_at_planning,
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "updated_at": action.updated_at.isoformat() if action.updated_at else None,
        "execution_result": (
            {
                "id": str(exec_result.id),
                "provider": exec_result.provider,
                "mode": exec_result.mode,
                "provider_order_id": exec_result.provider_order_id,
                "status": exec_result.status,
                "request_payload": exec_result.request_payload,
                "response_payload": exec_result.response_payload,
                "created_at": (
                    exec_result.created_at.isoformat() if exec_result.created_at else None
                ),
            }
            if exec_result is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# GET /actions/{action_id}/audit
# ---------------------------------------------------------------------------
@router.get(
    "/actions/{action_id}/audit",
    status_code=status.HTTP_200_OK,
    summary="某 action 的审计事件时间线（前端审计重放页面用）",
)
def get_action_audit_events(
    action_id: UUID,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """``GET /api/actions/{action_id}/audit``

    返回该 action 下属于当前用户的全部审计事件，按 ``created_at`` 升序。

    前端 ``AuditReplayPage`` 直接消费 ``{events, trace_id, action_id}``——
    与 ``/api/audit/events?action_id=X`` 是两套接口（前者 action-centric,
    后者 generic 过滤），都强制 user_id 隔离。

    跨用户 / 不存在 action 统一 404。
    """
    # 先校验 action 归属（避免别人的 action 被探测）
    action = db.execute(
        select(AgentAction)
        .where(AgentAction.id == action_id)
        .where(AgentAction.user_id == current_user.id)
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "action not found"},
        )

    events = list(
        db.execute(
            select(AuditEvent)
            .where(AuditEvent.action_id == action_id)
            .where(AuditEvent.user_id == current_user.id)
            .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        )
        .scalars()
        .all()
    )

    return {
        "action_id": str(action_id),
        "trace_id": str(action.trace_id) if action.trace_id else None,
        "events": [AuditEventResponse.from_orm_event(e).model_dump(mode="json") for e in events],
        "count": len(events),
    }


__all__ = ["router"]
