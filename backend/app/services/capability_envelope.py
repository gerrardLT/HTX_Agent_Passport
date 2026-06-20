"""能力包构建器与默认模板（任务 5.2 / Req 4 / Property 4）。

本模块实现 PRD §9.2 的三个内置 Policy 模板，以及一个轻量构建器
:func:`build_policy_from_template`，让前端「护照向导」可一步从模板生成
:class:`PolicyDSLv0` 实例，并对自定义字段做严格的 schema + 业务规则校验。

设计动机
--------
- **方法论 §6 能力包**：每个 Passport 等价于一次 Capability Envelope；
  模板就是「在常见场景下预先打包好的能力包」，让用户「先选模板再微调」
  而不是从空白 schema 起手——这与方法论「显式化能力包内容」一致。
- **PRD §9.2**：内置三档（只读研究 / 小额现货 / DAO 司库）覆盖典型用户群，
  数值与 §17 demo seed（small_spot_executor: 20 / 100）严格对齐；任务 19
  种子加载器直接 ``build_policy_from_template(SMALL_SPOT_EXECUTOR)``。
- **Property 4 能力包封闭性**：所有 ALLOW 裁决必须落在 capabilities 中
  声明为 true 的 action_type 上。本任务先实现纯函数
  :func:`is_action_type_allowed_by_capabilities`，让任务 8 Policy Engine
  Step 2 能直接复用——这条 Property 用 PBT 在本测试里就锁住，避免引擎实现
  时再绕一圈。

模板自检（启动期 sanity check）
------------------------------
模块加载时立即对三份模板字典各跑一次 :func:`validate_policy_dsl`，把
「模板里硬编码了一个不合法 DSL」这类回归挡在 import 时就报错。生产环境
import 一次后就不会再校验，开销可忽略。

线程安全与不变性
----------------
:data:`TEMPLATES` 字典是模块级常量；构建器使用 ``copy.deepcopy`` 复制后
再合并 overrides，外部对返回的 PolicyDSLv0 任意修改都不会污染模板源。
"""

from __future__ import annotations

import copy
from enum import Enum
from typing import Any, Final

from app.schemas.policy import Capabilities, PolicyDSLv0
from app.services.policy_validator import (
    InvalidPolicyError,
    validate_policy_dsl,
)


# ---------------------------------------------------------------------------
# 1. 模板枚举
# ---------------------------------------------------------------------------
class PolicyTemplate(str, Enum):
    """三档内置模板的稳定标识符（PRD §9.2）。

    继承 ``str`` + ``Enum`` 让 Pydantic、SQLAlchemy 与前端 JSON
    序列化路径都能直接把它当字符串处理（``json.dumps(template) → "small_spot_executor"``），
    省去自定义 encoder。

    取值与 PRD §17 demo seed.passport.policy_template 字段一致；任务 19 种子
    加载器与任务 5.3 Passport 注册中心创建路由都用这套字符串作为 API 入参。
    """

    READONLY_RESEARCHER = "readonly_researcher"
    SMALL_SPOT_EXECUTOR = "small_spot_executor"
    DAO_TREASURY_GUARDED = "dao_treasury_guarded"


# ---------------------------------------------------------------------------
# 2. 三档模板（PRD §9.2）
# ---------------------------------------------------------------------------
# 每个模板都是「一份完整、可直接通过 validate_policy_dsl 的 PolicyDSLv0 dict」。
# 与 PRD §9.2 YAML 表达式相比，本模块把「未在 §9.2 显式列出但 schema 必填」
# 的字段（如 limits.allowed_symbols / max_orders_per_day / blocked_actions）
# 也补齐为合理默认值——否则模板自身都过不了 schema 校验。
#
# 数值锚点（必须保持不变，会被任务 19 种子数据引用）：
# - small_spot_executor: max_notional=20, daily=100  ← PRD §17 demo seed
# - dao_treasury_guarded: max_notional=50, daily=200 ← PRD §9.2
# - readonly_researcher: 全部限额=0（不下单）       ← PRD §9.2 仅声明 capabilities

# 防御性 blocked_actions：所有写操作类型都列上，即便 capabilities 已经禁用
# 它们；这是「双闸门」——能力包关一道、blocked_actions 再关一道，避免
# 模型幻觉绕过 capability 检查时（理论不该发生，但属于深度防御）。
_DEFAULT_BLOCKED_ACTIONS: Final[list[str]] = [
    "withdraw",
    "borrow",
    "margin",
    "transfer_out",
]


