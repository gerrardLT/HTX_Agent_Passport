"""任务 2.3 状态机转换校验工具的单元测试 + PBT。

覆盖维度
--------
1. **合法边 parametrize**：枚举三张表的全部出边，逐条断言 ``validate_transition=True``。
2. **终态封闭**：所有终态的转换集合为空 ``frozenset()``。
3. **非法转换拒绝**：精选典型非法边（如 DRAFT→PAUSED、EXECUTED→PLANNING）返回 ``False``。
4. **未知状态容错**：未知 ``current`` 返回 ``False`` 不抛异常。
5. **assert_transition 异常**：非法时抛 ``IllegalStateTransition`` 携带完整上下文。
6. **PBT 等价性（Property 3）**：随机抽样 (current, target) 验证函数实现与
   ``target ∈ transitions.get(current, frozenset())`` 完全等价。
7. **PBT 跨域**：用 PassportState 域名喂 ACTION_TRANSITIONS（取无重叠子集）必返回 False。
8. **不可变保护**：转换表不可被赋值/popitem 篡改。

所有用例**不依赖数据库 / 网络 / 文件系统**，纯函数测试。
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.state_machine import (
    ACTION_TRANSITIONS,
    CREDENTIAL_TRANSITIONS,
    PASSPORT_TRANSITIONS,
    IllegalStateTransition,
    assert_transition,
    validate_transition,
)
from app.models.enums import ActionState, CredentialState, PassportState


# ---------------------------------------------------------------------------
# Helper: 把转换表展开为 (current, target) 边列表，给 parametrize 用
# ---------------------------------------------------------------------------
def _enumerate_edges(transitions) -> list[tuple[str, str]]:
    return [(c, t) for c, targets in transitions.items() for t in sorted(targets)]


PASSPORT_EDGES = _enumerate_edges(PASSPORT_TRANSITIONS)
ACTION_EDGES = _enumerate_edges(ACTION_TRANSITIONS)
CREDENTIAL_EDGES = _enumerate_edges(CREDENTIAL_TRANSITIONS)


# ---------------------------------------------------------------------------
# 1. 合法边逐条覆盖
# ---------------------------------------------------------------------------
class TestLegalEdges:
    """parametrize 出每张表里的每条声明边。"""

    @pytest.mark.parametrize(("current", "target"), PASSPORT_EDGES)
    def test_passport_legal_edges(self, current: str, target: str) -> None:
        """PASSPORT_TRANSITIONS 中所有声明边必须 validate_transition=True。"""
        assert validate_transition(current, target, PASSPORT_TRANSITIONS) is True

    @pytest.mark.parametrize(("current", "target"), ACTION_EDGES)
    def test_action_legal_edges(self, current: str, target: str) -> None:
        """ACTION_TRANSITIONS 中所有声明边必须 validate_transition=True。"""
        assert validate_transition(current, target, ACTION_TRANSITIONS) is True

    @pytest.mark.parametrize(("current", "target"), CREDENTIAL_EDGES)
    def test_credential_legal_edges(self, current: str, target: str) -> None:
        """CREDENTIAL_TRANSITIONS 中所有声明边必须 validate_transition=True。"""
        assert validate_transition(current, target, CREDENTIAL_TRANSITIONS) is True

    def test_edges_count_matches_design(self) -> None:
        """边总数与 design.md 声明数量一致，作为冗余护栏防止漏配/多配。"""
        assert len(PASSPORT_EDGES) == 7  # DRAFT(2) + ACTIVE(3) + PAUSED(2)
        # ACTION 边 18 = 任务 2.3 原始 17 条 + 任务 5.3 新增「APPROVAL_REQUIRED → CANCELLED」
        # （passport 撤销级联取消下属待审批 action，Req 3 AC5）
        assert len(ACTION_EDGES) == 18
        assert len(CREDENTIAL_EDGES) == 13


# ---------------------------------------------------------------------------
# 2. 终态封闭性
# ---------------------------------------------------------------------------
class TestTerminalStates:
    """终态的转换集合必须为空 ``frozenset()``，且任何 target 都返回 False。"""

    PASSPORT_TERMINALS = (
        PassportState.REVOKED,
        PassportState.EXPIRED,
        PassportState.DELETED,
    )
    ACTION_TERMINALS = (
        ActionState.EXECUTED,
        ActionState.AUTO_REJECTED,
        ActionState.REJECTED_BY_USER,
        ActionState.EXECUTION_FAILED,
        ActionState.FAILED,
        ActionState.CANCELLED,
        ActionState.PLAN_INVALID,
        ActionState.EXPIRED,
    )
    CREDENTIAL_TERMINALS = (CredentialState.REVOKED, CredentialState.DELETED)

    @pytest.mark.parametrize("terminal", PASSPORT_TERMINALS)
    def test_passport_terminal_has_no_outgoing(self, terminal: str) -> None:
        assert PASSPORT_TRANSITIONS[terminal] == frozenset()

    @pytest.mark.parametrize("terminal", ACTION_TERMINALS)
    def test_action_terminal_has_no_outgoing(self, terminal: str) -> None:
        assert ACTION_TRANSITIONS[terminal] == frozenset()

    @pytest.mark.parametrize("terminal", CREDENTIAL_TERMINALS)
    def test_credential_terminal_has_no_outgoing(self, terminal: str) -> None:
        assert CREDENTIAL_TRANSITIONS[terminal] == frozenset()

    @pytest.mark.parametrize("terminal", PASSPORT_TERMINALS)
    def test_passport_terminal_rejects_any_target(self, terminal: str) -> None:
        """终态 + 任何域内目标 → False（含尝试自循环）。"""
        for target in PassportState.ALL:
            assert validate_transition(terminal, target, PASSPORT_TRANSITIONS) is False

    @pytest.mark.parametrize("terminal", ACTION_TERMINALS)
    def test_action_terminal_rejects_any_target(self, terminal: str) -> None:
        for target in ActionState.ALL:
            assert validate_transition(terminal, target, ACTION_TRANSITIONS) is False

    @pytest.mark.parametrize("terminal", CREDENTIAL_TERMINALS)
    def test_credential_terminal_rejects_any_target(self, terminal: str) -> None:
        for target in CredentialState.ALL:
            assert (
                validate_transition(terminal, target, CREDENTIAL_TRANSITIONS) is False
            )


# ---------------------------------------------------------------------------
# 3. 典型非法转换
# ---------------------------------------------------------------------------
class TestIllegalEdges:
    """选取设计意图明确的非法边作为回归护栏。"""

    PASSPORT_ILLEGAL = [
        # DRAFT 不能直接 → PAUSED（必须先 ACTIVE）
        (PassportState.DRAFT, PassportState.PAUSED),
        # DRAFT 不能直接 → REVOKED / EXPIRED
        (PassportState.DRAFT, PassportState.REVOKED),
        (PassportState.DRAFT, PassportState.EXPIRED),
        # ACTIVE 不能回退 DRAFT
        (PassportState.ACTIVE, PassportState.DRAFT),
        # PAUSED 不能直接 EXPIRED（设计上 EXPIRED 仅来自 ACTIVE）
        (PassportState.PAUSED, PassportState.EXPIRED),
        # PAUSED 不能 DELETED
        (PassportState.PAUSED, PassportState.DELETED),
        # 同状态自循环不允许
        (PassportState.ACTIVE, PassportState.ACTIVE),
    ]

    ACTION_ILLEGAL = [
        # 终态再激活：EXECUTED → PLANNING
        (ActionState.EXECUTED, ActionState.PLANNING),
        # 跳步：REQUESTED 不能直达 EXECUTING
        (ActionState.REQUESTED, ActionState.EXECUTING),
        # 跳步：PLAN_VALIDATED 不能直达 APPROVED（必须经 RISK_CHECKING）
        (ActionState.PLAN_VALIDATED, ActionState.APPROVED),
        # 不能从 APPROVED 回到 APPROVAL_REQUIRED
        (ActionState.APPROVED, ActionState.APPROVAL_REQUIRED),
        # AUTO_REJECTED 不能转为 EXECUTING
        (ActionState.AUTO_REJECTED, ActionState.EXECUTING),
    ]

    CREDENTIAL_ILLEGAL = [
        # CREATED 不能直接 → READ_ONLY（必须经 VALIDATING）
        (CredentialState.CREATED, CredentialState.READ_ONLY),
        (CredentialState.CREATED, CredentialState.TRADE_ENABLED),
        # 终态不能复活
        (CredentialState.REVOKED, CredentialState.READ_ONLY),
        (CredentialState.DELETED, CredentialState.VALIDATING),
        # READ_ONLY 不能直接 INVALID（INVALID 由 VALIDATING 产生）
        (CredentialState.READ_ONLY, CredentialState.INVALID),
    ]

    @pytest.mark.parametrize(("current", "target"), PASSPORT_ILLEGAL)
    def test_passport_illegal(self, current: str, target: str) -> None:
        assert validate_transition(current, target, PASSPORT_TRANSITIONS) is False

    @pytest.mark.parametrize(("current", "target"), ACTION_ILLEGAL)
    def test_action_illegal(self, current: str, target: str) -> None:
        assert validate_transition(current, target, ACTION_TRANSITIONS) is False

    @pytest.mark.parametrize(("current", "target"), CREDENTIAL_ILLEGAL)
    def test_credential_illegal(self, current: str, target: str) -> None:
        assert validate_transition(current, target, CREDENTIAL_TRANSITIONS) is False


# ---------------------------------------------------------------------------
# 4. 未知状态容错
# ---------------------------------------------------------------------------
class TestUnknownStates:
    """未知 current 或 target 应返回 False，绝不抛异常（deny→ask→allow 友好）。"""

    UNKNOWN_INPUTS = [
        ("BOGUS_STATE", PassportState.ACTIVE),
        ("", PassportState.ACTIVE),
        (PassportState.DRAFT, "BOGUS_STATE"),
        ("UNKNOWN_X", "UNKNOWN_Y"),
        # 大小写敏感：``draft`` 不等于 ``DRAFT``
        ("draft", PassportState.ACTIVE),
        # 含空白
        (" DRAFT", "ACTIVE"),
    ]

    @pytest.mark.parametrize(("current", "target"), UNKNOWN_INPUTS)
    def test_unknown_state_returns_false_no_exception(
        self, current: str, target: str
    ) -> None:
        # 不抛异常 + 返回 False
        assert validate_transition(current, target, PASSPORT_TRANSITIONS) is False
        assert validate_transition(current, target, ACTION_TRANSITIONS) is False
        assert validate_transition(current, target, CREDENTIAL_TRANSITIONS) is False


# ---------------------------------------------------------------------------
# 5. assert_transition 异常路径
# ---------------------------------------------------------------------------
class TestAssertTransition:
    """便捷断言版本：合法静默通过，非法抛 IllegalStateTransition。"""

    def test_legal_transition_returns_none(self) -> None:
        # 合法转换：无返回值（None），不抛异常
        result = assert_transition(
            PassportState.DRAFT, PassportState.ACTIVE, PASSPORT_TRANSITIONS, "passport"
        )
        assert result is None

    def test_illegal_transition_raises(self) -> None:
        with pytest.raises(IllegalStateTransition) as exc_info:
            assert_transition(
                PassportState.DRAFT,
                PassportState.PAUSED,
                PASSPORT_TRANSITIONS,
                "passport",
            )
        exc = exc_info.value
        assert exc.current == PassportState.DRAFT
        assert exc.target == PassportState.PAUSED
        assert exc.machine_name == "passport"
        # 默认 str(exc) 必须包含状态名，便于日志排查
        msg = str(exc)
        assert "DRAFT" in msg
        assert "PAUSED" in msg
        assert "passport" in msg

    def test_exception_inherits_value_error(self) -> None:
        """``except ValueError`` 应能捕获，方便上层统一错误处理。"""
        with pytest.raises(ValueError):  # noqa: PT011
            assert_transition(
                ActionState.EXECUTED,
                ActionState.PLANNING,
                ACTION_TRANSITIONS,
                "action",
            )

    def test_machine_name_optional(self) -> None:
        """machine_name 为空字符串时也能正常工作；消息不带前缀。"""
        with pytest.raises(IllegalStateTransition) as exc_info:
            assert_transition(
                CredentialState.REVOKED,
                CredentialState.READ_ONLY,
                CREDENTIAL_TRANSITIONS,
            )
        assert exc_info.value.machine_name == ""

    def test_unknown_current_raises_too(self) -> None:
        """未知状态在 assert 版本下也应抛异常（区别于 validate_transition 静默 False）。"""
        with pytest.raises(IllegalStateTransition):
            assert_transition(
                "BOGUS",
                PassportState.ACTIVE,
                PASSPORT_TRANSITIONS,
                "passport",
            )


# ---------------------------------------------------------------------------
# 6. Property-Based Tests（Property 3：状态机合法性）
#
# **Validates: Requirements 3**
#
# 核心思路：用独立的「target ∈ transitions.get(current, frozenset())」表达式
# 重新计算预期值，再与 ``validate_transition`` 返回值对比。
# 任何实现回归（误把空集合换成 set / 改变查找语义 / 把 current 类型变换等）
# 都会被 Hypothesis 的随机抽样捕获。
# ---------------------------------------------------------------------------
PASSPORT_STATES = sorted(PassportState.ALL)
ACTION_STATES = sorted(ActionState.ALL)
CREDENTIAL_STATES = sorted(CredentialState.ALL)


@pytest.mark.pbt
class TestPropertyStateMachineValidity:
    """**Validates: Requirements 3**（Property 3：状态机合法性）。

    对三张表分别抽样 (current, target)，验证 ``validate_transition`` 与
    ``target ∈ transitions.get(current, frozenset())`` 在所有输入上等价。
    """

    @given(
        current=st.sampled_from(PASSPORT_STATES),
        target=st.sampled_from(PASSPORT_STATES),
    )
    @settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_passport_validate_transition_matches_predicate(
        self, current: str, target: str
    ) -> None:
        expected = target in PASSPORT_TRANSITIONS.get(current, frozenset())
        assert validate_transition(current, target, PASSPORT_TRANSITIONS) is expected

    @given(
        current=st.sampled_from(ACTION_STATES),
        target=st.sampled_from(ACTION_STATES),
    )
    @settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_action_validate_transition_matches_predicate(
        self, current: str, target: str
    ) -> None:
        expected = target in ACTION_TRANSITIONS.get(current, frozenset())
        assert validate_transition(current, target, ACTION_TRANSITIONS) is expected

    @given(
        current=st.sampled_from(CREDENTIAL_STATES),
        target=st.sampled_from(CREDENTIAL_STATES),
    )
    @settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_credential_validate_transition_matches_predicate(
        self, current: str, target: str
    ) -> None:
        expected = target in CREDENTIAL_TRANSITIONS.get(current, frozenset())
        assert (
            validate_transition(current, target, CREDENTIAL_TRANSITIONS) is expected
        )


# ---------------------------------------------------------------------------
# 7. PBT 跨 enum 域
# ---------------------------------------------------------------------------
# 选出每个 enum 域中**不与其他域重叠**的状态名，确保跨域注入时
# current 一定不在目标转换表中，结果必为 False。
_PASSPORT_EXCLUSIVE = sorted(
    PassportState.ALL - ActionState.ALL - CredentialState.ALL
)  # {DRAFT, ACTIVE, PAUSED}
_ACTION_EXCLUSIVE = sorted(
    ActionState.ALL - PassportState.ALL - CredentialState.ALL
)  # {REQUESTED, PLANNING, ...}
_CREDENTIAL_EXCLUSIVE = sorted(
    CredentialState.ALL - PassportState.ALL - ActionState.ALL
)  # {CREATED, VALIDATING, READ_ONLY, TRADE_ENABLED, INVALID}


@pytest.mark.pbt
class TestPropertyCrossDomainRejection:
    """**Validates: Requirements 3**。

    Property: 把 A 域的私有状态当作 current 喂给 B 域的转换表，
    必须返回 False（不能误命中）。
    """

    @given(
        current=st.sampled_from(_PASSPORT_EXCLUSIVE),
        target=st.sampled_from(ACTION_STATES),
    )
    @settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_passport_state_in_action_table_returns_false(
        self, current: str, target: str
    ) -> None:
        assert validate_transition(current, target, ACTION_TRANSITIONS) is False

    @given(
        current=st.sampled_from(_PASSPORT_EXCLUSIVE),
        target=st.sampled_from(CREDENTIAL_STATES),
    )
    @settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_passport_state_in_credential_table_returns_false(
        self, current: str, target: str
    ) -> None:
        assert validate_transition(current, target, CREDENTIAL_TRANSITIONS) is False

    @given(
        current=st.sampled_from(_ACTION_EXCLUSIVE),
        target=st.sampled_from(PASSPORT_STATES),
    )
    @settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_action_state_in_passport_table_returns_false(
        self, current: str, target: str
    ) -> None:
        assert validate_transition(current, target, PASSPORT_TRANSITIONS) is False

    @given(
        current=st.sampled_from(_CREDENTIAL_EXCLUSIVE),
        target=st.sampled_from(PASSPORT_STATES),
    )
    @settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_credential_state_in_passport_table_returns_false(
        self, current: str, target: str
    ) -> None:
        assert validate_transition(current, target, PASSPORT_TRANSITIONS) is False


_ALL_KNOWN_STATES: frozenset[str] = (
    PassportState.ALL | ActionState.ALL | CredentialState.ALL
)


@pytest.mark.pbt
class TestPropertyArbitraryStringRejection:
    """**Validates: Requirements 3**。

    Property: 任意非转换表 key 的字符串（含纯随机 ASCII / Unicode）
    作为 current 喂入，validate_transition 永远返回 False，永不抛异常。
    """

    @given(
        current=st.text(min_size=0, max_size=64).filter(
            lambda s: s not in _ALL_KNOWN_STATES
        ),
        target=st.sampled_from(PASSPORT_STATES),
    )
    @settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_arbitrary_unknown_string_in_passport(
        self, current: str, target: str
    ) -> None:
        assert validate_transition(current, target, PASSPORT_TRANSITIONS) is False

    @given(
        current=st.text(min_size=0, max_size=64).filter(
            lambda s: s not in _ALL_KNOWN_STATES
        ),
        target=st.sampled_from(ACTION_STATES),
    )
    @settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_arbitrary_unknown_string_in_action(
        self, current: str, target: str
    ) -> None:
        assert validate_transition(current, target, ACTION_TRANSITIONS) is False


# ---------------------------------------------------------------------------
# 8. 转换表不可变保护
# ---------------------------------------------------------------------------
class TestImmutability:
    """``MappingProxyType`` 包裹 + ``frozenset`` 值 => 整张表运行期不可变。"""

    def test_cannot_assign_to_passport_table(self) -> None:
        with pytest.raises(TypeError):
            PASSPORT_TRANSITIONS["DRAFT"] = frozenset({"NEW_STATE"})  # type: ignore[index]

    def test_cannot_pop_from_action_table(self) -> None:
        with pytest.raises((TypeError, AttributeError)):
            PASSPORT_TRANSITIONS.popitem()  # type: ignore[attr-defined]

    def test_cannot_mutate_target_set(self) -> None:
        """每个目标集合本身是 frozenset，调用 add 抛 AttributeError。"""
        targets = PASSPORT_TRANSITIONS[PassportState.DRAFT]
        with pytest.raises(AttributeError):
            targets.add("NEW_TARGET")  # type: ignore[attr-defined]
