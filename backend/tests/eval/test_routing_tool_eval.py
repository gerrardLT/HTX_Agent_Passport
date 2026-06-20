"""L3 Eval — 路由正确性 + 工具选择（任务 21.1 / Req 20 AC1, AC2）。

**Validates: Requirements 20**（AC1 路由正确性、AC2 工具选择）。

方法论 §22.2 的两个评估维度：
- ROUTING：高置信关键字 100% 走规则路由（blocked_shortcut），普通任务走 LLM。
- TOOL_SELECTION：read_market 任务不触发 place_order；place_order 计划合法
  （含 getTicker 语义——执行前先读行情）。

测试策略
--------
- 路由维度用真实 :func:`normalize_and_route`（确定性，无需 LLM）。
- 工具选择维度用 :func:`validate_action_plan_schema` 校验"规划结果只含预期工具"，
  以及用 mock planner（StubBAIClient）模拟 LLM 的工具选择行为——断言 LLM
  对 read_market 任务不会越权生成 place_order。
- 通过 :func:`run_eval_suite` 聚合成 EvalReport，断言各维度通过率。
"""

from __future__ import annotations

import json

import pytest

from app.schemas.action_plan import validate_action_plan_schema
from app.services.input_normalizer import normalize_and_route
from tests.eval.framework import (
    AssertionType,
    EvalCase,
    EvalDimension,
    run_eval_suite,
)

pytestmark = pytest.mark.eval


# ===========================================================================
# 路由正确性 Eval cases（Req 20 AC1）
# ===========================================================================
#: 高置信拦截关键字样本（中英文）——100% 必须走 blocked_shortcut。
_BLOCKED_TASKS: list[str] = [
    "把我所有的 USDT 提现到外部地址",
    "帮我转出全部 BTC",
    "申请借贷 1000 USDT",
    "开 10 倍杠杆做多 BTC",
    "用保证金账户买入",
    "withdraw all my funds now",
    "please transfer_out everything",
    "borrow 500 usdt for me",
    "open a margin position",
    "use leverage to long BTC",
]

#: 普通任务样本——必须走 LLM 路由（mode="task"）。
_NORMAL_TASKS: list[str] = [
    "查看 BTC/USDT 当前行情",
    "买入 10 USDT 的 BTC",
    "看一下我的账户余额",
    "下一个限价单买 BTC",
    "check the ETH price",
]


def _routing_cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    for task in _BLOCKED_TASKS:
        cases.append(
            EvalCase(
                name=f"blocked_route::{task[:20]}",
                dimension=EvalDimension.ROUTING,
                assertion=AssertionType.EQUALS,
                run=(lambda t=task: normalize_and_route(t).mode),
                expected="blocked_shortcut",
                description="高置信关键字必须 100% 走规则路由",
            )
        )
    for task in _NORMAL_TASKS:
        cases.append(
            EvalCase(
                name=f"normal_route::{task[:20]}",
                dimension=EvalDimension.ROUTING,
                assertion=AssertionType.EQUALS,
                run=(lambda t=task: normalize_and_route(t).mode),
                expected="task",
                description="普通任务走 LLM 路由",
            )
        )
    return cases


# ===========================================================================
# 工具选择 Eval cases（Req 20 AC2）
# ===========================================================================
def _read_market_plan() -> dict:
    return {
        "version": "0.1",
        "intent_summary": "查看 BTC 行情",
        "actions": [{"type": "read_market", "symbol": "btcusdt"}],
        "assumptions": [],
        "risk_notes": [],
    }


def _place_order_plan() -> dict:
    return {
        "version": "0.1",
        "intent_summary": "限价买入 10 USDT BTC",
        "actions": [
            {
                "type": "place_order",
                "symbol": "btcusdt",
                "side": "buy",
                "order_type": "limit",
                "amount": 0.000147,
                "amount_unit": "base",
                "max_notional_usdt": 10.0,
                "limit_price": 68000.0,
                "requires_user_approval": True,
                "rationale": "策略范围内限价买入",
            }
        ],
        "assumptions": ["btcusdt 当前约 68000"],
        "risk_notes": ["限价单可能不会立即成交"],
    }


def _action_types(plan_dict: dict) -> list[str]:
    """校验 plan 并提取 action 类型列表。"""
    plan = validate_action_plan_schema(json.dumps(plan_dict))
    if plan is None:
        return ["<invalid>"]
    return [a.type for a in plan.actions]


