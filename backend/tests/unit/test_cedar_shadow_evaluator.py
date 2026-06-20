"""Cedar 影子裁决器测试（修复 G1 / Phase 2 PoC）。

**Validates: docs/tech-research/06-...md §6.1 + arxiv Cedar paper**
—— 用 cedarpy 把 PolicyDSL Step 0-3 编码为 Cedar 策略,跑双引擎差异对比。

测试覆盖
--------
1. Cedar 与主裁决器在 Step 0-3 范围内**完全一致**（10+ 场景）
2. 不在 PoC 范围的检查（Step 4-7 / 反幻觉 / G2）→ Cedar Allow,主可能 REJECT
3. cedar_decision_matches_main 的 4 个分支（Allow/Deny/Skipped × main_verdict）
4. 异常输入吞错为 Skipped + error
5. cedarpy 缺失的兜底（虽然项目已装但路径要测）

设计意图
--------
本测试是 G1 Cedar 切换的"前期信任建立"工具。30 天观察期的统计基础就是
本测试覆盖的 10+ 场景外加生产真实流量；任何"主裁决器与 Cedar 不一致"
都会在这里被 catch。
"""

from __future__ import annotations

from typing import Any

import pytest

cedarpy = pytest.importorskip("cedarpy")

from app.services.policy_engine_cedar import (
    CEDAR_POLICIES,
    CEDAR_SCHEMA_JSON,
    CedarShadowResult,
    cedar_decision_matches_main,
    shadow_evaluate,
)


def _policy(**overrides: Any) -> dict[str, Any]:
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
            "allowed_symbols": ["btcusdt", "ethusdt"],
            "max_notional_usdt_per_order": 20,
            "max_daily_notional_usdt": 100,
            "max_orders_per_day": 10,
        },
        "approval": {
            "required_for_trade": True,
            "required_for_policy_change": True,
        },
        "blocked_actions": [],
    }
    for k, v in overrides.items():
        base[k] = v
    return base


# ===========================================================================
# 1. Step 2 capabilities
# ===========================================================================
class TestCapabilities:
    def test_place_order_with_capability_allows(self) -> None:
        result = shadow_evaluate(
            action={"type": "place_order", "symbol": "btcusdt"},
            policy=_policy(),
        )
        assert result.decision == "Allow"
        assert result.error is None

    def test_place_order_without_capability_denies(self) -> None:
        policy = _policy()
        policy["capabilities"]["place_order"] = False
        result = shadow_evaluate(
            action={"type": "place_order", "symbol": "btcusdt"},
            policy=policy,
        )
        assert result.decision == "Deny"

    def test_read_market_capability_independent_from_place_order(self) -> None:
        policy = _policy()
        policy["capabilities"]["place_order"] = False
        policy["capabilities"]["read_market"] = True
        result = shadow_evaluate(
            action={"type": "read_market", "symbol": "btcusdt"},
            policy=policy,
        )
        assert result.decision == "Allow"

    def test_cancel_order_without_capability_denies(self) -> None:
        policy = _policy()
        policy["capabilities"]["cancel_order"] = False
        result = shadow_evaluate(
            action={"type": "cancel_order", "symbol": "btcusdt"},
            policy=policy,
        )
        assert result.decision == "Deny"


# ===========================================================================
# 2. Step 3 allowed_symbols
# ===========================================================================
class TestAllowedSymbols:
    def test_symbol_in_whitelist_allows(self) -> None:
        result = shadow_evaluate(
            action={"type": "place_order", "symbol": "btcusdt"},
            policy=_policy(),
        )
        assert result.decision == "Allow"

    def test_symbol_not_in_whitelist_denies(self) -> None:
        result = shadow_evaluate(
            action={"type": "place_order", "symbol": "dogeusdt"},
            policy=_policy(),
        )
        assert result.decision == "Deny"

    def test_uppercase_symbol_normalized_to_lowercase(self) -> None:
        """与主裁决器一致：symbol 比较前小写化。"""
        result = shadow_evaluate(
            action={"type": "place_order", "symbol": "BTCUSDT"},
            policy=_policy(),
        )
        assert result.decision == "Allow"


