"""任务 8.2 Policy Engine PBT 测试套件（Req 7 / Req 18 + Property 1/4/9/10）。

本文件仅做 hypothesis 驱动的 property-based testing，不引入新的实现。
与 ``test_policy_engine.py``（基础单元 + 弱版本属性）并列；本文件做完整 PBT。

测试类
------
1. ``TestPolicyEngineDeterminism``        — Property 1（相同输入恒同输出）
2. ``TestVerdictInvariants``              — verdict / reason_codes / risk_score / normalized_action 形态
3. ``TestCapabilityClosureProperty``      — Property 4（能力包封闭性）
4. ``TestKillSwitchCoverageProperty``     — Property 10（kill switch 全覆盖）
5. ``TestHallucinationProperty``          — Property 9（反幻觉）
6. ``TestBoundaryValues``                 — 参数化边界值（非 PBT）

为何 max_examples 控制在 60-80？
-------------------------------
- 与 ``test_audit_chain`` / ``test_state_machine`` 等其他 PBT 文件保持一致；
- 实测 200 examples 时单测 > 6s，60 examples 时 < 1s 仍能稳定 shrink 反例；
- Property 1 / 4 / 9 / 10 都是「∀输入 → ...」全称命题，60-80 例 + hypothesis
  的"corner case shrinking"足够覆盖关键反例（边界 / 0 / max / 重复 key 等）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.policy_engine import (
    REASON_CODES_SET,
    DailyActionHistory,
    GlobalConfig,
    PolicyVerdict,
    evaluate_policy,
)

# ---------------------------------------------------------------------------
# 共享常量与策略
# ---------------------------------------------------------------------------
#: 交易对池——固定字符串列表便于在 policy / action / market_snapshot 之间建立
#: 关联（symbol 落在 allowed 内 / 外的概率可控）。
_SYMBOL_POOL: list[str] = ["btcusdt", "ethusdt", "solusdt", "dogeusdt", "ghostusdt"]

#: 全部 ActionPlan v0 type，包含 no_op；额外加 "withdraw"、"unknown_tool_call" 用于
#: blocked_action 路径覆盖（schema 之外的 type 通过 dict 直接构造）。
_ACTION_TYPES_FOR_PBT: list[str] = [
    "read_market",
    "read_account",
    "place_order",
    "cancel_order",
    "no_op",
    "withdraw",
    "unknown_tool_call",
]

#: 固定的"现在"时间——time_window 测试在测试类内单独构造，这里用作非时间敏感
#: 测试的默认值，确保 evaluate_policy 路径稳定。
_FIXED_NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)

#: PBT 全局 settings——所有 PBT 装饰器共用，便于一次性调速。
_PBT_SETTINGS = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
        HealthCheck.function_scoped_fixture,
    ],
)

#: 一些性质需要更高的样本量（如确定性 / 不变量）才能稳定覆盖；
#: 与 ``_PBT_SETTINGS`` 唯一差异是 max_examples=80。
_PBT_SETTINGS_HEAVY = settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
        HealthCheck.function_scoped_fixture,
    ],
)


# ---------------------------------------------------------------------------
# Composite strategies
# ---------------------------------------------------------------------------
@st.composite
def hhmm_strategy(draw) -> str:
    """生成 ``"HH:MM"`` 字符串。"""
    hh = draw(st.integers(min_value=0, max_value=23))
    mm = draw(st.integers(min_value=0, max_value=59))
    return f"{hh:02d}:{mm:02d}"


@st.composite
def policies(draw) -> dict[str, Any]:
    """生成一个完整、合法的 PolicyDSLv0 dict。

    每次调用都会从 ``_SYMBOL_POOL`` 抽取至少 1 个 symbol 作为 ``allowed_symbols``，
    capabilities 五字段独立 booleans，blocked_actions 从 5 个枚举值中随机子集。

    特别说明
    --------
    - ``withdraw`` 在 capabilities 里**永远是 False**（与 PolicyDSLv0 schema 一致；
      Req 4 AC2 + Req 15 AC6）。
    - ``allowed_time_utc`` 字段 50% 概率包含——既覆盖"启用时间窗"也覆盖"未配置"。
    - ``required_for_trade`` 默认 True 但允许 False，用于覆盖 ALLOW 出口（Req 7 AC9）。
    """
    allowed = draw(
        st.lists(
            st.sampled_from(_SYMBOL_POOL),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    max_notional_per_order = draw(
        st.floats(
            min_value=1.0,
            max_value=10000.0,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    # max_daily ≥ max_notional_per_order，保持限额逻辑自洽
    max_daily_factor = draw(st.integers(min_value=1, max_value=200))
    max_daily = max_notional_per_order * max_daily_factor

    policy: dict[str, Any] = {
        "version": "0.1",
        "capabilities": {
            "read_market": draw(st.booleans()),
            "read_account": draw(st.booleans()),
            "place_order": draw(st.booleans()),
            "cancel_order": draw(st.booleans()),
            "withdraw": False,
        },
        "limits": {
            "allowed_symbols": allowed,
            "max_notional_usdt_per_order": max_notional_per_order,
            "max_daily_notional_usdt": max_daily,
            "max_orders_per_day": draw(st.integers(min_value=1, max_value=1000)),
        },
        "approval": {
            "required_for_trade": draw(st.booleans()),
            "required_for_policy_change": True,
            "expires_after_seconds": 300,
        },
        "blocked_actions": draw(
            st.lists(
                st.sampled_from(
                    [
                        "withdraw",
                        "borrow",
                        "margin",
                        "transfer_out",
                        "unknown_tool_call",
                    ]
                ),
                max_size=5,
                unique=True,
            )
        ),
    }

    # 50% 概率包含 allowed_time_utc
    if draw(st.booleans()):
        policy["limits"]["allowed_time_utc"] = {
            "start": draw(hhmm_strategy()),
            "end": draw(hhmm_strategy()),
        }

    return policy


@st.composite
def actions(draw) -> dict[str, Any]:
    """生成单个 action dict（含 5 种 ActionPlan type + 2 种 schema 之外的 blocked type）。

    设计意图
    --------
    - 故意把 ``"withdraw"`` / ``"unknown_tool_call"`` 也放入抽样池——它们在
      ActionPlan schema 阶段会被拦下，但 evaluate_policy 必须做"深度防御"
      （Req 7 AC2），即便绕过 schema 也要 REJECT。
    - ``symbol`` 从全 ``_SYMBOL_POOL`` 抽取，可能落在 ``allowed_symbols`` 内或外，
      触发 SYMBOL_NOT_ALLOWED / PLAN_HALLUCINATION 等不同路径。
    - ``max_notional_usdt`` 跨度 [0, 100000] 让限额检查能命中边界与远超情况。
    """
    action_type = draw(st.sampled_from(_ACTION_TYPES_FOR_PBT))
    symbol = draw(st.sampled_from(_SYMBOL_POOL))
    if action_type == "no_op":
        return {"type": "no_op", "rationale": "pbt-generated no_op"}
    return {
        "type": action_type,
        "symbol": symbol,
        "side": draw(st.sampled_from(["buy", "sell", "none"])),
        "order_type": draw(st.sampled_from(["limit", "market", "none"])),
        "amount": draw(
            st.floats(
                min_value=0.0,
                max_value=10000.0,
                allow_nan=False,
                allow_infinity=False,
            )
        ),
        "amount_unit": draw(st.sampled_from(["base", "quote", "none"])),
        "max_notional_usdt": draw(
            st.floats(
                min_value=0.0,
                max_value=100000.0,
                allow_nan=False,
                allow_infinity=False,
            )
        ),
    }


@st.composite
def histories(draw) -> DailyActionHistory:
    return DailyActionHistory(
        total_notional_today_utc=draw(
            st.floats(
                min_value=0.0,
                max_value=10000.0,
                allow_nan=False,
                allow_infinity=False,
            )
        ),
        order_count_today_utc=draw(st.integers(min_value=0, max_value=100)),
    )


@st.composite
def market_snapshots(draw) -> dict[str, dict[str, Any]]:
    """生成 ``{symbol: {"last": price}}`` 形态的市场快照。"""
    chosen = draw(
        st.lists(
            st.sampled_from(_SYMBOL_POOL),
            max_size=len(_SYMBOL_POOL),
            unique=True,
        )
    )
    snapshot: dict[str, dict[str, Any]] = {}
    for sym in chosen:
        price = draw(
            st.floats(
                min_value=0.01,
                max_value=100000.0,
                allow_nan=False,
                allow_infinity=False,
            )
        )
        snapshot[sym] = {"last": price}
    return snapshot


@st.composite
def global_configs(draw) -> GlobalConfig:
    return GlobalConfig(demo_disable_execution=draw(st.booleans()))


# ---------------------------------------------------------------------------
# 1. Property 1: 确定性
# ---------------------------------------------------------------------------
@pytest.mark.pbt
class TestPolicyEngineDeterminism:
    """**Validates: Requirements 7**（Property 1：Policy Engine 确定性）。"""

    @given(
        action=actions(),
        policy=policies(),
        history=histories(),
        snapshot=market_snapshots(),
        cfg=global_configs(),
    )
    @_PBT_SETTINGS_HEAVY
    def test_same_input_same_verdict(
        self,
        action: dict[str, Any],
        policy: dict[str, Any],
        history: DailyActionHistory,
        snapshot: dict[str, dict[str, Any]],
        cfg: GlobalConfig,
    ) -> None:
        """**Validates: Requirements 7**

        对任意 (action, policy, history, snapshot, cfg)：两次调用
        :func:`evaluate_policy` 必须返回 dataclass 等值的 ``PolicyVerdict``。
        """
        v1 = evaluate_policy(action, policy, history, snapshot, cfg, now=_FIXED_NOW)
        v2 = evaluate_policy(action, policy, history, snapshot, cfg, now=_FIXED_NOW)
        assert v1 == v2, (
            f"non-deterministic verdict for action={action} policy={policy} "
            f"history={history} cfg={cfg} snapshot_keys={list(snapshot)}"
        )


# ---------------------------------------------------------------------------
# 2. Verdict 形态不变量
# ---------------------------------------------------------------------------
@pytest.mark.pbt
class TestVerdictInvariants:
    """对任意输入：verdict / reason_codes / risk_score / normalized_action 形态正确。"""

    @given(
        action=actions(),
        policy=policies(),
        history=histories(),
        snapshot=market_snapshots(),
        cfg=global_configs(),
    )
    @_PBT_SETTINGS_HEAVY
    def test_verdict_shape_invariants(
        self,
        action: dict[str, Any],
        policy: dict[str, Any],
        history: DailyActionHistory,
        snapshot: dict[str, dict[str, Any]],
        cfg: GlobalConfig,
    ) -> None:
        """**Validates: Requirements 7**

        - ``verdict`` ∈ {ALLOW, REQUIRE_APPROVAL, REJECT}
        - ``reason_codes`` 是 :data:`REASON_CODES_SET` 的子集
        - ``0 ≤ risk_score ≤ 100``
        - ``normalized_action`` 是 dict；若含 ``symbol`` 字段则必为小写
        """
        v: PolicyVerdict = evaluate_policy(
            action, policy, history, snapshot, cfg, now=_FIXED_NOW
        )

        assert v.verdict in {"ALLOW", "REQUIRE_APPROVAL", "REJECT"}
        unknown_codes = set(v.reason_codes) - REASON_CODES_SET
        assert not unknown_codes, f"unknown reason codes: {unknown_codes!r}"
        assert 0 <= v.risk_score <= 100, f"risk_score out of range: {v.risk_score}"
        assert isinstance(v.normalized_action, dict)
        if "symbol" in v.normalized_action and isinstance(
            v.normalized_action["symbol"], str
        ):
            assert v.normalized_action["symbol"] == v.normalized_action["symbol"].lower(), (
                f"symbol not lowercased: {v.normalized_action['symbol']!r}"
            )

    @given(
        action=actions(),
        policy=policies(),
        history=histories(),
        snapshot=market_snapshots(),
        cfg=global_configs(),
    )
    @_PBT_SETTINGS
    def test_allow_has_no_reason_codes(
        self,
        action: dict[str, Any],
        policy: dict[str, Any],
        history: DailyActionHistory,
        snapshot: dict[str, dict[str, Any]],
        cfg: GlobalConfig,
    ) -> None:
        """ALLOW verdict 不应携带 reason_codes（与单元测试约定一致）。"""
        v = evaluate_policy(action, policy, history, snapshot, cfg, now=_FIXED_NOW)
        if v.verdict == "ALLOW":
            assert v.reason_codes == (), f"ALLOW with reason_codes: {v.reason_codes}"

    @given(
        action=actions(),
        policy=policies(),
        history=histories(),
        snapshot=market_snapshots(),
        cfg=global_configs(),
    )
    @_PBT_SETTINGS
    def test_reject_has_at_least_one_reason_code(
        self,
        action: dict[str, Any],
        policy: dict[str, Any],
        history: DailyActionHistory,
        snapshot: dict[str, dict[str, Any]],
        cfg: GlobalConfig,
    ) -> None:
        """REJECT verdict 必至少含 1 条 reason_code（用户必须知道为什么）。"""
        v = evaluate_policy(action, policy, history, snapshot, cfg, now=_FIXED_NOW)
        if v.verdict == "REJECT":
            assert len(v.reason_codes) >= 1, "REJECT without reason_codes"


# ---------------------------------------------------------------------------
# 3. Property 4: 能力包封闭性
# ---------------------------------------------------------------------------
@pytest.mark.pbt
class TestCapabilityClosureProperty:
    """**Validates: Requirements 4**（Property 4：能力包封闭性）。

    ALLOW 裁决只可能出现在 capability 字段为 True 的 action.type 上。
    no_op 不需要 capability，因此豁免。
    """

    @given(
        action=actions(),
        policy=policies(),
        history=histories(),
        snapshot=market_snapshots(),
        cfg=global_configs(),
    )
    @_PBT_SETTINGS
    def test_allow_implies_capability_true_or_no_op(
        self,
        action: dict[str, Any],
        policy: dict[str, Any],
        history: DailyActionHistory,
        snapshot: dict[str, dict[str, Any]],
        cfg: GlobalConfig,
    ) -> None:
        """**Validates: Requirements 4**

        若 verdict == ALLOW 且 action.type 不是 no_op，则
        policy.capabilities[action.type] 必为 True。
        """
        v = evaluate_policy(action, policy, history, snapshot, cfg, now=_FIXED_NOW)
        if v.verdict != "ALLOW":
            return
        action_type = action.get("type")
        if action_type == "no_op":
            return  # no_op 不需要 capability
        # 4 个会被 capability 检查的 type
        if action_type in ("read_market", "read_account", "place_order", "cancel_order"):
            cap_value = policy["capabilities"].get(action_type)
            assert cap_value is True, (
                f"ALLOW with capability false: action_type={action_type} "
                f"caps={policy['capabilities']!r}"
            )


# ---------------------------------------------------------------------------
# 4. Property 10: Kill Switch 全覆盖
# ---------------------------------------------------------------------------
@pytest.mark.pbt
class TestKillSwitchCoverageProperty:
    """**Validates: Requirements 7**（Property 10：Kill Switch 全覆盖）。

    DEMO_DISABLE_EXECUTION=true 时所有「非只读非 no_op」action 必 REJECT 含 EXECUTION_DISABLED。
    """

    @given(
        action=actions(),
        policy=policies(),
        history=histories(),
        snapshot=market_snapshots(),
    )
    @_PBT_SETTINGS
    def test_kill_switch_rejects_all_non_readonly_actions(
        self,
        action: dict[str, Any],
        policy: dict[str, Any],
        history: DailyActionHistory,
        snapshot: dict[str, dict[str, Any]],
    ) -> None:
        """**Validates: Requirements 7**

        当 demo_disable_execution=True 且 action.type ∉ {read_market, read_account, no_op}：
        verdict=REJECT 且 reason_codes 含 EXECUTION_DISABLED。
        """
        cfg = GlobalConfig(demo_disable_execution=True)
        v = evaluate_policy(action, policy, history, snapshot, cfg, now=_FIXED_NOW)

        action_type = action.get("type")
        if action_type in ("read_market", "read_account", "no_op"):
            return  # 这三种不受 kill switch 拦截

        assert v.verdict == "REJECT", (
            f"kill switch should REJECT non-readonly action_type={action_type}, "
            f"got verdict={v.verdict!r}"
        )
        assert "EXECUTION_DISABLED" in v.reason_codes, (
            f"kill switch should set EXECUTION_DISABLED, got reason_codes={v.reason_codes!r}"
        )

    @given(
        policy=policies(),
        history=histories(),
        snapshot=market_snapshots(),
        symbol=st.sampled_from(_SYMBOL_POOL),
        action_type=st.sampled_from(["read_market", "read_account", "no_op"]),
    )
    @_PBT_SETTINGS
    def test_kill_switch_does_not_block_readonly_or_no_op(
        self,
        policy: dict[str, Any],
        history: DailyActionHistory,
        snapshot: dict[str, dict[str, Any]],
        symbol: str,
        action_type: str,
    ) -> None:
        """**Validates: Requirements 7**

        当 demo_disable_execution=True 且 action.type ∈ {read_market, read_account, no_op}：
        verdict 不应是 REJECT(EXECUTION_DISABLED)（其他 reason 仍可能拒绝，但 EXECUTION_DISABLED
        不该出现）。
        """
        if action_type == "no_op":
            action: dict[str, Any] = {"type": "no_op", "rationale": "kill switch test"}
        else:
            action = {
                "type": action_type,
                "symbol": symbol,
                "side": "none",
                "order_type": "none",
                "amount": 0.0,
                "amount_unit": "none",
                "max_notional_usdt": 0.0,
            }
        cfg = GlobalConfig(demo_disable_execution=True)
        v = evaluate_policy(action, policy, history, snapshot, cfg, now=_FIXED_NOW)

        assert "EXECUTION_DISABLED" not in v.reason_codes, (
            f"kill switch should NOT block {action_type}, "
            f"got reason_codes={v.reason_codes!r}"
        )


# ---------------------------------------------------------------------------
# 5. Property 9: 反幻觉
# ---------------------------------------------------------------------------
@pytest.mark.pbt
class TestHallucinationProperty:
    """**Validates: Requirements 16**（Property 9：反幻觉）。

    若 ActionPlan 中 symbol 不在 market_snapshot 中（针对 place_order），
    则 Policy Engine 必返回 REJECT(PLAN_HALLUCINATION)。
    """

    @given(
        symbol=st.sampled_from(_SYMBOL_POOL),
        max_notional=st.floats(
            min_value=0.0,
            max_value=10000.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        amount=st.floats(
            min_value=0.0,
            max_value=10000.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        order_count=st.integers(min_value=0, max_value=50),
        total_notional=st.floats(
            min_value=0.0,
            max_value=10.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @_PBT_SETTINGS
    def test_place_order_with_symbol_not_in_snapshot_rejects(
        self,
        symbol: str,
        max_notional: float,
        amount: float,
        order_count: int,
        total_notional: float,
    ) -> None:
        """**Validates: Requirements 16**

        构造让 place_order 通过前置所有检查（capabilities 全开、symbol 在
        allowed_symbols 中、未超限额、订单数未达上限），但 market_snapshot
        故意不含该 symbol → 必 REJECT 含 PLAN_HALLUCINATION。
        """
        # 构造极宽松的 policy，只为让前置检查全过
        policy = {
            "version": "0.1",
            "capabilities": {
                "read_market": True,
                "read_account": True,
                "place_order": True,
                "cancel_order": True,
                "withdraw": False,
            },
            "limits": {
                "allowed_symbols": [symbol],
                # 必须 ≥ max_notional，否则会先撞 LIMIT_MAX_NOTIONAL_EXCEEDED
                "max_notional_usdt_per_order": max_notional + 1.0,
                "max_daily_notional_usdt": max_notional + total_notional + 1.0,
                "max_orders_per_day": order_count + 10,
            },
            "approval": {
                "required_for_trade": True,
                "required_for_policy_change": True,
                "expires_after_seconds": 300,
            },
            "blocked_actions": [],
        }
        action = {
            "type": "place_order",
            "symbol": symbol,
            "side": "buy",
            "order_type": "limit",
            "amount": amount,
            "amount_unit": "quote",
            "max_notional_usdt": max_notional,
        }
        history = DailyActionHistory(
            total_notional_today_utc=total_notional,
            order_count_today_utc=order_count,
        )
        # 关键：market_snapshot 不含 symbol（用一个绝不会与 _SYMBOL_POOL 冲突的占位 key）
        snapshot: dict[str, dict[str, Any]] = {"_placeholder_not_a_symbol_": {"last": 1.0}}

        v = evaluate_policy(
            action, policy, history, snapshot, GlobalConfig(), now=_FIXED_NOW
        )
        assert v.verdict == "REJECT", (
            f"expected REJECT for hallucinated symbol, got verdict={v.verdict!r} "
            f"reason_codes={v.reason_codes!r}"
        )
        assert "PLAN_HALLUCINATION" in v.reason_codes, (
            f"expected PLAN_HALLUCINATION, got reason_codes={v.reason_codes!r}"
        )

    @given(
        symbol=st.sampled_from(_SYMBOL_POOL),
        price=st.floats(
            min_value=0.01,
            max_value=100000.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    @_PBT_SETTINGS
    def test_place_order_with_symbol_in_snapshot_no_hallucination_code(
        self,
        symbol: str,
        price: float,
    ) -> None:
        """**Validates: Requirements 16**

        当 symbol 在 market_snapshot 中且其它检查全过 → reason_codes 不含 PLAN_HALLUCINATION。
        """
        policy = {
            "version": "0.1",
            "capabilities": {
                "read_market": True,
                "read_account": True,
                "place_order": True,
                "cancel_order": True,
                "withdraw": False,
            },
            "limits": {
                "allowed_symbols": [symbol],
                "max_notional_usdt_per_order": 100.0,
                "max_daily_notional_usdt": 1000.0,
                "max_orders_per_day": 10,
            },
            "approval": {
                "required_for_trade": True,
                "required_for_policy_change": True,
                "expires_after_seconds": 300,
            },
            "blocked_actions": [],
        }
        action = {
            "type": "place_order",
            "symbol": symbol,
            "side": "buy",
            "order_type": "limit",
            "amount": 1.0,
            "amount_unit": "quote",
            "max_notional_usdt": 10.0,
        }
        history = DailyActionHistory(
            total_notional_today_utc=0.0, order_count_today_utc=0
        )
        snapshot: dict[str, dict[str, Any]] = {symbol: {"last": price}}

        v = evaluate_policy(
            action, policy, history, snapshot, GlobalConfig(), now=_FIXED_NOW
        )
        assert "PLAN_HALLUCINATION" not in v.reason_codes, (
            f"unexpected PLAN_HALLUCINATION, reason_codes={v.reason_codes!r}"
        )


# ---------------------------------------------------------------------------
# 6. 边界值参数化测试（非 PBT；锁住 ≤ vs > 的关键边界）
# ---------------------------------------------------------------------------
def _strict_policy(
    *,
    max_notional_per_order: float = 20.0,
    max_daily_notional: float = 100.0,
    max_orders_per_day: int = 10,
) -> dict[str, Any]:
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
            "max_notional_usdt_per_order": max_notional_per_order,
            "max_daily_notional_usdt": max_daily_notional,
            "max_orders_per_day": max_orders_per_day,
        },
        "approval": {
            "required_for_trade": True,
            "required_for_policy_change": True,
            "expires_after_seconds": 300,
        },
        "blocked_actions": ["withdraw", "borrow", "margin", "transfer_out"],
    }


def _strict_place_order(*, max_notional_usdt: float = 10.0) -> dict[str, Any]:
    return {
        "type": "place_order",
        "symbol": "btcusdt",
        "side": "buy",
        "order_type": "limit",
        "amount": max_notional_usdt,
        "amount_unit": "quote",
        "max_notional_usdt": max_notional_usdt,
    }


_STRICT_SNAPSHOT: dict[str, dict[str, Any]] = {"btcusdt": {"last": 68000.0}}


class TestBoundaryValues:
    """≤/< 边界关键节点；用 parametrize 锁死防回归。"""

    @pytest.mark.parametrize(
        ("max_notional", "expected_verdict", "expected_reason_in"),
        [
            (20.0, "REQUIRE_APPROVAL", None),  # ==
            (20.0001, "REJECT", "LIMIT_MAX_NOTIONAL_EXCEEDED"),  # >
            (19.9999, "REQUIRE_APPROVAL", None),  # <
            (0.0, "REQUIRE_APPROVAL", None),  # 0 也通过
        ],
    )
    def test_max_notional_boundary(
        self,
        max_notional: float,
        expected_verdict: str,
        expected_reason_in: str | None,
    ) -> None:
        v = evaluate_policy(
            _strict_place_order(max_notional_usdt=max_notional),
            _strict_policy(),
            DailyActionHistory(),
            _STRICT_SNAPSHOT,
            GlobalConfig(),
            now=_FIXED_NOW,
        )
        assert v.verdict == expected_verdict, f"verdict={v.verdict} reasons={v.reason_codes}"
        if expected_reason_in is not None:
            assert expected_reason_in in v.reason_codes

    @pytest.mark.parametrize(
        ("daily_total", "action_notional", "expected_verdict", "expected_reason_in"),
        [
            (90.0, 10.0, "REQUIRE_APPROVAL", None),  # 90+10=100 == max → 通过
            (90.0, 10.0001, "REJECT", "DAILY_LIMIT_EXCEEDED"),  # > max
            (89.9999, 10.0, "REQUIRE_APPROVAL", None),  # < max
            (0.0, 10.0, "REQUIRE_APPROVAL", None),  # 起点
        ],
    )
    def test_daily_notional_boundary(
        self,
        daily_total: float,
        action_notional: float,
        expected_verdict: str,
        expected_reason_in: str | None,
    ) -> None:
        history = DailyActionHistory(
            total_notional_today_utc=daily_total, order_count_today_utc=0
        )
        v = evaluate_policy(
            _strict_place_order(max_notional_usdt=action_notional),
            _strict_policy(),
            history,
            _STRICT_SNAPSHOT,
            GlobalConfig(),
            now=_FIXED_NOW,
        )
        assert v.verdict == expected_verdict, f"verdict={v.verdict} reasons={v.reason_codes}"
        if expected_reason_in is not None:
            assert expected_reason_in in v.reason_codes

    @pytest.mark.parametrize(
        ("order_count", "expected_verdict", "expected_reason_in"),
        [
            (10, "REJECT", "DAILY_ORDER_COUNT_EXCEEDED"),  # ==
            (9, "REQUIRE_APPROVAL", None),  # < max
            (11, "REJECT", "DAILY_ORDER_COUNT_EXCEEDED"),  # > max
            (0, "REQUIRE_APPROVAL", None),  # 起点
        ],
    )
    def test_orders_per_day_boundary(
        self,
        order_count: int,
        expected_verdict: str,
        expected_reason_in: str | None,
    ) -> None:
        history = DailyActionHistory(
            total_notional_today_utc=0.0, order_count_today_utc=order_count
        )
        v = evaluate_policy(
            _strict_place_order(),
            _strict_policy(),
            history,
            _STRICT_SNAPSHOT,
            GlobalConfig(),
            now=_FIXED_NOW,
        )
        assert v.verdict == expected_verdict
        if expected_reason_in is not None:
            assert expected_reason_in in v.reason_codes
