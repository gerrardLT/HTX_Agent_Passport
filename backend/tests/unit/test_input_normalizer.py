"""单元测试：输入归一化器 + 规则路由器 + 上下文构建器（任务 9 / Req 5 + Req 18 AC4）。

覆盖范围：
1. 拦截关键字覆盖（中英文 10 个关键字 + 大小写不敏感 + 句中包含）
2. 非拦截输入（正常交易任务、行情查询、下单指令）
3. 边界输入（空字符串、纯空白、超长输入）
4. build_blocked_action_plan 输出校验
5. build_planner_context 构建与压缩
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.context_builder import (
    MAX_TOKENS,
    PlannerContext,
    build_planner_context,
    estimate_tokens,
)
from app.services.input_normalizer import (
    NormalizedInput,
    build_blocked_action_plan,
    normalize_and_route,
)


# ===========================================================================
# 1. 拦截关键字覆盖
# ===========================================================================
class TestBlockKeywordsEnglish:
    """英文拦截关键字测试。"""

    def test_blocks_withdraw_en(self) -> None:
        result = normalize_and_route("I want to withdraw all USDT")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_WITHDRAW"

    def test_blocks_transfer_out_en(self) -> None:
        result = normalize_and_route("please transfer_out my BTC")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_TRANSFER_OUT"

    def test_blocks_borrow_en(self) -> None:
        result = normalize_and_route("I want to borrow some USDT")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_BORROW"

    def test_blocks_margin_en(self) -> None:
        result = normalize_and_route("open a margin position on BTC")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_MARGIN"

    def test_blocks_leverage_en(self) -> None:
        result = normalize_and_route("use 10x leverage on ETH")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_LEVERAGE"


class TestBlockKeywordsChinese:
    """中文拦截关键字测试。"""

    def test_blocks_提现_zh(self) -> None:
        result = normalize_and_route("请帮我提现所有 USDT")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_提现"

    def test_blocks_转出_zh(self) -> None:
        result = normalize_and_route("把 BTC 转出到外部钱包")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_转出"

    def test_blocks_借贷_zh(self) -> None:
        result = normalize_and_route("我想借贷一些 USDT")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_借贷"

    def test_blocks_杠杆_zh(self) -> None:
        result = normalize_and_route("开 10 倍杠杆做多 BTC")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_杠杆"

    def test_blocks_保证金_zh(self) -> None:
        result = normalize_and_route("追加保证金")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_保证金"


class TestBlockKeywordsCaseInsensitive:
    """大小写不敏感测试。"""

    def test_blocks_uppercase_withdraw(self) -> None:
        result = normalize_and_route("WITHDRAW all my funds")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_WITHDRAW"

    def test_blocks_mixed_case_withdraw(self) -> None:
        result = normalize_and_route("Withdraw my BTC please")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_WITHDRAW"

    def test_blocks_keyword_in_sentence(self) -> None:
        """关键字出现在句子中间也应拦截。"""
        result = normalize_and_route("请帮我提现所有 USDT 到我的钱包")
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_提现"


# ===========================================================================
# 2. 非拦截输入
# ===========================================================================
class TestNormalTaskPasses:
    """正常任务不应被拦截。"""

    def test_normal_task_passes(self) -> None:
        result = normalize_and_route("查看 BTC/USDT 并准备 10 USDT 限价买入")
        assert result.mode == "task"
        assert result.blocked_reason is None

    def test_read_market_passes(self) -> None:
        result = normalize_and_route("查看 BTC/USDT 行情")
        assert result.mode == "task"
        assert result.blocked_reason is None

    def test_place_order_passes(self) -> None:
        result = normalize_and_route("买入 10 USDT 的 BTC")
        assert result.mode == "task"
        assert result.blocked_reason is None


# ===========================================================================
# 3. 边界输入
# ===========================================================================
class TestBoundaryInputs:
    """边界输入测试。"""

    def test_empty_string_passes(self) -> None:
        """空字符串不拦截，让后续流程处理。"""
        result = normalize_and_route("")
        assert result.mode == "task"
        assert result.blocked_reason is None

    def test_whitespace_only_passes(self) -> None:
        """纯空白不拦截。"""
        result = normalize_and_route("   \t\n  ")
        assert result.mode == "task"
        assert result.blocked_reason is None

    def test_very_long_input_with_keyword(self) -> None:
        """超长输入中间含关键字仍应拦截。"""
        padding = "a" * 5000
        task = f"{padding}withdraw{padding}"
        result = normalize_and_route(task)
        assert result.mode == "blocked_shortcut"
        assert result.blocked_reason == "BLOCKED_KEYWORD_WITHDRAW"


# ===========================================================================
# 4. build_blocked_action_plan
# ===========================================================================
class TestBuildBlockedActionPlan:
    """build_blocked_action_plan 输出校验。"""

    def test_generates_valid_no_op_plan(self) -> None:
        """输出包含 ActionPlan v0 schema 所有顶层必填字段。"""
        plan = build_blocked_action_plan("提现所有 USDT", "BLOCKED_KEYWORD_提现")

        # 顶层必填字段
        assert plan["version"] == "0.1"
        assert "intent_summary" in plan
        assert "actions" in plan
        assert "assumptions" in plan
        assert "risk_notes" in plan

        # actions 结构
        assert len(plan["actions"]) == 1
        action = plan["actions"][0]
        assert action["type"] == "no_op"
        assert "rationale" in action

    def test_plan_contains_blocked_reason(self) -> None:
        """rationale 包含 blocked_reason。"""
        reason = "BLOCKED_KEYWORD_WITHDRAW"
        plan = build_blocked_action_plan("withdraw all", reason)

        action = plan["actions"][0]
        assert reason in action["rationale"]
        assert reason in plan["intent_summary"]

    def test_plan_truncates_long_task(self) -> None:
        """超长任务文本在 rationale 中被截断到 200 字符。"""
        long_task = "x" * 500
        plan = build_blocked_action_plan(long_task, "BLOCKED_KEYWORD_WITHDRAW")

        action = plan["actions"][0]
        # rationale 中的原始任务部分不超过 200 字符
        assert long_task[:200] in action["rationale"]
        assert long_task[:201] not in action["rationale"]


# ===========================================================================
# 5. build_planner_context
# ===========================================================================
class TestBuildPlannerContext:
    """上下文构建器测试。"""

    def _sample_policy(self) -> dict:
        return {
            "version": "0.1",
            "capabilities": {"read_market": True, "place_order": True},
            "limits": {
                "allowed_symbols": ["btcusdt"],
                "max_notional_usdt_per_order": 20,
                "max_daily_notional_usdt": 100,
                "max_orders_per_day": 10,
            },
            "approval": {"required_for_trade": True, "expires_after_seconds": 300},
            "blocked_actions": ["withdraw", "borrow"],
        }

    def _sample_market_snapshot(self) -> dict:
        return {"btcusdt": {"last": 68000, "bid": 67999, "ask": 68001}}

    def test_basic_context_construction(self) -> None:
        """正常构建 PlannerContext。"""
        now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        ctx = build_planner_context(
            passport_policy=self._sample_policy(),
            task="查看 BTC 行情",
            market_snapshot=self._sample_market_snapshot(),
            recent_actions=None,
            now=now,
        )

        assert isinstance(ctx, PlannerContext)
        assert ctx.passport_policy_json == self._sample_policy()
        assert ctx.current_market_snapshot == self._sample_market_snapshot()
        assert ctx.user_task == "查看 BTC 行情"
        assert "2024-06-15" in ctx.current_time_utc
        assert ctx.recent_actions_summary == "无历史操作"
        assert ctx.estimated_tokens > 0

    def test_context_under_8k_tokens(self) -> None:
        """正常输入 estimated_tokens < 8K。"""
        ctx = build_planner_context(
            passport_policy=self._sample_policy(),
            task="买入 10 USDT 的 BTC",
            market_snapshot=self._sample_market_snapshot(),
            recent_actions=[
                {"state": "EXECUTED", "natural_language_request": "查看行情"},
                {"state": "EXECUTED", "natural_language_request": "买入 BTC"},
            ],
        )

        assert ctx.estimated_tokens < MAX_TOKENS

    def test_context_compression_when_over_8k(self) -> None:
        """大量 recent_actions 时压缩到 < 8K。"""
        # 构造一个大的 market_snapshot（~7300 tokens），
        # 加上 5 条中文 action 摘要（~1000 tokens）会超过 8K，触发压缩。
        large_snapshot = {
            f"token{i}usdt": {"last": 100 + i, "bid": 99 + i, "ask": 101 + i}
            for i in range(400)
        }

        # 每条 action 的 natural_language_request 用 200 个中文字符
        large_actions = [
            {
                "state": "EXECUTED",
                "natural_language_request": f"任务{i}：" + "这是详细的操作描述内容" * 20,
            }
            for i in range(10)
        ]

        ctx = build_planner_context(
            passport_policy=self._sample_policy(),
            task="查看行情",
            market_snapshot=large_snapshot,
            recent_actions=large_actions,
        )

        # 压缩后 summary 只包含最近 2 条（压缩后的限制）
        lines = [l for l in ctx.recent_actions_summary.split("\n") if l.strip()]
        assert len(lines) <= 2

    def test_empty_market_snapshot(self) -> None:
        """空 snapshot 不报错。"""
        ctx = build_planner_context(
            passport_policy=self._sample_policy(),
            task="查看行情",
            market_snapshot={},
            recent_actions=None,
        )

        assert isinstance(ctx, PlannerContext)
        assert ctx.current_market_snapshot == {}

    def test_empty_recent_actions(self) -> None:
        """无历史 action 不报错。"""
        ctx = build_planner_context(
            passport_policy=self._sample_policy(),
            task="查看行情",
            market_snapshot=self._sample_market_snapshot(),
            recent_actions=[],
        )

        assert isinstance(ctx, PlannerContext)
        assert ctx.recent_actions_summary == "无历史操作"


# ===========================================================================
# 6. estimate_tokens 辅助函数
# ===========================================================================
class TestEstimateTokens:
    """Token 估算函数测试。"""

    def test_empty_string(self) -> None:
        assert estimate_tokens("") == 0

    def test_english_text(self) -> None:
        # "hello" = 5 bytes UTF-8 → (5 + 2) // 3 = 2
        result = estimate_tokens("hello")
        assert result > 0

    def test_chinese_text(self) -> None:
        # 中文每字 3 bytes UTF-8 → 3 chars = 9 bytes → (9 + 2) // 3 = 3
        result = estimate_tokens("你好世界")
        assert result > 0

    def test_returns_positive_for_nonempty(self) -> None:
        assert estimate_tokens("a") >= 1