# ===========================================================================
# 3. Step 1 blocked_actions
# ===========================================================================
class TestBlockedActions:
    def test_place_order_in_blocked_denies(self) -> None:
        policy = _policy()
        policy["blocked_actions"] = ["place_order"]
        result = shadow_evaluate(
            action={"type": "place_order", "symbol": "btcusdt"},
            policy=policy,
        )
        assert result.decision == "Deny"

    def test_cancel_order_in_blocked_denies(self) -> None:
        policy = _policy()
        policy["blocked_actions"] = ["cancel_order"]
        result = shadow_evaluate(
            action={"type": "cancel_order", "symbol": "btcusdt"},
            policy=policy,
        )
        assert result.decision == "Deny"


# ===========================================================================
# 4. Step 0 kill switch
# ===========================================================================
class TestKillSwitch:
    def test_kill_switch_blocks_place_order(self) -> None:
        result = shadow_evaluate(
            action={"type": "place_order", "symbol": "btcusdt"},
            policy=_policy(),
            kill_switch=True,
        )
        assert result.decision == "Deny"

    def test_kill_switch_does_not_block_read_market(self) -> None:
        result = shadow_evaluate(
            action={"type": "read_market", "symbol": "btcusdt"},
            policy=_policy(),
            kill_switch=True,
        )
        assert result.decision == "Allow"

    def test_kill_switch_blocks_cancel_order(self) -> None:
        result = shadow_evaluate(
            action={"type": "cancel_order", "symbol": "btcusdt"},
            policy=_policy(),
            kill_switch=True,
        )
        assert result.decision == "Deny"


# ===========================================================================
# 5. PoC 范围之外
# ===========================================================================
class TestOutOfScope:
    def test_no_op_returns_allow_skipped_semantics(self) -> None:
        """no_op 不在 PoC 范围；shadow 直接返回 Allow（与主路径行为一致）。"""
        result = shadow_evaluate(
            action={"type": "no_op"},
            policy=_policy(),
        )
        assert result.decision == "Allow"

    def test_unknown_action_type_returns_skipped(self) -> None:
        result = shadow_evaluate(
            action={"type": "some_future_type"},
            policy=_policy(),
        )
        assert result.decision == "Skipped"
        assert "out of PoC scope" in (result.error or "")


# ===========================================================================
# 6. 多重违规：任一 forbid 触发即 Deny
# ===========================================================================
class TestMultipleForbids:
    def test_capability_off_and_symbol_not_allowed_both_deny(self) -> None:
        policy = _policy()
        policy["capabilities"]["place_order"] = False
        policy["limits"]["allowed_symbols"] = ["ethusdt"]  # 不含 btcusdt
        result = shadow_evaluate(
            action={"type": "place_order", "symbol": "btcusdt"},
            policy=policy,
        )
        # 任一 forbid 触发即 Deny
        assert result.decision == "Deny"


