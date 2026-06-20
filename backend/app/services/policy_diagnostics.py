"""Policy 诊断 / cumulative reason_codes 累积（修复 G3 / Phase 2）。

设计原因
--------
当前 :func:`app.services.policy_engine.evaluate_policy` 是 **first-match-wins**
早返回模式——任一步触发 REJECT 即返回单一 reason_code。这是 Req 7 AC1 严格
顺序裁决的实现，PBT 与确定性测试都依赖这一语义。

但 docs/tech-research/06-...md §6.1.5（G3）指出实际场景常需要"看一眼这条
action 究竟违反了多少规则"——例如：

- 用户调试 policy 时希望一眼看到所有问题，而非反复试错。
- 审计员拿到 REJECTED action，希望了解"违反 N 条规则中的哪些"。
- 切换到 Cedar 后，多个 ``forbid`` 同时触发是天然语义。

实施策略
--------
**不改动现有 evaluate_policy**——避免破坏 Property 1 的"同输入恒同输出"
PBT 与 30 例策略测试。新增 :func:`diagnose_policy` 函数，**只读取**输入,
返回所有"假设单独评估"会触发的 reason_codes 集合。

调用方式
--------
- 主要给前端 / 调试 / 审计场景用：``GET /api/audit/.../diagnose`` 类端点
  暴露 cumulative 视图。
- 不进入 evaluate_policy 调用链——执行/审批路径仍走 first-match-wins
  的强语义保证。

设计权衡
--------
- **不复用 evaluate_policy 的早返回逻辑**：那些早返回是出于"暴露 reason
  最少信息"的安全考虑（Step 0 kill switch 优先于其他原因输出）；
  diagnose 是"全景透视"，刻意暴露所有违规。
- **reason_codes 顺序保留 Step 顺序**：与 Req 7 AC1 的 7 步骨架一致,
  方便用户对照设计文档定位问题。
- **不报告 ALLOW 时的"近似违规"**：例如 notional 接近上限但未超时,
  不会出现在 reason_codes 中——本函数只列触发的 reason，不列"差点触发"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.services.policy_engine import (
    DEFAULT_PROVENANCE_WHEN_MISSING,
    REASON_CODES_SET,
    TRUSTED_MARKET_PROVENANCES,
    DailyActionHistory,
    GlobalConfig,
    is_within_time_window,
    normalize_action,
)


@dataclass(frozen=True)
class PolicyDiagnosis:
    """累积式策略诊断结果。

    Attributes
    ----------
    triggered_reason_codes : tuple[str, ...]
        本 action 假设独立评估**所有**检查项，触发的全部 reason_codes。
        顺序与 :func:`diagnose_policy` 内的检查顺序一致（与 Req 7 AC1 的
        7 步骨架对齐）。
    would_be_rejected : bool
        是否至少有一项被触发。``True`` 等价于 evaluate_policy 会返回
        ``REJECT``——但**不一定**返回相同的 reason_code（first-match 早
        返回可能是更靠前的 step）。
    normalized_action : dict[str, Any]
        与 evaluate_policy 一致的归一化 action（symbol 小写）。
    """

    triggered_reason_codes: tuple[str, ...]
    would_be_rejected: bool
    normalized_action: dict[str, Any]


# 内部分类常量（与 policy_engine 同步；本模块刻意不 import 私有常量,
# 避免 policy_engine 内部 refactor 触动本模块）。
_READ_ONLY_TYPES = frozenset({"read_market", "read_account"})
_WRITE_TYPES = frozenset({"place_order", "cancel_order"})
_NO_OP = "no_op"
_ACTION_CAPABILITY_MAP = {
    "read_market": "read_market",
    "read_account": "read_account",
    "place_order": "place_order",
    "cancel_order": "cancel_order",
}


def diagnose_policy(
    action: dict[str, Any],
    policy: dict[str, Any],
    daily_history: DailyActionHistory,
    market_snapshot: dict[str, dict[str, Any]],
    global_config: GlobalConfig,
    *,
    now: datetime | None = None,
) -> PolicyDiagnosis:
    """累积式诊断：返回 action 假设独立评估时**所有**触发的 reason_codes。

    本函数与 :func:`evaluate_policy` 的关键区别：

    1. **不早返回**：跑完所有检查项,把全部触发的 reason_codes 收集到
       一个 tuple 里。
    2. **kill switch / blocked_actions 不静音其他检查**：``EXECUTION_DISABLED``
       / ``BLOCKED_ACTION_*`` 可以与 ``CAPABILITY_NOT_GRANTED`` 等同时出现。
    3. **始终运行 7 步检查**：即便某检查项理论上"在前一步早返回后就不再
       评估"，本函数仍评估并报告。

    Parameters
    ----------
    与 :func:`evaluate_policy` 完全一致。

    Returns
    -------
    PolicyDiagnosis
        累积诊断结果。``triggered_reason_codes`` 顺序与检查顺序一致;
        ``would_be_rejected`` = ``len(triggered_reason_codes) > 0``。

    Notes
    -----
    本函数**不写审计事件**——和 evaluate_policy 一致的 pure-function 设计。
    主要消费方是前端 audit-replay 页面或 CLI 调试工具。
    """
    normalized = normalize_action(action)
    action_type = normalized.get("type")
    triggered: list[str] = []

    # ---- Step 0: 全局 kill switch ----
    # cumulative 模式下不"静默"——同时报告 EXECUTION_DISABLED 和后续 violations,
    # 让用户知道 kill switch 是当前阻断的原因之一,而非唯一原因。
    if (
        global_config.demo_disable_execution
        and action_type not in _READ_ONLY_TYPES
        and action_type != _NO_OP
    ):
        triggered.append("EXECUTION_DISABLED")

    # ---- Step 1: blocked_actions ----
    blocked_actions: list[str] = list(policy.get("blocked_actions", []))
    if isinstance(action_type, str) and action_type in blocked_actions:
        reason = f"BLOCKED_ACTION_{action_type.upper()}"
        if reason not in REASON_CODES_SET:
            reason = "BLOCKED_ACTION_UNKNOWN_TOOL_CALL"
        triggered.append(reason)

    # ---- Step 2: capabilities ----
    capabilities: dict[str, Any] = policy.get("capabilities", {})
    if action_type != _NO_OP:
        cap_field = _ACTION_CAPABILITY_MAP.get(action_type or "")
        if cap_field is None or not bool(capabilities.get(cap_field, False)):
            triggered.append("CAPABILITY_NOT_GRANTED")

    # 之后检查仅对带 symbol 的写操作 / 读操作有意义；no_op 跳过
    if action_type == _NO_OP:
        return PolicyDiagnosis(
            triggered_reason_codes=tuple(triggered),
            would_be_rejected=bool(triggered),
            normalized_action=normalized,
        )

    limits: dict[str, Any] = policy.get("limits", {})
    symbol = normalized.get("symbol")

    # ---- Step 3: allowed_symbols ----
    if isinstance(symbol, str):
        allowed_symbols = {
            s.lower() for s in limits.get("allowed_symbols", []) if isinstance(s, str)
        }
        if symbol not in allowed_symbols:
            triggered.append("SYMBOL_NOT_ALLOWED")

    # ---- Step 4: max_notional_usdt_per_order ----
    if action_type == "place_order":
        max_notional_per_order = float(limits.get("max_notional_usdt_per_order", 0))
        action_notional = float(normalized.get("max_notional_usdt", 0))
        if action_notional > max_notional_per_order:
            triggered.append("LIMIT_MAX_NOTIONAL_EXCEEDED")

        # ---- Step 5: max_daily_notional_usdt ----
        max_daily_notional = float(limits.get("max_daily_notional_usdt", 0))
        if (
            daily_history.total_notional_today_utc + action_notional
            > max_daily_notional
        ):
            triggered.append("DAILY_LIMIT_EXCEEDED")

    # ---- Step 6: max_orders_per_day ----
    if action_type in _WRITE_TYPES:
        max_orders_per_day = int(limits.get("max_orders_per_day", 0))
        if daily_history.order_count_today_utc >= max_orders_per_day:
            triggered.append("DAILY_ORDER_COUNT_EXCEEDED")

    # ---- Step 7: allowed_time_utc ----
    allowed_time = limits.get("allowed_time_utc")
    if (
        isinstance(allowed_time, dict)
        and allowed_time.get("start")
        and allowed_time.get("end")
    ):
        current_now = now if now is not None else datetime.now(UTC)
        if current_now.tzinfo is None:
            current_now = current_now.replace(tzinfo=UTC)
        if not is_within_time_window(
            current_now.astimezone(UTC).time(),
            allowed_time["start"],
            allowed_time["end"],
        ):
            triggered.append("TIME_WINDOW_VIOLATION")

    # ---- 反幻觉 + G2 provenance ----
    if action_type == "place_order":
        if not isinstance(symbol, str) or symbol not in market_snapshot:
            triggered.append("PLAN_HALLUCINATION")
        elif global_config.enforce_market_provenance:
            entry = market_snapshot.get(symbol, {}) or {}
            provenance = (
                entry.get("provenance", DEFAULT_PROVENANCE_WHEN_MISSING)
                if isinstance(entry, dict)
                else DEFAULT_PROVENANCE_WHEN_MISSING
            )
            if provenance not in TRUSTED_MARKET_PROVENANCES:
                triggered.append("MARKET_DATA_UNTRUSTED")

    return PolicyDiagnosis(
        triggered_reason_codes=tuple(triggered),
        would_be_rejected=bool(triggered),
        normalized_action=normalized,
    )


__all__ = [
    "PolicyDiagnosis",
    "diagnose_policy",
]
