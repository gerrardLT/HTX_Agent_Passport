"""G18 风险分级自动审批测试（Phase 3 / Facio L1 实现）。

**Validates: docs/tech-research/07-...md §7.1**
—— passport 级 ``approval.auto_approval_thresholds`` 让低风险高频操作不消耗
人工审批配额，缓解审批疲劳（Anthropic 与 Facio 都把这个识别为 HITL 头号
长期问题）。

测试策略
--------
- **纯函数 + 集成两层**：直接调 ``evaluate_policy``，断言 verdict + reason_codes。
- **保守默认覆盖**：未配置 / 部分配置 / 字段缺失都应走人工审批路径。
- **AND 语义**：4 个阈值必须**全部**满足才放行；任一不通过即不通过。
- **边界值**：阈值边界（恰好等于、恰好超过）行为符合规则。

关键不变量
----------
1. 默认配置（无 auto_approval_thresholds）→ 与原 REQUIRE_APPROVAL 等价。
2. 显式启用 + 全部满足 → ALLOW + reason_codes 含 ``AUTO_APPROVED_LOW_RISK``。
3. risk_score=0（与 ALLOW 一致），不被误算成 REQUIRE_APPROVAL 的 40。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.policy_engine import (
    DailyActionHistory,
    GlobalConfig,
    PolicyVerdict,
    evaluate_policy,
)

# ---------------------------------------------------------------------------
# 共享：构造 small_spot_executor-like policy + thresholds
# ---------------------------------------------------------------------------
_FIXED_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


def _make_policy(thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造一个最小但合法的策略，可选注入 auto_approval_thresholds。"""
    return {
        "version": "0.1",
        "capabilities": {
            "read_market": True,
            "read_account": True,
            "place_order": True,
            "cancel_order": True,
            "withdraw": False,
        },
        "limits": {
            "allowed_symbols": ["btcusdt"],
            "max_notional_usdt_per_order": 20,
            "max_daily_notional_usdt": 100,
            "max_orders_per_day": 10,
        },
        "approval": {
            "required_for_trade": True,
            "required_for_policy_change": True,
            "auto_approval_thresholds": thresholds,
        },
        "blocked_actions": [],
    }


def _place_order_action(notional: float = 5.0) -> dict[str, Any]:
    return {
        "type": "place_order",
        "symbol": "btcusdt",
        "side": "buy",
        "order_type": "limit",
        "amount": 0.0001,
        "amount_unit": "base",
        "max_notional_usdt": notional,
        "limit_price": 68000,
        "requires_user_approval": True,
        "rationale": "test",
    }


def _market_snapshot() -> dict[str, dict[str, Any]]:
    return {"btcusdt": {"last": 68000.0}}


def _evaluate(
    *,
    action: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    daily_history: DailyActionHistory | None = None,
    reputation: int | None = None,
) -> PolicyVerdict:
    return evaluate_policy(
        action=action if action is not None else _place_order_action(),
        policy=policy if policy is not None else _make_policy(),
        daily_history=daily_history or DailyActionHistory(),
        market_snapshot=_market_snapshot(),
        global_config=GlobalConfig(passport_reputation_score=reputation),
        now=_FIXED_NOW,
    )


# ===========================================================================
# 1. 保守默认：未配置 → 走人工审批
# ===========================================================================
class TestDefaultsRequireApproval:
    """**关键向后兼容保证**：未启用 G18 的 passport 行为与原版完全一致。"""

    def test_no_thresholds_field_requires_approval(self) -> None:
        """policy.approval 不含 auto_approval_thresholds → REQUIRE_APPROVAL。"""
        policy = _make_policy()
        # 移除字段（None 等价 / 不出现也等价）
        policy["approval"].pop("auto_approval_thresholds", None)
        verdict = _evaluate(policy=policy)
        assert verdict.verdict == "REQUIRE_APPROVAL"
        assert verdict.reason_codes == ()

    def test_thresholds_null_requires_approval(self) -> None:
        """显式 null → 等价"未启用"。"""
        policy = _make_policy(thresholds=None)
        verdict = _evaluate(policy=policy)
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_thresholds_empty_dict_requires_approval(self) -> None:
        """空 dict → 必填字段缺失 → 不放行（保守默认）。"""
        verdict = _evaluate(policy=_make_policy(thresholds={}))
        assert verdict.verdict == "REQUIRE_APPROVAL"


