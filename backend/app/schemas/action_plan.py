"""ActionPlan v0 校验器（任务 6 / Req 6）。

本模块对应 PRD §10.1 「ActionPlan v0 JSON Schema」与 Req 6 的 6 条 acceptance
criteria，是 B.AI Planner 输出与 Policy Engine 输入之间的边界类型层。设计
要点：

1. **Discriminated union by ``type``**：把 PRD §10.1 的 flat schema 拆成三个变种
   - :class:`PlaceOrCancelOrderAction` —— ``place_order`` / ``cancel_order``
     的 7 个核心字段全部必填（Req 6 AC1）。
   - :class:`ReadAction` —— ``read_market`` / ``read_account`` 仅 ``symbol``
     必填，其余字段提供"none / 0"语义默认值（Req 6 AC2）。
   - :class:`NoOpAction` —— 仅 ``type`` + ``rationale`` 必填，其余字段不应
     出现（Req 6 AC3）。
   Pydantic 通过 ``Field(discriminator='type')`` 在 schema 阶段就路由到正确
   变种；遇到 ``"exotic_action"`` 等未知 type 直接报 union_tag_invalid，与
   Req 15 AC7「未知 action type SHALL 被 REJECT」契合（这一层先拦下，避免
   把脏数据带到 Policy Engine 任务 8）。

2. **每层 ``extra='forbid'``**：顶层 :class:`ActionPlanV0` 与每个 action 变种
   都拒绝未知字段。这是 Req 7 AC11 「ActionPlan 含 Policy DSL v0 schema 未
   定义的未知字段 → REJECT(UNKNOWN_FIELD_DETECTED)」的第一道防线——schema
   阶段就把多余字段过滤掉，Policy Engine 看不到它们。

3. **失败语义统一为 ``None``**：:func:`validate_action_plan_schema` 对所有失败
   场景（非 JSON / 缺字段 / 越界 / 未知 type / 未知字段）统一返回 ``None``，
   由调用方（任务 10 Planner 适配器）把 action 转入 ``PLAN_INVALID`` 状态
   （Req 5 AC2）。这样调用方代码只需 ``if plan is None: ...``，不必针对每
   种错误分类处理。**故意不抛异常**：planner 输出错误是常态而非异常，与
   schema 失败有别。

4. **Symbol 小写归一化**（Req 6 AC6）：``ActionPlanV0`` 上挂 ``field_validator
   ('actions', mode='after')``，对每个 action 的 ``symbol`` 字段（如果存在）
   原地小写。这与 :func:`app.services.policy_validator.normalize_symbol_list`
   保持同一约定（HTX 内部一律小写），让 Policy Engine 任务 8 的 allowed_symbols
   比较不必再做 case-folding。

为何不用 JSON Schema + jsonschema 双层校验（如 :mod:`policy_validator`）？
-----------------------------------------------------------------------
Policy DSL v0 是发布给「AI 编程代理 + 第三方」的 contract，需要纯 JSON Schema
形态便于跨语言消费。ActionPlan v0 反而是**纯内部边界**：只有服务端 planner
适配器产生、只有 Policy Engine 消费——既然没有外部消费方，单一 Pydantic
校验已足够覆盖所有需求，没必要再维护一份 JSON Schema 副本。如果未来需要
对外暴露 ActionPlan schema（如让前端 SDK 校验），再补一份 JSON Schema 即可。

PRD §10.1 与 Req 6 的差异处理
----------------------------
PRD §10.1 把 ``[type, symbol, side, order_type, amount, amount_unit,
max_notional_usdt]`` 7 字段都列为 required。Req 6 AC1-3 把这一规则按 type
条件化。**以 Req 6 为准**——design.md 的 ``validate_action_plan_schema``
伪代码也走条件必填路线。这是因为 PRD 的 flat schema 在实际场景下会强迫
planner 给 read_market 也填 ``side="none" / amount=0`` 等无意义字段，徒
增噪音；条件必填让 planner 输出更紧凑。
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# 公共类型别名 / 常量
# ---------------------------------------------------------------------------
# action.type 的枚举集合——与 PRD §10.1 / requirements.md Glossary 一致。
# 单独抽出便于后续 Policy Engine（任务 8）做"未知 type → REJECT"判断时复用，
# 避免散落多处字面量。
ActionType = Literal[
    "read_market",
    "read_account",
    "place_order",
    "cancel_order",
    "no_op",
]

# 顶层必填字段集合——validator 失败信息中可引用，确保与 PRD §10.1 同步。
TOP_LEVEL_REQUIRED: tuple[str, ...] = (
    "version",
    "intent_summary",
    "actions",
    "assumptions",
    "risk_notes",
)


# ---------------------------------------------------------------------------
# ActionItem 基类与变种
# ---------------------------------------------------------------------------
class ActionItemBase(BaseModel):
    """ActionPlan 中单个 action 的共享基类。

    只声明所有变种共享的字段：
    - ``type``：被各变种用 ``Literal[...]`` 收紧成 discriminator。
    - ``rationale``：planner 给出的中文解释，所有变种均可携带；上限 800 字符
      （PRD §10.1）防止 planner 在这里 dump 过长上下文撑爆 token 预算。

    ``extra='forbid'`` 是 Req 7 AC11 的第一道闸：planner 多吐的字段在 schema
    阶段就被拒绝，避免污染下游审计 / 执行链路。
    """

    model_config = ConfigDict(extra="forbid")

    # 基类的 type 留作宽松 str；具体变种用 Literal 收紧——这样 mypy/IDE 在
    # 拿到 ``ActionItemBase`` 引用时仍能 ``action.type`` 访问，不会因为
    # discriminator 是 union 类型而类型推导失败。
    type: str
    rationale: str | None = Field(default=None, max_length=800)


class PlaceOrCancelOrderAction(ActionItemBase):
    """``place_order`` / ``cancel_order`` 变种（Req 6 AC1）。

    PRD §10.1 7 个核心字段全部必填。``limit_price`` 与 ``requires_user_approval``
    保持可选——前者只在 ``order_type='limit'`` 时有意义，后者由 Policy Engine
    根据 ``passport.policy.approval.required_for_trade`` 最终决定（Req 7 AC9），
    planner 给的值仅作为参考。

    ``side`` / ``order_type`` / ``amount_unit`` 都用 ``Literal`` 锁死合法枚举值；
    PRD §10.1 显式列出 ``"none"`` 也算合法（即便对 trade action 语义略奇怪），
    保留与 ReadAction 一致的 surface 便于调用方拷贝 dict 模板时少踩坑。
    """

    type: Literal["place_order", "cancel_order"]
    symbol: str
    side: Literal["buy", "sell", "none"]
    order_type: Literal["limit", "market", "none"]
    # ``amount`` 与 ``max_notional_usdt`` 走 ``Field(ge=0)``——负数明显异常
    # （planner 没理由输出负数手数），直接 schema 阶段拦下，对应 PRD §10.1
    # ``"minimum": 0`` 约束。
    amount: float = Field(ge=0)
    amount_unit: Literal["base", "quote", "none"]
    max_notional_usdt: float = Field(ge=0)
    # ``limit_price`` 在 PRD §10.1 是 ``["number", "null"]``；这里 ``float | None``
    # 等价表达，默认 None 让 market 单不必显式置空。
    limit_price: float | None = Field(default=None, ge=0)
    # planner 建议是否需用户审批；最终决定权在 Policy Engine——这里给默认 True
    # 是"宁可多请审批不可少请"的安全倾向（Req 8 AC1 操作摘要展示流程触发）。
    requires_user_approval: bool = True


class ReadAction(ActionItemBase):
    """``read_market`` / ``read_account`` 变种（Req 6 AC2）。

    仅 ``symbol`` 必填；``side`` / ``order_type`` / ``amount`` / ``amount_unit``
    / ``max_notional_usdt`` / ``requires_user_approval`` 全部带"语义中性"
    默认值（``"none"`` / 0 / False）。这与 PRD §10.1 的 flat schema 形成兼容：

    - flat schema 要求所有 7 字段填值 → planner 可以全填（默认值不变）。
    - Req 6 AC2 允许只填 symbol → planner 也可以省掉，由 schema 默认补齐。

    两种方式都能通过 :class:`ReadAction`，让 planner 实现保留灵活度。

    Notes
    -----
    没有 ``limit_price`` —— 读操作没有"价格"语义，任何 limit_price 字段对
    read_* action 都是 noise；用 ``extra='forbid'`` 让多余字段直接报错而非
    被静默忽略。
    """

    type: Literal["read_market", "read_account"]
    symbol: str
    side: Literal["buy", "sell", "none"] = "none"
    order_type: Literal["limit", "market", "none"] = "none"
    amount: float = Field(default=0, ge=0)
    amount_unit: Literal["base", "quote", "none"] = "none"
    max_notional_usdt: float = Field(default=0, ge=0)
    # read 操作天然不需审批（Req 7 AC8 → ALLOW / AUTO_APPROVED）；默认 False
    # 让 planner 不必为每个 read_market 显式置 false。
    requires_user_approval: bool = False


class NoOpAction(ActionItemBase):
    """``no_op`` 变种（Req 6 AC3）。

    仅 ``type`` + ``rationale`` 必填；``symbol`` 可选，其它字段一律不允许出现
    （由 ``extra='forbid'`` 把控）。这一刚性 surface 与 PRD §10.2 planner
    prompt「当用户请求超出护照策略或要求提现/杠杆/借贷或非法活动时，你
    必须设置 type=no_op」的语义匹配——no_op 是「拒绝 + 解释」，没有"金额 /
    方向"等交易字段的容身之地。

    Why ``rationale`` is required here
    ----------------------------------
    基类把 ``rationale`` 设为 ``Optional[str]``；no_op 把它收紧为必填——因为
    no_op 唯一价值就是「告诉用户为什么不动作」，少了 rationale 这个 action
    就毫无用处。Pydantic v2 的字段 override 会让这里的 ``rationale``
    （无 default）覆盖基类的 ``Optional`` 声明。
    """

    type: Literal["no_op"]
    # 注意：这里**没有**给 default，让 Pydantic 视为 required；与基类的
    # ``rationale: str | None = None`` 不同——field override 在 Pydantic v2
    # 中是合法的，不会触发警告。
    rationale: str = Field(max_length=800)
    # symbol 在 no_op 上可选——planner 经常给 ``no_op`` 时附带 symbol 用以
    # 注释「我对哪个 symbol 拒绝了」；不给也合法。
    symbol: str | None = None


# Discriminated union：Pydantic 看到 ``type`` 字段值后路由到对应变种。
# 多值 Literal discriminator（如 ``Literal["place_order", "cancel_order"]``）
# 在 Pydantic v2.5+ 已正式支持。未知 type 触发 ``union_tag_invalid``。
ActionItem = Annotated[
    PlaceOrCancelOrderAction | ReadAction | NoOpAction,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# ActionPlan v0 顶层模型
# ---------------------------------------------------------------------------
class ActionPlanV0(BaseModel):
    """ActionPlan v0 顶层（PRD §10.1 / Req 6 AC4-6）。

    五个必填顶层字段缺一报错；``actions`` 数组长度 1-3（min/max）；``version``
    锁死 ``"0.1"``——保留升级到 v1 时的契约破坏点。

    序列化语义
    ----------
    - ``model_dump()`` 输出的 ``actions`` 已是各变种的 dict 形态，
      ``symbol`` 已小写归一。可直接喂给 Policy Engine，也可入库
      ``agent_actions.normalized_action_json``（design.md「数据模型」）。
    - ``model_dump_json()`` 输出标准 JSON 字符串；与 :func:`validate_action_plan_schema`
      互逆——validate 后再 dump 应得到 idempotent 结果。
    """

    model_config = ConfigDict(extra="forbid")

    # ``Literal["0.1"]`` 同时约束类型与值；PRD §10.1 ``"const": "0.1"`` 的
    # Pydantic 等价表达。``"0.2"`` 等非匹配值会被 schema 阶段拒绝。
    version: Literal["0.1"]
    # 用户意图的简短摘要——上限 500 字符防 planner 输出整段长文。
    intent_summary: str = Field(max_length=500)
    # actions 长度 1-3：方法论 §6 / Req 6 AC4「过长 plan 拒绝」的硬约束。
    actions: list[ActionItem] = Field(min_length=1, max_length=3)
    # planner 列出的假设；空列表合法（planner 觉得没需要说明的假设）。
    assumptions: list[str]
    # planner 列出的风险点；空列表合法但 audit 重放界面会显眼提示「无风险注释」。
    risk_notes: list[str]

    @field_validator("actions")
    @classmethod
    def normalize_symbols(cls, actions: list[ActionItem]) -> list[ActionItem]:
        """把每个 action 的 ``symbol`` 字段（如果是字符串）原地小写化（Req 6 AC6）。

        - ``ReadAction`` / ``PlaceOrCancelOrderAction`` 的 ``symbol`` 是必填 str
          → 一定走小写化。
        - ``NoOpAction`` 的 ``symbol`` 是 ``str | None``，None 时跳过。

        ``mode='after'`` 默认值意味着：当此函数被调用时，``actions`` 已是构造
        好的 ActionItem 实例列表，可安全 ``action.symbol = ...`` 直接赋值
        （Pydantic v2 默认 ``validate_assignment=False``，赋值不会触发 re-validation）。
        """
        for action in actions:
            # 用 ``getattr(..., None)`` 防御性兼容——即使未来加新变种没声明
            # symbol，本 validator 也不会 AttributeError。
            current = getattr(action, "symbol", None)
            if isinstance(current, str):
                action.symbol = current.lower()
        return actions


# ---------------------------------------------------------------------------
# 公共校验入口
# ---------------------------------------------------------------------------
def validate_action_plan_schema(raw: str | dict[str, Any] | None) -> ActionPlanV0 | None:
    """校验 ActionPlan v0 输入；任一失败统一返回 ``None``（Req 5 AC2 / Req 6）。

    Parameters
    ----------
    raw : str | dict | None
        - ``str``：planner 直出的 JSON 文本（最常见路径）。
        - ``dict``：调用方已 ``json.loads`` 过的 dict（测试 / 内部调用）。
        - ``None`` 或其它类型：直接视为非法。

    Returns
    -------
    ActionPlanV0 | None
        - 合法 → 返回已小写归一的 :class:`ActionPlanV0` 实例。
        - 非法（任一原因）→ 返回 ``None``。

    失败统一返回 None 而非抛异常的设计理由
    ------------------------------------
    planner 输出错误是**常态**而非异常——B.AI 偶尔吐回 markdown-fenced JSON、
    缺字段、引用未声明 symbol 都属业务逻辑常见路径，对应方法论 §11「错误恢复
    是主路径」。把"错误结果"转成 ``None`` 让调用方代码扁平：

        plan = validate_action_plan_schema(response_text)
        if plan is None:
            await transition(action_id, "PLAN_INVALID")
            return

    若改用异常，每个调用点都要 try/except，且不同失败类型都映射到同一状态
    （PLAN_INVALID），异常的额外信息没用武之地。

    具体失败分类（全部返回 None）
    ----------------------------
    - 输入非 str 也非 dict（含 None / list / 数字）。
    - JSON 解析失败（``json.JSONDecodeError``）。
    - 顶层缺必填字段 / version != "0.1" / actions 越界。
    - 任一 action 缺类型必填字段 / 数值越界 / 未知 type。
    - 任一层级出现 schema 未声明字段（``extra='forbid'``）。

    Examples
    --------
    >>> plan = validate_action_plan_schema(
    ...     '{"version":"0.1","intent_summary":"x","actions":'
    ...     '[{"type":"read_market","symbol":"BTCUSDT"}],"assumptions":[],"risk_notes":[]}'
    ... )
    >>> plan.actions[0].symbol  # 已小写归一
    'btcusdt'
    >>> validate_action_plan_schema("not a json")  # 解析失败
    >>> validate_action_plan_schema(None)  # 非法输入
    """
    # 第一步：把输入归一为 dict（或返回 None）。
    if isinstance(raw, str):
        try:
            data: Any = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # 注：``json.JSONDecodeError`` 是 ``ValueError`` 子类；这里两个
            # 都列出仅为可读性。
            return None
    elif isinstance(raw, dict):
        data = raw
    else:
        # None / list / 数字 / 其它类型一律拒绝——ActionPlan 顶层必须是 JSON
        # object（dict 形态）。
        return None

    # 二次防御：``json.loads("123")`` 返回 int；``json.loads("[]")`` 返回 list；
    # 这些都不是合法的 ActionPlan 顶层。Pydantic 会报错，下面 try/except 接住。
    if not isinstance(data, dict):
        return None

    # 第二步：交给 Pydantic 做完整 schema + 类型 + 业务校验。
    try:
        return ActionPlanV0.model_validate(data)
    except Exception:
        # 所有 ValidationError / TypeError / 自定义校验异常统一吞掉。
        # 调用方只关心成功/失败，不关心具体哪个字段错；想看详情可在调用前
        # 直接 ``ActionPlanV0.model_validate(data)`` 自行 catch。
        return None


__all__ = [
    "TOP_LEVEL_REQUIRED",
    "ActionItem",
    "ActionItemBase",
    "ActionPlanV0",
    "ActionType",
    "NoOpAction",
    "PlaceOrCancelOrderAction",
    "ReadAction",
    "validate_action_plan_schema",
]
