"""输入归一化器 + 规则路由器（任务 9 / Req 5 AC8 + Req 18 AC4）。

本模块实现感知层的两个核心职责：
1. **规则路由**：高置信关键字（提现/withdraw/转出/transfer_out/借贷/borrow/
   杠杆/margin/保证金/leverage）直接拦截，不浪费 LLM 预算。
2. **拦截后生成 no_op ActionPlan**：命中关键字时直接生成 type=no_op 的
   ActionPlan dict，不调用 B.AI。

设计依据
--------
- 方法论 §3：高置信意图先用规则拦截，不浪费模型预算。
- Req 5 AC8：包含高置信拦截关键字时，在调用 B.AI 之前通过规则路由直接
  生成 type=no_op 的 ActionPlan。
- Req 18 AC4：单元测试覆盖每个拦截关键字（中英文）、非拦截输入、边界输入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal


# ---------------------------------------------------------------------------
# 1. 数据类型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NormalizedInput:
    """归一化后的用户输入。

    Attributes
    ----------
    raw : str
        用户原始输入文本。
    mode : Literal["task", "blocked_shortcut"]
        - ``"task"``：正常任务，需要调用 B.AI 规划器。
        - ``"blocked_shortcut"``：命中高置信关键字，直接拦截。
    blocked_reason : str | None
        拦截原因码，格式为 ``BLOCKED_KEYWORD_{KEYWORD_UPPER}``。
        仅当 ``mode == "blocked_shortcut"`` 时非 None。
    """

    raw: str
    mode: Literal["task", "blocked_shortcut"]
    blocked_reason: str | None = None


# ---------------------------------------------------------------------------
# 2. 拦截关键字常量
# ---------------------------------------------------------------------------
#: 中英文高置信拦截关键字（Req 5 AC8 + design.md 感知层）。
#: 这些关键字代表 MVP 中硬性禁止的操作意图——即便用户通过自然语言
#: 表达，也不应浪费 LLM 预算去规划。
BLOCK_KEYWORDS: Final[dict[str, list[str]]] = {
    "zh": ["提现", "转出", "借贷", "杠杆", "保证金"],
    "en": ["withdraw", "transfer_out", "borrow", "margin", "leverage"],
}


# ---------------------------------------------------------------------------
# 3. 核心函数
# ---------------------------------------------------------------------------
def normalize_and_route(task: str) -> NormalizedInput:
    """归一化用户输入并执行规则路由（方法论 §3）。

    对输入做 ``lower()`` 后逐一匹配 :data:`BLOCK_KEYWORDS` 中的关键字。
    命中任一关键字即返回 ``blocked_shortcut`` 模式，不再继续匹配。

    Parameters
    ----------
    task : str
        用户原始自然语言任务文本。可以是空字符串或纯空白——
        这些边界情况不会命中任何关键字，返回 ``mode="task"``
        让后续流程（如 B.AI 调用前的参数校验）处理。

    Returns
    -------
    NormalizedInput
        归一化结果。``mode="blocked_shortcut"`` 表示命中拦截关键字。
    """
    task_lower = task.lower()
    for _lang, keywords in BLOCK_KEYWORDS.items():
        for kw in keywords:
            if kw in task_lower:
                return NormalizedInput(
                    raw=task,
                    mode="blocked_shortcut",
                    blocked_reason=f"BLOCKED_KEYWORD_{kw.upper()}",
                )
    return NormalizedInput(raw=task, mode="task", blocked_reason=None)


def build_blocked_action_plan(task: str, blocked_reason: str) -> dict:
    """为被拦截的任务生成 no_op ActionPlan dict（不调用 B.AI）。

    生成的 ActionPlan 遵循 ActionPlan v0 schema（Req 6），包含所有
    顶层必填字段：version / intent_summary / actions / assumptions / risk_notes。

    Parameters
    ----------
    task : str
        用户原始任务文本（截断到 200 字符写入 rationale）。
    blocked_reason : str
        拦截原因码，如 ``BLOCKED_KEYWORD_WITHDRAW``。

    Returns
    -------
    dict
        符合 ActionPlan v0 schema 的 dict，actions 中仅含一个 no_op action。
    """
    return {
        "version": "0.1",
        "intent_summary": f"任务被规则路由拦截: {blocked_reason}",
        "actions": [
            {
                "type": "no_op",
                "rationale": (
                    f"规则路由拦截: {blocked_reason}. "
                    f"原始任务: {task[:200]}"
                ),
            }
        ],
        "assumptions": [],
        "risk_notes": [
            "该请求包含被阻止的关键字，已被规则路由直接拒绝，未调用 LLM"
        ],
    }


__all__ = [
    "BLOCK_KEYWORDS",
    "NormalizedInput",
    "build_blocked_action_plan",
    "normalize_and_route",
]