TEMPLATE_READONLY_RESEARCHER: Final[dict[str, Any]] = {
    "version": "0.1",
    "capabilities": {
        "read_market": True,
        "read_account": False,
        "place_order": False,
        "cancel_order": False,
        "withdraw": False,
    },
    "limits": {
        # 占位 symbol，用户必须在向导里覆盖为实际感兴趣的交易对。
        # 选 btcusdt 是因为它是 HTX 流动性最深的 pair，对「研究」场景默认安全。
        "allowed_symbols": ["btcusdt"],
        # 三个写操作限额全为 0：即便用户后续误把 place_order 改 true，
        # max_notional=0 仍能在 Policy Engine Step 4 拦下任何下单尝试。
        "max_notional_usdt_per_order": 0,
        "max_daily_notional_usdt": 0,
        "max_orders_per_day": 0,
    },
    "approval": {
        "required_for_trade": True,
        "required_for_policy_change": True,
        "expires_after_seconds": 300,
    },
    "blocked_actions": list(_DEFAULT_BLOCKED_ACTIONS),
}


TEMPLATE_SMALL_SPOT_EXECUTOR: Final[dict[str, Any]] = {
    "version": "0.1",
    "capabilities": {
        "read_market": True,
        "read_account": True,
        "place_order": True,
        "cancel_order": True,
        "withdraw": False,
    },
    "limits": {
        # PRD §17 demo seed: ["btcusdt", "ethusdt"]
        "allowed_symbols": ["btcusdt", "ethusdt"],
        # PRD §9.2 / §17: 单笔 20 USDT、每日 100 USDT
        "max_notional_usdt_per_order": 20,
        "max_daily_notional_usdt": 100,
        # PRD §9.2 未指定；选 10 单 / 日为「小额现货」常识默认（约半小时一单）。
        "max_orders_per_day": 10,
    },
    "approval": {
        "required_for_trade": True,
        "required_for_policy_change": True,
        "expires_after_seconds": 300,
    },
    "blocked_actions": list(_DEFAULT_BLOCKED_ACTIONS),
}


TEMPLATE_DAO_TREASURY_GUARDED: Final[dict[str, Any]] = {
    "version": "0.1",
    "capabilities": {
        "read_market": True,
        "read_account": True,
        "place_order": True,
        "cancel_order": True,
        "withdraw": False,
    },
    "limits": {
        # 司库场景默认双 pair；用户通常会替换成自己持仓的代币对。
        "allowed_symbols": ["btcusdt", "ethusdt"],
        # PRD §9.2: 单笔 50 USDT、每日 200 USDT
        "max_notional_usdt_per_order": 50,
        "max_daily_notional_usdt": 200,
        # PRD §9.2 未指定；司库场景偏「少而精」，5 单 / 日。
        "max_orders_per_day": 5,
    },
    "approval": {
        "required_for_trade": True,
        "required_for_policy_change": True,
        "expires_after_seconds": 300,
    },
    "blocked_actions": list(_DEFAULT_BLOCKED_ACTIONS),
}


# 模板注册表：枚举 → dict。前端 / API 路由通过 PolicyTemplate 值反查模板。
TEMPLATES: Final[dict[PolicyTemplate, dict[str, Any]]] = {
    PolicyTemplate.READONLY_RESEARCHER: TEMPLATE_READONLY_RESEARCHER,
    PolicyTemplate.SMALL_SPOT_EXECUTOR: TEMPLATE_SMALL_SPOT_EXECUTOR,
    PolicyTemplate.DAO_TREASURY_GUARDED: TEMPLATE_DAO_TREASURY_GUARDED,
}


# 简短中文描述：前端模板选择卡片显示。保持「一句话定位」。
TEMPLATE_DESCRIPTIONS: Final[dict[PolicyTemplate, str]] = {
    PolicyTemplate.READONLY_RESEARCHER: "只读研究：可看不可下单，适合行情分析与策略调研。",
    PolicyTemplate.SMALL_SPOT_EXECUTOR: "小额现货：单笔 20 USDT / 日累计 100 USDT，需审批。",
    PolicyTemplate.DAO_TREASURY_GUARDED: "DAO 司库：单笔 50 USDT / 日累计 200 USDT，需审批。",
}


