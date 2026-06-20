"""Passport 注册中心相关的 Pydantic 请求 / 响应模型（任务 5.3 / Req 3 / Req 4）。

设计契约
--------
本模块对应 design.md「API 设计 / 护照管理」的请求 / 响应结构，是任务 5.3
路由 (:mod:`app.routers.passports`) 与服务层 (:mod:`app.services.passports`)
之间的边界类型层。

所有模型 ``ConfigDict(extra='forbid')``：

- 防止前端误传额外字段（"无声漂移"）。
- 与 :class:`app.schemas.policy.PolicyDSLv0` 的 ``extra='forbid'`` 形成一致的
  「未知字段拒绝」边界（Req 4 AC8）。

策略来源互斥（核心校验）
------------------------
:class:`PassportCreateRequest` 支持两种策略来源：

1. ``policy``: 直接传入完整 PolicyDSLv0 dict（自定义策略）。
2. ``template_name`` + 可选 ``overrides``: 从内置模板生成（PRD §9.2）。

二者**互斥**——必须给且仅给其中一种。Pydantic 的 :func:`model_validator` 把
互斥约束做成结构性校验，未通过会以 422 VALIDATION_ERROR 形式返回，避免在
服务层再写一遍同名 ``ValueError``。

为何不让服务层也校验
~~~~~~~~~~~~~~~~~~
服务层 :func:`app.services.passports.create_passport` 仍保留对应分支
（兼顾内部直接调用场景，如 demo 种子加载），但走 HTTP 路径时
Pydantic 会先一步触发 422——此时 422 message 会用任务 spec 描述的
「``policy`` 与 ``template_name`` 必须二选一」措辞。

字段命名约定
------------
- API 字段使用 ``policy``（PolicyDSLv0 dict）而非 ``policy_dict``——前端友好；
  服务层把它接收为 ``policy_dict`` 仅是 Python 关键字 ``policy`` 与 dict 字面量
  的命名习惯差异，对外契约统一为 ``policy``。

时间字段
--------
:class:`PassportResponse` 的 ``expires_at`` 来自 :class:`AgentPassport.expires_at`
（``DateTime(timezone=True) NULL``）；活跃护照通常为 ``None``。MVP 不实现自动
过期定时器（Req 3 AC8 留待后续任务），字段先暴露占位。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.capability_envelope import PolicyTemplate


class PassportCreateRequest(BaseModel):
    """``POST /api/passports`` 请求体（design.md「API 设计 / 护照管理」）。

    Attributes
    ----------
    name : str
        Passport 展示名（用户起的 alias，如 ``"my-spot-bot"``）。
    agent_type : str
        代理类型自由字符串（如 ``"trader"`` / ``"researcher"`` /
        ``"dao_treasurer"``）。schema 不限制具体值，留给前端自由扩展。
    api_credential_id : UUID | None
        关联的凭证 ID。
        - 给 → 必须属本人且 state ∈ {READ_ONLY, TRADE_ENABLED}（已验证）；
          创建后 state=ACTIVE。
        - 不给 → 创建后 state=DRAFT，等用户后续补凭证。
    policy : dict | None
        完整 PolicyDSLv0 dict；与 ``template_name`` 互斥。
    template_name : PolicyTemplate | None
        内置模板枚举（``readonly_researcher`` / ``small_spot_executor`` /
        ``dao_treasury_guarded``）；与 ``policy`` 互斥。
    overrides : dict | None
        仅在 ``template_name`` 模式下生效——把模板顶层节级覆盖为新值。
        独立给 ``policy`` 时此字段会被忽略（更严格地说：根 validator 会拦下
        「``policy`` + ``overrides``」组合，认定为参数搭配错误）。

    Notes
    -----
    Pydantic 的 ``extra='forbid'`` 会拒绝任何未声明字段；前端误传
    ``state="ACTIVE"`` 试图越权设置状态会被这层 schema 直接拦下。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=200,
        description="Passport 展示名。",
    )
    agent_type: str = Field(
        min_length=1,
        max_length=100,
        description="代理类型自由字符串。",
    )
    api_credential_id: UUID | None = Field(
        default=None,
        description="关联凭证 ID；不给则创建为 DRAFT 状态。",
    )
    policy: dict[str, Any] | None = Field(
        default=None,
        description="完整 PolicyDSLv0 dict；与 template_name 互斥。",
    )
    template_name: PolicyTemplate | None = Field(
        default=None,
        description="内置模板枚举值；与 policy 互斥。",
    )
    overrides: dict[str, Any] | None = Field(
        default=None,
        description="仅 template_name 模式下生效的顶层节覆盖字典。",
    )

    @model_validator(mode="after")
    def _exactly_one_policy_source(self) -> PassportCreateRequest:
        """``policy`` 与 ``template_name`` 必须二选一。

        - 都给 → 拒绝（无法判断哪个优先，避免歧义）。
        - 都不给 → 拒绝（缺少能力包定义）。
        - 给 ``policy`` 但同时传 ``overrides`` → 拒绝（``overrides`` 仅模板模式有意义）。

        Raises
        ------
        ValueError
            违反互斥规则；Pydantic 会包成 ValidationError →
            FastAPI 转 422。
        """
        has_policy = self.policy is not None
        has_template = self.template_name is not None

        if has_policy and has_template:
            raise ValueError(
                "policy and template_name are mutually exclusive; "
                "provide exactly one"
            )
        if not has_policy and not has_template:
            raise ValueError(
                "either policy or template_name must be provided"
            )
        if has_policy and self.overrides is not None:
            # overrides 仅在模板模式有意义；与显式 policy 一起给会让语义变模糊。
            raise ValueError(
                "overrides is only valid with template_name; "
                "remove overrides or switch to template_name"
            )
        return self


