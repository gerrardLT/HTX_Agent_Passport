"""凭证管理路由（任务 4.2 / Req 2 / Req 15）。

实现 design.md 「API 设计 / 凭证管理」的 4 个端点：

```
POST   /api/credentials/htx           → 201 创建凭证
POST   /api/credentials/{id}/validate → 200 触发验证（推动状态机）
GET    /api/credentials               → 200 列出当前用户活跃凭证
DELETE /api/credentials/{id}          → 200 软删除
```

所有路由都依赖 :func:`app.core.dependencies.get_current_user` 解析 JWT；
未授权一律 401（Req 1 AC7）。

业务异常由 :mod:`app.core.errors` 注册的 handler 统一映射：

- :class:`DuplicateCredentialError`  → 409 DUPLICATE_CREDENTIAL
- :class:`IllegalStateTransition`    → 409 ILLEGAL_STATE_TRANSITION
- :class:`CredentialNotFoundError`   → 404 NOT_FOUND

事务边界：路由层调用服务后 ``db.commit()``，让"业务写 + 审计写"在同一事务里
（Req 11 AC7：审计写失败必须阻止业务转换）。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, get_db
from app.models import User
from app.schemas.credential import (
    CredentialCreateRequest,
    CredentialListResponse,
    CredentialResponse,
    CredentialValidateResponse,
)
from app.services.credentials import (
    create_credential,
    delete_credential,
    list_credentials,
    validate_credential,
)

router = APIRouter()


@router.post(
    "/htx",
    response_model=CredentialResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建一条 HTX API 凭证（加密入库 + 审计）",
)
def create_htx_credential(
    payload: CredentialCreateRequest,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CredentialResponse:
    """``POST /api/credentials/htx``

    请求体的 ``access_key`` / ``secret_key`` 在内存中短暂存活后立即被 AES-256-GCM
    加密（Req 2 AC1）。响应只回传 ``id`` / ``state`` / ``permissions`` 等
    可公开字段——**永不**回传密钥任何形态（Req 2 AC2 / Req 15 AC1）。
    """
    credential = create_credential(
        db,
        user_id=current_user.id,
        label=payload.label,
        access_key=payload.access_key,
        secret_key=payload.secret_key,
    )
    db.commit()
    db.refresh(credential)
    return CredentialResponse.from_orm_model(credential)


@router.post(
    "/{credential_id}/validate",
    response_model=CredentialValidateResponse,
    status_code=status.HTTP_200_OK,
    summary="触发凭证验证（推动 CREATED/INVALID/READ_ONLY/TRADE_ENABLED → VALIDATING → 终态）",
)
def validate_htx_credential(
    credential_id: UUID,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CredentialValidateResponse:
    """``POST /api/credentials/{id}/validate``

    Demo 模式下调用 mock 验证器：返回 read=true / trade=true / withdraw=true，
    服务层硬覆盖 withdraw=false（Req 2 AC4 / Req 15 AC6），最终状态 TRADE_ENABLED。
    异常映射：

    - 不存在 / 不属于本人 → 404
    - 状态不允许转 VALIDATING（如 REVOKED / DELETED）→ 409 ILLEGAL_STATE_TRANSITION
    """
    credential = validate_credential(
        db,
        credential_id=credential_id,
        user_id=current_user.id,
    )
    db.commit()
    db.refresh(credential)
    return CredentialValidateResponse.from_orm_model(credential)


@router.get(
    "",
    response_model=CredentialListResponse,
    status_code=status.HTTP_200_OK,
    summary="列出当前用户的活跃凭证（自动过滤软删除）",
)
def list_htx_credentials(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CredentialListResponse:
    """``GET /api/credentials``

    自动过滤 ``deleted_at IS NULL``（Req 2 AC6）；按 ``created_at DESC`` 排序。
    响应永不包含密钥任何字段。
    """
    credentials = list_credentials(db, user_id=current_user.id)
    return CredentialListResponse(
        credentials=[CredentialResponse.from_orm_model(c) for c in credentials]
    )


@router.delete(
    "/{credential_id}",
    response_model=CredentialResponse,
    status_code=status.HTTP_200_OK,
    summary="软删除凭证（state=DELETED + deleted_at=now()，加密数据保留）",
)
def delete_htx_credential(
    credential_id: UUID,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> CredentialResponse:
    """``DELETE /api/credentials/{id}``

    软删除而非物理删除（Req 2 AC6）；响应包含 ``state="DELETED"`` 与 ``deleted_at``。
    被软删除的凭证后续 GET 不再可见，再次 DELETE 返回 404
    （已被 :func:`_get_owned_credential` 过滤掉）。
    """
    credential = delete_credential(
        db,
        credential_id=credential_id,
        user_id=current_user.id,
    )
    db.commit()
    db.refresh(credential)
    return CredentialResponse.from_orm_model(credential)


__all__ = ["router"]