# ---------------------------------------------------------------------------
# 3. 启动期模板自检
# ---------------------------------------------------------------------------
# 在模块加载时立刻对每份模板跑一次 validate_policy_dsl；
# 任何「模板字典里把 withdraw 写成 true」「max_notional 给负数」之类的回归
# 都会让 import 阶段就报错，避免运行期才发现。
#
# 这里特意吃掉返回值——只在意「会不会抛 InvalidPolicyError」。
def _self_check_templates() -> None:
    """模块加载期对所有模板做一次合法性自检。

    Raises
    ------
    InvalidPolicyError
        任一模板字典不通过 :func:`validate_policy_dsl` 时抛出，让 import
        立刻失败，把回归挡在源头。
    """
    for tpl_id, tpl_dict in TEMPLATES.items():
        try:
            # validate 会就地把 allowed_symbols 归一化；模板已是小写所以幂等。
            # 用 deepcopy 防止 validator 修改模板源（虽然当前实现只动 list 元素，
            # 但显式深拷贝更安全）。
            validate_policy_dsl(copy.deepcopy(tpl_dict))
        except InvalidPolicyError as exc:  # pragma: no cover - 启动期自检
            # 这里 reraise 而不是 logger.error 是有意为之：
            # 模板非法属于「程序员错误」（PRD §9.2 数值与 schema 不一致），
            # 应在测试 / CI 阶段被立刻发现，不应让进程带着坏数据继续启动。
            raise InvalidPolicyError(
                f"built-in template {tpl_id.value!r} failed self-check: {exc}",
                errors=exc.errors,
            ) from exc


_self_check_templates()


# ---------------------------------------------------------------------------
# 4. 构建器
# ---------------------------------------------------------------------------
def build_policy_from_template(
    template: PolicyTemplate,
    overrides: dict[str, Any] | None = None,
) -> PolicyDSLv0:
    """从内置模板构建合法的 :class:`PolicyDSLv0` 实例。

    流程
    ----
    1. 用 ``copy.deepcopy`` 取出模板副本，防止外部修改污染源数据。
    2. 把 ``overrides`` 里的顶层 key（version / capabilities / limits /
       approval / blocked_actions）整体替换到副本上——这是**浅合并**：
       要替整个 ``limits`` 节就传完整 ``limits`` dict。需要细粒度合并的
       场景（例如「我只改 max_notional」）由调用方自己先合并好再传入，
       本函数不做嵌套 merge——避免「合并语义」成为隐式契约。
    3. 调 :func:`validate_policy_dsl` 校验合并结果，任何非法（withdraw=True、
       超限、未知字段等）都会抛 :class:`InvalidPolicyError`。
    4. 返回校验通过的强类型实例（``allowed_symbols`` 已小写归一化）。

    Parameters
    ----------
    template : PolicyTemplate
        三档枚举之一；非枚举值会因 dict 查不到而 KeyError。
    overrides : dict[str, Any] | None, default None
        顶层节级别的覆盖字典；只有 5 个顶层 key 被识别，未知顶层 key 在
        校验阶段被 schema 的 ``additionalProperties: false`` 拦截。

    Returns
    -------
    PolicyDSLv0
        合法策略实例，可直接 ``model_dump()`` 入库。

    Raises
    ------
    InvalidPolicyError
        合并后的策略字典未通过 schema 或业务规则校验。

    Examples
    --------
    >>> p = build_policy_from_template(PolicyTemplate.SMALL_SPOT_EXECUTOR)
    >>> p.limits.max_notional_usdt_per_order
    20.0
    >>> # 自定义 symbol 列表（替换整个 limits 节）
    >>> p2 = build_policy_from_template(
    ...     PolicyTemplate.SMALL_SPOT_EXECUTOR,
    ...     overrides={"limits": {
    ...         "allowed_symbols": ["solusdt"],
    ...         "max_notional_usdt_per_order": 10,
    ...         "max_daily_notional_usdt": 30,
    ...         "max_orders_per_day": 3,
    ...     }},
    ... )
    >>> p2.limits.allowed_symbols
    ['solusdt']
    """
    if template not in TEMPLATES:
        # 枚举类型理论上保证只有 3 个值；这里防御性地处理「有人塞了字符串」的情况。
        raise InvalidPolicyError(
            f"unknown template: {template!r}",
            errors=[
                {
                    "path": "<template>",
                    "message": f"{template!r} not in {[t.value for t in PolicyTemplate]}",
                    "validator": "enum",
                }
            ],
        )

    merged: dict[str, Any] = copy.deepcopy(TEMPLATES[template])
    if overrides:
        for key, value in overrides.items():
            # 浅合并：直接整节替换。合并嵌套 dict 是调用方的责任。
            merged[key] = copy.deepcopy(value)

    # 校验 + 归一化 + 强类型转换；任何非法都抛 InvalidPolicyError。
    return validate_policy_dsl(merged)