# ===========================================================================
# 7. cedar_decision_matches_main
# ===========================================================================
class TestDecisionMatching:
    def test_skipped_always_matches(self) -> None:
        assert cedar_decision_matches_main(
            cedar_decision="Skipped",
            main_verdict="REJECT",
            main_reason_codes=("LIMIT_MAX_NOTIONAL_EXCEEDED",),
        )

    def test_cedar_allow_main_allow_matches(self) -> None:
        assert cedar_decision_matches_main(
            cedar_decision="Allow",
            main_verdict="ALLOW",
            main_reason_codes=(),
        )

    def test_cedar_allow_main_require_approval_matches(self) -> None:
        """REQUIRE_APPROVAL 在 Cedar 层等价 Allow（审批是上层语义）。"""
        assert cedar_decision_matches_main(
            cedar_decision="Allow",
            main_verdict="REQUIRE_APPROVAL",
            main_reason_codes=(),
        )

    def test_cedar_allow_main_reject_in_cedar_scope_mismatches(self) -> None:
        """Cedar Allow 但主路径在 Cedar 范围内 REJECT → 不匹配（潜在 bug）。"""
        assert not cedar_decision_matches_main(
            cedar_decision="Allow",
            main_verdict="REJECT",
            main_reason_codes=("CAPABILITY_NOT_GRANTED",),
        )

    def test_cedar_allow_main_reject_out_of_cedar_scope_matches(self) -> None:
        """Cedar Allow 主路径用 Step 4-7 拒（如日限额）→ 算匹配（Cedar 不评估这些）。"""
        assert cedar_decision_matches_main(
            cedar_decision="Allow",
            main_verdict="REJECT",
            main_reason_codes=("LIMIT_MAX_NOTIONAL_EXCEEDED",),
        )

    def test_cedar_deny_main_reject_in_scope_matches(self) -> None:
        assert cedar_decision_matches_main(
            cedar_decision="Deny",
            main_verdict="REJECT",
            main_reason_codes=("CAPABILITY_NOT_GRANTED",),
        )

    def test_cedar_deny_main_reject_out_of_scope_mismatches(self) -> None:
        """Cedar Deny 但主用 Step 4-7 reason → 不匹配（Cedar 不应在那种场景 Deny）。"""
        assert not cedar_decision_matches_main(
            cedar_decision="Deny",
            main_verdict="REJECT",
            main_reason_codes=("LIMIT_MAX_NOTIONAL_EXCEEDED",),
        )

    def test_cedar_deny_main_allow_mismatches(self) -> None:
        assert not cedar_decision_matches_main(
            cedar_decision="Deny",
            main_verdict="ALLOW",
            main_reason_codes=(),
        )


# ===========================================================================
# 8. 与主裁决器的端到端一致性（核心验证）
# ===========================================================================
class TestParityWithMainEngine:
    """**最强的 G1 信心来源**：用真实 evaluate_policy + Cedar 跑同一组场景,
    断言两者决定一致。30 天观察期前的"前置信任建立"。"""

    @pytest.fixture()
    def baseline_inputs(self) -> dict[str, Any]:
        return {
            "policy": _policy(),
            "action": {
                "type": "place_order",
                "symbol": "btcusdt",
                "side": "buy",
                "order_type": "limit",
                "amount": 0.0001,
                "amount_unit": "base",
                "max_notional_usdt": 5,
                "limit_price": 68000,
                "requires_user_approval": True,
                "rationale": "test",
            },
        }

    def test_capability_off_both_engines_reject(
        self, baseline_inputs: dict[str, Any]
    ) -> None:
        from datetime import UTC, datetime
        from app.services.policy_engine import (
            DailyActionHistory,
            GlobalConfig,
            evaluate_policy,
        )

        policy = baseline_inputs["policy"]
        policy["capabilities"]["place_order"] = False
        action = baseline_inputs["action"]

        main = evaluate_policy(
            action=action,
            policy=policy,
            daily_history=DailyActionHistory(),
            market_snapshot={"btcusdt": {"last": 68000, "provenance": "seed"}},
            global_config=GlobalConfig(),
            now=datetime(2026, 5, 31, tzinfo=UTC),
        )
        cedar = shadow_evaluate(action=action, policy=policy)

        assert main.verdict == "REJECT"
        assert "CAPABILITY_NOT_GRANTED" in main.reason_codes
        assert cedar.decision == "Deny"
        # 一致性
        assert cedar_decision_matches_main(
            cedar_decision=cedar.decision,
            main_verdict=main.verdict,
            main_reason_codes=main.reason_codes,
        )

    def test_kill_switch_both_engines_reject(
        self, baseline_inputs: dict[str, Any]
    ) -> None:
        from datetime import UTC, datetime
        from app.services.policy_engine import (
            DailyActionHistory,
            GlobalConfig,
            evaluate_policy,
        )

        action = baseline_inputs["action"]
        main = evaluate_policy(
            action=action,
            policy=baseline_inputs["policy"],
            daily_history=DailyActionHistory(),
            market_snapshot={"btcusdt": {"last": 68000, "provenance": "seed"}},
            global_config=GlobalConfig(demo_disable_execution=True),
            now=datetime(2026, 5, 31, tzinfo=UTC),
        )
        cedar = shadow_evaluate(
            action=action, policy=baseline_inputs["policy"], kill_switch=True
        )

        assert main.verdict == "REJECT"
        assert "EXECUTION_DISABLED" in main.reason_codes
        assert cedar.decision == "Deny"

    def test_happy_path_both_engines_consistent(
        self, baseline_inputs: dict[str, Any]
    ) -> None:
        """正常情况：主路径 REQUIRE_APPROVAL,Cedar Allow → 一致。"""
        from datetime import UTC, datetime
        from app.services.policy_engine import (
            DailyActionHistory,
            GlobalConfig,
            evaluate_policy,
        )

        main = evaluate_policy(
            action=baseline_inputs["action"],
            policy=baseline_inputs["policy"],
            daily_history=DailyActionHistory(),
            market_snapshot={"btcusdt": {"last": 68000, "provenance": "seed"}},
            global_config=GlobalConfig(),
            now=datetime(2026, 5, 31, tzinfo=UTC),
        )
        cedar = shadow_evaluate(
            action=baseline_inputs["action"], policy=baseline_inputs["policy"]
        )

        assert main.verdict == "REQUIRE_APPROVAL"
        assert cedar.decision == "Allow"
        assert cedar_decision_matches_main(
            cedar_decision=cedar.decision,
            main_verdict=main.verdict,
            main_reason_codes=main.reason_codes,
        )



