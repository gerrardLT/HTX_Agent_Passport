"""FastAPI 共享依赖。

本任务（3）提供两枚依赖：

- :func:`get_db` —— 暴露 SQLAlchemy ``Session``，由 :mod:`app.core.database` 工厂创建。
- :func:`get_current_user` —— 解析 ``Authorization: Bearer <jwt>``，校验后返回 ORM ``User``。

未授权一律抛 ``HTTPException(401)`` + ``detail`` 为业务结构 dict，
最终由 :mod:`app.core.errors` 把它转成统一错误响应。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import InvalidTokenError, decode_access_token
from app.core.database import get_db_session
from app.models import User


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个数据库 ``Session``。

    简单代理到 :func:`app.core.database.get_db_session`，
    保持 router 仅依赖 ``app.core.dependencies``，便于测试覆盖。
    """
    yield from get_db_session()


def _unauthorized(message: str) -> HTTPException:
    """构造 401 异常，``detail`` 用结构化 dict 让 errors handler 提取业务 code。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """解析 Bearer token 并加载 :class:`User`。

    失败场景统一 401（Req 1 AC7）：

    - 缺失 ``Authorization`` 头
    - 头格式错误（非 ``Bearer <token>``）
    - JWT 过期 / 签名错 / 结构错（由 :func:`decode_access_token` 抛 :class:`InvalidTokenError`）
    - ``sub`` 不是合法 UUID
    - 数据库中找不到对应用户（账号被删 / token 已废）
    """
    if not authorization:
        raise _unauthorized("missing Authorization header")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise _unauthorized("invalid Authorization header; expected 'Bearer <token>'")
    token = parts[1]

    try:
        payload = decode_access_token(token)
    except InvalidTokenError as exc:
        # 让 errors handler 看到 detail 结构，统一封装
        raise _unauthorized(str(exc)) from exc

    try:
        user_uuid = UUID(payload.sub)
    except (ValueError, AttributeError) as exc:
        raise _unauthorized("invalid user id in token") from exc

    user = db.get(User, user_uuid)
    if user is None:
        raise _unauthorized("user not found")
    return user


__all__ = ["get_current_user", "get_db"]
