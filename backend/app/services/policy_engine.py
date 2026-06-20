"""确定性 Policy Engine（任务 8.1 / Req 7 + Req 16 + Property 1/9/10）。

本模块是「执行层」最关键的确定性组件——给定 ``(action, policy, daily_history,
market_snapshot, global_config, now)``，:func:`evaluate_policy` 返回唯一确定的
:class:`PolicyVerdict`，从而支撑 design.md 「Property 1: Policy Engine 确定性」。

设计骨架（Req 7 AC1）
---------------------
裁决严格按以下顺序进行（任一步触发 REJECT 即返回，后续步骤不再评估）：

::

  ┌─ Step 0  全局 kill switch（DEMO_DISABLE_EXECUTION）─ Req 7 AC12 / Property 10
  │            └─ 仅对「非只读操作」生效（read_market / read_account / no_op 不受影响）
  ├─ Step 1  blocked_actions ─ Req 7 AC2
  ├─ Step 2  capabilities ─ Req 7 AC3
  ├─ Step 3  allowed_symbols ─ Req 7 AC4 / Req 6 AC6
  ├─ Step 4  max_notional_usdt_per_order ─ Req 7 AC5
  ├─ Step 5  max_daily_notional_usdt ─ Req 7 AC6（UTC 日边界）
  ├─ Step 6  max_orders_per_day ─ Req 7 AC7
  ├─ Step 7  allowed_time_utc（含跨午夜）─ Req 7 AC1 末尾 / Req 4 AC4
  ├─ Anti-hallucination：place_order.symbol 必须在 market_snapshot 内 ─ Req 16 AC1 / Property 9
  └─ Final verdict
       ├─ read_market / read_account → ALLOW (AUTO_APPROVED)
       ├─ place_order / cancel_order:
       │    ├─ approval.required_for_trade=true → REQUIRE_APPROVAL
       │    └─ false → ALLOW
       └─ no_op → ALLOW（语义：什么也不做，上层 short-circuit 跳过）

为何把 ``no_op`` 放入 ALLOW 出口？
-------------------------------
``no_op`` 经 ActionPlan schema 校验后到达 Policy Engine（Req 6 AC3），它表示
「不应执行任何写操作」——把它放入 ALLOW 让上层调度器把状态置为 EXECUTING
直接 short-circuit 完成，不进入审批 / HTX 适配器路径。这与 Req 8 AC1
「READ_MARKET / READ_ACCOUNT / no_op → AUTO_APPROVED」的语义一致。

**确定性 / 纯函数**
-------------------
- :func:`evaluate_policy` **不写审计事件**——纯函数无副作用，方便 PBT 重放、
  方便 design.md 「Property 1 确定性」断言。
- 调用方（任务 11 审批服务、任务 13 执行网关）持有 :class:`AuditWriter` 与
  ``trace_id``，应在拿到 verdict 后调用 :func:`write_policy_check_completed_audit_event`
  把裁决结果写入审计哈希链（Req 7 AC10）。
- ``now`` 默认 ``None`` 时取 ``datetime.now(UTC)``——但调用方与测试都应显式
  传入"请求级当前时间"以保证全程同一时间点；Property 1 PBT 必须显式传入。

输入数据形态
-----------
- ``action: dict``：已通过 :class:`app.schemas.action_plan.ActionPlanV0` 校验
  并 ``model_dump()`` 后的单个 action dict。symbol 已小写（Req 6 AC6）。
- ``policy: dict``：Passport.policy_json，等价于
  :class:`app.schemas.policy.PolicyDSLv0` 的 ``model_dump()``。
- ``market_snapshot: dict[str, dict]``：``{symbol_lowercase: {"last": price, ...}}``，
  由调用方在调度前从 HTX 适配器（任务 12）拉取。
- ``daily_history``：当日（UTC 0:00 至 ``now``）已执行 + 待执行的累计统计；
  由调用方按 UTC 日边界从 ``agent_actions`` 表聚合。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import AuditEvent
from app.models.enums import AuditEventType
from app.services.audit_writer import (
    ACTOR_TYPE_POLICY_ENGINE,
    write_audit_event,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. 类型定义
# ---------------------------------------------------------------------------
Verdict = Literal["ALLOW", "REQUIRE_APPROVAL", "REJECT"]


@dataclass(frozen=True)
class DailyActionHistory:
    """当日（UTC 日边界）累计统计——已执行 + 待执行（Req 4 AC5 / Req 7 AC6-7）。

    Attributes
    ----------
    total_notional_today_utc : float
        当日所有已执行 + 已审批待执行 place_order 的 ``max_notional_usdt`` 之和。
        调用方按 ``date_trunc('day', created_at AT TIME ZONE 'UTC')`` 聚合。
    order_count_today_utc : int
        当日 place_order + cancel_order 总数。
    auto_approved_count_today_utc : int
        当日**自动审批**（AUTO_APPROVED 走 G18 路径）的 action 次数。用于
        ``approval.auto_approval_thresholds.max_per_day`` 上限校验。
        默认 0；调用方按需聚合。

    Notes
    -----
    本数据结构是**纯快照**：Policy Engine 拿到时就视为不可变。调用方负责
    在每次裁决前重新聚合（不要缓存——会误判 Property 1 的"同输入恒同输出"）。
    """

    total_notional_today_utc: float = 0.0
    order_count_today_utc: int = 0
    auto_approved_count_today_utc: int = 0


@dataclass(frozen=True)
class GlobalConfig:
    """运行时全局开关——目前仅一个字段：kill switch（Req 7 AC12 / Property 10）。

    把全局配置抽象成数据类的两个好处：
    1. 让测试在不修改环境变量的情况下精确控制 kill switch；
    2. 未来追加全局开关（如「全局只读模式」/ 「合规审查暂停」）只需扩字段，
       evaluate_policy 签名稳定。
    """

    demo_disable_execution: bool = False
    #: G18 风险分级自动审批：当前 passport 的 reputation_score（0-100）。
    #: 默认 None，表示"调用方未提供 reputation"，等价于"任何 reputation 阈值都不通过"——
    #: 这是保守默认（与 auto_approval_thresholds 字段全不配置时同义）。
    passport_reputation_score: int | None = None
    #: G2 信息流追踪开关（Phase 2 / docs/tech-research/06-...md §6.2）。
    #:
    #: 默认 ``False`` = 不强制 provenance 校验（向后兼容现有调用方与测试）。
    #: 生产部署应通过 ``ENFORCE_MARKET_PROVENANCE=true`` 启用,让
    #: place_order 路径拒绝 ``provenance ∉ TRUSTED_MARKET_PROVENANCES`` 的
    #: market data——防"用户上传文档诱导按伪造价格下单"。
    #:
    #: 启用前提：所有调用方都已显式给 market_snapshot 条目标 ``provenance``
    #: 字段（``"seed"`` / ``"htx_real"`` / ``"htx_cached"`` 等可信来源）。
    enforce_market_provenance: bool = False


@dataclass(frozen=True)
class PolicyVerdict:
    """Policy Engine 裁决结果（Req 7 AC10）。

    Attributes
    ----------
    verdict : Literal["ALLOW", "REQUIRE_APPROVAL", "REJECT"]
        三态裁决；ALLOW 等价于 PRD §7.2 的 AUTO_APPROVED。
    reason_codes : tuple[str, ...]
        机器可读原因码列表；用 ``tuple`` 而非 ``list`` 是为了让整个 verdict
        可哈希且不可变（PBT 时方便比较）。所有可能值均枚举在
        :data:`REASON_CODES` 中——这是审计 / 前端展示的契约。
    risk_score : int
        0-100 的风险评分；ALLOW 接近 0、REQUIRE_APPROVAL ~ 40、REJECT ≥ 60。
        具体值见 :data:`_RISK_SCORE_BY_REASON`。
    normalized_action : dict
        规范化后的 action dict——symbol 已小写、移除未知字段（schema 已保证）。
        即便是 REJECT 也填充：审计重放时需要展示「这个 action 长什么样」。
    """

    verdict: Verdict
    reason_codes: tuple[str, ...]
    risk_score: int
    normalized_action: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 2. REASON_CODES 常量与风险评分表（design.md 完整列表）
# ---------------------------------------------------------------------------
#: design.md「执行层：Policy Engine」中 REASON_CODES 全集。
#:
#: 任何 :class:`PolicyVerdict` 的 ``reason_codes`` 必须是本元组的**子集**——
#: 这是 Property 1 的间接保证：返回未知 reason_code 立刻被测试 catch。
REASON_CODES: Final[tuple[str, ...]] = (
    # blocked_actions（Req 7 AC2）
    "BLOCKED_ACTION_WITHDRAW",
    "BLOCKED_ACTION_BORROW",
    "BLOCKED_ACTION_MARGIN",
    "BLOCKED_ACTION_TRANSFER_OUT",
    "BLOCKED_ACTION_UNKNOWN_TOOL_CALL",
    # capabilities / symbol / limits（Req 7 AC3-7）
    "CAPABILITY_NOT_GRANTED",
    "SYMBOL_NOT_ALLOWED",
    "LIMIT_MAX_NOTIONAL_EXCEEDED",
    "DAILY_LIMIT_EXCEEDED",
    "DAILY_ORDER_COUNT_EXCEEDED",
    "TIME_WINDOW_VIOLATION",
    # 反幻觉 / unknown field（Req 7 AC11 / Req 16）
    "UNKNOWN_FIELD_DETECTED",
    "PLAN_HALLUCINATION",
    # passport 状态（任务 11 审批 / 任务 13 执行网关使用）
    "PASSPORT_REVOKED",
    "PASSPORT_PAUSED",
    "PASSPORT_EXPIRED",
    # 全局 kill switch（Req 7 AC12 / Property 10）
    "EXECUTION_DISABLED",
    # 错误恢复（任务 14）
    "LOOP_DETECTED",
    "MODEL_UNAVAILABLE",
    # 审批/执行延迟后 market snapshot 重校验（修复 G16 / Req 16 AC2）。
    # 与上述 reason_codes 不同，这两类不在 evaluate_policy 内部产生——
    # 由 stale_price_check 模块在审批/执行重裁决时返回；列入 REASON_CODES
    # 是为了让审计 / 前端展示有统一的契约（写入 event_data.reason_code 时合法）。
    "MARKET_SLIPPAGE_EXCEEDED",
    "MARKET_SNAPSHOT_STALE",
    "AUTO_APPROVED_LOW_RISK",
    # G2 信息流追踪（Phase 2 / docs/tech-research/06-...md §6.2）：
    # 当 place_order 引用的 market_snapshot 来源被标记为不可信
    # （``provenance="user_provided"``），policy_engine 直接 REJECT。
    # 防"用户上传 PDF / 第三方 RAG 文档诱导 agent 按伪造价格下单"。
    "MARKET_DATA_UNTRUSTED",
)

#: 集合形式的 :data:`REASON_CODES`，用于 O(1) 成员判断（测试 / 调试时使用）。
REASON_CODES_SET: Final[frozenset[str]] = frozenset(REASON_CODES)


#: 各 reason_code 的默认 risk_score 映射（design.md 「执行层：Policy Engine」）。
#:
#: 数字含义：
#: - 100：硬性禁止（kill switch / blocked_actions）
#: - 90-95：能力 / 反幻觉违反
#: - 80-85：限额超出
#: - 60-70：时间 / 频次
#: - 40：审批
#: - 0：通过
_RISK_SCORE_BY_REASON: Final[dict[str, int]] = {
    "EXECUTION_DISABLED": 100,
    "BLOCKED_ACTION_WITHDRAW": 100,
    "BLOCKED_ACTION_BORROW": 100,
    "BLOCKED_ACTION_MARGIN": 100,
    "BLOCKED_ACTION_TRANSFER_OUT": 100,
    "BLOCKED_ACTION_UNKNOWN_TOOL_CALL": 100,
    "PLAN_HALLUCINATION": 95,
    "CAPABILITY_NOT_GRANTED": 90,
    "LIMIT_MAX_NOTIONAL_EXCEEDED": 85,
    "SYMBOL_NOT_ALLOWED": 80,
    "DAILY_LIMIT_EXCEEDED": 80,
    "DAILY_ORDER_COUNT_EXCEEDED": 70,
    "TIME_WINDOW_VIOLATION": 60,
    "UNKNOWN_FIELD_DETECTED": 90,
    "PASSPORT_REVOKED": 100,
    "PASSPORT_PAUSED": 100,
    "PASSPORT_EXPIRED": 100,
    "LOOP_DETECTED": 90,
    "MODEL_UNAVAILABLE": 50,
    # G16 stale price 重校验：MARKET_SLIPPAGE_EXCEEDED 比 MARKET_SNAPSHOT_STALE
    # 严重（已知偏离 vs 不知道当前价），分数略高。
    "MARKET_SLIPPAGE_EXCEEDED": 80,
    "MARKET_SNAPSHOT_STALE": 70,
    # G18 风险分级自动审批：标记本次裁决走了 auto_approval_thresholds 路径。
    # risk_score=0（与 ALLOW 一致），但 reason_codes 含此码便于审计区分
    # "默认 ALLOW"与"L1 自动审批 ALLOW"。
    "AUTO_APPROVED_LOW_RISK": 0,
    # G2 不可信市场数据：与 PLAN_HALLUCINATION 同重——若数据源被标记为
    # 不可信，即便 symbol 在 snapshot 内也不能信。
    "MARKET_DATA_UNTRUSTED": 95,
}

#: 通过审批 / 自动通过时的 risk_score。
_RISK_SCORE_REQUIRE_APPROVAL: Final[int] = 40
_RISK_SCORE_ALLOW: Final[int] = 0


# ---------------------------------------------------------------------------
# 3. 内部分类常量
# ---------------------------------------------------------------------------
#: 只读 action 集合——kill switch 不影响这两类（Req 7 AC12「非只读」）。
_READ_ONLY_ACTION_TYPES: Final[frozenset[str]] = frozenset(
    {"read_market", "read_account"}
)

#: ``no_op`` 在裁决里走「自动 ALLOW」路径——不需 capability、不进入限额检查。
_NO_OP_ACTION_TYPE: Final[str] = "no_op"

#: 写入类 action——参与 daily_notional / max_notional / orders_per_day 限额。
_WRITE_ACTION_TYPES: Final[frozenset[str]] = frozenset(
    {"place_order", "cancel_order"}
)

#: 把 action_type 映射到 capabilities 字段名（与 capability_envelope 一致）。
_ACTION_TYPE_TO_CAPABILITY: Final[dict[str, str]] = {
    "read_market": "read_market",
    "read_account": "read_account",
    "place_order": "place_order",
    "cancel_order": "cancel_order",
}


# ---------------------------------------------------------------------------
# G2 信息流追踪 / market data provenance（Phase 2）
# ---------------------------------------------------------------------------
#: 各 market_snapshot 条目的 ``provenance`` 字段允许的值。
#:
#: 设计依据（多源交叉验证 / docs/tech-research/06-...md §6.2）：
#:
#: - **CaMeL**（Google DeepMind, arxiv 2503.18813）的 capabilities 是带
#:   provenance + readers 的 metadata 标签，每个值都带"来源"。
#: - **Tessera** 的核心不变量："trust label 的 min, 不是 max"——一段不可信
#:   就把整个 context 拉到 untrusted。
#: - **Anthropic Zero Trust for AI Agents**：所有外部数据视为 untrusted by
#:   default,需要明确"信任来源"才能进入决策路径。
#:
#: 我们的简化版：3 个明确级别，policy_engine 在 place_order 路径只信任前 2 个。
#: 不区分用户上传 PDF / RAG 文档 / 网页 fetch 等子类型——一律视为 user_provided。
TRUSTED_MARKET_PROVENANCES: Final[frozenset[str]] = frozenset(
    {
        # 服务端 demo / fixture 数据，永远可信
        "seed",
        # HTX 公共行情 API 直接拉取，已通过 sanity 校验（见 htx_adapter.validate_ticker_sanity）
        "htx_real",
        # 缓存层（Redis / DB），等价 htx_real（缓存仅延长 htx_real 的生命周期）
        "htx_cached",
    }
)
#: ``provenance`` 字段缺失时的兜底语义。``"unknown"`` 当作不可信处理——
#: **保守默认**：未明确标记的数据不允许影响 place_order。这强制 caller
#: 显式声明 provenance,而非"沉默落入受信"。
DEFAULT_PROVENANCE_WHEN_MISSING: Final[str] = "unknown"


# ---------------------------------------------------------------------------
# 4. 工具函数（pure / no I/O）
# ---------------------------------------------------------------------------
def _parse_hhmm(s: str) -> time:
    """把 ``"HH:MM"`` 字符串解析为 ``datetime.time``。

    schema 已用 pattern 约束格式，这里只做 split + int。失败时 ``ValueError``
    会冒泡到 :func:`evaluate_policy`，调用方应当确保 policy 已过 validator。
    """
    hh_str, mm_str = s.split(":")
    return time(hour=int(hh_str), minute=int(mm_str))


def is_within_time_window(now_time: time, start_str: str, end_str: str) -> bool:
    """判断 ``now_time`` 是否在 ``[start, end]`` 时间窗内（Req 4 AC4 跨午夜）。

    规则
    ----
    - 普通窗口（``start <= end``）：``start <= now_time <= end``。
    - 跨午夜窗口（``start > end``，例 22:00→02:00）：
      ``now_time >= start`` 或 ``now_time <= end``。
    - ``start == end`` 视为普通窗口的退化（仅这一刻在窗口内），刻意保留
      与 :func:`app.services.policy_validator.is_cross_midnight` 的语义对齐
      （它返回 False，意味着同日内 0 长度窗口）。

    Parameters
    ----------
    now_time : time
        当前 UTC 时间的 time 部分（不含日期）。
    start_str, end_str : str
        ``"HH:MM"`` 形式；调用方负责保证已通过 schema pattern 校验。

    Returns
    -------
    bool

    Examples
    --------
    >>> is_within_time_window(time(23, 0), "22:00", "02:00")  # 跨午夜
    True
    >>> is_within_time_window(time(12, 0), "22:00", "02:00")  # 跨午夜白天不在
    False
    >>> is_within_time_window(time(10, 0), "09:00", "17:00")  # 普通
    True
    >>> is_within_time_window(time(8, 0), "09:00", "17:00")
    False
    """
    start = _parse_hhmm(start_str)
    end = _parse_hhmm(end_str)
    if start <= end:
        return start <= now_time <= end
    # 跨午夜
    return now_time >= start or now_time <= end


def normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    """规范化 action dict：symbol 小写化、shallow copy（Req 6 AC6）。

    Schema 阶段已保证「无未知字段」（``extra='forbid'``），所以这里只需要
    对 symbol 做 lower()。返回 dict 是浅拷贝，调用方修改不影响原始 action。

    Parameters
    ----------
    action : dict[str, Any]
        ActionPlan v0 的单个 action（已经过 :class:`ActionPlanV0` 校验）。

    Returns
    -------
    dict[str, Any]
        浅拷贝 + symbol 小写化后的 dict。
    """
    normalized = dict(action)
    sym = normalized.get("symbol")
    if isinstance(sym, str):
        normalized["symbol"] = sym.lower()
    return normalized


def _make_reject(
    reason_code: str,
    normalized: dict[str, Any],
    extra_codes: tuple[str, ...] = (),
) -> PolicyVerdict:
    """构造 REJECT verdict 的语法糖——保证 risk_score 与 reason_code 的映射一致。

    ``extra_codes`` 让一次 REJECT 可携带多个 reason_codes（当前实现里仅
    单一 reason_code，但 dataclass 字段保留 tuple 形态便于未来扩展）。
    """
    codes = (reason_code,) + extra_codes
    risk = max(_RISK_SCORE_BY_REASON.get(c, 50) for c in codes)
    return PolicyVerdict(
        verdict="REJECT",
        reason_codes=codes,
        risk_score=risk,
        normalized_action=normalized,
    )


def _passes_auto_approval_thresholds(
    normalized: dict[str, Any],
    thresholds: dict[str, Any] | None,
    *,
    daily_history: DailyActionHistory,
    global_config: GlobalConfig,
) -> bool:
    """G18 风险分级自动审批阈值检查（Phase 3）。

    返回 True ⇔ 本 action 满足**所有**配置的阈值，可走 L1 自动放行路径。

    设计原则
    --------
    1. **保守默认**：``thresholds`` 为 None / 空 dict / 任一字段缺失 → False
       （继续走人工审批）。这是"显式启用"语义。
    2. **任一不通过即不通过**：所有配置的字段都是 AND 关系。
    3. **未配置字段视为不放行**：例如只配 ``max_notional_usdt`` 不配
       ``allowed_action_types`` → 永远不放行（防"用户配错半套规则导致全自动批"）。

    阈值含义
    --------
    - ``allowed_action_types``：本字段必填（防御默认全放）；空列表等价不允许。
    - ``max_notional_usdt``：仅 place_order 受限；cancel_order 不下新单 notional=0
      → 此项天然通过。
    - ``min_reputation_score``：调用方需通过 ``global_config.passport_reputation_score``
      传入；为 None 时此检查不通过（保守）。
    - ``max_per_day``：daily_history.auto_approved_count_today_utc 须 < 阈值。

    Notes
    -----
    本函数纯函数（无 I/O / 不依赖 ``now``），与 ``evaluate_policy`` 整体的
    Property 1 确定性保证一致。
    """
    if not thresholds:
        return False

    # 1. action_type 白名单（必填字段）
    allowed_types = thresholds.get("allowed_action_types")
    if not allowed_types or not isinstance(allowed_types, list):
        return False
    action_type = normalized.get("type")
    if action_type not in allowed_types:
        return False

    # 2. max_notional_usdt（仅对 place_order 有意义）
    max_notional = thresholds.get("max_notional_usdt")
    if action_type == "place_order":
        if max_notional is None:
            return False  # 未配置此项 → 不放行 place_order
        action_notional = float(normalized.get("max_notional_usdt", 0) or 0)
        if action_notional > float(max_notional):
            return False

    # 3. min_reputation_score
    min_rep = thresholds.get("min_reputation_score")
    if min_rep is None:
        return False  # 未配置 → 不放行
    actual_rep = global_config.passport_reputation_score
    if actual_rep is None or int(actual_rep) < int(min_rep):
        return False

    # 4. max_per_day
    max_per_day = thresholds.get("max_per_day")
    if max_per_day is None:
        return False  # 未配置 → 不放行（保守）
    if daily_history.auto_approved_count_today_utc >= int(max_per_day):
        return False

    return True


# ---------------------------------------------------------------------------
# 5. 主入口：evaluate_policy
# ---------------------------------------------------------------------------
def evaluate_policy(
    action: dict[str, Any],
    policy: dict[str, Any],
    daily_history: DailyActionHistory,
    market_snapshot: dict[str, dict[str, Any]],
    global_config: GlobalConfig,
    *,
    now: datetime | None = None,
) -> PolicyVerdict:
    """确定性 Policy Engine 裁决（Req 7 + Req 16 + Property 1/9/10）。

    Parameters
    ----------
    action : dict[str, Any]
        ActionPlan v0 单个 action 的 dict（已通过 schema 校验）。
    policy : dict[str, Any]
        Passport.policy_json（等价于 :class:`PolicyDSLv0` 的 ``model_dump()``）。
    daily_history : DailyActionHistory
        当日（UTC）累计统计；调用方按 UTC 日边界聚合。
    market_snapshot : dict[str, dict]
        当前市场快照，``{symbol_lowercase: {"last": price, ...}}``。
        用于反幻觉校验（Req 16 AC1）。
    global_config : GlobalConfig
        全局开关——主要承载 kill switch 状态。
    now : datetime | None, default None
        当前 UTC 时间——用于 time_window 检查。``None`` 时使用
        ``datetime.now(UTC)``（注意此分支让函数变非确定性；测试与 PBT
        必须显式传入固定值以保证 Property 1）。

    Returns
    -------
    PolicyVerdict
        裁决结果，永远包含 ``normalized_action``（即便 REJECT）。
    """
    normalized = normalize_action(action)
    action_type = normalized.get("type")

    # ---- Step 0: 全局 kill switch ----------------------------------------
    # Req 7 AC12 / Property 10：DEMO_DISABLE_EXECUTION=true 时所有「非只读」
    # 操作（即不在 _READ_ONLY_ACTION_TYPES 且不是 no_op）一律 REJECT。
    # 把 kill switch 放在最前是为了在「演示时禁止任何写操作」场景下
    # 不暴露后续步骤的 reason_code（避免泄露策略细节）。
    if global_config.demo_disable_execution and action_type not in _READ_ONLY_ACTION_TYPES and action_type != _NO_OP_ACTION_TYPE:
        return _make_reject("EXECUTION_DISABLED", normalized)

    # ---- Step 1: blocked_actions ----------------------------------------
    # Req 7 AC2：``action.type ∈ policy.blocked_actions`` → REJECT(BLOCKED_ACTION_*)。
    # 注意：ActionPlan schema 仅允许 5 种 type（read_*/place_/cancel_/no_op），
    # 因此此检查要触发，前提是 policy.blocked_actions 含「withdraw / borrow / margin /
    # transfer_out / unknown_tool_call」之类——同时调用方绕过了 schema 把脏 type 送进来。
    # 这是**深度防御**：即便 schema 防线被绕，blocked_actions 仍能拦下。
    blocked_actions: list[str] = list(policy.get("blocked_actions", []))
    if isinstance(action_type, str) and action_type in blocked_actions:
        reason = f"BLOCKED_ACTION_{action_type.upper()}"
        # 防御：未在 REASON_CODES 中的 type → 用 UNKNOWN_TOOL_CALL 兜底
        if reason not in REASON_CODES_SET:
            reason = "BLOCKED_ACTION_UNKNOWN_TOOL_CALL"
        return _make_reject(reason, normalized)

    # ---- Step 2: capabilities -------------------------------------------
    # Req 7 AC3 / Property 4：action.type 必须在 capabilities 中声明为 true。
    # no_op 不需要 capability（永远 True），与 capability_envelope 模块对齐。
    capabilities: dict[str, Any] = policy.get("capabilities", {})
    if action_type != _NO_OP_ACTION_TYPE:
        cap_field = _ACTION_TYPE_TO_CAPABILITY.get(action_type or "")
        if cap_field is None or not bool(capabilities.get(cap_field, False)):
            return _make_reject("CAPABILITY_NOT_GRANTED", normalized)

    # 之后的检查仅对「带 symbol 的 action」有意义；no_op 会跳过
    # symbol / 限额 / 反幻觉 等所有写检查，直接进入 Final verdict 的 ALLOW 路径。
    if action_type == _NO_OP_ACTION_TYPE:
        return PolicyVerdict(
            verdict="ALLOW",
            reason_codes=(),
            risk_score=_RISK_SCORE_ALLOW,
            normalized_action=normalized,
        )

    limits: dict[str, Any] = policy.get("limits", {})

    # ---- Step 3: allowed_symbols（小写比较）-----------------------------
    # Req 7 AC4 + Req 6 AC6：normalized.symbol 已小写；policy.allowed_symbols
    # 也已经过 normalize_symbol_list 归一化，但再 lower 一遍以防 policy 是
    # 测试构造的原始 dict（没走 validate_policy_dsl 路径）。
    symbol = normalized.get("symbol")
    if isinstance(symbol, str):
        allowed_symbols = {
            s.lower() for s in limits.get("allowed_symbols", []) if isinstance(s, str)
        }
        if symbol not in allowed_symbols:
            return _make_reject("SYMBOL_NOT_ALLOWED", normalized)

    # ---- Step 4: max_notional_usdt_per_order ----------------------------
    # Req 7 AC5：仅 place_order 受此约束；cancel_order 不下新单不计 notional。
    if action_type == "place_order":
        max_notional_per_order = float(limits.get("max_notional_usdt_per_order", 0))
        action_notional = float(normalized.get("max_notional_usdt", 0))
        if action_notional > max_notional_per_order:
            return _make_reject("LIMIT_MAX_NOTIONAL_EXCEEDED", normalized)

        # ---- Step 5: max_daily_notional_usdt ---------------------------
        # Req 7 AC6 / Req 4 AC5：UTC 日边界由调用方在聚合 daily_history
        # 时把控；本函数只做加法 + 比较。
        max_daily_notional = float(limits.get("max_daily_notional_usdt", 0))
        if (
            daily_history.total_notional_today_utc + action_notional
            > max_daily_notional
        ):
            return _make_reject("DAILY_LIMIT_EXCEEDED", normalized)

    # ---- Step 6: max_orders_per_day -------------------------------------
    # Req 7 AC7：place_order + cancel_order 均计入。这样 cancel 满限额后
    # 同样无法继续 cancel——避免恶意 cancel 风暴。
    if action_type in _WRITE_ACTION_TYPES:
        max_orders_per_day = int(limits.get("max_orders_per_day", 0))
        if daily_history.order_count_today_utc >= max_orders_per_day:
            return _make_reject("DAILY_ORDER_COUNT_EXCEEDED", normalized)

    # ---- Step 7: allowed_time_utc（含跨午夜，Req 4 AC4）----------------
    allowed_time = limits.get("allowed_time_utc")
    if (
        isinstance(allowed_time, dict)
        and allowed_time.get("start")
        and allowed_time.get("end")
    ):
        current_now = now if now is not None else datetime.now(UTC)
        # 防御：传入 naive datetime 时假设为 UTC（与 audit_writer 同语义）。
        if current_now.tzinfo is None:
            current_now = current_now.replace(tzinfo=UTC)
        if not is_within_time_window(
            current_now.astimezone(UTC).time(),
            allowed_time["start"],
            allowed_time["end"],
        ):
            return _make_reject("TIME_WINDOW_VIOLATION", normalized)

    # ---- 反幻觉校验（Req 16 AC1 / Property 9）---------------------------
    # 仅对 place_order 有意义：cancel_order 引用的是已存在订单 id，read_*
    # 是请求行情、不需事先存在 snapshot 中。symbol 已小写，market_snapshot
    # 的 key 由调用方按小写约定填充。
    #
    # G2 信息流追踪扩展（Phase 2 / 默认关闭）：
    # 每个 snapshot 条目可带 ``provenance`` 字段标记数据来源；启用
    # ``global_config.enforce_market_provenance=True`` 后,place_order 路径
    # 只信任 :data:`TRUSTED_MARKET_PROVENANCES` 内的来源。不在白名单内
    # （含 ``"user_provided"`` / ``"unknown"`` / 字段缺失）一律拒绝——
    # 防"用户上传文档 / RAG 文档诱导 agent 按伪造价格下单"。
    #
    # **向后兼容**：默认 ``enforce_market_provenance=False``,现有调用方
    # 行为不变。生产部署应启用,届时所有调用方需为 snapshot 条目显式
    # 标注 ``provenance="seed"`` / ``"htx_real"`` 等可信值。
    if action_type == "place_order":
        if not isinstance(symbol, str) or symbol not in market_snapshot:
            return _make_reject("PLAN_HALLUCINATION", normalized)
        # G2: provenance trust label 校验（仅在 enforce_market_provenance=True 时启用）。
        # 默认关闭以向后兼容；生产部署应启用——所有 market_snapshot 条目都
        # 必须带 trusted provenance 才能进 place_order 路径。
        if global_config.enforce_market_provenance:
            snapshot_entry = market_snapshot.get(symbol, {}) or {}
            provenance = (
                snapshot_entry.get("provenance", DEFAULT_PROVENANCE_WHEN_MISSING)
                if isinstance(snapshot_entry, dict)
                else DEFAULT_PROVENANCE_WHEN_MISSING
            )
            if provenance not in TRUSTED_MARKET_PROVENANCES:
                return _make_reject("MARKET_DATA_UNTRUSTED", normalized)

    # ---- Final verdict --------------------------------------------------
    # 1) read_market / read_account 通过所有检查 → ALLOW（Req 7 AC8）。
    if action_type in _READ_ONLY_ACTION_TYPES:
        return PolicyVerdict(
            verdict="ALLOW",
            reason_codes=(),
            risk_score=_RISK_SCORE_ALLOW,
            normalized_action=normalized,
        )

    # 2) place_order / cancel_order：看 approval.required_for_trade（Req 7 AC9）。
    approval_cfg: dict[str, Any] = policy.get("approval", {})
    if approval_cfg.get("required_for_trade", True):
        # G18 风险分级自动审批：若配置了 auto_approval_thresholds 且本 action
        # 满足全部阈值 → 直接 ALLOW（写 APPROVAL_AUTO_APPROVED 审计由调用方负责）。
        # 设计依据：docs/tech-research/07-...md §7.1 Facio 4 层 L1 实现。
        if _passes_auto_approval_thresholds(
            normalized,
            approval_cfg.get("auto_approval_thresholds"),
            daily_history=daily_history,
            global_config=global_config,
        ):
            return PolicyVerdict(
                verdict="ALLOW",
                reason_codes=("AUTO_APPROVED_LOW_RISK",),
                risk_score=_RISK_SCORE_ALLOW,
                normalized_action=normalized,
            )
        return PolicyVerdict(
            verdict="REQUIRE_APPROVAL",
            reason_codes=(),
            risk_score=_RISK_SCORE_REQUIRE_APPROVAL,
            normalized_action=normalized,
        )
    return PolicyVerdict(
        verdict="ALLOW",
        reason_codes=(),
        risk_score=_RISK_SCORE_ALLOW,
        normalized_action=normalized,
    )


# ---------------------------------------------------------------------------
# 6. 审计写入助手（POLICY_CHECK_COMPLETED 事件）
# ---------------------------------------------------------------------------
def write_policy_check_completed_audit_event(
    db: Session,
    *,
    user_id: UUID,
    passport_id: UUID | None,
    action_id: UUID | None,
    trace_id: UUID | None,
    verdict: PolicyVerdict,
) -> AuditEvent:
    """把 :class:`PolicyVerdict` 写入 POLICY_CHECK_COMPLETED 审计事件（Req 7 AC10）。

    本函数被 :func:`evaluate_policy` 故意排除在外——pure-function 设计让
    PBT 重放更稳定（Property 1）。任务 11/13 在拿到 verdict 后调用本函数
    以保证 design.md「反馈层 / 审计哈希链」的事件覆盖率。

    Parameters
    ----------
    db : Session
        当前请求 / 测试用例的 SQLAlchemy 会话；事务由调用方管理（与
        :class:`AuditWriter` 一致）。
    user_id : UUID
        审计事件归属的用户。
    passport_id, action_id, trace_id : UUID | None
        审计事件外键 / 追踪 id；与其它 ``write_audit_event`` 调用保持一致。
    verdict : PolicyVerdict
        :func:`evaluate_policy` 返回值。

    Returns
    -------
    AuditEvent
        ``flush`` 后的审计事件 ORM 行（``event_hash`` 已计算）。

    Raises
    ------
    AuditWriteError
        审计写入失败（Req 11 AC7：调用方应捕获并回滚业务事务）。
    """
    return write_audit_event(
        db,
        event_type=AuditEventType.POLICY_CHECK_COMPLETED,
        user_id=user_id,
        passport_id=passport_id,
        action_id=action_id,
        actor_type=ACTOR_TYPE_POLICY_ENGINE,
        actor_id=ACTOR_TYPE_POLICY_ENGINE,
        trace_id=trace_id,
        event_data={
            "verdict": verdict.verdict,
            "reason_codes": list(verdict.reason_codes),
            "risk_score": verdict.risk_score,
            "normalized_action": verdict.normalized_action,
        },
    )


__all__ = [
    "REASON_CODES",
    "REASON_CODES_SET",
    "DailyActionHistory",
    "GlobalConfig",
    "PolicyVerdict",
    "Verdict",
    "evaluate_policy",
    "is_within_time_window",
    "normalize_action",
    "write_policy_check_completed_audit_event",
]
