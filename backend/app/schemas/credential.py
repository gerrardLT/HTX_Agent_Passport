"""凭证管理相关的 Pydantic 请求 / 响应模型（任务 4.2 / Req 2 / Req 15）。

## 安全契约

所有响应模型 **绝不包含 access_key / secret_key 任何字段**——即使是哈希形态：

- 请求体里出现明文密钥，仅在内存中短暂存活后立即被 :class:`app.core.vault.CredentialVault`
  加密入库（Req 2 AC1 / Req 15 AC1）。
- 响应体只回传 ``id`` / ``provider`` / ``label`` / ``state`` / ``permissions`` /
  时间戳等 **可公开** 字段。``permissions`` 通过单独的 dict 形态暴露，便于前端
  展示「该凭证当前能做什么」（read / trade / withdraw 三态布尔），与
  ``permission_read`` / ``permission_trade`` / ``permission_withdraw`` 数据库字段一一对应。

## 与 design.md 的对齐

```
POST /api/credentials/htx
  Request: { label, access_key, secret_key }
  Response 201: { id, provider, label, state, permissions, created_at, ... }

POST /api/credentials/{id}/validate
  Response 200: { id, state, permissions }

GET /api/credentials
  Response 200: { credentials: [...] }

DELETE /api/credentials/{id}
  Response 200: { id, state: "DELETED" }
```

DELETE 的轻量 ``{id, state}`` 响应也走 :class:`CredentialResponse` 但 deleted_at 字段会被填充。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CredentialCreateRequest(BaseModel):
    """``POST /api/credentials/htx`` 请求体（design.md「API 设计 / 凭证管理」）。

    ``extra='forbid'`` 防止前端误传额外字段（如 ``permission_withdraw=true``
    试图绕过 MVP 限制——Req 2 AC4 / Req 15 AC6）。

    Attributes
    ----------
    label : str
        用户给凭证起的展示名称（例如 ``"my-htx-readonly"``）。可重复。
    access_key : str
        HTX API access key 明文。**仅用于即时加密入库**，绝不直接存储。
    secret_key : str
        HTX API secret key 明文。**仅用于即时加密入库**，绝不直接存储。
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=200, description="凭证展示名称。")
    access_key: str = Field(
        min_length=1,
        max_length=512,
        description="HTX access key 明文；仅用于一次性加密。",
    )
    secret_key: str = Field(
        min_length=1,
        max_length=512,
        description="HTX secret key 明文；仅用于一次性加密。",
    )


class CredentialResponse(BaseModel):
    """对外暴露的凭证摘要。

    **绝不**携带 access_key / secret_key（明文 / 密文 / 哈希均不暴露——
    Req 2 AC2 / Req 2 AC7 / Req 15 AC1）。``permissions`` 字段以 dict 形态暴露，
    便于前端按 capability 渲染（"该凭证可只读 / 可下单 / 不可提现"）。

    Attributes
    ----------
    id : UUID
        凭证主键。
    provider : str
        交易所标识，目前固定 ``"HTX"``。
    label : str
        用户起的展示名称。
    state : str
        当前状态字符串（``CredentialState`` 之一）：
        CREATED / VALIDATING / READ_ONLY / TRADE_ENABLED / INVALID / REVOKED / DELETED。
    permissions : dict[str, bool]
        三个 capability 标记：``{"read": ..., "trade": ..., "withdraw": ...}``。
        ``withdraw`` MVP 阶段恒为 ``false``（Req 2 AC4 / Req 15 AC6）。
    created_at : datetime
        创建时间（TIMESTAMPTZ）。
    last_validated_at : datetime | None
        最近一次成功 / 失败的验证时间；尚未验证为 ``None``。
    deleted_at : datetime | None
        软删除时间；活跃凭证为 ``None``，软删除后非空（Req 2 AC6）。
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    provider: str
    label: str
    state: str
    permissions: dict[str, bool]
    created_at: datetime
    last_validated_at: datetime | None = None
    deleted_at: datetime | None = None

    @classmethod
    def from_orm_model(cls, credential: Any) -> CredentialResponse:
        """从 :class:`app.models.ApiCredential` ORM 实例组装响应。

        独立的 classmethod 而非依赖 pydantic 的 ``from_attributes``，因为
        ``permissions`` 需要把三个独立 boolean 列合并成 dict。

        Parameters
        ----------
        credential
            ``ApiCredential`` ORM 行；需带有 ``permission_read`` /
            ``permission_trade`` / ``permission_withdraw`` 等列。

        Returns
        -------
        CredentialResponse
            序列化安全的响应对象。
        """
        return cls(
            id=credential.id,
            provider=credential.provider,
            label=credential.label,
            state=credential.state,
            permissions={
                "read": bool(credential.permission_read),
                "trade": bool(credential.permission_trade),
                "withdraw": bool(credential.permission_withdraw),
            },
            created_at=credential.created_at,
            last_validated_at=credential.last_validated_at,
            deleted_at=credential.deleted_at,
        )


class CredentialValidateResponse(BaseModel):
    """``POST /api/credentials/{id}/validate`` 响应体（design.md「API 设计」）。

    精简版的 :class:`CredentialResponse`，仅返回前端审视"验证结果"所需字段。
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    state: str
    permissions: dict[str, bool]

    @classmethod
    def from_orm_model(cls, credential: Any) -> CredentialValidateResponse:
        return cls(
            id=credential.id,
            state=credential.state,
            permissions={
                "read": bool(credential.permission_read),
                "trade": bool(credential.permission_trade),
                "withdraw": bool(credential.permission_withdraw),
            },
        )


class CredentialListResponse(BaseModel):
    """``GET /api/credentials`` 响应体。

    用 ``{"credentials": [...]}`` 包裹列表（design.md「API 设计」），
    便于将来不破坏兼容性地添加分页 / 过滤元数据。
    """

    model_config = ConfigDict(extra="forbid")

    credentials: list[CredentialResponse]


__all__ = [
    "CredentialCreateRequest",
    "CredentialListResponse",
    "CredentialResponse",
    "CredentialValidateResponse",
]