# ===========================================================================
# 2. 部分配置 → 不放行（"全或无"语义）
# ===========================================================================
class TestPartialConfigDoesNotAutoApprove:
    """4 个阈值是 AND 关系；任一缺失 → 走人工审批（防配错半套规则误放行）。"""

    @pytest.fixture()
    def full_thresholds(self) -> dict[str, Any]:
        return {
            "max_notional_usdt": 5.0,
            "min_reputation_score": 80,
            "allowed_action_types": ["place_order"],
            "max_per_day": 10,
        }

    def test_missing_action_types_falls_through(
        self, full_thresholds: dict[str, Any]
    ) -> None:
        full_thresholds.pop("allowed_action_types")
        verdict = _evaluate(
            policy=_make_policy(thresholds=full_thresholds),
            reputation=90,
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_missing_max_notional_falls_through(
        self, full_thresholds: dict[str, Any]
    ) -> None:
        full_thresholds.pop("max_notional_usdt")
        verdict = _evaluate(
            policy=_make_policy(thresholds=full_thresholds),
            reputation=90,
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_missing_min_reputation_falls_through(
        self, full_thresholds: dict[str, Any]
    ) -> None:
        full_thresholds.pop("min_reputation_score")
        verdict = _evaluate(
            policy=_make_policy(thresholds=full_thresholds),
            reputation=90,
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_missing_max_per_day_falls_through(
        self, full_thresholds: dict[str, Any]
    ) -> None:
        full_thresholds.pop("max_per_day")
        verdict = _evaluate(
            policy=_make_policy(thresholds=full_thresholds),
            reputation=90,
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"


# ===========================================================================
# 3. 全部满足 → AUTO_APPROVED_LOW_RISK
# ===========================================================================
class TestAllConditionsPassAutoApproves:
    @pytest.fixture()
    def thresholds(self) -> dict[str, Any]:
        return {
            "max_notional_usdt": 5.0,
            "min_reputation_score": 80,
            "allowed_action_types": ["place_order"],
            "max_per_day": 10,
        }

    def test_low_notional_high_rep_low_count_passes(
        self, thresholds: dict[str, Any]
    ) -> None:
        """notional=$2 ≤ $5, rep=90 ≥ 80, count=0 < 10 → ALLOW。"""
        verdict = _evaluate(
            action=_place_order_action(notional=2.0),
            policy=_make_policy(thresholds=thresholds),
            reputation=90,
        )
        assert verdict.verdict == "ALLOW"
        assert "AUTO_APPROVED_LOW_RISK" in verdict.reason_codes
        assert verdict.risk_score == 0

    def test_boundary_notional_passes(self, thresholds: dict[str, Any]) -> None:
        """notional=5.0（恰好等于阈值）→ 放行。"""
        verdict = _evaluate(
            action=_place_order_action(notional=5.0),
            policy=_make_policy(thresholds=thresholds),
            reputation=80,
        )
        assert verdict.verdict == "ALLOW"

    def test_boundary_reputation_passes(self, thresholds: dict[str, Any]) -> None:
        """reputation=80（恰好等于阈值）→ 放行。"""
        verdict = _evaluate(
            action=_place_order_action(notional=2.0),
            policy=_make_policy(thresholds=thresholds),
            reputation=80,
        )
        assert verdict.verdict == "ALLOW"


# ===========================================================================
# 4. 任一不满足 → REQUIRE_APPROVAL
# ===========================================================================
class TestAnyConditionFails:
    @pytest.fixture()
    def thresholds(self) -> dict[str, Any]:
        return {
            "max_notional_usdt": 5.0,
            "min_reputation_score": 80,
            "allowed_action_types": ["place_order"],
            "max_per_day": 10,
        }

    def test_notional_above_threshold(self, thresholds: dict[str, Any]) -> None:
        """notional=$10 > $5 → REQUIRE_APPROVAL。"""
        verdict = _evaluate(
            action=_place_order_action(notional=10.0),
            policy=_make_policy(thresholds=thresholds),
            reputation=90,
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_reputation_below_threshold(self, thresholds: dict[str, Any]) -> None:
        """rep=50 < 80 → REQUIRE_APPROVAL。"""
        verdict = _evaluate(
            policy=_make_policy(thresholds=thresholds),
            reputation=50,
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_reputation_none_blocks_auto_approval(
        self, thresholds: dict[str, Any]
    ) -> None:
        """**保守默认**：调用方未传 reputation → 不放行。

        防御场景：execution_gateway / approval_service 升级前的旧版本未传
        ``passport_reputation_score`` → 自动审批不应"默认放行"。
        """
        verdict = _evaluate(
            policy=_make_policy(thresholds=thresholds),
            reputation=None,
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_action_type_not_in_whitelist(
        self, thresholds: dict[str, Any]
    ) -> None:
        """thresholds 只允许 place_order → cancel_order 走人工审批。"""
        cancel_action = {
            "type": "cancel_order",
            "symbol": "btcusdt",
            "side": "none",
            "order_type": "limit",
            "amount": 0,
            "amount_unit": "none",
            "max_notional_usdt": 0,
            "limit_price": None,
            "requires_user_approval": True,
            "rationale": "cancel test",
        }
        verdict = _evaluate(
            action=cancel_action,
            policy=_make_policy(thresholds=thresholds),
            reputation=90,
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_daily_count_at_max_blocks(self, thresholds: dict[str, Any]) -> None:
        """auto_approved_count_today_utc=10（恰好等于 max_per_day）→ 拒。"""
        history = DailyActionHistory(
            total_notional_today_utc=0,
            order_count_today_utc=0,
            auto_approved_count_today_utc=10,  # ≥ max_per_day=10
        )
        verdict = _evaluate(
            action=_place_order_action(notional=2.0),
            policy=_make_policy(thresholds=thresholds),
            daily_history=history,
            reputation=90,
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_daily_count_below_max_passes(
        self, thresholds: dict[str, Any]
    ) -> None:
        """auto_approved_count_today_utc=9 < 10 → 放行。"""
        history = DailyActionHistory(
            total_notional_today_utc=0,
            order_count_today_utc=0,
            auto_approved_count_today_utc=9,
        )
        verdict = _evaluate(
            action=_place_order_action(notional=2.0),
            policy=_make_policy(thresholds=thresholds),
            daily_history=history,
            reputation=90,
        )
        assert verdict.verdict == "ALLOW"


# ===========================================================================
# 5. 与现有路径的交互
# ===========================================================================
class TestInteractionWithExistingChecks:
    """G18 不影响 REJECT 路径（blocked_actions / 限额超出 / SYMBOL_NOT_ALLOWED 等
    优先级更高）。"""

    def test_auto_approval_does_not_override_capability_reject(self) -> None:
        """capability 关闭 → 即便配了 auto_approval 也是 REJECT。"""
        policy = _make_policy(
            thresholds={
                "max_notional_usdt": 5.0,
                "min_reputation_score": 80,
                "allowed_action_types": ["place_order"],
                "max_per_day": 10,
            }
        )
        policy["capabilities"]["place_order"] = False
        verdict = _evaluate(policy=policy, reputation=90)
        assert verdict.verdict == "REJECT"
        assert "CAPABILITY_NOT_GRANTED" in verdict.reason_codes

    def test_auto_approval_does_not_override_blocked_action(self) -> None:
        """blocked_actions 命中 → 即便配了 auto_approval 也是 REJECT。"""
        policy = _make_policy(
            thresholds={
                "max_notional_usdt": 5.0,
                "min_reputation_score": 80,
                "allowed_action_types": ["place_order"],
                "max_per_day": 10,
            }
        )
        # 把 place_order 加入 blocked（人为）
        policy["blocked_actions"] = ["unknown_tool_call"]
        # 让 action.type = unknown_tool_call → blocked_actions 命中
        action = {
            "type": "unknown_tool_call",
            "symbol": "btcusdt",
            "side": "buy",
            "order_type": "limit",
            "amount": 0.0001,
            "amount_unit": "base",
            "max_notional_usdt": 2,
            "limit_price": 68000,
            "requires_user_approval": True,
            "rationale": "test",
        }
        verdict = _evaluate(action=action, policy=policy, reputation=90)
        assert verdict.verdict == "REJECT"
        assert "BLOCKED_ACTION_UNKNOWN_TOOL_CALL" in verdict.reason_codes

    def test_auto_approval_does_not_override_daily_limit(self) -> None:
        """日限额超出 → REJECT；G18 配置不能绕过。"""
        history = DailyActionHistory(
            total_notional_today_utc=99,  # 加 2 就 > 100
            order_count_today_utc=5,
            auto_approved_count_today_utc=0,
        )
        verdict = _evaluate(
            action=_place_order_action(notional=2.0),
            policy=_make_policy(
                thresholds={
                    "max_notional_usdt": 5.0,
                    "min_reputation_score": 80,
                    "allowed_action_types": ["place_order"],
                    "max_per_day": 10,
                }
            ),
            daily_history=history,
            reputation=90,
        )
        assert verdict.verdict == "REJECT"
        assert "DAILY_LIMIT_EXCEEDED" in verdict.reason_codes
