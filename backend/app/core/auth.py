"""JWT 签发与解码（Req 1 / 任务 3）。

提供：

- :class:`TokenPayload` —— 解码后的 JWT 负载 Pydantic 模型。
- :class:`InvalidTokenError` —— 签名无效 / 过期 / 结构异常时抛出（继承 ``ValueError``）。
- :func:`create_access_token` —— 用 HS256 签发 JWT，默认 24 小时过期。
- :func:`decode_access_token` —— 校验签名/过期，返回 ``TokenPayload``。

设计依据：Req 1 AC4（JWT 过期 ≤ 24h）/ design.md 「认证服务」 / 方法论 §13（权限门槛）。

注意：本模块只做"签发-解码"双向操作；用户存在性校验、Authorization Header 解析、
HTTP 401 响应均在 :mod:`app.core.dependencies` 与 :mod:`app.core.errors` 完成。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from jose import ExpiredSignatureError, JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings


class InvalidTokenError(ValueError):
    """JWT 解码失败（签名无效 / 过期 / 结构损坏）。

    继承 ``ValueError`` 让上层既可精确捕获 ``InvalidTokenError``，
    也可宽松捕获 ``ValueError``。
    """


class TokenPayload(BaseModel):
    """JWT 解码后的负载结构。

    - ``sub`` 使用 OAuth2/JWT 标准字段名存储 ``user_id``（UUID 字符串形式）。
    - ``wallet`` 演示场景下与 user 绑定的钱包地址，便于前端展示。
    - ``iat`` / ``exp`` 为 Unix 秒时间戳（int），与 ``jose`` 默认序列化保持一致。
    """

    model_config = ConfigDict(extra="forbid")

    sub: str = Field(description="用户 UUID 的字符串形式。")
    wallet: str = Field(description="演示模式钱包地址。")
    iat: int = Field(description="签发时间（Unix 秒）。")
    exp: int = Field(description="过期时间（Unix 秒）。")


def create_access_token(
    user_id: UUID,
    wallet: str,
    expires_delta: timedelta | None = None,
) -> str:
    """签发 JWT。

    Parameters
    ----------
    user_id : UUID
        用户主键，将作为 ``sub`` 写入负载。
    wallet : str
        当前会话绑定的钱包地址，写入 ``wallet`` 字段。
    expires_delta : timedelta | None
        过期时长；缺省使用 :attr:`Settings.JWT_EXPIRE_HOURS` 小时（默认 24h）。
        允许传负值，用于测试模拟"立即过期"。

    Returns
    -------
    str
        编码后的 JWT 字符串。
    """
    settings = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(hours=settings.JWT_EXPIRE_HOURS)

    now = datetime.now(UTC)
    expire_at = now + expires_delta
    payload: dict[str, str | int] = {
        "sub": str(user_id),
        "wallet": wallet,
        "iat": int(now.timestamp()),
        "exp": int(expire_at.timestamp()),
    }
    token: str = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token


def decode_access_token(token: str) -> TokenPayload:
    """校验并解码 JWT。

    Raises
    ------
    InvalidTokenError
        - token 已过期
        - 签名无效 / 算法不匹配 / 结构损坏
        - 负载不符合 :class:`TokenPayload` schema
    """
    settings = get_settings()
    try:
        decoded = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except ExpiredSignatureError as exc:
        # 单独区分"过期"以便后续做更精细的反馈；目前与其他失败一并归为 InvalidTokenError
        raise InvalidTokenError("token expired") from exc
    except JWTError as exc:
        raise InvalidTokenError("invalid token") from exc

    try:
        return TokenPayload(**decoded)
    except Exception as exc:  # pydantic ValidationError 等
        raise InvalidTokenError("invalid token payload") from exc


__all__ = [
    "InvalidTokenError",
    "TokenPayload",
    "create_access_token",
    "decode_access_token",
]
