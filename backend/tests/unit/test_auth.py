"""任务 3 单元测试：JWT 签发、过期校验、无效 token 处理。

覆盖 Req 1 AC4 / AC7：
- JWT 默认过期 ≤ 24 小时
- 过期 / 签名错误 / 结构损坏 → ``InvalidTokenError``（最终在 API 层映射成 401）
"""

from __future__ import annotations

import time
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from jose import jwt as jose_jwt

from app.core.auth import (
    InvalidTokenError,
    TokenPayload,
    create_access_token,
    decode_access_token,
)
from app.core.config import get_settings


def test_create_access_token_round_trip() -> None:
    """签发后能 decode 出原 user_id 与 wallet。"""
    user_id = uuid4()
    wallet = "0xA11CE00000000000000000000000000000000001"

    token = create_access_token(user_id=user_id, wallet=wallet)
    payload = decode_access_token(token)

    assert isinstance(payload, TokenPayload)
    assert UUID(payload.sub) == user_id
    assert payload.wallet == wallet
    # iat / exp 应为合理 Unix 时间戳
    assert payload.exp > payload.iat


def test_token_expires_after_delta() -> None:
    """expires_delta=timedelta(seconds=-1) 模拟立即过期；decode 抛 InvalidTokenError。"""
    token = create_access_token(
        user_id=uuid4(),
        wallet="0xdeadbeef",
        expires_delta=timedelta(seconds=-1),
    )

    with pytest.raises(InvalidTokenError) as exc_info:
        decode_access_token(token)

    assert "expired" in str(exc_info.value).lower()


def test_invalid_signature_raises() -> None:
    """用别的 secret 签发的 token 在解码时抛 InvalidTokenError。"""
    settings = get_settings()
    forged = jose_jwt.encode(
        {
            "sub": str(uuid4()),
            "wallet": "0xforged",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        "totally-different-secret",
        algorithm=settings.JWT_ALGORITHM,
    )

    with pytest.raises(InvalidTokenError):
        decode_access_token(forged)


def test_malformed_token_raises() -> None:
    """非 JWT 字符串 → InvalidTokenError。"""
    with pytest.raises(InvalidTokenError):
        decode_access_token("not-a-jwt-token")

    with pytest.raises(InvalidTokenError):
        decode_access_token("aaa.bbb.ccc")  # 三段但内容无效


def test_default_expire_24h() -> None:
    """不指定 delta 时 ``exp - iat`` 应等于 settings.JWT_EXPIRE_HOURS 小时。"""
    settings = get_settings()
    token = create_access_token(user_id=uuid4(), wallet="0x1")
    payload = decode_access_token(token)

    seconds = payload.exp - payload.iat
    assert seconds == settings.JWT_EXPIRE_HOURS * 3600


def test_token_payload_rejects_extra_fields() -> None:
    """token 负载结构异常（多余字段）也走 InvalidTokenError 分支。

    构造一个签名正确但 payload 比 ``TokenPayload`` schema 多一个字段的 token。
    pydantic 在 ``extra='forbid'`` 下应拒绝它。
    """
    settings = get_settings()
    token = jose_jwt.encode(
        {
            "sub": str(uuid4()),
            "wallet": "0x1",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "unexpected_field": "boom",
        },
        settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token)
