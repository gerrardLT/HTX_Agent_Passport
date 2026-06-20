"""任务 6 ActionPlan v0 校验器单元测试（Req 6）。

直接调用 :mod:`app.schemas.action_plan`（不经过 HTTP 或数据库），聚焦
"协议契约 + 条件必填 + 鲁棒性输入"三层。

覆盖策略
--------
1. **顶层结构**（Req 6 AC4 / AC5）：5 必填子节缺一返 None；version 必须 "0.1"；
   actions 长度严格 1-3；intent_summary ≤ 500 字符。
2. **type 条件必填**（Req 6 AC1 / AC2 / AC3）：
   - place_order / cancel_order 7 字段全必填，缺一返 None。
   - read_market / read_account 仅 symbol 必填；缺 symbol 返 None。
   - no_op 仅 type + rationale 必填；缺 rationale 返 None。
   - 未知 type 返 None（与 Req 15 AC7「未知 action type SHALL 被 REJECT」对齐）。
3. **数据规范化**（Req 6 AC6）：symbol 自动小写；负数 amount / max_notional 返 None。
4. **输入鲁棒性**：非 JSON / markdown 包裹 / 空串 / None / 顶层未知字段 /
   action 内未知字段全部返 None（"failure 静默化"，对应 :func:`validate_action_plan_schema`
   的 None 语义契约）。

为何用纯函数 + None 断言而非异常断言？
-----------------------------------
:func:`validate_action_plan_schema` 故意不抛异常（详见模块 docstring）——失败
是"业务常态"而非"系统异常"。因此本测试模块只断言 "返回值是 None / 是 ActionPlanV0
实例"，不去 introspect 具体哪一条 ValidationError——那些细节是 Pydantic
内部演化，不应被测试代码绑死。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.schemas.action_plan import (
    ActionPlanV0,
    NoOpAction,
    PlaceOrCancelOrderAction,
    ReadAction,
    validate_action_plan_schema,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def _make_read_action(symbol: str = "btcusdt") -> dict[str, Any]:
    """构造一个最小合法的 read_market action dict。"""
    return {"type": "read_market", "symbol": symbol}


def _make_place_order_action(**overrides: Any) -> dict[str, Any]:
    """构造一个完整合法的 place_order action dict；用 overrides 覆盖单个字段。"""
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
        "rationale": "buy 10 USDT of BTC at 68000",
    }
    base.update(overrides)
    return base


def _make_cancel_order_action(**overrides: Any) -> dict[str, Any]:
    """构造一个完整合法的 cancel_order action dict。"""
    base: dict[str, Any] = {
        "type": "cancel_order",
        "symbol": "btcusdt",
        "side": "none",
        "order_type": "none",
        "amount": 0.0,
        "amount_unit": "none",
        "max_notional_usdt": 0.0,
        "rationale": "cancel previous open order",
    }
    base.update(overrides)
    return base


def _wrap_plan(actions: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    """把 action 列表包装成完整的 ActionPlan v0 dict。"""
    base: dict[str, Any] = {
        "version": "0.1",
        "intent_summary": "demo intent",
        "actions": actions,
        "assumptions": [],
        "risk_notes": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. 顶层校验（Req 6 AC4 / AC5）
# ---------------------------------------------------------------------------
class TestTopLevelStructure:
    """5 个顶层必填字段缺一返 None；version / actions 长度严格约束。"""

    def test_valid_minimal_plan(self) -> None:
        """含 1 个 read_market action 的最小合法 plan → 通过校验。"""
        plan = validate_action_plan_schema(_wrap_plan([_make_read_action()]))
        assert isinstance(plan, ActionPlanV0)
        assert plan.version == "0.1"
        assert len(plan.actions) == 1
        assert isinstance(plan.actions[0], ReadAction)
        assert plan.actions[0].type == "read_market"
        assert plan.actions[0].symbol == "btcusdt"

    def test_valid_full_plan_with_three_action_types(self) -> None:
        """同时包含 read_market / place_order / no_op 三种 type 的 plan → 通过校验。"""
        actions = [
            _make_read_action("btcusdt"),
            _make_place_order_action(),
            {"type": "no_op", "rationale": "user requested withdrawal, blocked"},
        ]
        plan = validate_action_plan_schema(
            _wrap_plan(
                actions,
                intent_summary="check, place, then refuse",
                assumptions=["btcusdt is in market_snapshot"],
                risk_notes=["limit price within slippage"],
            )
        )
        assert isinstance(plan, ActionPlanV0)
        assert len(plan.actions) == 3
        # discriminator 正确路由到 3 种变种
        assert isinstance(plan.actions[0], ReadAction)
        assert isinstance(plan.actions[1], PlaceOrCancelOrderAction)
        assert isinstance(plan.actions[2], NoOpAction)
        # 顶层字段如实保留
        assert plan.intent_summary == "check, place, then refuse"
        assert plan.assumptions == ["btcusdt is in market_snapshot"]
        assert plan.risk_notes == ["limit price within slippage"]

    @pytest.mark.parametrize(
        "missing_key",
        ["version", "intent_summary", "actions", "assumptions", "risk_notes"],
    )
    def test_missing_top_level_required_returns_none(self, missing_key: str) -> None:
        """缺任一顶层必填节 → 返 None（Req 6 AC5）。"""
        plan_dict = _wrap_plan([_make_read_action()])
        del plan_dict[missing_key]
        assert validate_action_plan_schema(plan_dict) is None

    def test_wrong_version_const_returns_none(self) -> None:
        """version != "0.1" → 返 None（Req 6 AC5 关联：顶层 const 不匹配视为缺失）。"""
        plan_dict = _wrap_plan([_make_read_action()], version="0.2")
        assert validate_action_plan_schema(plan_dict) is None

    def test_version_wrong_type_returns_none(self) -> None:
        """version 必须是字符串 "0.1"，数字 0.1 也应被拒。"""
        plan_dict = _wrap_plan([_make_read_action()], version=0.1)
        assert validate_action_plan_schema(plan_dict) is None

    def test_actions_too_few_returns_none(self) -> None:
        """空 actions list → 返 None（Req 6 AC4 衍生：minItems=1）。"""
        plan_dict = _wrap_plan([])
        assert validate_action_plan_schema(plan_dict) is None

    def test_actions_too_many_returns_none(self) -> None:
        """4 个 actions → 返 None（Req 6 AC4：maxItems=3）。"""
        plan_dict = _wrap_plan([_make_read_action() for _ in range(4)])
        assert validate_action_plan_schema(plan_dict) is None

    def test_actions_max_three_passes(self) -> None:
        """正好 3 个 actions → 通过（边界值）。"""
        plan = validate_action_plan_schema(
            _wrap_plan([_make_read_action() for _ in range(3)])
        )
        assert isinstance(plan, ActionPlanV0)
        assert len(plan.actions) == 3

    def test_intent_summary_too_long_returns_none(self) -> None:
        """intent_summary > 500 字符 → 返 None。

        PRD §10.1 明确 ``maxLength: 500``，防 planner 输出整段中文长文撑爆 token。
        """
        plan_dict = _wrap_plan([_make_read_action()], intent_summary="a" * 501)
        assert validate_action_plan_schema(plan_dict) is None

    def test_intent_summary_at_500_passes(self) -> None:
        """intent_summary 恰好 500 字符 → 通过（边界）。"""
        plan = validate_action_plan_schema(
            _wrap_plan([_make_read_action()], intent_summary="a" * 500)
        )
        assert isinstance(plan, ActionPlanV0)
        assert len(plan.intent_summary) == 500


# ---------------------------------------------------------------------------
# 2. type 条件必填（Req 6 AC1 / AC2 / AC3）
# ---------------------------------------------------------------------------
class TestTypeConditionalRequiredFields:
    """每种 action.type 的字段必填 / 可选规则。"""

    # ---- 2a. place_order / cancel_order（AC1）----
    def test_place_order_full_fields_pass(self) -> None:
        """place_order 7 个核心字段全填 → 通过。"""
        plan = validate_action_plan_schema(_wrap_plan([_make_place_order_action()]))
        assert isinstance(plan, ActionPlanV0)
        action = plan.actions[0]
        assert isinstance(action, PlaceOrCancelOrderAction)
        assert action.type == "place_order"
        assert action.symbol == "btcusdt"
        assert action.side == "buy"
        assert action.order_type == "limit"
        assert action.amount == 10.0
        assert action.amount_unit == "quote"
        assert action.max_notional_usdt == 10.0

    @pytest.mark.parametrize(
        "missing_field",
        [
            "symbol",
            "side",
            "order_type",
            "amount",
            "amount_unit",
            "max_notional_usdt",
        ],
    )
    def test_place_order_missing_required_field_returns_none(
        self, missing_field: str
    ) -> None:
        """place_order 任一核心字段缺失 → 返 None（Req 6 AC1）。"""
        action = _make_place_order_action()
        del action[missing_field]
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_place_order_missing_amount_returns_none(self) -> None:
        """显式断言：缺 amount → None（spec 测试要求项之一）。"""
        action = _make_place_order_action()
        del action["amount"]
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_place_order_missing_max_notional_returns_none(self) -> None:
        """显式断言：缺 max_notional_usdt → None（spec 测试要求项）。"""
        action = _make_place_order_action()
        del action["max_notional_usdt"]
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_cancel_order_full_fields_pass(self) -> None:
        """cancel_order 完整字段 → 通过。"""
        plan = validate_action_plan_schema(_wrap_plan([_make_cancel_order_action()]))
        assert isinstance(plan, ActionPlanV0)
        action = plan.actions[0]
        assert isinstance(action, PlaceOrCancelOrderAction)
        assert action.type == "cancel_order"

    def test_cancel_order_missing_amount_unit_returns_none(self) -> None:
        """cancel_order 缺 amount_unit → None（spec 测试要求项）。"""
        action = _make_cancel_order_action()
        del action["amount_unit"]
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_place_order_optional_limit_price_default_none(self) -> None:
        """place_order 不传 limit_price → 默认 None（market 单常见）。"""
        action = _make_place_order_action()
        del action["limit_price"]
        plan = validate_action_plan_schema(_wrap_plan([action]))
        assert isinstance(plan, ActionPlanV0)
        result_action = plan.actions[0]
        assert isinstance(result_action, PlaceOrCancelOrderAction)
        assert result_action.limit_price is None

    def test_place_order_optional_requires_user_approval_default_true(self) -> None:
        """place_order 不传 requires_user_approval → 默认 True（保守倾向）。"""
        action = _make_place_order_action()
        del action["requires_user_approval"]
        plan = validate_action_plan_schema(_wrap_plan([action]))
        assert isinstance(plan, ActionPlanV0)
        result_action = plan.actions[0]
        assert isinstance(result_action, PlaceOrCancelOrderAction)
        assert result_action.requires_user_approval is True

    # ---- 2b. read_market / read_account（AC2）----
    def test_read_market_with_only_symbol_pass(self) -> None:
        """read_market 仅给 type + symbol → 通过（其余字段用默认值，Req 6 AC2）。"""
        plan = validate_action_plan_schema(_wrap_plan([_make_read_action("btcusdt")]))
        assert isinstance(plan, ActionPlanV0)
        action = plan.actions[0]
        assert isinstance(action, ReadAction)
        # 默认值"中性"——none / 0 / False
        assert action.side == "none"
        assert action.order_type == "none"
        assert action.amount == 0
        assert action.amount_unit == "none"
        assert action.max_notional_usdt == 0
        assert action.requires_user_approval is False

    def test_read_market_missing_symbol_returns_none(self) -> None:
        """read_market 缺 symbol → 返 None（Req 6 AC2 中 symbol 必填）。"""
        action: dict[str, Any] = {"type": "read_market"}
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_read_account_with_only_symbol_pass(self) -> None:
        """read_account 仅给 type + symbol → 通过。"""
        plan = validate_action_plan_schema(
            _wrap_plan([{"type": "read_account", "symbol": "ethusdt"}])
        )
        assert isinstance(plan, ActionPlanV0)
        action = plan.actions[0]
        assert isinstance(action, ReadAction)
        assert action.type == "read_account"
        assert action.symbol == "ethusdt"

    def test_read_account_missing_symbol_returns_none(self) -> None:
        action: dict[str, Any] = {"type": "read_account"}
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_read_action_with_full_optional_fields_pass(self) -> None:
        """read_market 给齐"none/0"语义可选字段 → 通过（兼容 PRD §10.1 flat 写法）。"""
        action: dict[str, Any] = {
            "type": "read_market",
            "symbol": "btcusdt",
            "side": "none",
            "order_type": "none",
            "amount": 0,
            "amount_unit": "none",
            "max_notional_usdt": 0,
            "requires_user_approval": False,
            "rationale": "snapshot before placing order",
        }
        plan = validate_action_plan_schema(_wrap_plan([action]))
        assert isinstance(plan, ActionPlanV0)

    # ---- 2c. no_op（AC3）----
    def test_no_op_with_only_rationale_pass(self) -> None:
        """no_op 仅给 type + rationale → 通过（Req 6 AC3）。"""
        plan = validate_action_plan_schema(
            _wrap_plan([{"type": "no_op", "rationale": "user requested withdrawal"}])
        )
        assert isinstance(plan, ActionPlanV0)
        action = plan.actions[0]
        assert isinstance(action, NoOpAction)
        assert action.type == "no_op"
        assert action.rationale == "user requested withdrawal"
        assert action.symbol is None  # 可选字段未给

    def test_no_op_missing_rationale_returns_none(self) -> None:
        """no_op 缺 rationale → 返 None（Req 6 AC3 唯一必填字段）。"""
        action: dict[str, Any] = {"type": "no_op"}
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_no_op_with_optional_symbol_pass(self) -> None:
        """no_op 可携带 symbol（标注"对哪个 symbol 拒绝"）→ 通过。"""
        plan = validate_action_plan_schema(
            _wrap_plan(
                [
                    {
                        "type": "no_op",
                        "rationale": "withdrawal blocked",
                        "symbol": "btcusdt",
                    }
                ]
            )
        )
        assert isinstance(plan, ActionPlanV0)
        action = plan.actions[0]
        assert isinstance(action, NoOpAction)
        assert action.symbol == "btcusdt"

    def test_no_op_rationale_too_long_returns_none(self) -> None:
        """no_op rationale > 800 字符 → 返 None（基类 maxLength=800）。"""
        action: dict[str, Any] = {"type": "no_op", "rationale": "x" * 801}
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    # ---- 2d. 未知 type ----
    def test_unknown_action_type_returns_none(self) -> None:
        """type="exotic_action" 不在 5 枚举中 → 返 None（discriminator 不匹配）。

        与 Req 15 AC7「未知 action type SHALL 被 REJECT」对齐——在 schema 阶段
        就拦下，不让脏数据进入 Policy Engine。
        """
        action: dict[str, Any] = {
            "type": "exotic_action",
            "symbol": "btcusdt",
            "side": "buy",
            "order_type": "limit",
            "amount": 1,
            "amount_unit": "quote",
            "max_notional_usdt": 1,
        }
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_missing_action_type_returns_none(self) -> None:
        """action 缺 type 字段 → 返 None（discriminator 缺失）。"""
        action: dict[str, Any] = {"symbol": "btcusdt"}
        assert validate_action_plan_schema(_wrap_plan([action])) is None


# ---------------------------------------------------------------------------
# 3. 数据规范化（Req 6 AC6 + 数值约束）
# ---------------------------------------------------------------------------
class TestDataNormalization:
    """symbol 小写归一化；负数字段拒绝。"""

    def test_symbol_lowercased_for_read_action(self) -> None:
        """read_market 传入 BTCUSDT → action.symbol == "btcusdt"（Req 6 AC6）。"""
        plan = validate_action_plan_schema(
            _wrap_plan([{"type": "read_market", "symbol": "BTCUSDT"}])
        )
        assert isinstance(plan, ActionPlanV0)
        assert plan.actions[0].symbol == "btcusdt"

    def test_symbol_lowercased_for_place_order_action(self) -> None:
        """place_order 传入 ETHUSDT → action.symbol == "ethusdt"。"""
        plan = validate_action_plan_schema(
            _wrap_plan([_make_place_order_action(symbol="ETHUSDT")])
        )
        assert isinstance(plan, ActionPlanV0)
        assert plan.actions[0].symbol == "ethusdt"

    def test_symbol_lowercased_for_no_op_action(self) -> None:
        """no_op 携带 symbol="BTCUSDT" → 同样小写化。"""
        plan = validate_action_plan_schema(
            _wrap_plan(
                [{"type": "no_op", "rationale": "x", "symbol": "BTCUSDT"}]
            )
        )
        assert isinstance(plan, ActionPlanV0)
        assert plan.actions[0].symbol == "btcusdt"

    def test_symbol_mixed_case_lowercased(self) -> None:
        """混合大小写 BtcUsdt → btcusdt。"""
        plan = validate_action_plan_schema(
            _wrap_plan([{"type": "read_market", "symbol": "BtcUsdt"}])
        )
        assert isinstance(plan, ActionPlanV0)
        assert plan.actions[0].symbol == "btcusdt"

    def test_negative_amount_returns_none(self) -> None:
        """place_order amount=-1 → 返 None（Field(ge=0)）。"""
        action = _make_place_order_action(amount=-1)
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_negative_max_notional_returns_none(self) -> None:
        """place_order max_notional_usdt=-1 → 返 None。"""
        action = _make_place_order_action(max_notional_usdt=-1)
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_negative_limit_price_returns_none(self) -> None:
        """place_order limit_price=-1 → 返 None（Field(ge=0)）。"""
        action = _make_place_order_action(limit_price=-1)
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_zero_amount_passes(self) -> None:
        """amount=0 合法（边界值，cancel_order 场景常见）。"""
        action = _make_cancel_order_action(amount=0)
        plan = validate_action_plan_schema(_wrap_plan([action]))
        assert isinstance(plan, ActionPlanV0)


# ---------------------------------------------------------------------------
# 4. 输入鲁棒性
# ---------------------------------------------------------------------------
class TestInputRobustness:
    """各种"脏"输入统一返 None；不抛异常。"""

    def test_non_json_string_returns_none(self) -> None:
        """纯文本（非 JSON）→ 返 None。"""
        assert validate_action_plan_schema("not a json") is None

    def test_markdown_fenced_json_returns_none(self) -> None:
        """B.AI 偶尔吐回 ```json {...} ``` 包裹的 markdown → 返 None。

        这是 planner adapter 的常见污染源；validator 不做"剥 markdown fence"
        的容错——保持纯 JSON 契约，让上游适配器（任务 10）负责清洗。
        """
        markdown = '```json\n{"version":"0.1","intent_summary":"x","actions":[],"assumptions":[],"risk_notes":[]}\n```'
        assert validate_action_plan_schema(markdown) is None

    def test_dict_input_pass(self) -> None:
        """直接传 dict（已 parsed） → 通过校验。"""
        plan_dict = _wrap_plan([_make_read_action()])
        plan = validate_action_plan_schema(plan_dict)
        assert isinstance(plan, ActionPlanV0)

    def test_json_string_input_pass(self) -> None:
        """传 JSON 字符串 → 通过校验（最常见路径，对应 planner 直出文本）。"""
        plan_dict = _wrap_plan([_make_read_action()])
        plan = validate_action_plan_schema(json.dumps(plan_dict))
        assert isinstance(plan, ActionPlanV0)

    def test_empty_string_returns_none(self) -> None:
        """空字符串 → JSON 解析失败 → 返 None。"""
        assert validate_action_plan_schema("") is None

    def test_whitespace_only_string_returns_none(self) -> None:
        """纯空白 → JSON 解析失败 → 返 None。"""
        assert validate_action_plan_schema("   \n\t  ") is None

    def test_none_input_returns_none(self) -> None:
        """None 输入 → 直接拒绝。"""
        assert validate_action_plan_schema(None) is None

    def test_list_input_returns_none(self) -> None:
        """list 输入（非 dict 顶层） → 拒绝。"""
        assert validate_action_plan_schema([{"type": "no_op", "rationale": "x"}]) is None  # type: ignore[arg-type]

    def test_integer_input_returns_none(self) -> None:
        """int 输入 → 拒绝。"""
        assert validate_action_plan_schema(123) is None  # type: ignore[arg-type]

    def test_json_array_string_returns_none(self) -> None:
        """``"[]"`` 解析后是 list 不是 dict → 返 None。"""
        assert validate_action_plan_schema("[]") is None

    def test_json_number_string_returns_none(self) -> None:
        """``"123"`` 解析后是 int → 返 None。"""
        assert validate_action_plan_schema("123") is None

    def test_extra_field_at_top_level_returns_none(self) -> None:
        """顶层未知字段 → 返 None（extra='forbid'，对应 Req 7 AC11 第一道闸）。"""
        plan_dict = _wrap_plan([_make_read_action()], extra_field="surprise")
        assert validate_action_plan_schema(plan_dict) is None

    def test_extra_field_in_action_returns_none(self) -> None:
        """单个 action 内未知字段 → 返 None。"""
        action = _make_read_action()
        action["mystery_param"] = 42  # type: ignore[assignment]
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_extra_field_in_no_op_action_returns_none(self) -> None:
        """no_op 含未知字段（如 amount） → 返 None。

        no_op 不应该有 amount/side 等交易字段；planner 误填会被 schema 拦下。
        """
        action: dict[str, Any] = {
            "type": "no_op",
            "rationale": "x",
            "amount": 10,  # 这是 no_op 不应有的字段
        }
        assert validate_action_plan_schema(_wrap_plan([action])) is None

    def test_malformed_json_returns_none(self) -> None:
        """不完整 JSON（如缺尾 brace） → 返 None。"""
        assert validate_action_plan_schema('{"version":"0.1"') is None

    def test_json_with_extra_trailing_data_returns_none(self) -> None:
        """JSON 后面跟随脏数据 → 解析失败 → 返 None。"""
        valid = json.dumps(_wrap_plan([_make_read_action()]))
        assert validate_action_plan_schema(valid + " extra junk") is None


# ---------------------------------------------------------------------------
# 5. round-trip 行为
# ---------------------------------------------------------------------------
class TestRoundTrip:
    """validate → model_dump → validate 的 idempotency。"""

    def test_roundtrip_idempotent_after_normalization(self) -> None:
        """validate 一次后 model_dump 再 validate → 同样合法且语义不变。

        归一化（symbol 小写）后再次 validate 应产生相同结构——保证审计链路
        中"plan_dump → re-validate"不会因为大小写差异引发 PLAN_INVALID。
        """
        plan = validate_action_plan_schema(
            _wrap_plan([_make_place_order_action(symbol="BTCUSDT")])
        )
        assert isinstance(plan, ActionPlanV0)
        dumped = plan.model_dump()
        re_validated = validate_action_plan_schema(dumped)
        assert isinstance(re_validated, ActionPlanV0)
        # symbol 已被首次 validate 小写化，二次 validate 后仍是 btcusdt
        assert re_validated.actions[0].symbol == "btcusdt"
        # 全字段 dump 等价
        assert re_validated.model_dump() == dumped
