"""状态机转换校验工具（任务 2.3）。

实现 design.md 「状态机转换表」 + Property 3「状态机合法性」：
Passport / Action / Credential 的状态转换只能沿 TRANSITIONS 表中定义的边进行；
任何非法转换必须被拒绝。

设计要点
--------
1. **转换表是 module 级 ``Final`` 常量**，使用 ``MappingProxyType`` 包装防止意外修改。
   表的 key 与 ``app.models.enums`` 中的字符串常量保持一致——
   通过直接引用 ``PassportState.DRAFT`` 等常量构造，避免硬编码字符串散落各处。

2. **终态用空 ``frozenset()``**，``validate_transition`` 对终态的查询返回 False，
   语义上等价于「终态没有出边」。

3. ``validate_transition`` 在 ``current`` 未知时返回 ``False`` 而非抛异常——
   便于调用方做 deny→ask→allow 风格的策略控制（方法论 §13）。

4. ``assert_transition`` 是便捷断言版本，非法时抛 ``IllegalStateTransition``，
   该异常继承 ``ValueError`` 让上层用 ``except ValueError`` 即可捕获。

对应 Requirements: Req 3 AC4-7（Passport 状态机）、Req 8 AC4-7（Action 状态机）、
Req 2 AC3-6（Credential 状态机）。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from app.models.enums import ActionState, CredentialState, PassportState


# ---------------------------------------------------------------------------
# IllegalStateTransition
# ---------------------------------------------------------------------------
class IllegalStateTransition(ValueError):
    """非法状态转换异常。

    继承 ``ValueError`` 使上层既可以用 ``except IllegalStateTransition`` 精确捕获，
    也可以用 ``except ValueError`` 在不关心具体子类时统一处理。

    Attributes
    ----------
    current : str
        当前状态（可能是未知字符串，例如未受控输入）。
    target : str
        尝试转换到的目标状态。
    machine_name : str
        状态机名（``"passport"`` / ``"action"`` / ``"credential"`` 等），便于审计与日志。
    """

    def __init__(self, current: str, target: str, machine_name: str = "") -> None:
        self.current = current
        self.target = target
        self.machine_name = machine_name
        prefix = f"[{machine_name}] " if machine_name else ""
        super().__init__(
            f"{prefix}illegal state transition: {current!r} -> {target!r}"
        )


# ---------------------------------------------------------------------------
# 转换表常量
# ---------------------------------------------------------------------------
def _freeze(table: Mapping[str, frozenset[str]]) -> Mapping[str, frozenset[str]]:
    """用 MappingProxyType 包裹底层 dict，使其在运行期不可写。

    底层值已是 ``frozenset``，再加一层 ``MappingProxyType`` 后整个映射就达到了
    「key/value 都不可变」的效果，避免被业务代码意外篡改。
    """
    return MappingProxyType(dict(table))


#: Passport 状态机：DRAFT/ACTIVE/PAUSED 可流转，REVOKED/EXPIRED/DELETED 为终态。
#: 对应 design.md PASSPORT_TRANSITIONS / Req 3 AC4-7。
PASSPORT_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = _freeze(
    {
        PassportState.DRAFT: frozenset({PassportState.ACTIVE, PassportState.DELETED}),
        PassportState.ACTIVE: frozenset(
            {PassportState.PAUSED, PassportState.REVOKED, PassportState.EXPIRED}
        ),
        PassportState.PAUSED: frozenset({PassportState.ACTIVE, PassportState.REVOKED}),
        PassportState.REVOKED: frozenset(),
        PassportState.EXPIRED: frozenset(),
        PassportState.DELETED: frozenset(),
    }
)

#: Action 状态机：完整微回合循环（REQUESTED → PLANNING → ... → EXECUTED 等）。
#: 对应 design.md ACTION_TRANSITIONS / Req 8 / Req 14。
ACTION_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = _freeze(
    {
        ActionState.REQUESTED: frozenset(
            {ActionState.PLANNING, ActionState.CANCELLED}
        ),
        ActionState.PLANNING: frozenset(
            {
                ActionState.PLAN_VALIDATED,
                ActionState.PLAN_INVALID,
                ActionState.FAILED,
            }
        ),
        ActionState.PLAN_VALIDATED: frozenset({ActionState.RISK_CHECKING}),
        ActionState.PLAN_INVALID: frozenset(),
        ActionState.RISK_CHECKING: frozenset(
            {
                ActionState.APPROVAL_REQUIRED,
                ActionState.AUTO_REJECTED,
                ActionState.AUTO_APPROVED,
            }
        ),
        ActionState.APPROVAL_REQUIRED: frozenset(
            {
                ActionState.APPROVED,
                ActionState.REJECTED_BY_USER,
                ActionState.EXPIRED,
                # passport 撤销时级联取消下属待审批 action（Req 3 AC5）
                ActionState.CANCELLED,
            }
        ),
        ActionState.AUTO_APPROVED: frozenset({ActionState.EXECUTING}),
        ActionState.APPROVED: frozenset({ActionState.EXECUTING}),
        ActionState.EXECUTING: frozenset(
            {
                ActionState.EXECUTED,
                ActionState.EXECUTION_FAILED,
                ActionState.CANCELLED,
            }
        ),
        # 终态
        ActionState.EXECUTED: frozenset(),
        ActionState.AUTO_REJECTED: frozenset(),
        ActionState.REJECTED_BY_USER: frozenset(),
        ActionState.EXECUTION_FAILED: frozenset(),
        ActionState.FAILED: frozenset(),
        ActionState.CANCELLED: frozenset(),
        ActionState.EXPIRED: frozenset(),
    }
)

#: Credential 状态机：CREATED → VALIDATING → READ_ONLY/TRADE_ENABLED/INVALID。
#: 对应 design.md CREDENTIAL_TRANSITIONS / Req 2 AC3-6。
CREDENTIAL_TRANSITIONS: Final[Mapping[str, frozenset[str]]] = _freeze(
    {
        CredentialState.CREATED: frozenset(
            {CredentialState.VALIDATING, CredentialState.DELETED}
        ),
        CredentialState.VALIDATING: frozenset(
            {
                CredentialState.READ_ONLY,
                CredentialState.TRADE_ENABLED,
                CredentialState.INVALID,
            }
        ),
        CredentialState.READ_ONLY: frozenset(
            {
                CredentialState.VALIDATING,
                CredentialState.REVOKED,
                CredentialState.DELETED,
            }
        ),
        CredentialState.TRADE_ENABLED: frozenset(
            {
                CredentialState.VALIDATING,
                CredentialState.REVOKED,
                CredentialState.DELETED,
            }
        ),
        CredentialState.INVALID: frozenset(
            {CredentialState.VALIDATING, CredentialState.DELETED}
        ),
        CredentialState.REVOKED: frozenset(),
        CredentialState.DELETED: frozenset(),
    }
)


# ---------------------------------------------------------------------------
# 校验函数
# ---------------------------------------------------------------------------
def validate_transition(
    current: str,
    target: str,
    transitions: Mapping[str, frozenset[str]],
) -> bool:
    """校验状态转换是否合法（非抛异常版本）。

    Parameters
    ----------
    current : str
        当前状态字符串。
    target : str
        目标状态字符串。
    transitions : Mapping[str, frozenset[str]]
        转换表（PASSPORT/ACTION/CREDENTIAL 之一）。

    Returns
    -------
    bool
        ``target ∈ transitions.get(current, frozenset())``。

    Notes
    -----
    - 当 ``current`` 不在表中时返回 ``False``，**不抛异常**——
      便于调用方在策略链路中做 deny→ask→allow 风格的判断。
    - 当 ``current`` 是终态（其转换集合为空）时，任何 ``target`` 都返回 ``False``。
    - 同状态自转换（``current == target``）若不在转换表的允许集合中也返回 ``False``，
      由各状态机自行决定是否声明自循环边。当前 3 张表均不允许自转换。
    """
    return target in transitions.get(current, frozenset())


def assert_transition(
    current: str,
    target: str,
    transitions: Mapping[str, frozenset[str]],
    machine_name: str = "",
) -> None:
    """断言版校验：非法转换抛 :class:`IllegalStateTransition`。

    Parameters
    ----------
    current : str
        当前状态。
    target : str
        目标状态。
    transitions : Mapping[str, frozenset[str]]
        转换表。
    machine_name : str
        状态机名，写入异常以便排查（建议传入 ``"passport"`` / ``"action"`` /
        ``"credential"``）。

    Raises
    ------
    IllegalStateTransition
        当 ``validate_transition`` 返回 ``False`` 时抛出。
    """
    if not validate_transition(current, target, transitions):
        raise IllegalStateTransition(current, target, machine_name)


__all__ = [
    "ACTION_TRANSITIONS",
    "CREDENTIAL_TRANSITIONS",
    "IllegalStateTransition",
    "PASSPORT_TRANSITIONS",
    "assert_transition",
    "validate_transition",
]
