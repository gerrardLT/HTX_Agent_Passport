"""G2 信息流追踪 / market_snapshot.provenance 测试（Phase 2）。

**Validates: docs/tech-research/06-...md §6.2**
—— policy_engine 在 place_order 路径检查 market_snapshot 条目的 provenance
来源，仅信任白名单内的来源；防"用户上传文档/RAG 文档诱导按伪造价格下单"。

设计依据：CaMeL 的 capabilities 模型 + Tessera 的"trust label min, not max"
+ Anthropic Zero Trust。

测试覆盖
--------
1. 默认关闭（``enforce_market_provenance=False``）→ 现有 fixture 行为不变
2. 启用后 ``provenance="seed"`` / ``"htx_real"`` / ``"htx_cached"`` 都通过
3. 启用后 ``provenance="user_provided"`` / ``"unknown"`` / 缺失字段 → REJECT
4. read_market / cancel_order / no_op 路径不受影响（只对 place_order 强制）
5. SEED_MARKET_DATA 已带 provenance="seed"，与启用后的 policy 兼容
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.policy_engine import (
    DEFAULT_PROVENANCE_WHEN_MISSING,
    REASON_CODES_SET,
    TRUSTED_MARKET_PROVENANCES,
    DailyActionHistory,
    GlobalConfig,
    PolicyVerdict,
    evaluate_policy,
)


_FIXED_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


def _make_policy() -> dict[str, Any]:
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
            "required_for_trade": False,  # 简化：直接 ALLOW，便于断言
            "required_for_policy_change": False,
        },
        "blocked_actions": [],
    }


def _place_order_action() -> dict[str, Any]:
    return {
        "type": "place_order",
        "symbol": "btcusdt",
        "side": "buy",
        "order_type": "limit",
        "amount": 0.0001,
        "amount_unit": "base",
        "max_notional_usdt": 5,
        "limit_price": 68000,
        "requires_user_approval": False,
        "rationale": "test",
    }


def _read_market_action() -> dict[str, Any]:
    return {
        "type": "read_market",
        "symbol": "btcusdt",
        "side": "none",
        "order_type": "limit",
        "amount": 0,
        "amount_unit": "none",
        "max_notional_usdt": 0,
        "limit_price": None,
        "requires_user_approval": False,
        "rationale": "read",
    }


def _evaluate(
    *,
    snapshot: dict[str, dict[str, Any]],
    enforce: bool = False,
    action: dict[str, Any] | None = None,
) -> PolicyVerdict:
    return evaluate_policy(
        action=action if action is not None else _place_order_action(),
        policy=_make_policy(),
        daily_history=DailyActionHistory(),
        market_snapshot=snapshot,
        global_config=GlobalConfig(enforce_market_provenance=enforce),
        now=_FIXED_NOW,
    )


# ===========================================================================
# 0. 常量 sanity
# ===========================================================================
class TestConstants:
    def test_trusted_provenances_is_frozenset(self) -> None:
        """白名单是 frozenset → 不可变 + O(1) lookup。"""
        assert isinstance(TRUSTED_MARKET_PROVENANCES, frozenset)
        assert "seed" in TRUSTED_MARKET_PROVENANCES
        assert "htx_real" in TRUSTED_MARKET_PROVENANCES
        assert "htx_cached" in TRUSTED_MARKET_PROVENANCES

    def test_user_provided_not_trusted(self) -> None:
        assert "user_provided" not in TRUSTED_MARKET_PROVENANCES
        assert "unknown" not in TRUSTED_MARKET_PROVENANCES

    def test_default_when_missing_is_unknown(self) -> None:
        """字段缺失 → 视为 ``"unknown"``,不在白名单 → 不放行。"""
        assert DEFAULT_PROVENANCE_WHEN_MISSING == "unknown"
        assert DEFAULT_PROVENANCE_WHEN_MISSING not in TRUSTED_MARKET_PROVENANCES

    def test_market_data_untrusted_reason_code_registered(self) -> None:
        """新 reason_code 已登记到 REASON_CODES_SET。"""
        assert "MARKET_DATA_UNTRUSTED" in REASON_CODES_SET


# ===========================================================================
# 1. 默认关闭：行为与原版一致（向后兼容）
# ===========================================================================
class TestDefaultDisabled:
    """默认 ``enforce_market_provenance=False``——现有 fixture 不需要 provenance 字段。"""

    def test_no_provenance_field_passes_when_disabled(self) -> None:
        """无 provenance 字段也能下单 → 兼容现有调用方。"""
        snapshot = {"btcusdt": {"last": 68000.0}}  # 无 provenance
        verdict = _evaluate(snapshot=snapshot, enforce=False)
        assert verdict.verdict == "ALLOW"

    def test_user_provided_passes_when_disabled(self) -> None:
        """**关键**：默认关闭时连 user_provided 都放行——这是向后兼容代价。"""
        snapshot = {"btcusdt": {"last": 68000.0, "provenance": "user_provided"}}
        verdict = _evaluate(snapshot=snapshot, enforce=False)
        assert verdict.verdict == "ALLOW"


# ===========================================================================
# 2. 启用后：白名单内通过
# ===========================================================================
class TestEnforceEnabledTrustedSources:
    """生产配置 ``ENFORCE_MARKET_PROVENANCE=true`` 时，白名单 3 种来源放行。"""

    @pytest.mark.parametrize(
        "trusted_provenance",
        ["seed", "htx_real", "htx_cached"],
    )
    def test_trusted_provenance_passes(
        self, trusted_provenance: str
    ) -> None:
        snapshot = {
            "btcusdt": {"last": 68000.0, "provenance": trusted_provenance}
        }
        verdict = _evaluate(snapshot=snapshot, enforce=True)
        assert verdict.verdict == "ALLOW"
        # 不应触发 MARKET_DATA_UNTRUSTED
        assert "MARKET_DATA_UNTRUSTED" not in verdict.reason_codes


# ===========================================================================
# 3. 启用后：白名单外拒绝
# ===========================================================================
class TestEnforceEnabledUntrustedSources:
    """启用后，非白名单来源 + 字段缺失 + 字段类型异常 → 都 REJECT。"""

    def test_user_provided_rejected(self) -> None:
        snapshot = {
            "btcusdt": {"last": 68000.0, "provenance": "user_provided"}
        }
        verdict = _evaluate(snapshot=snapshot, enforce=True)
        assert verdict.verdict == "REJECT"
        assert "MARKET_DATA_UNTRUSTED" in verdict.reason_codes
        assert verdict.risk_score == 95

    def test_unknown_rejected(self) -> None:
        """显式 unknown → REJECT。"""
        snapshot = {"btcusdt": {"last": 68000.0, "provenance": "unknown"}}
        verdict = _evaluate(snapshot=snapshot, enforce=True)
        assert verdict.verdict == "REJECT"
        assert "MARKET_DATA_UNTRUSTED" in verdict.reason_codes

    def test_missing_field_rejected(self) -> None:
        """**关键防御**：字段缺失 → 等价 unknown → REJECT。"""
        snapshot = {"btcusdt": {"last": 68000.0}}  # 无 provenance 字段
        verdict = _evaluate(snapshot=snapshot, enforce=True)
        assert verdict.verdict == "REJECT"
        assert "MARKET_DATA_UNTRUSTED" in verdict.reason_codes

    def test_typo_provenance_rejected(self) -> None:
        """拼错的 provenance → REJECT（防"sed" / "real" 等手误漏过）。"""
        snapshot = {
            "btcusdt": {"last": 68000.0, "provenance": "sed"}  # typo of seed
        }
        verdict = _evaluate(snapshot=snapshot, enforce=True)
        assert verdict.verdict == "REJECT"

    def test_non_dict_entry_rejected(self) -> None:
        """非 dict 类型的 entry（防御性）→ REJECT 而非 crash。"""
        snapshot = {"btcusdt": "garbage_string"}  # type: ignore[dict-item]
        # 注意：snapshot 中 symbol 仍存在,所以 PLAN_HALLUCINATION 不会触发,
        # 直接走 provenance 检查
        verdict = _evaluate(snapshot=snapshot, enforce=True)
        assert verdict.verdict == "REJECT"
        assert "MARKET_DATA_UNTRUSTED" in verdict.reason_codes


# ===========================================================================
# 4. 仅对 place_order 强制；其他 action_type 不受影响
# ===========================================================================
class TestProvenanceOnlyAffectsPlaceOrder:
    def test_read_market_passes_with_user_provided(self) -> None:
        """``read_market`` 不需要 snapshot 内有 symbol → 跳过 provenance 检查。

        因为 read_market 的语义是"去拉行情"，本身就是要更新 snapshot,
        不能反过来要求 snapshot 已经"可信"。
        """
        snapshot = {"btcusdt": {"last": 68000.0, "provenance": "user_provided"}}
        verdict = _evaluate(
            snapshot=snapshot, enforce=True, action=_read_market_action()
        )
        assert verdict.verdict == "ALLOW"

    def test_cancel_order_passes_with_user_provided(self) -> None:
        """``cancel_order`` 引用的是已存在 order_id，不依赖当前价格 → 跳过。"""
        snapshot = {"btcusdt": {"last": 68000.0, "provenance": "user_provided"}}
        cancel_action = {
            "type": "cancel_order",
            "symbol": "btcusdt",
            "side": "none",
            "order_type": "limit",
            "amount": 0,
            "amount_unit": "none",
            "max_notional_usdt": 0,
            "limit_price": None,
            "requires_user_approval": False,
            "rationale": "cancel",
        }
        verdict = _evaluate(
            snapshot=snapshot, enforce=True, action=cancel_action
        )
        assert verdict.verdict == "ALLOW"


# ===========================================================================
# 5. SEED_MARKET_DATA 默认带 provenance="seed"——启用后兼容
# ===========================================================================
class TestSeedMarketDataCompatibility:
    """**关键集成保证**：现有 SEED_MARKET_DATA 已带 ``provenance="seed"``,
    生产配置切到 ``ENFORCE_MARKET_PROVENANCE=true`` 后不需要额外修改。"""

    def test_seed_market_data_includes_provenance(self) -> None:
        from app.services.htx_adapter import SEED_MARKET_DATA as HTX_SEED
        from app.services.seed_data import SEED_MARKET_DATA as APP_SEED

        for symbol, entry in HTX_SEED.items():
            assert entry.get("provenance") == "seed", (
                f"htx_adapter SEED_MARKET_DATA[{symbol!r}] missing provenance=seed"
            )
        for symbol, entry in APP_SEED.items():
            assert entry.get("provenance") == "seed", (
                f"seed_data SEED_MARKET_DATA[{symbol!r}] missing provenance=seed"
            )

    def test_seed_market_data_passes_enforced_check(self) -> None:
        """直接用 SEED_MARKET_DATA 做 snapshot,启用强制后仍能下单。"""
        from app.services.htx_adapter import SEED_MARKET_DATA

        verdict = _evaluate(
            snapshot=dict(SEED_MARKET_DATA), enforce=True
        )
        assert verdict.verdict == "ALLOW"


# ===========================================================================
# 6. 与现有 PLAN_HALLUCINATION 路径的优先级
# ===========================================================================
class TestPriorityWithPlanHallucination:
    """symbol 不在 snapshot → PLAN_HALLUCINATION 优先（先于 provenance 检查）。"""

    def test_unknown_symbol_returns_plan_hallucination_not_provenance(
        self,
    ) -> None:
        # snapshot 不含 btcusdt
        snapshot = {"ethusdt": {"last": 3600, "provenance": "seed"}}
        # 但允许 btcusdt（ allowed_symbols 含）
        action = _place_order_action()  # symbol=btcusdt
        verdict = _evaluate(snapshot=snapshot, enforce=True, action=action)
        # PLAN_HALLUCINATION 早于 provenance 触发
        assert verdict.verdict == "REJECT"
        assert "PLAN_HALLUCINATION" in verdict.reason_codes
