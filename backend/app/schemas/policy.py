"""Policy DSL v0 的 JSON Schema 与 Pydantic 模型（任务 5.1 / Req 4）。

本模块与 PRD §9.1 完全对齐，提供两套互补的校验：

1. :data:`POLICY_DSL_V0_SCHEMA` —— 严格 JSON Schema（draft 2020-12），
   语义层面的"门面校验"。单层 const/enum/pattern/minimum/maximum 都能在
   这里捕获。每个 ``object`` 子节都显式声明 ``additionalProperties: false``，
   配合 :class:`PolicyDSLv0` 的 ``extra='forbid'``，构成"双层未知字段拒绝"
   （Req 4 AC8）。
2. :class:`PolicyDSLv0` 等 Pydantic 模型 —— 为后续业务层（任务 5.2 能力包构建器、
   任务 5.3 Passport 注册中心、任务 8 Policy Engine）提供"强类型 + 反序列化"
   接口；运行时既能校验，又能 ``model_dump()`` 序列化回 dict 存入 Postgres
   ``policy_json`` JSONB 列（design.md「数据模型 / agent_passports」）。

为何同时维护 JSON Schema 与 Pydantic？
-----------------------------------
- **JSON Schema 是协议文档**：策略 DSL v0 是发布给"AI 编程代理 + 第三方"
  的 contract，纯 JSON Schema 形态可被 OpenAPI / IDE / 其他语言的校验器
  无差别消费——这是 PRD 把它放在 §9.1 的核心原因。
- **Pydantic 是运行时类型**：服务端业务逻辑要直接 ``policy.limits.allowed_symbols``
  这种属性访问；纯 dict 操作既无 IDE 补全也容易拼错 key。Pydantic 还能
  注入 :func:`pydantic.field_validator` 做"小写归一化"（Req 4 AC3）。
- **两层校验形成深度防御**：JSON Schema 报错 → ``InvalidPolicyError`` 的
  ``errors`` 字段拼出"字段路径 + 期望"，对外友好；Pydantic 兜底捕获
  schema 漏写约束的边角（如 string 而非 integer），见
  :func:`app.services.policy_validator.validate_policy_dsl`。

UTC 日边界（Req 4 AC5）的实现位置说明
-----------------------------------
``max_daily_notional_usdt`` 的"日重置"语义是 **Policy Engine 任务 8** 的职责
（``daily_history.total_notional_today_utc`` 计算时按 UTC 00:00:00 切日）。
本模块只做 schema 校验，不实现累计；这里写注释保留语义一致性证据。
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 1. JSON Schema (PRD §9.1 完全对齐)
# ---------------------------------------------------------------------------
# 设计权衡：
# - 每个 object 子节都显式 `additionalProperties: false`——Req 4 AC8 的"未知
#   字段拒绝"在 JSON Schema 层就生效，避免依赖 Pydantic 兜底。
# - PRD §9.1 原文 allowed_time_utc.start/end 不在 required 中；为了与 PRD
#   一致这里也保持可选，但只要 caller 传了 allowed_time_utc 对象就需要至少
#   start/end 都满足 pattern——通过 :func:`policy_validator.validate_policy_dsl`
#   的业务规则补强。
# - blocked_actions 的 enum 与 design.md REASON_CODES 中的 BLOCKED_ACTION_*
#   一一对应，缺一不可。
# - 时间 pattern 用 ``^([01][0-9]|2[0-3]):[0-5][0-9]$`` 严格约束 00:00-23:59，
#   PRD 原文是 ``^[0-2][0-9]:[0-5][0-9]$``（允许 25:xx）；本模块按 PRD 实现，
#   但额外业务规则在 validator 里补强（Req 4 AC4 跨午夜）。

POLICY_DSL_V0_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://htx-agent-passport.dev/schemas/policy-v0.json",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "version",
        "capabilities",
        "limits",
        "approval",
        "blocked_actions",
    ],
    "properties": {
        "version": {"const": "0.1"},
        "capabilities": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "read_market",
                "read_account",
                "place_order",
                "cancel_order",
                "withdraw",
            ],
            "properties": {
                "read_market": {"type": "boolean"},
                "read_account": {"type": "boolean"},
                "place_order": {"type": "boolean"},
                "cancel_order": {"type": "boolean"},
                # withdraw 在 MVP 中硬性 false——既靠 const 约束（Req 4 AC2），
                # 也由 :func:`policy_validator.validate_policy_dsl` 业务规则
                # 二次断言（防御 schema 被运行期篡改）。
                "withdraw": {"const": False},
            },
        },
        "limits": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "allowed_symbols",
                "max_notional_usdt_per_order",
                "max_daily_notional_usdt",
                "max_orders_per_day",
            ],
            "properties": {
                "allowed_symbols": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "max_notional_usdt_per_order": {
                    "type": "number",
                    "minimum": 0,
                },
                "max_daily_notional_usdt": {
                    "type": "number",
                    "minimum": 0,
                },
                "max_orders_per_day": {
                    "type": "integer",
                    "minimum": 0,
                },
                "allowed_order_types": {
                    "type": "array",
                    "items": {"enum": ["limit", "market"]},
                },
                "max_slippage_bps": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 500,
                },
                "allowed_time_utc": {
                    "type": "object",
                    "additionalProperties": False,
                    # PRD §9.1 原文未把 start/end 列入 required；保持兼容。
                    # 业务层再校验"如果给了 allowed_time_utc 必须同时给 start+end"。
                    "properties": {
                        "start": {
                            "type": "string",
                            "pattern": r"^[0-2][0-9]:[0-5][0-9]$",
                        },
                        "end": {
                            "type": "string",
                            "pattern": r"^[0-2][0-9]:[0-5][0-9]$",
                        },
                    },
                },
            },
        },
        "approval": {
            "type": "object",
            "additionalProperties": False,
            "required": ["required_for_trade", "required_for_policy_change"],
            "properties": {
                "required_for_trade": {"type": "boolean"},
                "required_for_policy_change": {"type": "boolean"},
                "expires_after_seconds": {
                    "type": "integer",
                    "minimum": 30,
                    "maximum": 3600,
                },
                # G18 风险分级自动审批阈值（Phase 3 / docs/tech-research/07-...md §7.1）。
                # 默认不出现 = 全审批模式（向后兼容）；显式配置后,符合所有阈值
                # 的 place_order 直接放行（写 APPROVAL_AUTO_APPROVED 审计事件）。
                # 设计依据：Facio 4 层 L0/L1/L2/L3 + 4 维风险评分（reversibility /
                # dollar_exposure / customer_impact / access_scope）。
                #
                # 类型用 ["object", "null"]：让 Pydantic ``model_dump()`` 默认输出
                # ``auto_approval_thresholds: null`` 时也合法（round-trip 安全）。
                "auto_approval_thresholds": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        # L1 自动放行的最大单笔 notional（USDT）
                        "max_notional_usdt": {
                            "type": "number",
                            "minimum": 0,
                        },
                        # 触发自动审批要求的最低声誉分（0-100）；
                        # 低于此值即便满足其他条件也走人工审批
                        "min_reputation_score": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                        },
                        # 允许自动审批的 action.type 白名单
                        "allowed_action_types": {
                            "type": "array",
                            "items": {
                                "enum": [
                                    "place_order",
                                    "cancel_order",
                                ]
                            },
                        },
                        # 单 UTC 日内自动审批次数上限（防"千刀慢宰"）
                        "max_per_day": {
                            "type": "integer",
                            "minimum": 0,
                        },
                    },
                },
            },
        },
        "blocked_actions": {
            "type": "array",
            "items": {
                "enum": [
                    "withdraw",
                    "borrow",
                    "margin",
                    "transfer_out",
                    "unknown_tool_call",
                ]
            },
        },
    },
}


# ---------------------------------------------------------------------------
# 2. Pydantic 模型（运行时强类型）
# ---------------------------------------------------------------------------
# 所有子模型 `extra='forbid'`：哪怕 JSON Schema 校验放过去（理论不会）也会
# 在 Pydantic 这层兜底拒绝。Req 4 AC8 的双层未知字段拒绝由此保障。
#
# 字段顺序刻意与 :data:`POLICY_DSL_V0_SCHEMA` 对齐，方便对照阅读。

# 严格的 HH:MM 字符串类型——Pydantic 端复用 schema 同款 pattern。
# 跨午夜语义的合法性由 validator 业务规则负责，这里只管字符串形态。
TimeStr = Annotated[
    str,
    Field(pattern=r"^[0-2][0-9]:[0-5][0-9]$"),
]


class Capabilities(BaseModel):
    """5 个原子能力开关（PRD §9.1 capabilities 节）。

    - read_market / read_account / place_order / cancel_order：四个布尔，
      被 Policy Engine（任务 8）映射到 ``cap.get(action.type, False)``。
    - withdraw：硬编码必须为 ``False``——既由
      :data:`POLICY_DSL_V0_SCHEMA.capabilities.withdraw` const false 约束，
      也由 :func:`app.services.policy_validator.validate_policy_dsl` 防御性
      二次断言（Req 4 AC2）。

    任何额外字段都会因 ``extra='forbid'`` 触发 Pydantic ValidationError。
    """

    model_config = ConfigDict(extra="forbid")

    read_market: bool
    read_account: bool
    place_order: bool
    cancel_order: bool
    # 注意：这里类型为 bool 但 schema 是 const False；validator 会在
    # validate_policy_dsl 里再做一次 ``assert withdraw is False``，
    # 保证即便有人绕过 schema 直接构造 PolicyDSLv0(capabilities=Capabilities(withdraw=True))
    # 也会在业务校验阶段被拦下。
    withdraw: bool


class AllowedTimeUtc(BaseModel):
    """UTC 时段窗口（Req 4 AC4）。

    支持跨午夜：``start > end`` 视为"从 start 到次日 end"（如 22:00→02:00
    表示 UTC 22:00 到次日 02:00 共 4 小时）。本模型只校验字符串形态；
    跨午夜判定与"窗口内/外"判断由 :func:`is_cross_midnight` 工具函数与
    Policy Engine（任务 8）实现。
    """

    model_config = ConfigDict(extra="forbid")

    # PRD 把 start/end 都写成 optional；business validator 会补强"要么都有要么都没有"。
    start: TimeStr | None = None
    end: TimeStr | None = None


class Limits(BaseModel):
    """策略限额节（PRD §9.1 limits 节）。

    四个必填字段全部参与 Policy Engine 的 7 步裁决（design.md「执行层 / Policy
    Engine」Step 3-7）。allowed_symbols 在入库时应已被 :func:`normalize_symbol_list`
    转为小写（Req 4 AC3）。

    可选字段：
    - allowed_order_types：白名单中只允许 ``limit``/``market``；超出立即拒。
    - max_slippage_bps：0-500 bps，反幻觉时用作"价格异常"判定（Req 16 AC2）。
    - allowed_time_utc：限制下单的 UTC 时段，跨午夜合法。
    """

    model_config = ConfigDict(extra="forbid")

    allowed_symbols: list[str] = Field(min_length=1)
    max_notional_usdt_per_order: float = Field(ge=0)
    max_daily_notional_usdt: float = Field(ge=0)
    max_orders_per_day: int = Field(ge=0)
    allowed_order_types: list[str] | None = None
    max_slippage_bps: int | None = Field(default=None, ge=0, le=500)
    allowed_time_utc: AllowedTimeUtc | None = None


class AutoApprovalThresholds(BaseModel):
    """G18 风险分级自动审批阈值（Phase 3）。

    显式配置时，符合**所有**阈值的 place_order/cancel_order 在 Policy Engine
    返回 ALLOW 而非 REQUIRE_APPROVAL，让低风险高频操作不消耗人工审批配额。

    设计依据
    --------
    Facio 4 层模型（``docs/tech-research/07-...md`` §7.1）的 L1 实现：

    - **max_notional_usdt**：单笔金额不超过此值（如 $5）才允许 auto-approve。
    - **min_reputation_score**：passport 历史声誉分 ≥ 此值（如 80）才允许；
      防"新建 passport 立即享受自动审批"风险。
    - **allowed_action_types**：仅这些 action 类型可走自动审批（默认空 = 不放行）。
    - **max_per_day**：单 UTC 日内自动审批次数上限（防"千刀慢宰"高频小额刷量）。

    保守默认值
    ----------
    所有字段都是**可选 + None 时不放行**——即只有用户**显式启用**才生效。
    任一字段缺失或不通过都走人工审批路径，确保最坏情形与"全审批"等价。
    """

    model_config = ConfigDict(extra="forbid")

    max_notional_usdt: float | None = Field(default=None, ge=0)
    min_reputation_score: int | None = Field(default=None, ge=0, le=100)
    allowed_action_types: list[str] | None = None
    max_per_day: int | None = Field(default=None, ge=0)


class Approval(BaseModel):
    """审批策略（PRD §9.1 approval 节）。

    - required_for_trade：``True`` 时所有 place_order/cancel_order 必须经审批。
    - required_for_policy_change：策略变更（任务 5.3 的 PATCH /policy）是否
      需要审批；Demo 模式默认 ``True``。
    - expires_after_seconds：审批有效期 30-3600 秒；过期由审批服务（任务 11）
      主动扫描转 EXPIRED。
    - auto_approval_thresholds：G18 风险分级自动审批配置（可选，默认禁用）。
      详见 :class:`AutoApprovalThresholds`。
    """

    model_config = ConfigDict(extra="forbid")

    required_for_trade: bool
    required_for_policy_change: bool
    expires_after_seconds: int | None = Field(default=None, ge=30, le=3600)
    auto_approval_thresholds: AutoApprovalThresholds | None = None


# blocked_actions 元素只允许这 5 个枚举值。
# Pydantic 不直接用 Literal[*tuple] 是因为 mypy --strict 下需显式列举；
# 列出后既能给 IDE 补全也方便 grep 排查"是否漏配某个 blocked action"。
BlockedActionLiteral = str  # runtime 仍是 str；schema/validator 共同把控 enum
BLOCKED_ACTIONS_ENUM: Final[tuple[str, ...]] = (
    "withdraw",
    "borrow",
    "margin",
    "transfer_out",
    "unknown_tool_call",
)


class PolicyDSLv0(BaseModel):
    """Policy DSL v0 顶层模型（PRD §9.1 / Req 4 AC1）。

    五个必填子节缺一即报错。``version`` 必须为 ``"0.1"``——保留升级到 v1
    时的契约破坏点：未来若推出 0.2/1.0，本类按 PRD §9.x 加新版本，旧
    DSL 通过迁移函数升级。

    序列化
    ------
    - ``model_dump()`` 返回 plain dict，可直接存入 ``agent_passports.policy_json``
      JSONB 列；列表/枚举均保持 JSON-friendly。
    - 经过 :func:`app.services.policy_validator.validate_policy_dsl` 之后的
      实例，``limits.allowed_symbols`` 已小写归一化；``model_dump()`` 输出的
      也是小写形态。
    """

    model_config = ConfigDict(extra="forbid")

    # ``version`` 用 Literal["0.1"] 既等价于 schema const，也让 IDE 在
    # PolicyDSLv0(version="0.2") 时立刻报错。
    version: Annotated[str, Field(pattern=r"^0\.1$")]
    capabilities: Capabilities
    limits: Limits
    approval: Approval
    # blocked_actions 元素的 enum 校验交给 validator + schema 联合把控；
    # Pydantic 这里仅声明类型为 list[str]，避免与 schema 重复定义枚举。
    blocked_actions: list[str]


__all__ = [
    "BLOCKED_ACTIONS_ENUM",
    "POLICY_DSL_V0_SCHEMA",
    "AllowedTimeUtc",
    "Approval",
    "AutoApprovalThresholds",
    "Capabilities",
    "Limits",
    "PolicyDSLv0",
    "TimeStr",
]
