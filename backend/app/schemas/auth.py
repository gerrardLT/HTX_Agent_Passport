"""认证相关的 Pydantic 请求/响应模型。

- :class:`DemoLoginRequest` —— ``POST /api/auth/demo-login`` 请求体（演示场景全部可选）。
- :class:`UserResponse` —— 公开的用户信息（不含 email / role 等内部字段）。
- :class:`DemoLoginResponse` —— 登录成功后端返回结构（design.md「API 设计 / 认证」）。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DemoLoginRequest(BaseModel):
    """演示登录请求体。

    Demo 模式下全部字段可选；不传则使用 ``Settings.DEMO_WALLET`` 兜底。
    保留 ``wallet`` 字段是为了便于前端在多账号 demo / 自动化测试时切换身份。
    """

    model_config = ConfigDict(extra="forbid")

    wallet: str | None = Field(
        default=None,
        description="可选的钱包地址覆盖；为空时使用 settings.DEMO_WALLET。",
    )


class UserResponse(BaseModel):
    """对外暴露的用户摘要。"""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    wallet: str


class DemoLoginResponse(BaseModel):
    """登录成功响应：``{ token, user: { id, wallet } }``（design.md「API 设计」）。"""

    model_config = ConfigDict(extra="forbid")

    token: str
    user: UserResponse


__all__ = ["DemoLoginRequest", "DemoLoginResponse", "UserResponse"]