class PassportPolicyUpdateRequest(BaseModel):
    """``PATCH /api/passports/{id}/policy`` 请求体。

    只接受完整 PolicyDSLv0 dict——partial update 在 MVP 不支持，因为
    Policy DSL v0 的语义不天然支持「合并旧 + 新」（数组类字段如
    ``allowed_symbols`` 该追加还是替换？容易踩坑）。前端 UI 应在编辑
    时把整个策略 JSON 提交回来。

    版本号字段不在请求体中——服务层会自动 ``version += 1``，避免客户端
    传错版本造成意料之外的重置。
    """

    model_config = ConfigDict(extra="forbid")

    policy: dict[str, Any] = Field(
        description="完整 PolicyDSLv0 dict。",
    )


class PassportResponse(BaseModel):
    """Passport 的对外摘要（design.md「API 设计 / 护照管理」）。

    所有写端点（POST / PATCH / pause / resume / revoke）都返回这个 shape，
    便于前端复用同一份反序列化逻辑。

    安全说明
    --------
    本响应不携带 ``user_id``——Passport 隐含归属当前 JWT 用户（路由用
    ``get_current_user`` 推导），暴露 user_id 反而可能被用作侧信道枚举。

    ``api_credential_id`` 是 Passport 关联的凭证 ID（不是凭证内容）；
    凭证密钥本身永远不会通过 Passport 端点泄露。

    Attributes
    ----------
    id : UUID
        Passport 主键。
    name, agent_type : str
        创建时传入的展示字段。
    state : str
        当前状态字符串（``PassportState`` 之一）。
    version : int
        策略版本号；每次 PATCH /policy 时 +1（Req 3 AC3）。
    policy : dict
        当前 PolicyDSLv0 dict（已小写归一化的 allowed_symbols）。
    reputation_score : int
        声誉分（Req 24，默认 50）。
    api_credential_id : UUID | None
        关联凭证 ID；DRAFT 状态时通常为 None。
    created_at, updated_at : datetime
        审计时间戳（``DateTime(timezone=True)``）。
    expires_at : datetime | None
        到期时间；MVP 通常为 None。
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    agent_type: str
    state: str
    version: int
    policy: dict[str, Any]
    reputation_score: int
    api_credential_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def from_orm_model(cls, passport: Any) -> PassportResponse:
        """从 :class:`app.models.AgentPassport` ORM 实例组装响应。

        独立 classmethod 而非靠 ``from_attributes``，因为 ORM 的
        ``policy_json`` 列名映射到 API 的 ``policy`` 字段——这是命名约定
        差异，集中在这里转换更清晰。

        Parameters
        ----------
        passport
            ``AgentPassport`` ORM 行；需带有 ``policy_json`` / ``state`` 等列。

        Returns
        -------
        PassportResponse
            序列化安全的响应对象。
        """
        return cls(
            id=passport.id,
            name=passport.name,
            agent_type=passport.agent_type,
            state=passport.state,
            version=passport.version,
            policy=dict(passport.policy_json),  # 拷贝防外部修改污染 ORM
            reputation_score=passport.reputation_score,
            api_credential_id=passport.api_credential_id,
            created_at=passport.created_at,
            updated_at=passport.updated_at,
            expires_at=passport.expires_at,
        )


class PassportListResponse(BaseModel):
    """``GET /api/passports`` 响应体。

    用 ``{"passports": [...]}`` 包裹列表（与 :class:`CredentialListResponse`
    风格一致），便于将来不破坏兼容性地添加分页 / 过滤元数据。
    """

    model_config = ConfigDict(extra="forbid")

    passports: list[PassportResponse]


class TemplateInfoResponse(BaseModel):
    """``GET /api/passports/templates`` 单个模板的元数据。

    与 :func:`app.services.capability_envelope.list_templates` 返回的 dict
    一一对应；用 Pydantic 包装让 OpenAPI 文档自动暴露 schema，便于前端
    用类型生成器（openapi-typescript）拿到精确类型。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="模板枚举字符串值（API 入参）。")
    description: str = Field(description="一句话适用场景与限额说明。")
    policy: dict[str, Any] = Field(description="完整 PolicyDSLv0 dict 预览。")


class PassportTemplatesResponse(BaseModel):
    """``GET /api/passports/templates`` 响应体。

    返回 3 个内置模板的元数据，前端「护照向导」第一步据此渲染卡片。
    """

    model_config = ConfigDict(extra="forbid")

    templates: list[TemplateInfoResponse]


__all__ = [
    "PassportCreateRequest",
    "PassportListResponse",
    "PassportPolicyUpdateRequest",
    "PassportResponse",
    "PassportTemplatesResponse",
    "TemplateInfoResponse",
]
