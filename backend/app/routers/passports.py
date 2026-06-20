"""Passport 注册中心路由（任务 5.3 / Req 3 / Req 4）。

实现 design.md 「API 设计 / 护照管理」的 8 个端点：

```
GET    /api/passports/templates       → 200 内置模板列表（不写库；最先注册避免被 /{id} 截胡）
POST   /api/passports                 → 201 创建 Passport（可选关联凭证）
GET    /api/passports                 → 200 当前用户活跃 Passport 列表（过滤 DELETED）
GET    /api/passports/{id}            → 200 单个 Passport 详情
PATCH  /api/passports/{id}/policy     → 200 更新策略（version +1）
POST   /api/passports/{id}/pause      → 200 ACTIVE → PAUSED
POST   /api/passports/{id}/resume     → 200 PAUSED → ACTIVE（PRD §7.1 转换允许）
POST   /api/passports/{id}/revoke     → 200 ACTIVE/PAUSED → REVOKED + 级联取消下属待审批 action
```

所有路由都依赖 :func:`app.core.dependencies.get_current_user` 解析 JWT；
未授权一律 401（Req 1 AC7）。

业务异常由 :mod:`app.core.errors` 注册的 handler 统一映射：

- :class:`PassportNotFoundError`        → 404 NOT_FOUND
- :class:`IllegalStateTransition`       → 409 ILLEGAL_STATE_TRANSITION
- :class:`PassportStateTransitionError` → 409 PASSPORT_STATE_INVALID / CREDENTIAL_NOT_ELIGIBLE
- :class:`InvalidPolicyError`           → 400 POLICY_INVALID
- :class:`CredentialNotFoundError`      → 404 NOT_FOUND（关联凭证不存在）

事务边界：路由层调用服务后 ``db.commit()``，让"业务写 + 审计写 + 级联写"
在同一事务里（Req 11 AC7：审计写失败必须阻止业务转换）。

路由顺序约定
------------
``/templates`` 必须**先于** ``/{passport_id}`` 注册——FastAPI 按声明顺序匹配，
若反转则 ``/templates`` 会被 ``/{passport_id}`` 当作 UUID 路径参数捕获，
返回 422（``"templates"`` 不是合法 UUID）。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, get_db
from app.models import User
from app.schemas.passport import (
    PassportCreateRequest,
    PassportListResponse,
    PassportPolicyUpdateRequest,
    PassportResponse,
    PassportTemplatesResponse,
    TemplateInfoResponse,
)
from app.services.capability_envelope import list_templates
from app.services.passports import (
    create_passport,
    get_passport,
    list_passports,
    pause_passport,
    resume_passport,
    revoke_passport,
    update_passport_policy,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# /templates 必须先于 /{id} 注册——FastAPI 按声明顺序做路径匹配。
# ---------------------------------------------------------------------------
@router.get(
    "/templates",
    response_model=PassportTemplatesResponse,
    status_code=status.HTTP_200_OK,
    summary="返回 3 个内置 Policy 模板的元数据（前端护照向导第一步用）",
)
def list_passport_templates(
    current_user: Annotated[User, Depends(get_current_user)],
) -> PassportTemplatesResponse:
    """``GET /api/passports/templates``

    保留登录依赖以简化 API 边界——「未登录用户不暴露任何 API 表面」是
    PRD §15 的常识默认。模板内容本身不含敏感信息，但要求登录可避免被
    用作匿名探测端点。

    返回 :func:`app.services.capability_envelope.list_templates` 的输出，
    每项含 ``name`` / ``description`` / ``policy``（深拷贝，前端修改不会
    污染服务端常量）。
    """
    # current_user 仅用于鉴权门槛，不参与模板内容生成
    _ = current_user
    items = [TemplateInfoResponse(**t) for t in list_templates()]
    return PassportTemplatesResponse(templates=items)


@router.post(
    "",
    response_model=PassportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建 Passport（policy 与 template_name 二选一；关联已验证凭证→ACTIVE，否则 DRAFT）",
)
def create_new_passport(
    payload: PassportCreateRequest,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PassportResponse:
    """``POST /api/passports``

    请求体的 ``policy`` 与 ``template_name`` 互斥（schema 阶段已校验）；
    服务层再做一次防御性互斥检查，便于内部直接调用场景命中同一规则。

    异常映射：

    - 422: 请求体结构错（schema / 互斥违反）。
    - 400: ``InvalidPolicyError``（policy DSL v0 业务规则失败）。
    - 404: 关联凭证不存在 / 不属本人 / 已软删除。
    - 409: 关联凭证 state 不在 {READ_ONLY, TRADE_ENABLED}。
    """
    passport = create_passport(
        db,
        user_id=current_user.id,
        name=payload.name,
        agent_type=payload.agent_type,
        api_credential_id=payload.api_credential_id,
        policy_dict=payload.policy,
        template_name=payload.template_name,
        overrides=payload.overrides,
    )
    db.commit()
    db.refresh(passport)
    return PassportResponse.from_orm_model(passport)


@router.get(
    "",
    response_model=PassportListResponse,
    status_code=status.HTTP_200_OK,
    summary="列出当前用户的 Passport（过滤 DELETED；REVOKED/EXPIRED 仍可见）",
)
def list_user_passports(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PassportListResponse:
    """``GET /api/passports``

    返回顺序按 ``created_at DESC``。REVOKED / EXPIRED 仍出现在列表里，
    便于审计回顾历史 passport——前端可按 ``state`` 字段做客户端过滤。
    """
    passports = list_passports(db, user_id=current_user.id)
    return PassportListResponse(
        passports=[PassportResponse.from_orm_model(p) for p in passports]
    )


@router.get(
    "/{passport_id}",
    response_model=PassportResponse,
    status_code=status.HTTP_200_OK,
    summary="按 ID 获取 Passport 详情（跨用户访问统一 404）",
)
def get_passport_by_id(
    passport_id: UUID,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PassportResponse:
    """``GET /api/passports/{id}``

    跨用户访问统一返回 404（避免存在性侧信道）。
    """
    passport = get_passport(db, passport_id=passport_id, user_id=current_user.id)
    return PassportResponse.from_orm_model(passport)


@router.patch(
    "/{passport_id}/policy",
    response_model=PassportResponse,
    status_code=status.HTTP_200_OK,
    summary="更新 Passport 策略（version 自动 +1；写 PASSPORT_POLICY_UPDATED 审计）",
)
def update_passport_policy_endpoint(
    passport_id: UUID,
    payload: PassportPolicyUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PassportResponse:
    """``PATCH /api/passports/{id}/policy``

    Req 3 AC3: 每次更新策略 ``version`` 字段递增 1。

    异常映射：

    - 400: ``InvalidPolicyError``（新 policy 不合法）。
    - 404: passport 不存在 / 不属本人。
    - 409: state 不是 ACTIVE 或 PAUSED（DRAFT/REVOKED/EXPIRED/DELETED 都拒绝编辑）。
    """
    passport = update_passport_policy(
        db,
        passport_id=passport_id,
        user_id=current_user.id,
        new_policy_dict=payload.policy,
    )
    db.commit()
    db.refresh(passport)
    return PassportResponse.from_orm_model(passport)


@router.post(
    "/{passport_id}/pause",
    response_model=PassportResponse,
    status_code=status.HTTP_200_OK,
    summary="ACTIVE → PAUSED（写 PASSPORT_PAUSED 审计）",
)
def pause_passport_endpoint(
    passport_id: UUID,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PassportResponse:
    """``POST /api/passports/{id}/pause``

    Req 3 AC4: 暂停期间不接受新的 action 创建（Req 3 AC6 由任务 8 Policy
    Engine / 任务 9 输入归一化器把守）。

    异常：当前 state 不是 ACTIVE → 409 ILLEGAL_STATE_TRANSITION。
    """
    passport = pause_passport(
        db, passport_id=passport_id, user_id=current_user.id
    )
    db.commit()
    db.refresh(passport)
    return PassportResponse.from_orm_model(passport)


@router.post(
    "/{passport_id}/resume",
    response_model=PassportResponse,
    status_code=status.HTTP_200_OK,
    summary="PAUSED → ACTIVE（PRD §7.1 转换允许；写 PASSPORT_RESUMED 审计）",
)
def resume_passport_endpoint(
    passport_id: UUID,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PassportResponse:
    """``POST /api/passports/{id}/resume``

    PRD §7.1 PASSPORT_TRANSITIONS 允许 PAUSED → ACTIVE，PRD §14 审计事件
    列表未显式列出 PASSPORT_RESUMED；任务 5.3 在 :class:`AuditEventType`
    中补齐这枚常量，让该转换有可审计的事件类型。

    异常：当前 state 不是 PAUSED（ACTIVE/REVOKED/DRAFT 等）→ 409。
    """
    passport = resume_passport(
        db, passport_id=passport_id, user_id=current_user.id
    )
    db.commit()
    db.refresh(passport)
    return PassportResponse.from_orm_model(passport)


@router.post(
    "/{passport_id}/revoke",
    response_model=PassportResponse,
    status_code=status.HTTP_200_OK,
    summary="ACTIVE/PAUSED → REVOKED + 级联取消下属待审批 action（写 PASSPORT_REVOKED 审计）",
)
def revoke_passport_endpoint(
    passport_id: UUID,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PassportResponse:
    """``POST /api/passports/{id}/revoke``

    Req 3 AC5: 撤销时取消该护照下所有 ``state=APPROVAL_REQUIRED`` 的 action
    （转 CANCELLED + 写 ACTION_CANCELLED 审计）；REVOKED 后不可恢复
    （Req 3 AC7）。

    异常：

    - 已是 REVOKED → 409（终态再 revoke 走 IllegalStateTransition）。
    - 当前 state 是 EXPIRED / DELETED / DRAFT → 409。
    """
    passport = revoke_passport(
        db, passport_id=passport_id, user_id=current_user.id
    )
    db.commit()
    db.refresh(passport)
    return PassportResponse.from_orm_model(passport)


__all__ = ["router"]
