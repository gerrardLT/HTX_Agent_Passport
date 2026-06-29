"""上下文构建器（任务 9 / Req 5 AC1,AC3）。

本模块实现感知层的上下文构建职责：
- 组装 planner prompt 所需的上下文（policy + market_snapshot + task + time + recent_actions）。
- 控制总 token 数 < 8K（Req 5 AC3）：超出时压缩 recent_actions_summary
  而非截断 policy 或 market snapshot。

设计依据
--------
- 方法论 §4：先补事实再决策；控制 < 8K tokens。
- Req 5 AC1：将 passport_policy_json、current_market_snapshot、user_task 注入 planner prompt。
- Req 5 AC3：prompt 总 token 数控制在 8K 以内，超出时压缩历史上下文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

# ---------------------------------------------------------------------------
# 1. 常量
# ---------------------------------------------------------------------------
#: 最大允许 token 数（Req 5 AC3）。
MAX_TOKENS: Final[int] = 8000

#: 压缩后保留的最近 action 数量。
COMPRESSED_RECENT_ACTIONS_LIMIT: Final[int] = 2

#: 正常保留的最近 action 数量。
DEFAULT_RECENT_ACTIONS_LIMIT: Final[int] = 5


# ---------------------------------------------------------------------------
# 2. 数据类型
# ---------------------------------------------------------------------------
@dataclass
class PlannerContext:
    """Planner 上下文——注入到 B.AI planner prompt 的结构化数据。

    Attributes
    ----------
    passport_policy_json : dict
        Passport 的 policy_json（能力包）。
    current_market_snapshot : dict
        当前市场快照（symbol → {last, ...}）。
    user_task : str
        用户原始自然语言任务。
    current_time_utc : str
        当前 UTC 时间的 ISO 格式字符串。
    recent_actions_summary : str
        最近 N 条 action 的摘要文本。
    estimated_tokens : int
        估算的总 token 数。
    """

    passport_policy_json: dict[str, Any]
    current_market_snapshot: dict[str, Any]
    user_task: str
    current_time_utc: str
    recent_actions_summary: str
    estimated_tokens: int


# ---------------------------------------------------------------------------
# 3. Token 估算
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中英混合按 UTF-8 字节数 / 3 的保守估计。

    对于纯英文文本，实际 token 数约为字符数 / 4；对于中文，约为
    字符数 / 1.5。使用 UTF-8 字节数 / 3 作为中英混合的折中估计。

    Parameters
    ----------
    text : str
        待估算的文本。

    Returns
    -------
    int
        估算的 token 数（向上取整）。
    """
    if not text:
        return 0
    byte_count = len(text.encode("utf-8"))
    # UTF-8 字节数 / 3 是中英混合的保守估计
    return max(1, (byte_count + 2) // 3)


# ---------------------------------------------------------------------------
# 4. 内部工具函数
# ---------------------------------------------------------------------------
def _format_recent_actions(
    recent_actions: list[dict[str, Any]] | None,
    limit: int = DEFAULT_RECENT_ACTIONS_LIMIT,
) -> str:
    """将最近 action 列表格式化为摘要文本。

    Parameters
    ----------
    recent_actions : list[dict] | None
        最近的 action 记录列表，每条包含 state / natural_language_request 等字段。
    limit : int
        最多保留的 action 数量。

    Returns
    -------
    str
        格式化后的摘要文本；无 action 时返回 "无历史操作"。
    """
    if not recent_actions:
        return "无历史操作"

    actions_to_show = recent_actions[:limit]
    lines: list[str] = []
    for i, action in enumerate(actions_to_show, 1):
        state = action.get("state", "UNKNOWN")
        task = action.get("natural_language_request", "")[:200]
        lines.append(f"{i}. [{state}] {task}")

    return "\n".join(lines)


def _compute_total_text(context: PlannerContext) -> str:
    """将 PlannerContext 的所有文本拼接，用于 token 估算。"""
    parts = [
        json.dumps(context.passport_policy_json, ensure_ascii=False),
        json.dumps(context.current_market_snapshot, ensure_ascii=False),
        context.user_task,
        context.current_time_utc,
        context.recent_actions_summary,
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 5. 主入口
# ---------------------------------------------------------------------------
def build_planner_context(
    passport_policy: dict[str, Any],
    task: str,
    market_snapshot: dict[str, Any],
    recent_actions: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> PlannerContext:
    """构建 Planner 上下文（方法论 §4：先补事实再决策；控制 < 8K tokens）。

    流程：
    1. 组装 recent_actions_summary（最近 5 条 action 的 state + task 摘要）。
    2. 估算 token 数。
    3. 若超 8K，压缩 recent_actions_summary（截断到最近 2 条）。
    4. 返回 PlannerContext。

    Parameters
    ----------
    passport_policy : dict
        Passport 的 policy_json。
    task : str
        用户自然语言任务。
    market_snapshot : dict
        当前市场快照。
    recent_actions : list[dict] | None
        最近的 action 记录列表。
    now : datetime | None
        当前 UTC 时间；None 时使用 ``datetime.now(UTC)``。

    Returns
    -------
    PlannerContext
        构建完成的上下文，estimated_tokens < 8K（除非 policy + snapshot
        本身就超 8K，此时仅压缩 recent_actions 部分）。
    """
    current_time = now if now is not None else datetime.now(UTC)
    current_time_utc = current_time.isoformat()

    # Step 1: 组装 recent_actions_summary
    summary = _format_recent_actions(recent_actions, limit=DEFAULT_RECENT_ACTIONS_LIMIT)

    # Step 2: 构建初始 context
    ctx = PlannerContext(
        passport_policy_json=passport_policy,
        current_market_snapshot=market_snapshot,
        user_task=task,
        current_time_utc=current_time_utc,
        recent_actions_summary=summary,
        estimated_tokens=0,
    )

    # Step 3: 估算 token 数
    total_text = _compute_total_text(ctx)
    tokens = estimate_tokens(total_text)
    ctx.estimated_tokens = tokens

    # Step 4: 若超 8K，压缩 recent_actions_summary
    if tokens > MAX_TOKENS:
        compressed_summary = _format_recent_actions(
            recent_actions, limit=COMPRESSED_RECENT_ACTIONS_LIMIT
        )
        ctx.recent_actions_summary = compressed_summary
        # 重新估算
        total_text = _compute_total_text(ctx)
        ctx.estimated_tokens = estimate_tokens(total_text)

    return ctx


__all__ = [
    "COMPRESSED_RECENT_ACTIONS_LIMIT",
    "DEFAULT_RECENT_ACTIONS_LIMIT",
    "MAX_TOKENS",
    "PlannerContext",
    "build_planner_context",
    "estimate_tokens",
]