def list_templates() -> list[dict[str, Any]]:
    """返回三档模板的元数据，供前端「护照向导」第一步展示。

    每项包含三个字段：

    - ``name``：枚举字符串值（如 ``"small_spot_executor"``），用作 API 入参；
    - ``description``：简短中文一句话，描述适用场景与关键限额；
    - ``policy``：完整 PolicyDSLv0 dict，前端可直接拿来预览或当作初始草稿。

    Returns
    -------
    list[dict[str, Any]]
        固定 3 项，顺序：``readonly_researcher`` → ``small_spot_executor`` →
        ``dao_treasury_guarded``（与 :class:`PolicyTemplate` 枚举声明一致）。

    Notes
    -----
    `policy` 字段是 deepcopy，前端 / 调用方修改它不会污染模块级常量。
    返回顺序刻意按「权限从小到大」排列：研究 → 小额 → 司库，引导用户
    「先选最严格再放宽」，符合 deny→ask→allow 的安全心智。
    """
    return [
        {
            "name": tpl.value,
            "description": TEMPLATE_DESCRIPTIONS[tpl],
            "policy": copy.deepcopy(TEMPLATES[tpl]),
        }
        for tpl in PolicyTemplate
    ]


# ---------------------------------------------------------------------------
# 5. 能力包封闭检查（Property 4 / Req 4 AC7）
# ---------------------------------------------------------------------------
# 任务 8 Policy Engine Step 2 会调用本函数把 ActionPlan.action.type 与
# passport.capabilities 比对；本任务先把它实现到位并用 PBT 锁住语义，
# 让 Policy Engine 实现时直接复用而不必重写。
#
# 设计权衡：把映射表做成模块级常量便于测试也便于 grep 排查
# 「哪些 action_type 算可被能力包许可」。

# action_type → capabilities 字段名 的固定映射。
# `no_op` 不需要任何 capability（它本身就是「什么都不做」）；
# 写操作 (place_order/cancel_order) 与读操作 (read_market/read_account)
# 一一对应到同名 capabilities 字段。
_ACTION_TYPE_TO_CAPABILITY: Final[dict[str, str]] = {
    "read_market": "read_market",
    "read_account": "read_account",
    "place_order": "place_order",
    "cancel_order": "cancel_order",
}


def is_action_type_allowed_by_capabilities(
    action_type: str,
    capabilities: Capabilities,
) -> bool:
    """判断 action_type 是否被 capabilities 声明为允许（Property 4）。

    规则
    ----
    1. ``action_type == "no_op"`` → 永远允许（语义：什么都不做，无需任何能力）。
    2. ``action_type ∈ {read_market, read_account, place_order, cancel_order}``
       → 当且仅当 capabilities 中**对应同名字段**为 ``True`` 时返回 True。
    3. 其他（``withdraw`` / 任意未知字符串）→ 返回 False。
       - 注：``withdraw`` 在本系统里**永远不允许**——schema 已硬约束
         ``capabilities.withdraw=False``，本函数也不为它建立映射，因此即便
         有人构造 ``Capabilities(withdraw=True)`` 也无法通过这条路径放行。

    Parameters
    ----------
    action_type : str
        ActionPlan.action.type 字段值；从 PRD §10.1 schema 限定为
        ``read_market`` / ``read_account`` / ``place_order`` / ``cancel_order`` /
        ``no_op``，但本函数也接受任意字符串以便防御性返回 False。
    capabilities : Capabilities
        Passport.policy_json.capabilities Pydantic 实例。

    Returns
    -------
    bool
        是否被允许。Policy Engine 拿到 False 时 reason_code 应为
        ``CAPABILITY_NOT_GRANTED``（见 design.md REASON_CODES）。

    Notes
    -----
    本函数纯查表，无副作用、确定性 100%——对应 design.md Property 1。
    任务 8 Policy Engine Step 2 直接复用，避免双方对「能力包封闭性」语义
    各写一份导致漂移。
    """
    if action_type == "no_op":
        return True
    cap_field = _ACTION_TYPE_TO_CAPABILITY.get(action_type)
    if cap_field is None:
        # 未知 action_type（含 withdraw / borrow / margin / transfer_out 等）→ 永远 False
        return False
    return bool(getattr(capabilities, cap_field))


__all__ = [
    "PolicyTemplate",
    "TEMPLATES",
    "TEMPLATE_DESCRIPTIONS",
    "TEMPLATE_READONLY_RESEARCHER",
    "TEMPLATE_SMALL_SPOT_EXECUTOR",
    "TEMPLATE_DAO_TREASURY_GUARDED",
    "build_policy_from_template",
    "list_templates",
    "is_action_type_allowed_by_capabilities",
]