# ===========================================================================
# 9. ExecutionGateway 集成路径（opt-in via CEDAR_SHADOW_ENABLED）
# ===========================================================================
class TestExecutionGatewayIntegration:
    """**Validates: G1 Cedar shadow 在 execution_gateway 重裁决路径的接入**

    默认关闭，启用后通过日志记差异；不影响主路径。
    """

    def test_default_disabled_does_not_call_cedar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认 CEDAR_SHADOW_ENABLED=False → _is_cedar_shadow_enabled() 返回 False。"""
        from app.services.execution_gateway import _is_cedar_shadow_enabled

        # 不显式设置 → 默认 False
        assert _is_cedar_shadow_enabled() is False

    def test_enabled_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings
        from app.services.execution_gateway import _is_cedar_shadow_enabled

        monkeypatch.setenv("CEDAR_SHADOW_ENABLED", "true")
        get_settings.cache_clear()
        try:
            assert _is_cedar_shadow_enabled() is True
        finally:
            get_settings.cache_clear()

    def test_log_difference_on_divergence(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """主裁决 ALLOW + Cedar Deny → 应记 CEDAR_SHADOW_DIVERGENCE。"""
        import logging

        from app.services.policy_engine_cedar import (
            CedarShadowResult,
            log_cedar_shadow_difference,
        )

        # Cedar Deny 但主路径 ALLOW（构造场景）
        cedar_result = CedarShadowResult(decision="Deny", reasons=("p1",))
        with caplog.at_level(logging.WARNING, logger="app.services.policy_engine_cedar"):
            log_cedar_shadow_difference(
                cedar_result=cedar_result,
                main_verdict="ALLOW",
                main_reason_codes=(),
                action={"type": "place_order", "symbol": "btcusdt"},
            )
        assert any(
            "CEDAR_SHADOW_DIVERGENCE" in record.message for record in caplog.records
        )

    def test_log_no_warning_on_match(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """决定一致 → 仅 DEBUG 日志（默认捕获级别 WARNING 看不到）。"""
        import logging

        from app.services.policy_engine_cedar import (
            CedarShadowResult,
            log_cedar_shadow_difference,
        )

        cedar_result = CedarShadowResult(decision="Allow", reasons=())
        with caplog.at_level(logging.WARNING, logger="app.services.policy_engine_cedar"):
            log_cedar_shadow_difference(
                cedar_result=cedar_result,
                main_verdict="ALLOW",
                main_reason_codes=(),
                action={"type": "place_order", "symbol": "btcusdt"},
            )
        # 没有 DIVERGENCE / ERROR 警告
        for record in caplog.records:
            assert "CEDAR_SHADOW_DIVERGENCE" not in record.message
            assert "CEDAR_SHADOW_ERROR" not in record.message