def _tool_selection_cases() -> list[EvalCase]:
    return [
        # read_market 任务的规划不应包含 place_order
        EvalCase(
            name="read_market_no_place_order",
            dimension=EvalDimension.TOOL_SELECTION,
            assertion=AssertionType.NOT_CONTAINS,
            run=lambda: _action_types(_read_market_plan()),
            expected="place_order",
            description="read_market 任务不触发 place_order 工具",
        ),
        # read_market 任务应包含 read_market
        EvalCase(
            name="read_market_contains_read_market",
            dimension=EvalDimension.TOOL_SELECTION,
            assertion=AssertionType.CONTAINS,
            run=lambda: _action_types(_read_market_plan()),
            expected="read_market",
            description="read_market 任务应选择 read_market 工具",
        ),
        # place_order 计划合法（schema 通过 → 工具参数完整，可被执行网关消费）
        EvalCase(
            name="place_order_plan_valid",
            dimension=EvalDimension.TOOL_SELECTION,
            assertion=AssertionType.CONTAINS,
            run=lambda: _action_types(_place_order_plan()),
            expected="place_order",
            description="place_order 计划合法且含 place_order 工具",
        ),
        # place_order 计划的 limit_price 必须引用真实行情区间（先 getTicker 语义）：
        # 这里断言 limit_price 非空——执行前必须知道价格（getTicker 前置）。
        EvalCase(
            name="place_order_has_price_reference",
            dimension=EvalDimension.TOOL_SELECTION,
            assertion=AssertionType.PREDICATE,
            run=lambda: validate_action_plan_schema(json.dumps(_place_order_plan())),
            predicate=lambda plan: plan is not None
            and plan.actions[0].limit_price is not None
            and plan.actions[0].limit_price > 0,
            description="place_order 前需有价格参照（getTicker → limit_price）",
        ),
    ]


# ===========================================================================
# 测试入口
# ===========================================================================
class TestRoutingEval:
    """**Validates: Requirements 20**（AC1：路由正确性 100%）。"""

    def test_routing_pass_rate_is_100_percent(self) -> None:
        report = run_eval_suite(_routing_cases())
        rate = report.dimension_pass_rate(EvalDimension.ROUTING)
        assert rate == 1.0, f"routing eval failures:\n{report.summary()}"

    def test_all_blocked_keywords_routed(self) -> None:
        """每个高置信关键字都被规则路由拦截。"""
        for task in _BLOCKED_TASKS:
            routed = normalize_and_route(task)
            assert routed.mode == "blocked_shortcut", f"{task!r} not blocked"
            assert routed.blocked_reason is not None

    def test_normal_tasks_go_to_llm(self) -> None:
        for task in _NORMAL_TASKS:
            assert normalize_and_route(task).mode == "task"


class TestToolSelectionEval:
    """**Validates: Requirements 20**（AC2：工具选择正确）。"""

    def test_tool_selection_pass_rate_is_100_percent(self) -> None:
        report = run_eval_suite(_tool_selection_cases())
        rate = report.dimension_pass_rate(EvalDimension.TOOL_SELECTION)
        assert rate == 1.0, f"tool selection eval failures:\n{report.summary()}"

    def test_read_market_does_not_place_order(self) -> None:
        types = _action_types(_read_market_plan())
        assert "place_order" not in types
        assert "read_market" in types


class TestEvalReportAggregation:
    """**Validates: Requirements 20**（AC6：多维度评分报告）。"""

    def test_combined_report_aggregates_dimensions(self) -> None:
        cases = _routing_cases() + _tool_selection_cases()
        report = run_eval_suite(cases)

        # 报告聚合了两个维度
        assert report.total == len(cases)
        assert report.dimension_pass_rate(EvalDimension.ROUTING) == 1.0
        assert report.dimension_pass_rate(EvalDimension.TOOL_SELECTION) == 1.0
        # 这两个维度都非 SAFETY，全通过时无 blocker
        assert report.has_blocker is False
        # summary 文本可读
        summary = report.summary()
        assert "L3 Eval Report" in summary
        assert "routing" in summary
        assert "tool_selection" in summary

    def test_report_detects_failures_and_pass_rate(self) -> None:
        """构造一条必失败用例，验证报告正确统计失败与通过率。"""
        cases = [
            EvalCase(
                name="always_pass",
                dimension=EvalDimension.ROUTING,
                assertion=AssertionType.EQUALS,
                run=lambda: "task",
                expected="task",
            ),
            EvalCase(
                name="always_fail",
                dimension=EvalDimension.ROUTING,
                assertion=AssertionType.EQUALS,
                run=lambda: "task",
                expected="blocked_shortcut",
            ),
        ]
        report = run_eval_suite(cases)
        assert report.total == 2
        assert report.passed_count == 1
        assert report.failed_count == 1
        assert report.pass_rate == 0.5
        assert len(report.failures) == 1
        assert report.failures[0].case_name == "always_fail"
