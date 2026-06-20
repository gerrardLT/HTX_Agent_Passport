"""任务 8.1 Policy Engine 核心裁决逻辑单元测试（Req 7 + Req 16 + Property 1/9/10 弱版本）。

本测试模块**只覆盖任务 8.1 范围**——确定性裁决的 7 步顺序、kill switch、
反幻觉校验、verdict 输出形态。Property 1（PBT 完整版本）放在任务 8.2 实现。

覆盖矩阵
--------
1. **基本裁决**（每步顺序触发对应 reason_code）
   - kill_switch_rejects_all_non_readonly
   - kill_switch_allows_read_market（kill switch 不影响只读）
   - blocked_action_withdraw / borrow / margin / transfer_out
   - capability_not_granted
   - symbol_not_allowed
   - max_notional_exceeded
   - daily_notional_exceeded
   - daily_order_count_exceeded
   - time_window_violation
   - plan_hallucination_symbol_not_in_market_snapshot
2. **裁决正向用例**：
   - read_market_passes_to_allow
   - place_order_with_approval_required
   - place_order_no_approval_required
3. **正确性属性（基础版）**：
   - verdict_is_deterministic_for_same_input
   - reason_codes_are_subset_of_known_codes
   - normalized_action_has_lowercase_symbol
   - risk_score_in_range_0_100
4. **跨午夜时间窗**：
   - time_window_overnight_22_to_02
   - time_window_normal_09_to_17
5. **额外**：no_op 走 ALLOW 出口、cancel_order 不需要 max_notional 检查
   但参与 orders_per_day。

PBT（Property 1 完整版）将放在任务 8.2，这里仅做「弱确定性」断言（同输入两次调用相同）。
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any

import pytest

from app.services.policy_engine import (
    REASON_CODES,
    REASON_CODES_SET,
    DailyActionHistory,
    GlobalConfig,
    PolicyVerdict,
    evaluate_policy,
    is_within_time_window,
    normalize_action,
)


# ---------------------------------------------------------------------------
# Fixtures / factory helpers
# ---------------------------------------------------------------------------
def _base_policy(**overrides: Any) -> dict[str, Any]:
    """构造一个完整、合法的 small_spot_executor 风格 policy dict。"""
    base: dict[str, Any] = {
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
            "max_notional_usdt_per_order": 20.0,
            "max_daily_notional_usdt": 100.0,
            "max_orders_per_day": 10,
        },
        "approval": {
            "required_for_trade": True,
            "required_for_policy_change": True,
            "expires_after_seconds": 300,
        },
        "blocked_actions": ["withdraw", "borrow", "margin", "transfer_out"],
    }
    # 浅 merge：override 整个顶层节
    for key, value in overrides.items():
        base[key] = value
    return base


def _place_order_action(**overrides: Any) -> dict[str, Any]:
    """构造一个完整的 place_order action dict。"""
    base: dict[str, Any] = {
        "type": "place_order",
        "symbol": "btcusdt",
        "side": "buy",
        "order_type": "limit",
        "amount": 10.0,
        "amount_unit": "quote",
        "max_notional_usdt": 10.0,
        "limit_price": 68000.0,
        "requires_user_approval": True,
        "rationale": "test order",
    }
    base.update(overrides)
    return base


def _read_market_action(symbol: str = "btcusdt") -> dict[str, Any]:
    return {"type": "read_market", "symbol": symbol}


def _read_account_action(symbol: str = "btcusdt") -> dict[str, Any]:
    return {"type": "read_account", "symbol": symbol}


def _no_op_action(rationale: str = "blocked by user intent") -> dict[str, Any]:
    return {"type": "no_op", "rationale": rationale}


def _cancel_order_action(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "cancel_order",
        "symbol": "btcusdt",
        "side": "none",
        "order_type": "none",
        "amount": 0.0,
        "amount_unit": "none",
        "max_notional_usdt": 0.0,
    }
    base.update(overrides)
    return base


# 默认市场快照：包含模板里所有 allowed_symbols + 一条 dogeusdt 用于反例。
_DEFAULT_MARKET_SNAPSHOT: dict[str, dict[str, Any]] = {
    "btcusdt": {"last": 68000.0},
    "ethusdt": {"last": 3600.0},
    "dogeusdt": {"last": 0.15},
}

_NOON_UTC = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


def _evaluate(
    *,
    action: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    daily_history: DailyActionHistory | None = None,
    market_snapshot: dict[str, dict[str, Any]] | None = None,
    global_config: GlobalConfig | None = None,
    now: datetime | None = None,
) -> PolicyVerdict:
    """简化测试调用：所有参数有合理默认值。"""
    return evaluate_policy(
        action=action if action is not None else _place_order_action(),
        policy=policy if policy is not None else _base_policy(),
        daily_history=daily_history if daily_history is not None else DailyActionHistory(),
        market_snapshot=market_snapshot
        if market_snapshot is not None
        else _DEFAULT_MARKET_SNAPSHOT,
        global_config=global_config if global_config is not None else GlobalConfig(),
        now=now if now is not None else _NOON_UTC,
    )


# ---------------------------------------------------------------------------
# 1. Kill switch（Req 7 AC12 / Property 10 弱版本）
# ---------------------------------------------------------------------------
class TestKillSwitch:
    """DEMO_DISABLE_EXECUTION=true 拒绝所有非只读 action。"""

    def test_kill_switch_rejects_place_order(self) -> None:
        verdict = _evaluate(global_config=GlobalConfig(demo_disable_execution=True))
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("EXECUTION_DISABLED",)
        assert verdict.risk_score == 100

    def test_kill_switch_rejects_cancel_order(self) -> None:
        verdict = _evaluate(
            action=_cancel_order_action(),
            global_config=GlobalConfig(demo_disable_execution=True),
        )
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("EXECUTION_DISABLED",)

    def test_kill_switch_allows_read_market(self) -> None:
        """kill switch 不拦截只读（design.md 与 Req 7 AC12「非只读」措辞一致）。"""
        verdict = _evaluate(
            action=_read_market_action(),
            global_config=GlobalConfig(demo_disable_execution=True),
        )
        assert verdict.verdict == "ALLOW"
        assert verdict.reason_codes == ()
        assert verdict.risk_score == 0

    def test_kill_switch_allows_read_account(self) -> None:
        verdict = _evaluate(
            action=_read_account_action(),
            global_config=GlobalConfig(demo_disable_execution=True),
        )
        assert verdict.verdict == "ALLOW"

    def test_kill_switch_allows_no_op(self) -> None:
        """no_op 也不被 kill switch 拦截（语义：什么都不做，安全）。"""
        verdict = _evaluate(
            action=_no_op_action(),
            global_config=GlobalConfig(demo_disable_execution=True),
        )
        assert verdict.verdict == "ALLOW"


# ---------------------------------------------------------------------------
# 2. blocked_actions（Req 7 AC2）
# ---------------------------------------------------------------------------
class TestBlockedActions:
    """``action.type ∈ blocked_actions`` 一律 REJECT。

    注意：ActionPlan v0 schema 已禁止 type=withdraw 等枚举之外的值；要让
    blocked_actions 检查触发，调用方必须绕过 schema 直接构造 action dict
    （这正是「深度防御」要保护的场景）。
    """

    @pytest.mark.parametrize(
        ("type_str", "expected_reason"),
        [
            ("withdraw", "BLOCKED_ACTION_WITHDRAW"),
            ("borrow", "BLOCKED_ACTION_BORROW"),
            ("margin", "BLOCKED_ACTION_MARGIN"),
            ("transfer_out", "BLOCKED_ACTION_TRANSFER_OUT"),
        ],
    )
    def test_blocked_action_returns_reject(
        self, type_str: str, expected_reason: str
    ) -> None:
        action: dict[str, Any] = {
            "type": type_str,
            "symbol": "btcusdt",
            "max_notional_usdt": 10.0,
        }
        verdict = _evaluate(action=action)
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == (expected_reason,)
        assert verdict.risk_score == 100

    def test_blocked_action_unknown_tool_call_falls_back_to_unknown(self) -> None:
        """未列入 BLOCKED_ACTION_* 显式映射的字符串 → fallback 到 UNKNOWN_TOOL_CALL。

        注：当前 blocked_actions 只允许 5 个枚举值（policy schema 强制），但
        :func:`evaluate_policy` 仍做防御性 fallback——保证未来扩展时不会
        产出未在 REASON_CODES 中的字符串。
        """
        # 构造一个 policy.blocked_actions 含「unknown_tool_call」的场景
        policy = _base_policy(blocked_actions=["unknown_tool_call"])
        action: dict[str, Any] = {
            "type": "unknown_tool_call",
            "symbol": "btcusdt",
        }
        verdict = _evaluate(action=action, policy=policy)
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("BLOCKED_ACTION_UNKNOWN_TOOL_CALL",)


# ---------------------------------------------------------------------------
# 3. capabilities（Req 7 AC3 / Property 4）
# ---------------------------------------------------------------------------
class TestCapabilities:
    """``capabilities.<type>=false`` 时返回 CAPABILITY_NOT_GRANTED。"""

    def test_place_order_not_in_capabilities_returns_reject(self) -> None:
        policy = _base_policy(
            capabilities={
                "read_market": True,
                "read_account": True,
                "place_order": False,
                "cancel_order": True,
                "withdraw": False,
            }
        )
        verdict = _evaluate(policy=policy)
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("CAPABILITY_NOT_GRANTED",)

    def test_read_market_not_in_capabilities_returns_reject(self) -> None:
        policy = _base_policy(
            capabilities={
                "read_market": False,
                "read_account": False,
                "place_order": False,
                "cancel_order": False,
                "withdraw": False,
            }
        )
        verdict = _evaluate(action=_read_market_action(), policy=policy)
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("CAPABILITY_NOT_GRANTED",)


# ---------------------------------------------------------------------------
# 4. allowed_symbols（Req 7 AC4 / Req 6 AC6）
# ---------------------------------------------------------------------------
class TestSymbolNotAllowed:
    def test_symbol_not_in_allowed_list_returns_reject(self) -> None:
        verdict = _evaluate(action=_place_order_action(symbol="dogeusdt"))
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("SYMBOL_NOT_ALLOWED",)

    def test_symbol_uppercase_input_normalized_to_lowercase(self) -> None:
        """传入大写 symbol → 内部小写化后再比较；与 allowed_symbols（小写）一致。"""
        verdict = _evaluate(action=_place_order_action(symbol="BTCUSDT"))
        assert verdict.verdict == "REQUIRE_APPROVAL"
        # normalized_action 已是小写
        assert verdict.normalized_action["symbol"] == "btcusdt"

    def test_symbol_in_allowed_with_uppercase_policy_works(self) -> None:
        """policy.allowed_symbols 含大写 → 内部 lower() 后比较仍可命中。"""
        policy = _base_policy(
            limits={
                "allowed_symbols": ["BTCUSDT"],  # 故意大写
                "max_notional_usdt_per_order": 20.0,
                "max_daily_notional_usdt": 100.0,
                "max_orders_per_day": 10,
            }
        )
        verdict = _evaluate(policy=policy)
        assert verdict.verdict == "REQUIRE_APPROVAL"


# ---------------------------------------------------------------------------
# 5. max_notional_usdt_per_order（Req 7 AC5）
# ---------------------------------------------------------------------------
class TestMaxNotional:
    def test_exceeds_max_notional_returns_reject(self) -> None:
        verdict = _evaluate(action=_place_order_action(max_notional_usdt=25.0))
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("LIMIT_MAX_NOTIONAL_EXCEEDED",)

    def test_equal_to_max_notional_passes(self) -> None:
        """边界值：恰好等于 max_notional → 通过（≤ 而非 <）。"""
        verdict = _evaluate(action=_place_order_action(max_notional_usdt=20.0))
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_cancel_order_does_not_check_max_notional(self) -> None:
        """cancel_order 不下新单 → 跳过 max_notional 检查。"""
        verdict = _evaluate(action=_cancel_order_action(max_notional_usdt=999999.0))
        # 不会触发 LIMIT_MAX_NOTIONAL_EXCEEDED
        assert verdict.reason_codes != ("LIMIT_MAX_NOTIONAL_EXCEEDED",)


# ---------------------------------------------------------------------------
# 6. max_daily_notional_usdt（Req 7 AC6）
# ---------------------------------------------------------------------------
class TestDailyNotional:
    def test_history_plus_action_exceeds_daily_limit_returns_reject(self) -> None:
        history = DailyActionHistory(
            total_notional_today_utc=95.0, order_count_today_utc=5
        )
        verdict = _evaluate(
            action=_place_order_action(max_notional_usdt=10.0),
            daily_history=history,
        )
        # 95 + 10 = 105 > 100
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("DAILY_LIMIT_EXCEEDED",)

    def test_history_plus_action_equals_daily_limit_passes(self) -> None:
        """边界：累计正好 == max_daily → 通过（> 而非 ≥）。"""
        history = DailyActionHistory(
            total_notional_today_utc=90.0, order_count_today_utc=5
        )
        verdict = _evaluate(
            action=_place_order_action(max_notional_usdt=10.0),
            daily_history=history,
        )
        # 90 + 10 = 100 == limit → 不超
        assert verdict.verdict == "REQUIRE_APPROVAL"


# ---------------------------------------------------------------------------
# 7. max_orders_per_day（Req 7 AC7）
# ---------------------------------------------------------------------------
class TestDailyOrderCount:
    def test_order_count_at_limit_returns_reject(self) -> None:
        history = DailyActionHistory(
            total_notional_today_utc=0, order_count_today_utc=10
        )
        verdict = _evaluate(daily_history=history)
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("DAILY_ORDER_COUNT_EXCEEDED",)

    def test_order_count_below_limit_passes(self) -> None:
        history = DailyActionHistory(
            total_notional_today_utc=0, order_count_today_utc=9
        )
        verdict = _evaluate(daily_history=history)
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_cancel_order_counts_against_orders_per_day(self) -> None:
        """cancel_order 也计入每日订单数：防 cancel 风暴。"""
        history = DailyActionHistory(
            total_notional_today_utc=0, order_count_today_utc=10
        )
        verdict = _evaluate(action=_cancel_order_action(), daily_history=history)
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("DAILY_ORDER_COUNT_EXCEEDED",)

    def test_read_market_does_not_count_against_orders_per_day(self) -> None:
        """read_* 不计入每日订单数。"""
        history = DailyActionHistory(
            total_notional_today_utc=0, order_count_today_utc=10
        )
        verdict = _evaluate(action=_read_market_action(), daily_history=history)
        # 不会触发 DAILY_ORDER_COUNT_EXCEEDED
        assert verdict.verdict == "ALLOW"


# ---------------------------------------------------------------------------
# 8. allowed_time_utc（Req 7 AC1 末尾 / Req 4 AC4）
# ---------------------------------------------------------------------------
class TestAllowedTimeUtc:
    """普通窗口 + 跨午夜窗口 + 边界值。"""

    def test_no_time_window_passes(self) -> None:
        """policy 未配置 allowed_time_utc → 任何时间都通过。"""
        verdict = _evaluate()
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_normal_window_in_range_passes(self) -> None:
        """09:00-17:00 普通窗口；当前 UTC 12:00 在窗口内。"""
        policy = _base_policy(
            limits={
                **_base_policy()["limits"],
                "allowed_time_utc": {"start": "09:00", "end": "17:00"},
            }
        )
        verdict = _evaluate(
            policy=policy,
            now=datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC),
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_normal_window_outside_range_returns_reject(self) -> None:
        """09:00-17:00；UTC 08:00 在窗口外 → REJECT。"""
        policy = _base_policy(
            limits={
                **_base_policy()["limits"],
                "allowed_time_utc": {"start": "09:00", "end": "17:00"},
            }
        )
        verdict = _evaluate(
            policy=policy,
            now=datetime(2026, 5, 30, 8, 0, 0, tzinfo=UTC),
        )
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("TIME_WINDOW_VIOLATION",)

    def test_overnight_window_late_evening_passes(self) -> None:
        """22:00-02:00 跨午夜；UTC 23:00 在窗口内。"""
        policy = _base_policy(
            limits={
                **_base_policy()["limits"],
                "allowed_time_utc": {"start": "22:00", "end": "02:00"},
            }
        )
        verdict = _evaluate(
            policy=policy,
            now=datetime(2026, 5, 30, 23, 0, 0, tzinfo=UTC),
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_overnight_window_early_morning_passes(self) -> None:
        """22:00-02:00 跨午夜；UTC 01:30 在窗口内。"""
        policy = _base_policy(
            limits={
                **_base_policy()["limits"],
                "allowed_time_utc": {"start": "22:00", "end": "02:00"},
            }
        )
        verdict = _evaluate(
            policy=policy,
            now=datetime(2026, 5, 30, 1, 30, 0, tzinfo=UTC),
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_overnight_window_noon_outside_returns_reject(self) -> None:
        """22:00-02:00 跨午夜；UTC 12:00 在窗口外 → REJECT。"""
        policy = _base_policy(
            limits={
                **_base_policy()["limits"],
                "allowed_time_utc": {"start": "22:00", "end": "02:00"},
            }
        )
        verdict = _evaluate(
            policy=policy,
            now=datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC),
        )
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("TIME_WINDOW_VIOLATION",)

    def test_naive_datetime_now_treated_as_utc(self) -> None:
        """传入 naive datetime → 视为 UTC。"""
        policy = _base_policy(
            limits={
                **_base_policy()["limits"],
                "allowed_time_utc": {"start": "09:00", "end": "17:00"},
            }
        )
        # naive datetime（无 tzinfo）
        verdict = _evaluate(
            policy=policy,
            now=datetime(2026, 5, 30, 12, 0, 0),  # naive，应被当作 UTC
        )
        assert verdict.verdict == "REQUIRE_APPROVAL"


class TestIsWithinTimeWindow:
    """单独锁住 :func:`is_within_time_window` 的边界语义。"""

    @pytest.mark.parametrize(
        ("now_t", "start", "end", "expected"),
        [
            # 普通窗口
            (time(10, 0), "09:00", "17:00", True),
            (time(8, 59), "09:00", "17:00", False),
            (time(9, 0), "09:00", "17:00", True),  # 边界 inclusive
            (time(17, 0), "09:00", "17:00", True),  # 边界 inclusive
            (time(17, 1), "09:00", "17:00", False),
            # 跨午夜
            (time(23, 0), "22:00", "02:00", True),
            (time(22, 0), "22:00", "02:00", True),  # 边界
            (time(2, 0), "22:00", "02:00", True),  # 边界
            (time(2, 1), "22:00", "02:00", False),
            (time(21, 59), "22:00", "02:00", False),
            (time(12, 0), "22:00", "02:00", False),  # 跨午夜白天
            # 0 长度窗口（start == end）
            (time(12, 0), "12:00", "12:00", True),  # 仅那一刻
            (time(12, 1), "12:00", "12:00", False),
        ],
    )
    def test_window(
        self, now_t: time, start: str, end: str, expected: bool
    ) -> None:
        assert is_within_time_window(now_t, start, end) is expected


# ---------------------------------------------------------------------------
# 9. 反幻觉（Req 16 AC1 / Property 9 弱版本）
# ---------------------------------------------------------------------------
class TestPlanHallucination:
    def test_place_order_with_symbol_not_in_market_snapshot_rejects(self) -> None:
        """allowed_symbols 含 btcusdt，但 market_snapshot 不含 → REJECT(PLAN_HALLUCINATION)。"""
        # 让 allowed_symbols 含 ghostusdt 以绕过 SYMBOL_NOT_ALLOWED 闸
        policy = _base_policy(
            limits={
                **_base_policy()["limits"],
                "allowed_symbols": ["ghostusdt"],
            }
        )
        # market snapshot 不含 ghostusdt
        snapshot = {"btcusdt": {"last": 68000.0}}
        verdict = _evaluate(
            action=_place_order_action(symbol="ghostusdt"),
            policy=policy,
            market_snapshot=snapshot,
        )
        assert verdict.verdict == "REJECT"
        assert verdict.reason_codes == ("PLAN_HALLUCINATION",)
        assert verdict.risk_score == 95

    def test_place_order_with_symbol_in_market_snapshot_passes(self) -> None:
        verdict = _evaluate()  # btcusdt 在 _DEFAULT_MARKET_SNAPSHOT 中
        assert verdict.verdict == "REQUIRE_APPROVAL"

    def test_read_market_does_not_check_market_snapshot(self) -> None:
        """read_market 不需要 symbol 在 market_snapshot 里——这是要去拉的目标。"""
        snapshot: dict[str, dict[str, Any]] = {}  # 完全空
        # 同时把 allowed_symbols 含 btcusdt
        verdict = _evaluate(
            action=_read_market_action("btcusdt"),
            market_snapshot=snapshot,
        )
        assert verdict.verdict == "ALLOW"

    def test_cancel_order_does_not_check_market_snapshot(self) -> None:
        """cancel_order 引用的是已存在订单 id，不需要 market_snapshot 校验。"""
        snapshot: dict[str, dict[str, Any]] = {}
        verdict = _evaluate(action=_cancel_order_action(), market_snapshot=snapshot)
        # 不会触发 PLAN_HALLUCINATION
        assert verdict.reason_codes != ("PLAN_HALLUCINATION",)


# ---------------------------------------------------------------------------
# 10. 正向裁决用例（Req 7 AC8 / AC9）
# ---------------------------------------------------------------------------
class TestPositiveVerdicts:
    def test_read_market_passes_to_allow(self) -> None:
        verdict = _evaluate(action=_read_market_action())
        assert verdict.verdict == "ALLOW"
        assert verdict.reason_codes == ()
        assert verdict.risk_score == 0

    def test_read_account_passes_to_allow(self) -> None:
        verdict = _evaluate(action=_read_account_action())
        assert verdict.verdict == "ALLOW"

    def test_place_order_with_approval_required(self) -> None:
        verdict = _evaluate()
        assert verdict.verdict == "REQUIRE_APPROVAL"
        assert verdict.reason_codes == ()
        assert verdict.risk_score == 40

    def test_place_order_no_approval_required(self) -> None:
        """approval.required_for_trade=false → place_order 走 ALLOW（自动通过）。"""
        policy = _base_policy(
            approval={
                "required_for_trade": False,
                "required_for_policy_change": True,
                "expires_after_seconds": 300,
            }
        )
        verdict = _evaluate(policy=policy)
        assert verdict.verdict == "ALLOW"
        assert verdict.reason_codes == ()

    def test_no_op_returns_allow(self) -> None:
        """no_op 经检查走 ALLOW（语义：什么都不做，上层 short-circuit）。

        与 Req 8 AC1「READ_MARKET / READ_ACCOUNT / no_op → AUTO_APPROVED」一致。
        """
        verdict = _evaluate(action=_no_op_action())
        assert verdict.verdict == "ALLOW"
        assert verdict.reason_codes == ()


# ---------------------------------------------------------------------------
# 11. 正确性属性（基础版；Property 1 PBT 完整版在任务 8.2）
# ---------------------------------------------------------------------------
class TestCorrectnessProperties:
    """对每个返回的 verdict 都做形态级属性断言。"""

    def test_verdict_is_deterministic_for_same_input(self) -> None:
        """**Validates: Requirements 7**（Property 1 弱版本）。

        相同输入两次调用 → 完全相同的 verdict（用 dataclass 等值比较）。
        """
        v1 = _evaluate()
        v2 = _evaluate()
        assert v1 == v2

    def test_deterministic_for_kill_switch_path(self) -> None:
        cfg = GlobalConfig(demo_disable_execution=True)
        v1 = _evaluate(global_config=cfg)
        v2 = _evaluate(global_config=cfg)
        assert v1 == v2

    @pytest.mark.parametrize(
        "scenario",
        [
            "place_order_pass",
            "place_order_max_notional_exceeded",
            "place_order_daily_limit",
            "symbol_not_allowed",
            "kill_switch",
            "blocked_action",
            "no_op",
            "read_market",
        ],
    )
    def test_reason_codes_are_subset_of_known_codes(self, scenario: str) -> None:
        """**Validates: Requirements 7**。返回 reason_codes 必须 ⊆ REASON_CODES。"""
        if scenario == "place_order_pass":
            verdict = _evaluate()
        elif scenario == "place_order_max_notional_exceeded":
            verdict = _evaluate(action=_place_order_action(max_notional_usdt=999.0))
        elif scenario == "place_order_daily_limit":
            verdict = _evaluate(
                daily_history=DailyActionHistory(
                    total_notional_today_utc=99.0, order_count_today_utc=1
                ),
                action=_place_order_action(max_notional_usdt=5.0),
            )
        elif scenario == "symbol_not_allowed":
            verdict = _evaluate(action=_place_order_action(symbol="dogeusdt"))
        elif scenario == "kill_switch":
            verdict = _evaluate(global_config=GlobalConfig(demo_disable_execution=True))
        elif scenario == "blocked_action":
            verdict = _evaluate(
                action={"type": "withdraw", "symbol": "btcusdt", "max_notional_usdt": 1}
            )
        elif scenario == "no_op":
            verdict = _evaluate(action=_no_op_action())
        else:  # read_market
            verdict = _evaluate(action=_read_market_action())

        for code in verdict.reason_codes:
            assert code in REASON_CODES_SET, f"unknown reason_code {code!r}"

    def test_normalized_action_has_lowercase_symbol(self) -> None:
        """**Validates: Requirements 6**（symbol 小写归一化）。"""
        verdict = _evaluate(action=_place_order_action(symbol="BTCUSDT"))
        assert verdict.normalized_action["symbol"] == "btcusdt"

    def test_normalized_action_preserved_on_reject(self) -> None:
        """REJECT verdict 也应携带 normalized_action（审计重放用）。"""
        verdict = _evaluate(action=_place_order_action(symbol="DOGEUSDT"))
        assert verdict.verdict == "REJECT"
        assert verdict.normalized_action["symbol"] == "dogeusdt"

    @pytest.mark.parametrize(
        "scenario",
        [
            "allow",
            "require_approval",
            "reject_max_notional",
            "reject_kill_switch",
            "reject_symbol",
            "reject_capability",
            "reject_blocked",
            "reject_hallucination",
        ],
    )
    def test_risk_score_in_range_0_100(self, scenario: str) -> None:
        """所有 verdict 的 risk_score ∈ [0, 100]。"""
        if scenario == "allow":
            verdict = _evaluate(action=_read_market_action())
        elif scenario == "require_approval":
            verdict = _evaluate()
        elif scenario == "reject_max_notional":
            verdict = _evaluate(action=_place_order_action(max_notional_usdt=999.0))
        elif scenario == "reject_kill_switch":
            verdict = _evaluate(global_config=GlobalConfig(demo_disable_execution=True))
        elif scenario == "reject_symbol":
            verdict = _evaluate(action=_place_order_action(symbol="dogeusdt"))
        elif scenario == "reject_capability":
            policy = _base_policy(
                capabilities={
                    "read_market": True,
                    "read_account": True,
                    "place_order": False,
                    "cancel_order": True,
                    "withdraw": False,
                }
            )
            verdict = _evaluate(policy=policy)
        elif scenario == "reject_blocked":
            verdict = _evaluate(
                action={"type": "withdraw", "symbol": "btcusdt", "max_notional_usdt": 1}
            )
        else:  # reject_hallucination
            policy = _base_policy(
                limits={
                    **_base_policy()["limits"],
                    "allowed_symbols": ["ghostusdt"],
                }
            )
            verdict = _evaluate(
                action=_place_order_action(symbol="ghostusdt"),
                policy=policy,
                market_snapshot={"btcusdt": {"last": 68000.0}},
            )

        assert 0 <= verdict.risk_score <= 100

    def test_verdict_is_hashable_via_tuple_reason_codes(self) -> None:
        """``reason_codes`` 用 tuple 让 verdict 可被放入 set / dict key。"""
        verdict = _evaluate()
        # tuple 是可哈希的；list 不行
        hash(verdict.reason_codes)
        assert isinstance(verdict.reason_codes, tuple)


# ---------------------------------------------------------------------------
# 12. normalize_action 工具函数
# ---------------------------------------------------------------------------
class TestNormalizeAction:
    """`normalize_action` 工具函数：symbol 小写化 + 浅拷贝。"""

    def test_lowercases_symbol(self) -> None:
        result = normalize_action({"type": "place_order", "symbol": "BTCUSDT"})
        assert result["symbol"] == "btcusdt"

    def test_returns_copy_not_mutating_input(self) -> None:
        original = {"type": "place_order", "symbol": "BTCUSDT"}
        normalize_action(original)
        # 原 dict 未被修改
        assert original["symbol"] == "BTCUSDT"

    def test_preserves_other_fields(self) -> None:
        action: dict[str, Any] = {
            "type": "place_order",
            "symbol": "BTCUSDT",
            "amount": 10.0,
            "max_notional_usdt": 10.0,
        }
        result = normalize_action(action)
        assert result["amount"] == 10.0
        assert result["max_notional_usdt"] == 10.0

    def test_no_symbol_field_preserved(self) -> None:
        """no_op 不带 symbol → 原样返回。"""
        result = normalize_action({"type": "no_op", "rationale": "x"})
        assert result == {"type": "no_op", "rationale": "x"}

    def test_non_string_symbol_preserved(self) -> None:
        """symbol 字段非字符串（理论上 schema 已拦下）→ 不抛异常，原样保留。"""
        action: dict[str, Any] = {"type": "place_order", "symbol": None}
        result = normalize_action(action)
        assert result["symbol"] is None


# ---------------------------------------------------------------------------
# 13. REASON_CODES 常量自检
# ---------------------------------------------------------------------------
class TestReasonCodesConstant:
    """REASON_CODES 元组与 design.md 中的列表保持一致。"""

    def test_reason_codes_is_non_empty_tuple(self) -> None:
        assert isinstance(REASON_CODES, tuple)
        assert len(REASON_CODES) > 0

    def test_reason_codes_has_no_duplicates(self) -> None:
        assert len(set(REASON_CODES)) == len(REASON_CODES)

    def test_known_critical_codes_present(self) -> None:
        """这几个 code 是 spec 显式要求的——少了就是回归。"""
        critical = {
            "BLOCKED_ACTION_WITHDRAW",
            "CAPABILITY_NOT_GRANTED",
            "SYMBOL_NOT_ALLOWED",
            "LIMIT_MAX_NOTIONAL_EXCEEDED",
            "DAILY_LIMIT_EXCEEDED",
            "DAILY_ORDER_COUNT_EXCEEDED",
            "TIME_WINDOW_VIOLATION",
            "PLAN_HALLUCINATION",
            "EXECUTION_DISABLED",
            "UNKNOWN_FIELD_DETECTED",
        }
        assert critical.issubset(REASON_CODES_SET)
