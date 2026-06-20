"""G3 累积式 reason_codes 诊断测试（Phase 2）。

**Validates: docs/tech-research/06-...md §6.1.5**
—— 把"first-match-wins"早返回模式与"全景透视"的累积模式分开为两个函数:
``evaluate_policy``（强语义，确定性 PBT 守护）+ ``diagnose_policy``（前端
调试 / 审计场景全景）。

测试覆盖
--------
1. 单一违规 → 单一 reason_code（与 evaluate_policy 一致的边界）
2. 多重违规 → 多个 reason_codes 同时出现
3. EXECUTION_DISABLED + capability 缺失 同时报告（cumulative 不静默）
4. blocked_action + capability 缺失 同时报告
5. 全部检查通过 → would_be_rejected=False + 空 reason_codes
6. read_market 路径不报 PLAN_HALLUCINATION
7. no_op 路径只检查 kill switch 和 blocked_actions
8. provenance 与 PLAN_HALLUCINATION 互斥（symbol 不在 snapshot 内时不再查 provenance）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.services.policy_diagnostics import (
    PolicyDiagnosis,
    diagnose_policy,
)
from app.services.policy_engine import (
    DailyActionHistory,
    GlobalConfig,
)


_FIXED_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


def _make_policy(**overrides: Any) -> dict[str, Any]:
    base = {
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
            "required_for_trade": False,
            "required_for_policy_change": False,
        },
        "blocked_actions": [],
    }
    for k, v in overrides.items():
        base[k] = v
    return base


def _place_order(notional: float = 5, symbol: str = "btcusdt") -> dict[str, Any]:
    return {
        "type": "place_order",
        "symbol": symbol,
        "side": "buy",
        "order_type": "limit",
        "amount": 0.0001,
        "amount_unit": "base",
        "max_notional_usdt": notional,
        "limit_price": 68000,
        "requires_user_approval": False,
        "rationale": "test",
    }


def _diagnose(
    *,
    action: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    history: DailyActionHistory | None = None,
    snapshot: dict[str, dict[str, Any]] | None = None,
    config: GlobalConfig | None = None,
) -> PolicyDiagnosis:
    return diagnose_policy(
        action=action if action is not None else _place_order(),
        policy=policy if policy is not None else _make_policy(),
        daily_history=history if history is not None else DailyActionHistory(),
        market_snapshot=snapshot if snapshot is not None else {
            "btcusdt": {"last": 68000.0, "provenance": "seed"},
        },
        global_config=config if config is not None else GlobalConfig(),
        now=_FIXED_NOW,
    )


# ===========================================================================
# 1. 单一违规
# ===========================================================================
class TestSingleViolation:
    def test_capability_not_granted(self) -> None:
        policy = _make_policy()
        policy["capabilities"]["place_order"] = False
        result = _diagnose(policy=policy)
        assert result.would_be_rejected
        assert result.triggered_reason_codes == ("CAPABILITY_NOT_GRANTED",)

    def test_symbol_not_allowed(self) -> None:
        policy = _make_policy()
        policy["limits"]["allowed_symbols"] = ["ethusdt"]  # 不含 btcusdt
        result = _diagnose(policy=policy)
        assert result.would_be_rejected
        # cumulative 模式下"PLAN_HALLUCINATION"也会触发,因为 snapshot 含 btcusdt
        # 但 SYMBOL_NOT_ALLOWED 应该出现
        assert "SYMBOL_NOT_ALLOWED" in result.triggered_reason_codes

    def test_max_notional_exceeded(self) -> None:
        result = _diagnose(action=_place_order(notional=50))  # > 20
        assert "LIMIT_MAX_NOTIONAL_EXCEEDED" in result.triggered_reason_codes

    def test_no_violations_passes(self) -> None:
        result = _diagnose()
        assert result.would_be_rejected is False
        assert result.triggered_reason_codes == ()


# ===========================================================================
# 2. 多重违规累积
# ===========================================================================
class TestMultipleViolations:
    """**核心**：cumulative 模式同时报告所有违规,evaluate_policy 早返回不再丢信息。"""

    def test_capability_and_blocked_action_both_reported(self) -> None:
        """capability 关 + 同时被 blocked → 两个都出现。"""
        policy = _make_policy()
        policy["capabilities"]["place_order"] = False
        policy["blocked_actions"] = ["place_order"]
        # 但 BLOCKED_ACTION_PLACE_ORDER 不在 REASON_CODES_SET → 兜底为 UNKNOWN_TOOL_CALL
        result = _diagnose(policy=policy)
        assert result.would_be_rejected
        codes = result.triggered_reason_codes
        # 至少包含 capability + blocked
        assert "CAPABILITY_NOT_GRANTED" in codes
        assert "BLOCKED_ACTION_UNKNOWN_TOOL_CALL" in codes

    def test_kill_switch_and_other_violations_both_reported(self) -> None:
        """**关键差异**：evaluate_policy 早返回 EXECUTION_DISABLED 时静默后续;
        diagnose 同时报告。"""
        policy = _make_policy()
        policy["capabilities"]["place_order"] = False  # 同时关闭 capability
        result = _diagnose(
            policy=policy,
            config=GlobalConfig(demo_disable_execution=True),
        )
        codes = result.triggered_reason_codes
        assert "EXECUTION_DISABLED" in codes
        assert "CAPABILITY_NOT_GRANTED" in codes

    def test_notional_and_daily_limit_both_reported(self) -> None:
        """单笔超限 + 日累计超限 → 两个都出现。"""
        action = _place_order(notional=50)  # > 20 (单笔上限)
        history = DailyActionHistory(
            total_notional_today_utc=80,  # 80 + 50 > 100
            order_count_today_utc=2,
        )
        result = _diagnose(action=action, history=history)
        codes = result.triggered_reason_codes
        assert "LIMIT_MAX_NOTIONAL_EXCEEDED" in codes
        assert "DAILY_LIMIT_EXCEEDED" in codes

    def test_full_house_violations(self) -> None:
        """构造一个尽可能多违规的极端 action,验证 cumulative 完整报告。"""
        policy = _make_policy()
        policy["capabilities"]["place_order"] = False
        policy["limits"]["allowed_symbols"] = ["ethusdt"]  # 非 btcusdt
        policy["limits"]["allowed_time_utc"] = {
            "start": "01:00",
            "end": "02:00",
        }  # 12:00 不在窗口
        action = _place_order(notional=50)  # 超单笔
        history = DailyActionHistory(
            total_notional_today_utc=80,  # +50 超日累计
            order_count_today_utc=10,  # 达上限
        )
        # snapshot 仍含 btcusdt → 不触发 PLAN_HALLUCINATION
        result = _diagnose(action=action, policy=policy, history=history)
        codes = result.triggered_reason_codes
        # 同时报告
        assert "CAPABILITY_NOT_GRANTED" in codes
        assert "SYMBOL_NOT_ALLOWED" in codes
        assert "LIMIT_MAX_NOTIONAL_EXCEEDED" in codes
        assert "DAILY_LIMIT_EXCEEDED" in codes
        assert "DAILY_ORDER_COUNT_EXCEEDED" in codes
        assert "TIME_WINDOW_VIOLATION" in codes
        # 至少 6 个不同 reason
        assert len(codes) >= 6


# ===========================================================================
# 3. read_market / read_account / no_op 路径
# ===========================================================================
class TestNonWriteActionTypes:
    def test_read_market_passes_with_empty_snapshot(self) -> None:
        """read_market 不需要 snapshot 内有 symbol → 不报 PLAN_HALLUCINATION。"""
        action = {
            "type": "read_market",
            "symbol": "btcusdt",
            "side": "none",
            "order_type": "limit",
            "amount": 0,
            "amount_unit": "none",
            "max_notional_usdt": 0,
            "limit_price": None,
            "requires_user_approval": False,
            "rationale": "look",
        }
        result = _diagnose(action=action, snapshot={})  # 空 snapshot
        assert result.would_be_rejected is False

    def test_no_op_only_checks_kill_switch(self) -> None:
        """no_op 只走 Step 0/1，不进入 capability/symbol 等检查。"""
        action = {
            "type": "no_op",
            "rationale": "stand by",
        }
        result = _diagnose(action=action)
        # 默认 kill switch 关 → no violations
        assert result.triggered_reason_codes == ()

    def test_no_op_with_kill_switch_passes(self) -> None:
        """no_op 不受 kill switch 影响（与 evaluate_policy 一致）。"""
        action = {"type": "no_op", "rationale": "x"}
        result = _diagnose(
            action=action, config=GlobalConfig(demo_disable_execution=True)
        )
        assert result.would_be_rejected is False


# ===========================================================================
# 4. G2 provenance 互动
# ===========================================================================
class TestProvenanceWithDiagnose:
    def test_provenance_violation_when_enforced(self) -> None:
        snapshot = {"btcusdt": {"last": 68000, "provenance": "user_provided"}}
        result = _diagnose(
            snapshot=snapshot,
            config=GlobalConfig(enforce_market_provenance=True),
        )
        assert "MARKET_DATA_UNTRUSTED" in result.triggered_reason_codes

    def test_no_provenance_check_when_disabled(self) -> None:
        snapshot = {"btcusdt": {"last": 68000, "provenance": "user_provided"}}
        result = _diagnose(snapshot=snapshot)  # default enforce=False
        assert "MARKET_DATA_UNTRUSTED" not in result.triggered_reason_codes

    def test_plan_hallucination_priority_over_provenance(self) -> None:
        """symbol 不在 snapshot → 报 PLAN_HALLUCINATION,不进 provenance 检查。"""
        snapshot = {"ethusdt": {"last": 3600, "provenance": "user_provided"}}
        # 让 allowed_symbols 含 btcusdt 以避免 SYMBOL_NOT_ALLOWED
        result = _diagnose(
            snapshot=snapshot,
            config=GlobalConfig(enforce_market_provenance=True),
        )
        codes = result.triggered_reason_codes
        assert "PLAN_HALLUCINATION" in codes
        # MARKET_DATA_UNTRUSTED **不应**出现，因为 symbol 未进入 snapshot
        # （diagnose 内部 elif 分支保证互斥）
        assert "MARKET_DATA_UNTRUSTED" not in codes


# ===========================================================================
# 5. PolicyDiagnosis 数据类性质
# ===========================================================================
class TestPolicyDiagnosisDataClass:
    def test_immutable_frozen_dataclass(self) -> None:
        """PolicyDiagnosis frozen=True → 不可变,可哈希,可在 set/dict 中使用。"""
        result = _diagnose()
        # 不可变
        import dataclasses
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.triggered_reason_codes = ("X",)  # type: ignore[misc]

    def test_normalized_action_includes_lowercase_symbol(self) -> None:
        """normalized_action 与 evaluate_policy 一致——symbol 小写。"""
        action = _place_order()
        action["symbol"] = "BTCUSDT"  # 大写
        result = _diagnose(action=action)
        assert result.normalized_action["symbol"] == "btcusdt"


import pytest  # noqa: E402 — imported here for the FrozenInstanceError test only
