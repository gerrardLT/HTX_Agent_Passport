"""认证路由（Req 1 / 任务 3）。

实现 ``POST /api/auth/demo-login``：

1. 解析（可选的）请求体；缺省走 ``settings.DEMO_WALLET``。
2. 在 ``users`` 表中按 ``primary_wallet`` 懒创建用户（首次 demo 登录自动建账）。
3. 生成 ``trace_id`` 并写入 ``USER_LOGIN`` 审计事件（actor_type=USER）。
4. 用 :func:`app.core.auth.create_access_token` 签发 JWT，返回
   ``{token, user: {id, wallet}}``（与 design.md「API 设计」一致）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import create_access_token
from app.core.config import get_settings
from app.core.dependencies import get_db
from app.models import User
from app.models.enums import AuditEventType
from app.schemas.auth import DemoLoginRequest, DemoLoginResponse, UserResponse
from app.services.audit_writer import ACTOR_TYPE_USER, write_audit_event

router = APIRouter()


def _get_or_create_user(db: Session, wallet: str) -> tuple[User, bool]:
    """按钱包地址获取用户；不存在则创建。

    返回 ``(user, created)``，``created=True`` 表示本次新建。
    """
    existing = db.execute(
        select(User).where(User.primary_wallet == wallet)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    user = User(primary_wallet=wallet)
    db.add(user)
    # flush 让 user.id 在 commit 前就可读，便于审计事件引用
    db.flush()
    return user, True


@router.post(
    "/demo-login",
    response_model=DemoLoginResponse,
    status_code=status.HTTP_200_OK,
    summary="快速登录（懒创建预设钱包用户并签发 JWT）",
)
def demo_login(
    db: Annotated[Session, Depends(get_db)],
    payload: DemoLoginRequest | None = None,
) -> DemoLoginResponse:
    """快速登录端点。

    - 不带 body 时使用 ``settings.DEMO_WALLET`` 作为默认钱包地址。
    - 用户首次登录时自动创建 ``users`` 行；之后再登录复用同一 ``user.id``。
    - 写入一条 ``USER_LOGIN`` 审计事件，``trace_id`` 同时回传给客户端，便于联调。
    """
    settings = get_settings()
    wallet = (payload.wallet if payload and payload.wallet else settings.DEMO_WALLET)

    user, _created = _get_or_create_user(db, wallet)

    # 每次登录都生成独立 trace_id，串联本次请求的所有审计/日志（Req 13 AC2）
    trace_id = uuid.uuid4()

    write_audit_event(
        db,
        event_type=AuditEventType.USER_LOGIN,
        user_id=user.id,
        actor_type=ACTOR_TYPE_USER,
        actor_id=str(user.id),
        trace_id=trace_id,
        event_data={"wallet": wallet, "trace_id": str(trace_id)},
    )

    db.commit()

    token = create_access_token(user_id=user.id, wallet=wallet)
    # primary_wallet 一定非空（_get_or_create_user 总会写入 wallet），用 wallet 兜底取值
    user_wallet = user.primary_wallet or wallet
    return DemoLoginResponse(
        token=token,
        user=UserResponse(id=user.id, wallet=user_wallet),
    )


__all__ = ["router"]
