"""任务 5.2 能力包构建器与默认模板单元测试（Req 4 / Property 4）。

直接调用 :mod:`app.services.capability_envelope`，覆盖：

1. 三档内置模板（PRD §9.2）的字段值与 schema 合法性。
2. :func:`build_policy_from_template` 在「无 overrides」「合法 overrides」
   「非法 overrides」三类输入下的行为。
3. :func:`list_templates` 返回的元数据形态。
4. :func:`is_action_type_allowed_by_capabilities` 的全枚举矩阵。
5. **Property 4 能力包封闭性**（PBT）：对任意 capabilities + 任意 action_type，
   闭包检查函数只返回「对应 capabilities 字段为 True」时的 True；其余皆 False
   （含 ``no_op`` 永远 True、withdraw / 未知 type 永远 False）。

所有 PRD §9.2 / §17 数值锚点都被显式断言：
- ``small_spot_executor``: max_notional=20, daily=100  ← PRD §17 demo seed
- ``dao_treasury_guarded``: max_notional=50, daily=200 ← PRD §9.2
- ``readonly_researcher``: 写操作 capability 全 false（PRD §9.2 仅 read_market）

任务 19 种子加载会基于这些数值；本测试一旦 fail 就意味着 demo 流程也会出错。
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.schemas.policy import Capabilities, PolicyDSLv0
from app.services.capability_envelope import (
    TEMPLATE_DAO_TREASURY_GUARDED,
    TEMPLATE_READONLY_RESEARCHER,
    TEMPLATE_SMALL_SPOT_EXECUTOR,
    TEMPLATES,
    PolicyTemplate,
    build_policy_from_template,
    is_action_type_allowed_by_capabilities,
    list_templates,
)
from app.services.policy_validator import (
    InvalidPolicyError,
    validate_policy_dsl,
)


# 防御性 blocked_actions 必含的 4 个枚举值（withdraw / borrow / margin / transfer_out）。
# 即便 capabilities 已禁用 place_order，也要靠 blocked_actions 形成第二道闸。
EXPECTED_BLOCKED: set[str] = {"withdraw", "borrow", "margin", "transfer_out"}


# ---------------------------------------------------------------------------
# 1. 模板自身的 schema 合法性（启动期自检的运行时复核）
# ---------------------------------------------------------------------------
class TestTemplatesPassValidation:
    """每个模板都能直接通过 :func:`validate_policy_dsl`。

    模块加载时已跑过 ``_self_check_templates``；这里再独立跑一次的目的：
    - 让单测用例显式覆盖「模板 → validator」这条路径，CI 报错时一眼能看到。
    - 防止有人删掉 ``_self_check_templates()`` 调用后回归无声地通过。
    """

    @pytest.mark.parametrize("template", list(PolicyTemplate))
    def test_template_passes_schema(self, template: PolicyTemplate) -> None:
        policy = validate_policy_dsl(TEMPLATES[template])
        assert isinstance(policy, PolicyDSLv0)
        assert policy.version == "0.1"
        # 每个模板都必须显式 withdraw=False（Req 4 AC2）。
        assert policy.capabilities.withdraw is False


# ---------------------------------------------------------------------------
# 2. PRD §9.2 / §17 数值锚点
# ---------------------------------------------------------------------------
class TestReadonlyResearcherTemplate:
    """PRD §9.2 readonly_researcher 模板字段断言。"""

    def test_capabilities_match_prd(self) -> None:
        """capabilities: 仅 read_market=true，其余全部 false（PRD §9.2）。"""
        caps = TEMPLATE_READONLY_RESEARCHER["capabilities"]
        assert caps == {
            "read_market": True,
            "read_account": False,
            "place_order": False,
            "cancel_order": False,
            "withdraw": False,
        }

    def test_limits_zero_writes(self) -> None:
        """三个写限额全为 0：双闸门防御（capabilities false + limits 0）。"""
        limits = TEMPLATE_READONLY_RESEARCHER["limits"]
        assert limits["max_notional_usdt_per_order"] == 0
        assert limits["max_daily_notional_usdt"] == 0
        assert limits["max_orders_per_day"] == 0

    def test_approval_required_for_trade(self) -> None:
        """PRD §9.2: approval.required_for_trade=true。"""
        assert TEMPLATE_READONLY_RESEARCHER["approval"]["required_for_trade"] is True

    def test_blocked_actions_contains_defensive_set(self) -> None:
        """blocked_actions 至少包含 withdraw/borrow/margin/transfer_out。"""
        assert EXPECTED_BLOCKED.issubset(set(TEMPLATE_READONLY_RESEARCHER["blocked_actions"]))


class TestSmallSpotExecutorTemplate:
    """PRD §9.2 + §17 small_spot_executor 模板字段断言。

    数值与 demo seed 严格对齐——任务 19 种子加载会复用这份模板，任何漂移都会
    让 happy path 测试失败。
    """

    def test_capabilities_match_prd(self) -> None:
        """capabilities: 全 true 除 withdraw（PRD §9.2）。"""
        caps = TEMPLATE_SMALL_SPOT_EXECUTOR["capabilities"]
        assert caps == {
            "read_market": True,
            "read_account": True,
            "place_order": True,
            "cancel_order": True,
            "withdraw": False,
        }

    def test_limits_match_prd_section_17_demo_seed(self) -> None:
        """PRD §17: max_notional=20 USDT/单, max_daily=100 USDT/日。"""
        limits = TEMPLATE_SMALL_SPOT_EXECUTOR["limits"]
        assert limits["max_notional_usdt_per_order"] == 20
        assert limits["max_daily_notional_usdt"] == 100

    def test_allowed_symbols_match_demo_seed(self) -> None:
        """PRD §17 demo seed: ["btcusdt", "ethusdt"]。"""
        assert TEMPLATE_SMALL_SPOT_EXECUTOR["limits"]["allowed_symbols"] == [
            "btcusdt",
            "ethusdt",
        ]

    def test_max_orders_per_day_reasonable(self) -> None:
        """max_orders_per_day 大于 0（PRD §9.2 未指定，但模板必须可下单）。"""
        assert TEMPLATE_SMALL_SPOT_EXECUTOR["limits"]["max_orders_per_day"] > 0

    def test_approval_required_for_trade(self) -> None:
        assert TEMPLATE_SMALL_SPOT_EXECUTOR["approval"]["required_for_trade"] is True

    def test_blocked_actions_contains_defensive_set(self) -> None:
        assert EXPECTED_BLOCKED.issubset(set(TEMPLATE_SMALL_SPOT_EXECUTOR["blocked_actions"]))


class TestDaoTreasuryGuardedTemplate:
    """PRD §9.2 dao_treasury_guarded 模板字段断言。"""

    def test_capabilities_match_prd(self) -> None:
        """capabilities 与 small_spot_executor 同：全 true 除 withdraw。"""
        caps = TEMPLATE_DAO_TREASURY_GUARDED["capabilities"]
        assert caps == {
            "read_market": True,
            "read_account": True,
            "place_order": True,
            "cancel_order": True,
            "withdraw": False,
        }

    def test_limits_match_prd_section_9_2(self) -> None:
        """PRD §9.2: max_notional=50, max_daily=200。"""
        limits = TEMPLATE_DAO_TREASURY_GUARDED["limits"]
        assert limits["max_notional_usdt_per_order"] == 50
        assert limits["max_daily_notional_usdt"] == 200

    def test_approval_required_for_trade(self) -> None:
        assert TEMPLATE_DAO_TREASURY_GUARDED["approval"]["required_for_trade"] is True

    def test_blocked_actions_contains_defensive_set(self) -> None:
        assert EXPECTED_BLOCKED.issubset(set(TEMPLATE_DAO_TREASURY_GUARDED["blocked_actions"]))


# ---------------------------------------------------------------------------
# 3. 模板枚举一致性
# ---------------------------------------------------------------------------
class TestPolicyTemplateEnum:
    """枚举值与 PRD §17 demo seed.passport.policy_template 字符串一致。"""

    def test_enum_values(self) -> None:
        """三个枚举值的字符串形态固定（demo seed 与 API 均依赖）。"""
        assert PolicyTemplate.READONLY_RESEARCHER.value == "readonly_researcher"
        assert PolicyTemplate.SMALL_SPOT_EXECUTOR.value == "small_spot_executor"
        assert PolicyTemplate.DAO_TREASURY_GUARDED.value == "dao_treasury_guarded"

    def test_enum_is_str_subclass(self) -> None:
        """``PolicyTemplate(str, Enum)`` 让 JSON / Pydantic 自动识别为字符串。"""
        assert isinstance(PolicyTemplate.SMALL_SPOT_EXECUTOR, str)
        assert PolicyTemplate.SMALL_SPOT_EXECUTOR == "small_spot_executor"

    def test_templates_dict_covers_all_enum_members(self) -> None:
        """TEMPLATES 字典 key 与枚举成员一一对应（防止漏写）。"""
        assert set(TEMPLATES.keys()) == set(PolicyTemplate)


# ---------------------------------------------------------------------------
# 4. build_policy_from_template
# ---------------------------------------------------------------------------
class TestBuildPolicyFromTemplate:
    """构建器在合法 / 非法 overrides 下的行为。"""

    @pytest.mark.parametrize("template", list(PolicyTemplate))
    def test_no_overrides_returns_pydantic_instance(self, template: PolicyTemplate) -> None:
        """无 overrides → 返回模板对应的 PolicyDSLv0 实例。"""
        policy = build_policy_from_template(template)
        assert isinstance(policy, PolicyDSLv0)
        # capabilities 字段值与模板字典一致
        assert policy.capabilities.model_dump() == TEMPLATES[template]["capabilities"]

    def test_overrides_replace_top_level_section(self) -> None:
        """传入 ``overrides={'limits': {...}}`` 替换整个 limits 节。"""
        new_limits: dict[str, Any] = {
            "allowed_symbols": ["solusdt"],
            "max_notional_usdt_per_order": 5,
            "max_daily_notional_usdt": 25,
            "max_orders_per_day": 5,
        }
        policy = build_policy_from_template(
            PolicyTemplate.SMALL_SPOT_EXECUTOR,
            overrides={"limits": new_limits},
        )
        assert policy.limits.allowed_symbols == ["solusdt"]
        assert policy.limits.max_notional_usdt_per_order == 5
        assert policy.limits.max_daily_notional_usdt == 25
        assert policy.limits.max_orders_per_day == 5
        # 其他顶层节未被 override → 保持模板值
        assert policy.capabilities.place_order is True

    def test_overrides_with_invalid_value_raises(self) -> None:
        """overrides 让 withdraw=true → InvalidPolicyError。"""
        with pytest.raises(InvalidPolicyError):
            build_policy_from_template(
                PolicyTemplate.SMALL_SPOT_EXECUTOR,
                overrides={
                    "capabilities": {
                        "read_market": True,
                        "read_account": True,
                        "place_order": True,
                        "cancel_order": True,
                        "withdraw": True,  # ← 非法
                    }
                },
            )

    def test_overrides_with_unknown_top_level_key_rejected(self) -> None:
        """顶层未知 key 在 schema 阶段被 ``additionalProperties: false`` 拦下。"""
        with pytest.raises(InvalidPolicyError):
            build_policy_from_template(
                PolicyTemplate.SMALL_SPOT_EXECUTOR,
                overrides={"extra_field": "not allowed"},
            )

    def test_overrides_with_invalid_limit_value_rejected(self) -> None:
        """limits.max_notional 设为负数 → 失败（minimum 0）。"""
        with pytest.raises(InvalidPolicyError):
            build_policy_from_template(
                PolicyTemplate.SMALL_SPOT_EXECUTOR,
                overrides={
                    "limits": {
                        "allowed_symbols": ["btcusdt"],
                        "max_notional_usdt_per_order": -1,
                        "max_daily_notional_usdt": 100,
                        "max_orders_per_day": 5,
                    }
                },
            )

    def test_returned_policy_has_normalized_symbols(self) -> None:
        """overrides 中大写 symbol 应被 validator 归一化为小写（Req 4 AC3）。"""
        policy = build_policy_from_template(
            PolicyTemplate.SMALL_SPOT_EXECUTOR,
            overrides={
                "limits": {
                    "allowed_symbols": ["BTCUSDT", "EthUsdt"],
                    "max_notional_usdt_per_order": 10,
                    "max_daily_notional_usdt": 50,
                    "max_orders_per_day": 5,
                }
            },
        )
        assert policy.limits.allowed_symbols == ["btcusdt", "ethusdt"]

    def test_template_dict_not_mutated_by_builder(self) -> None:
        """构建器使用 deepcopy；多次调用不会污染模板源。"""
        original = dict(TEMPLATES[PolicyTemplate.SMALL_SPOT_EXECUTOR]["limits"])
        # 通过 overrides 改 limits
        build_policy_from_template(
            PolicyTemplate.SMALL_SPOT_EXECUTOR,
            overrides={
                "limits": {
                    "allowed_symbols": ["solusdt"],
                    "max_notional_usdt_per_order": 1,
                    "max_daily_notional_usdt": 1,
                    "max_orders_per_day": 1,
                }
            },
        )
        # 模板源未变
        assert TEMPLATES[PolicyTemplate.SMALL_SPOT_EXECUTOR]["limits"] == original

    def test_unknown_template_value_rejected(self) -> None:
        """非枚举值（直接传字符串）→ InvalidPolicyError。

        本函数虽签名要求 ``PolicyTemplate``，但运行期可能被绕过；
        防御性处理让错误信息明确。
        """
        with pytest.raises(InvalidPolicyError):
            build_policy_from_template("nonexistent_template")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5. list_templates
# ---------------------------------------------------------------------------
class TestListTemplates:
    """list_templates 元数据形态与不可变性。"""

    def test_returns_three_templates(self) -> None:
        items = list_templates()
        assert len(items) == 3

    def test_each_item_has_required_keys(self) -> None:
        for item in list_templates():
            assert set(item.keys()) == {"name", "description", "policy"}
            assert isinstance(item["name"], str)
            assert isinstance(item["description"], str)
            assert isinstance(item["policy"], dict)
            # description 非空
            assert len(item["description"]) > 0

    def test_names_match_enum_values(self) -> None:
        names = [item["name"] for item in list_templates()]
        assert names == [
            "readonly_researcher",
            "small_spot_executor",
            "dao_treasury_guarded",
        ]

    def test_each_policy_passes_validation(self) -> None:
        """list_templates 返回的 policy dict 都能通过 validator。"""
        for item in list_templates():
            validate_policy_dsl(item["policy"])

    def test_returned_policy_is_deepcopy(self) -> None:
        """修改返回的 policy 不会污染模块级模板。"""
        items = list_templates()
        items[0]["policy"]["limits"]["allowed_symbols"] = ["MUTATED"]
        # 重新取一次应仍是原值
        fresh = list_templates()
        assert fresh[0]["policy"]["limits"]["allowed_symbols"] != ["MUTATED"]


# ---------------------------------------------------------------------------
# 6. is_action_type_allowed_by_capabilities — 全枚举矩阵
# ---------------------------------------------------------------------------
def _make_caps(
    *,
    read_market: bool = False,
    read_account: bool = False,
    place_order: bool = False,
    cancel_order: bool = False,
) -> Capabilities:
    """构造 Capabilities 实例的语法糖；withdraw 永远 False。"""
    return Capabilities(
        read_market=read_market,
        read_account=read_account,
        place_order=place_order,
        cancel_order=cancel_order,
        withdraw=False,
    )


class TestIsActionTypeAllowedByCapabilities:
    """全枚举矩阵：(action_type, capability_value) → expected。"""

    def test_no_op_always_allowed_even_with_empty_capabilities(self) -> None:
        """no_op 永远 True：纯语义动作，不需任何能力。"""
        caps = _make_caps()  # 全 false
        assert is_action_type_allowed_by_capabilities("no_op", caps) is True

    def test_no_op_allowed_with_full_capabilities(self) -> None:
        """no_op 即便所有能力都开也仍是 True（不依赖 capabilities 状态）。"""
        caps = _make_caps(
            read_market=True, read_account=True, place_order=True, cancel_order=True
        )
        assert is_action_type_allowed_by_capabilities("no_op", caps) is True

    @pytest.mark.parametrize(
        ("action_type", "field_name"),
        [
            ("read_market", "read_market"),
            ("read_account", "read_account"),
            ("place_order", "place_order"),
            ("cancel_order", "cancel_order"),
        ],
    )
    def test_action_type_allowed_iff_capability_true(
        self, action_type: str, field_name: str
    ) -> None:
        """对四个核心 action_type，结果必须等于对应 capability 字段。"""
        # capability=True → 允许
        caps_on = _make_caps(**{field_name: True})  # type: ignore[arg-type]
        assert is_action_type_allowed_by_capabilities(action_type, caps_on) is True
        # capability=False → 拒绝
        caps_off = _make_caps()
        assert is_action_type_allowed_by_capabilities(action_type, caps_off) is False

    @pytest.mark.parametrize(
        "unknown_type",
        [
            "withdraw",  # 系统级禁用，永远不许
            "borrow",
            "margin",
            "transfer_out",
            "unknown_tool_call",
            "",  # 空串
            "READ_MARKET",  # 大小写敏感，大写不命中
            "read market",  # 含空格
            "stop_loss",  # 任意未支持类型
        ],
    )
    def test_unknown_or_blocked_action_type_always_false(self, unknown_type: str) -> None:
        """未知 / 被禁用的 action_type 永远 False，即便 capability 全 true。"""
        caps_full = _make_caps(
            read_market=True, read_account=True, place_order=True, cancel_order=True
        )
        assert is_action_type_allowed_by_capabilities(unknown_type, caps_full) is False


# ---------------------------------------------------------------------------
# 7. PBT — Property 4 能力包封闭性
# ---------------------------------------------------------------------------
# **Validates: Requirements 4**
#
# Property 4: Policy Engine 的 ALLOW 裁决只可能出现在 capabilities 中声明
# 为 true 的 action type 上。本任务先在「闭包检查函数」层面锁住语义：
# 对任意 capabilities 与任意 action_type，is_action_type_allowed_by_capabilities
# 返回 True 当且仅当：
#   - action_type == "no_op"，或
#   - action_type ∈ {read_market, read_account, place_order, cancel_order}
#     且 capabilities 对应同名字段为 True
#
# 任务 8 Policy Engine Step 2 复用本函数，因此这里的 PBT 实际上为 Property 4
# 在「能力包封闭」这条边上提供了形式化保证。

# 完整 action_type 候选集：5 个 schema 内合法 + 1 个被禁用 + 1 个未知。
ACTION_TYPES_FOR_PBT = [
    "read_market",
    "read_account",
    "place_order",
    "cancel_order",
    "no_op",
    "withdraw",  # 永远 false
    "unknown",  # 未知字符串
]


@pytest.mark.pbt
class TestPropertyCapabilityEnvelopeClosure:
    """**Validates: Requirements 4**（Property 4：能力包封闭性）。

    对随机生成的 capabilities + action_type 做组合，把 expected 用一个
    完全独立、形式化的表达式重算（避免「测试和实现写同一份逻辑」陷阱），
    再与 :func:`is_action_type_allowed_by_capabilities` 返回值比较。

    Hypothesis 会自动尝试边界（全 false / 全 true / 单个 true 等），任何
    实现回归——例如把 ``"no_op"`` 漏写、把 ``"withdraw"`` 错配到 ``withdraw``
    capability、或把 ``getattr`` 改成不安全形式——都会被随机抽样捕获。
    """

    @given(
        action_type=st.sampled_from(ACTION_TYPES_FOR_PBT),
        read_market=st.booleans(),
        read_account=st.booleans(),
        place_order=st.booleans(),
        cancel_order=st.booleans(),
    )
    @settings(
        max_examples=80,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_returns_true_iff_capability_field_true_or_no_op(
        self,
        action_type: str,
        read_market: bool,
        read_account: bool,
        place_order: bool,
        cancel_order: bool,
    ) -> None:
        caps = Capabilities(
            read_market=read_market,
            read_account=read_account,
            place_order=place_order,
            cancel_order=cancel_order,
            withdraw=False,  # schema 强制 false；PBT 也保持
        )

        # 形式化表达 expected：与实现独立，避免循环。
        if action_type == "no_op":
            expected = True
        elif action_type == "read_market":
            expected = read_market
        elif action_type == "read_account":
            expected = read_account
        elif action_type == "place_order":
            expected = place_order
        elif action_type == "cancel_order":
            expected = cancel_order
        else:
            # withdraw / unknown 等 → 永远 False
            expected = False

        actual = is_action_type_allowed_by_capabilities(action_type, caps)
        assert actual is expected, (
            f"action_type={action_type!r} caps=("
            f"rm={read_market} ra={read_account} po={place_order} co={cancel_order}"
            f") → got {actual}, expected {expected}"
        )

    @given(
        read_market=st.booleans(),
        read_account=st.booleans(),
        place_order=st.booleans(),
        cancel_order=st.booleans(),
    )
    @settings(
        max_examples=40,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_no_op_unconditionally_allowed(
        self,
        read_market: bool,
        read_account: bool,
        place_order: bool,
        cancel_order: bool,
    ) -> None:
        """单独锁住「no_op 永远 True」这条强属性。"""
        caps = Capabilities(
            read_market=read_market,
            read_account=read_account,
            place_order=place_order,
            cancel_order=cancel_order,
            withdraw=False,
        )
        assert is_action_type_allowed_by_capabilities("no_op", caps) is True

    @given(
        unknown_type=st.text(min_size=1, max_size=30).filter(
            lambda s: s
            not in {
                "no_op",
                "read_market",
                "read_account",
                "place_order",
                "cancel_order",
            }
        ),
        read_market=st.booleans(),
        read_account=st.booleans(),
        place_order=st.booleans(),
        cancel_order=st.booleans(),
    )
    @settings(
        max_examples=60,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_unknown_action_type_always_rejected(
        self,
        unknown_type: str,
        read_market: bool,
        read_account: bool,
        place_order: bool,
        cancel_order: bool,
    ) -> None:
        """对任意非 5 个合法 action_type 的字符串，永远返回 False。

        包含 ``withdraw`` / 空字符串 / 中文 / emoji / SQL 片段等——这条
        Property 把「未知字符串绝不通过 capability 检查」固化下来，是 Property 4
        的下半部分（封闭集合外永远 deny）。
        """
        caps = Capabilities(
            read_market=read_market,
            read_account=read_account,
            place_order=place_order,
            cancel_order=cancel_order,
            withdraw=False,
        )
        assert (
            is_action_type_allowed_by_capabilities(unknown_type, caps) is False
        ), f"unknown action_type {unknown_type!r} unexpectedly allowed"
