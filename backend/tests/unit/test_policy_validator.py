"""任务 5.1 Policy DSL v0 校验器单元测试（Req 4）。

直接调用 :mod:`app.services.policy_validator`，不经过 HTTP / 数据库；
聚焦"协议契约 + 业务规则"两层。

覆盖维度
--------
1. **顶层结构**：5 个必填子节缺一即失败；version 必须 ``"0.1"``；未知顶层
   字段被拒（Req 4 AC1 / AC8）。
2. **capabilities**：5 个原子能力必填；withdraw 必须 false（Req 4 AC2）。
3. **limits**：4 个必填 + 3 个可选；类型/边界全覆盖；allowed_symbols 小写
   归一化（Req 4 AC3）；allowed_time_utc 跨午夜合法（Req 4 AC4）。
4. **approval**：2 个必填 boolean + 可选 expires_after_seconds 30-3600。
5. **blocked_actions**：仅允许 5 个枚举值。
6. **未知字段**：默认 ``allow_unknown=False`` 拒绝；``allow_unknown=True``
   时未知字段被忽略（Req 4 AC8）。
7. **辅助函数 ``normalize_symbol_list``**：去空格 / 去重保序 / 全部小写。

测试基线策略 ``small_spot_executor_policy()`` 取自 PRD §17 demo seed，与
任务 19 种子数据保持一致；每个用例只改一个字段，方便 fail 时定位。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.schemas.policy import (
    BLOCKED_ACTIONS_ENUM,
    POLICY_DSL_V0_SCHEMA,
    PolicyDSLv0,
)
from app.services.policy_validator import (
    InvalidPolicyError,
    is_cross_midnight,
    normalize_symbol_list,
    validate_policy_dsl,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def small_spot_executor_policy() -> dict[str, Any]:
    """PRD §17 demo seed 的 small_spot_executor 完整 policy。

    - allowed_symbols 含 btcusdt / ethusdt（与种子行情一致）。
    - max_notional 20，daily 100（PRD §17）。
    - approval.required_for_trade=true（PRD §9.2 模板默认）。
    - blocked_actions 列出 5 个全枚举（保守拒绝）。
    """
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
            "allowed_symbols": ["btcusdt", "ethusdt"],
            "max_notional_usdt_per_order": 20,
            "max_daily_notional_usdt": 100,
            "max_orders_per_day": 10,
            "allowed_order_types": ["limit", "market"],
            "max_slippage_bps": 50,
            "allowed_time_utc": {"start": "00:00", "end": "23:59"},
        },
        "approval": {
            "required_for_trade": True,
            "required_for_policy_change": True,
            "expires_after_seconds": 300,
        },
        "blocked_actions": [
            "withdraw",
            "borrow",
            "margin",
            "transfer_out",
            "unknown_tool_call",
        ],
    }


def _expect_invalid(raw: dict[str, Any], *, allow_unknown: bool = False) -> InvalidPolicyError:
    """语法糖：断言 validate_policy_dsl 抛 InvalidPolicyError 并返回它。"""
    with pytest.raises(InvalidPolicyError) as exc_info:
        validate_policy_dsl(raw, allow_unknown=allow_unknown)
    return exc_info.value


# ---------------------------------------------------------------------------
# 1. Happy path：完整 small_spot_executor 通过
# ---------------------------------------------------------------------------
class TestHappyPath:
    """合法策略全字段通过校验。"""

    def test_small_spot_executor_full_passes(self) -> None:
        """PRD §17 demo seed 完整策略 → 校验通过；返回 PolicyDSLv0 实例。"""
        policy = validate_policy_dsl(small_spot_executor_policy())
        assert isinstance(policy, PolicyDSLv0)
        assert policy.version == "0.1"
        # capabilities 全字段还原
        assert policy.capabilities.read_market is True
        assert policy.capabilities.read_account is True
        assert policy.capabilities.place_order is True
        assert policy.capabilities.cancel_order is True
        assert policy.capabilities.withdraw is False
        # limits 全字段还原
        assert policy.limits.allowed_symbols == ["btcusdt", "ethusdt"]
        assert policy.limits.max_notional_usdt_per_order == 20
        assert policy.limits.max_daily_notional_usdt == 100
        assert policy.limits.max_orders_per_day == 10
        assert policy.limits.allowed_order_types == ["limit", "market"]
        assert policy.limits.max_slippage_bps == 50
        assert policy.limits.allowed_time_utc is not None
        assert policy.limits.allowed_time_utc.start == "00:00"
        assert policy.limits.allowed_time_utc.end == "23:59"
        # approval
        assert policy.approval.required_for_trade is True
        assert policy.approval.required_for_policy_change is True
        assert policy.approval.expires_after_seconds == 300
        # blocked_actions
        assert set(policy.blocked_actions) == set(BLOCKED_ACTIONS_ENUM)

    def test_minimal_required_only_passes(self) -> None:
        """只给必填字段（不给可选 limits / approval.expires_after_seconds）也应通过。"""
        raw: dict[str, Any] = {
            "version": "0.1",
            "capabilities": {
                "read_market": True,
                "read_account": False,
                "place_order": False,
                "cancel_order": False,
                "withdraw": False,
            },
            "limits": {
                "allowed_symbols": ["btcusdt"],
                "max_notional_usdt_per_order": 0,
                "max_daily_notional_usdt": 0,
                "max_orders_per_day": 0,
            },
            "approval": {
                "required_for_trade": True,
                "required_for_policy_change": True,
            },
            "blocked_actions": [],
        }
        policy = validate_policy_dsl(raw)
        assert policy.limits.allowed_order_types is None
        assert policy.limits.max_slippage_bps is None
        assert policy.limits.allowed_time_utc is None
        assert policy.approval.expires_after_seconds is None

    def test_returned_policy_can_be_serialized_back_to_dict(self) -> None:
        """``model_dump()`` 输出可直接存 JSONB；与输入语义等价（symbols 已小写归一化）。"""
        policy = validate_policy_dsl(small_spot_executor_policy())
        dumped = policy.model_dump()
        assert dumped["version"] == "0.1"
        assert dumped["limits"]["allowed_symbols"] == ["btcusdt", "ethusdt"]
        # 再次 round-trip 应仍然合法
        re_validated = validate_policy_dsl(dumped)
        assert re_validated.model_dump() == dumped


# ---------------------------------------------------------------------------
# 2. 顶层结构 / version
# ---------------------------------------------------------------------------
class TestTopLevelStructure:
    """5 大顶层节缺一即失败；version 必须 0.1。"""

    @pytest.mark.parametrize(
        "missing_key",
        ["version", "capabilities", "limits", "approval", "blocked_actions"],
    )
    def test_missing_top_level_required(self, missing_key: str) -> None:
        """缺任一顶层必填节 → InvalidPolicyError。"""
        raw = small_spot_executor_policy()
        del raw[missing_key]
        err = _expect_invalid(raw)
        # 错误指向缺失的字段（jsonschema 在 root 路径报 'required'）
        assert any(missing_key in e["message"] for e in err.errors), (
            f"expected missing key '{missing_key}' in errors: {err.errors}"
        )

    def test_version_wrong_value(self) -> None:
        raw = small_spot_executor_policy()
        raw["version"] = "0.2"
        err = _expect_invalid(raw)
        assert any("version" in e["path"] or "0.1" in e["message"] for e in err.errors)

    def test_version_wrong_type(self) -> None:
        """version 必须是 string '0.1'，不接受数字 0.1。"""
        raw = small_spot_executor_policy()
        raw["version"] = 0.1
        err = _expect_invalid(raw)
        assert any("version" in e["path"] for e in err.errors)

    def test_root_not_a_dict(self) -> None:
        """非 dict 输入（list / 字符串）应直接拒绝。"""
        err = _expect_invalid([])  # type: ignore[arg-type]
        assert any("object" in e["message"].lower() for e in err.errors)

    def test_unknown_top_level_field_rejected(self) -> None:
        """顶层未知字段 → InvalidPolicyError（Req 4 AC8）。"""
        raw = small_spot_executor_policy()
        raw["extra_field"] = 1  # type: ignore[assignment]
        err = _expect_invalid(raw)
        assert any(
            "additionalProperties" in e["validator"] or "extra_field" in e["message"]
            for e in err.errors
        )


# ---------------------------------------------------------------------------
# 3. capabilities
# ---------------------------------------------------------------------------
class TestCapabilities:
    """5 个能力必填，withdraw const false。"""

    @pytest.mark.parametrize(
        "missing_cap",
        ["read_market", "read_account", "place_order", "cancel_order", "withdraw"],
    )
    def test_missing_capability_field(self, missing_cap: str) -> None:
        raw = small_spot_executor_policy()
        del raw["capabilities"][missing_cap]
        err = _expect_invalid(raw)
        assert any(missing_cap in e["message"] for e in err.errors)

    def test_withdraw_true_rejected(self) -> None:
        """capabilities.withdraw=true → 失败（Req 4 AC2）。

        const 在 schema 阶段已捕获；test 同时确认错误 path 指向 withdraw。
        """
        raw = small_spot_executor_policy()
        raw["capabilities"]["withdraw"] = True
        err = _expect_invalid(raw)
        assert any("withdraw" in e["path"] for e in err.errors)

    def test_capability_non_boolean_rejected(self) -> None:
        """capabilities.read_market 必须是 bool，不接受 'true' 字符串。"""
        raw = small_spot_executor_policy()
        raw["capabilities"]["read_market"] = "true"  # type: ignore[assignment]
        err = _expect_invalid(raw)
        assert any("read_market" in e["path"] for e in err.errors)

    def test_unknown_capability_field_rejected(self) -> None:
        """capabilities 内未知字段 → 失败（Req 4 AC8）。"""
        raw = small_spot_executor_policy()
        raw["capabilities"]["read_chain"] = True  # type: ignore[assignment]
        err = _expect_invalid(raw)
        assert any(
            "additionalProperties" in e["validator"] or "read_chain" in e["message"]
            for e in err.errors
        )


# ---------------------------------------------------------------------------
# 4. limits
# ---------------------------------------------------------------------------
class TestLimits:
    """4 必填 + 3 可选；归一化 / 边界。"""

    @pytest.mark.parametrize(
        "missing_field",
        [
            "allowed_symbols",
            "max_notional_usdt_per_order",
            "max_daily_notional_usdt",
            "max_orders_per_day",
        ],
    )
    def test_missing_required_limit(self, missing_field: str) -> None:
        raw = small_spot_executor_policy()
        del raw["limits"][missing_field]
        err = _expect_invalid(raw)
        assert any(missing_field in e["message"] for e in err.errors)

    def test_allowed_symbols_empty_array_rejected(self) -> None:
        """allowed_symbols=[] → 失败（minItems=1）。"""
        raw = small_spot_executor_policy()
        raw["limits"]["allowed_symbols"] = []
        err = _expect_invalid(raw)
        assert any("allowed_symbols" in e["path"] for e in err.errors)

    def test_allowed_symbols_uppercase_normalized_to_lowercase(self) -> None:
        """大写或混合大小写应被归一化为全小写并保序去重（Req 4 AC3）。"""
        raw = small_spot_executor_policy()
        raw["limits"]["allowed_symbols"] = ["BTCUSDT", "EthUsdt", "btcusdt"]
        policy = validate_policy_dsl(raw)
        # 保序去重：BTCUSDT 先出现 → btcusdt 在前；EthUsdt 第二个 → ethusdt 在后
        assert policy.limits.allowed_symbols == ["btcusdt", "ethusdt"]

    def test_max_notional_negative_rejected(self) -> None:
        """max_notional_usdt_per_order 负数 → 失败（minimum 0）。"""
        raw = small_spot_executor_policy()
        raw["limits"]["max_notional_usdt_per_order"] = -1
        err = _expect_invalid(raw)
        assert any("max_notional_usdt_per_order" in e["path"] for e in err.errors)

    def test_max_orders_per_day_float_rejected(self) -> None:
        """max_orders_per_day 必须 integer，不接受 float（即便整数值也要拒绝小数）。"""
        raw = small_spot_executor_policy()
        raw["limits"]["max_orders_per_day"] = 5.5
        err = _expect_invalid(raw)
        assert any("max_orders_per_day" in e["path"] for e in err.errors)

    def test_allowed_order_types_unknown_value_rejected(self) -> None:
        """allowed_order_types 仅允许 limit/market；'stop' → 失败。"""
        raw = small_spot_executor_policy()
        raw["limits"]["allowed_order_types"] = ["limit", "stop"]
        err = _expect_invalid(raw)
        assert any("allowed_order_types" in e["path"] for e in err.errors)

    def test_max_slippage_bps_above_500_rejected(self) -> None:
        """max_slippage_bps > 500 → 失败。"""
        raw = small_spot_executor_policy()
        raw["limits"]["max_slippage_bps"] = 501
        err = _expect_invalid(raw)
        assert any("max_slippage_bps" in e["path"] for e in err.errors)

    def test_max_slippage_bps_negative_rejected(self) -> None:
        raw = small_spot_executor_policy()
        raw["limits"]["max_slippage_bps"] = -1
        err = _expect_invalid(raw)
        assert any("max_slippage_bps" in e["path"] for e in err.errors)

    def test_allowed_time_utc_invalid_format(self) -> None:
        """start='25:00' → 失败（schema pattern ^[0-2][0-9]:[0-5][0-9]$ 不匹配 25:xx）。

        PRD §9.1 的 pattern ``^[0-2][0-9]:[0-5][0-9]$`` 实际允许 24:00-29:59；
        25:00 因为首位是 2、第二位是 5 …… 等等，让我们明确 pattern 行为：
        - "25:00" → 匹配 ``^[0-2][0-9]:[0-5][0-9]$`` 吗？
          首字符 '2' ∈ [0-2] ✓；第二字符 '5' ∈ [0-9] ✓；'5' ∈ [0-5] ✓；'0' ∈ [0-9] ✓ → 通过！
        因此本测试需要的是真正不匹配的串。改用 "30:00"（首位 '3' 不在 [0-2] 中）。
        """
        raw = small_spot_executor_policy()
        assert raw["limits"]["allowed_time_utc"] is not None
        raw["limits"]["allowed_time_utc"]["start"] = "30:00"
        err = _expect_invalid(raw)
        assert any("start" in e["path"] for e in err.errors)

    def test_allowed_time_utc_invalid_minute(self) -> None:
        """end='12:99' → 失败（pattern 第二组 [0-5][0-9]）。"""
        raw = small_spot_executor_policy()
        assert raw["limits"]["allowed_time_utc"] is not None
        raw["limits"]["allowed_time_utc"]["end"] = "12:99"
        err = _expect_invalid(raw)
        assert any("end" in e["path"] for e in err.errors)

    def test_allowed_time_utc_cross_midnight_passes(self) -> None:
        """22:00 → 02:00 跨午夜应通过校验（Req 4 AC4）。"""
        raw = small_spot_executor_policy()
        raw["limits"]["allowed_time_utc"] = {"start": "22:00", "end": "02:00"}
        policy = validate_policy_dsl(raw)
        assert policy.limits.allowed_time_utc is not None
        assert policy.limits.allowed_time_utc.start == "22:00"
        assert policy.limits.allowed_time_utc.end == "02:00"
        # 跨午夜识别工具
        assert is_cross_midnight("22:00", "02:00") is True
        assert is_cross_midnight("00:00", "23:59") is False
        assert is_cross_midnight("12:00", "12:00") is False

    def test_allowed_time_utc_missing_start_rejected_by_business_rule(self) -> None:
        """给了 allowed_time_utc 对象但只填 end → 业务规则拒绝。"""
        raw = small_spot_executor_policy()
        raw["limits"]["allowed_time_utc"] = {"end": "12:00"}
        err = _expect_invalid(raw)
        assert any("allowed_time_utc" in e["path"] for e in err.errors)

    def test_unknown_field_inside_limits_rejected(self) -> None:
        raw = small_spot_executor_policy()
        raw["limits"]["weekend_only"] = True  # type: ignore[assignment]
        err = _expect_invalid(raw)
        assert any(
            "additionalProperties" in e["validator"] or "weekend_only" in e["message"]
            for e in err.errors
        )


# ---------------------------------------------------------------------------
# 5. approval
# ---------------------------------------------------------------------------
class TestApproval:
    """approval 必填 boolean + expires_after_seconds 30-3600。"""

    @pytest.mark.parametrize(
        "missing", ["required_for_trade", "required_for_policy_change"]
    )
    def test_missing_required(self, missing: str) -> None:
        raw = small_spot_executor_policy()
        del raw["approval"][missing]
        err = _expect_invalid(raw)
        assert any(missing in e["message"] for e in err.errors)

    def test_expires_too_short_rejected(self) -> None:
        raw = small_spot_executor_policy()
        raw["approval"]["expires_after_seconds"] = 29
        err = _expect_invalid(raw)
        assert any("expires_after_seconds" in e["path"] for e in err.errors)

    def test_expires_too_long_rejected(self) -> None:
        raw = small_spot_executor_policy()
        raw["approval"]["expires_after_seconds"] = 3601
        err = _expect_invalid(raw)
        assert any("expires_after_seconds" in e["path"] for e in err.errors)

    def test_expires_boundary_30_passes(self) -> None:
        raw = small_spot_executor_policy()
        raw["approval"]["expires_after_seconds"] = 30
        policy = validate_policy_dsl(raw)
        assert policy.approval.expires_after_seconds == 30

    def test_expires_boundary_3600_passes(self) -> None:
        raw = small_spot_executor_policy()
        raw["approval"]["expires_after_seconds"] = 3600
        policy = validate_policy_dsl(raw)
        assert policy.approval.expires_after_seconds == 3600


# ---------------------------------------------------------------------------
# 6. blocked_actions
# ---------------------------------------------------------------------------
class TestBlockedActions:
    """blocked_actions 仅允许 5 个 enum 值。"""

    def test_unknown_blocked_action_rejected(self) -> None:
        """transfer_in 不在 enum 中 → 失败。"""
        raw = small_spot_executor_policy()
        raw["blocked_actions"] = ["withdraw", "transfer_in"]
        err = _expect_invalid(raw)
        assert any(
            "transfer_in" in e["message"] or "blocked_actions" in e["path"]
            for e in err.errors
        )

    def test_empty_blocked_actions_passes(self) -> None:
        """空数组合法（PRD §9.1 未声明 minItems）。"""
        raw = small_spot_executor_policy()
        raw["blocked_actions"] = []
        policy = validate_policy_dsl(raw)
        assert policy.blocked_actions == []

    def test_all_five_enums_valid(self) -> None:
        """5 个枚举值全部允许；本测试也固化"枚举集合"作为契约。"""
        raw = small_spot_executor_policy()
        raw["blocked_actions"] = list(BLOCKED_ACTIONS_ENUM)
        policy = validate_policy_dsl(raw)
        assert set(policy.blocked_actions) == set(BLOCKED_ACTIONS_ENUM)


# ---------------------------------------------------------------------------
# 7. allow_unknown 开发模式
# ---------------------------------------------------------------------------
class TestAllowUnknown:
    """``allow_unknown=True`` 时未知字段被忽略（Req 4 AC8 例外条款）。"""

    def test_unknown_top_level_ignored_when_allow_unknown(self) -> None:
        raw = small_spot_executor_policy()
        raw["debug_note"] = "ignored in dev mode"  # type: ignore[assignment]
        policy = validate_policy_dsl(raw, allow_unknown=True)
        # 未知字段不会进入 PolicyDSLv0 — model_dump 中无 debug_note
        dumped = policy.model_dump()
        assert "debug_note" not in dumped

    def test_unknown_nested_field_ignored_when_allow_unknown(self) -> None:
        raw = small_spot_executor_policy()
        raw["limits"]["weekend_only"] = True  # type: ignore[assignment]
        policy = validate_policy_dsl(raw, allow_unknown=True)
        dumped = policy.model_dump()
        assert "weekend_only" not in dumped["limits"]

    def test_allow_unknown_still_enforces_required_and_const(self) -> None:
        """allow_unknown 不会放过必填缺失或 withdraw=true。"""
        raw = small_spot_executor_policy()
        raw["capabilities"]["withdraw"] = True
        with pytest.raises(InvalidPolicyError):
            validate_policy_dsl(raw, allow_unknown=True)

    def test_allow_unknown_still_enforces_min_items(self) -> None:
        raw = small_spot_executor_policy()
        raw["limits"]["allowed_symbols"] = []
        with pytest.raises(InvalidPolicyError):
            validate_policy_dsl(raw, allow_unknown=True)


# ---------------------------------------------------------------------------
# 8. normalize_symbol_list 直接单测
# ---------------------------------------------------------------------------
class TestNormalizeSymbolList:
    """辅助函数行为锁定：去空格 / 去重保序 / 全小写。"""

    def test_strips_and_lowercases(self) -> None:
        assert normalize_symbol_list([" BTCUSDT ", "ETH USDT".strip()]) == [
            "btcusdt",
            "eth usdt",
        ]
        # 内部空白不剔除（只 strip 前后）；这是设计决策——symbol 内一般无空格，
        # 真出现空格说明 caller 数据脏，让 SYMBOL_NOT_ALLOWED 自然报错更醒目。

    def test_dedupe_preserves_first_seen_order(self) -> None:
        """重复元素按首次出现顺序保留。"""
        assert normalize_symbol_list(
            ["BTCUSDT", "ETHUSDT", "btcusdt", "ETHUSDT"]
        ) == ["btcusdt", "ethusdt"]

    def test_empty_input_returns_empty(self) -> None:
        assert normalize_symbol_list([]) == []

    def test_only_whitespace_skipped(self) -> None:
        """全空白字符串被丢弃，不进结果列表。"""
        assert normalize_symbol_list(["  ", "btcusdt"]) == ["btcusdt"]

    def test_already_lowercase_unchanged(self) -> None:
        """已是小写且无重复 → 原样返回（顺序保持）。"""
        assert normalize_symbol_list(["btcusdt", "ethusdt", "solusdt"]) == [
            "btcusdt",
            "ethusdt",
            "solusdt",
        ]


# ---------------------------------------------------------------------------
# 9. is_cross_midnight 工具函数
# ---------------------------------------------------------------------------
class TestIsCrossMidnight:
    """跨午夜识别（Req 4 AC4）。

    本任务只判别"是否跨午夜"，"当前时间是否落在窗口内"由 Policy Engine（任务 8）实现。
    """

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            ("22:00", "02:00", True),  # 跨午夜
            ("23:59", "00:00", True),  # 极小跨午夜
            ("00:00", "23:59", False),  # 几乎全天
            ("09:00", "17:00", False),  # 工作时段
            ("12:00", "12:00", False),  # 退化（同一时刻）
            ("12:01", "12:00", True),  # 几乎全天但跨午夜
        ],
    )
    def test_is_cross_midnight(self, start: str, end: str, expected: bool) -> None:
        assert is_cross_midnight(start, end) is expected


# ---------------------------------------------------------------------------
# 10. Schema 完整性自检
# ---------------------------------------------------------------------------
class TestSchemaIntegrity:
    """对 :data:`POLICY_DSL_V0_SCHEMA` 自身的健全性断言。

    把"PRD §9.1 契约"固化成测试，避免有人无意修改 schema 后还能通过现有用例。
    """

    def test_schema_top_level_required_keys(self) -> None:
        assert set(POLICY_DSL_V0_SCHEMA["required"]) == {
            "version",
            "capabilities",
            "limits",
            "approval",
            "blocked_actions",
        }

    def test_schema_capabilities_all_required(self) -> None:
        cap = POLICY_DSL_V0_SCHEMA["properties"]["capabilities"]
        assert set(cap["required"]) == {
            "read_market",
            "read_account",
            "place_order",
            "cancel_order",
            "withdraw",
        }
        assert cap["properties"]["withdraw"] == {"const": False}

    def test_schema_limits_required_subset(self) -> None:
        limits = POLICY_DSL_V0_SCHEMA["properties"]["limits"]
        assert set(limits["required"]) == {
            "allowed_symbols",
            "max_notional_usdt_per_order",
            "max_daily_notional_usdt",
            "max_orders_per_day",
        }

    def test_schema_blocked_actions_enum(self) -> None:
        ba = POLICY_DSL_V0_SCHEMA["properties"]["blocked_actions"]
        assert set(ba["items"]["enum"]) == set(BLOCKED_ACTIONS_ENUM)

    def test_all_object_subschemas_disallow_additional_properties(self) -> None:
        """每个 object 子节都必须 ``additionalProperties: false``（Req 4 AC8 第一道闸）。"""
        # 顶层
        assert POLICY_DSL_V0_SCHEMA["additionalProperties"] is False
        # capabilities / limits / approval / allowed_time_utc
        for path in (
            ("properties", "capabilities"),
            ("properties", "limits"),
            ("properties", "approval"),
            ("properties", "limits", "properties", "allowed_time_utc"),
        ):
            node: Any = POLICY_DSL_V0_SCHEMA
            for k in path:
                node = node[k]
            assert node["additionalProperties"] is False, (
                f"object subschema at {path!r} must set additionalProperties=false"
            )
