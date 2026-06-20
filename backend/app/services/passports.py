"""Passport 注册中心业务逻辑（任务 5.3 / Req 3 / Req 4）。

本服务层把「能力包（Passport）的生命周期 CRUD + 状态机 + 审计写入」集中在
一处，对应 design.md「核心组件 / 护照注册中心」。

设计要点
--------
1. **状态机硬约束**：所有状态变更 (pause/resume/revoke) 都用
   :func:`app.core.state_machine.assert_transition` + ``PASSPORT_TRANSITIONS``
   把守。任何非法转换抛 :class:`IllegalStateTransition`，最终在
   :mod:`app.core.errors` 映射为 409 ``ILLEGAL_STATE_TRANSITION``。这样
   Property 3「状态机合法性」在 Passport 域就有运行期强约束。

2. **不直接 commit**：服务层仅 ``add()`` + ``flush()``，事务边界由路由层
   统一控制。让「业务写 + 审计写 + 级联写」放在同一事务，符合 Req 11 AC7
   「审计写入失败必须阻止业务转换」的语义。

3. **跨用户访问统一 404**：``_get_owned_passport`` 把「不存在 / 不属于本人」
   都映射到 :class:`PassportNotFoundError`，避免通过对比 404/403 推测
   他人 passport_id 是否存在的侧信道（与凭证服务一致）。

4. **撤销级联**（Req 3 AC5）：``revoke_passport`` 取消下属所有
   APPROVAL_REQUIRED 状态的 action（state=CANCELLED + 写每个 action 的
   ACTION_CANCELLED 审计）；不影响已 EXECUTED 的 action（最终一致性边界）。

5. **策略来源二选一**：``create_passport`` 同时支持 ``policy_dict`` 与
   ``template_name + overrides``。互斥逻辑在 :class:`PassportCreateRequest`
   schema 阶段已校验，但服务层也再做一次防御性检查，便于内部直接调用
   场景（如任务 19 demo seed 加载器）也能命中互斥规则。

6. **凭证关联约束**：若 ``api_credential_id`` 给定，必须属本人且 state ∈
   {READ_ONLY, TRADE_ENABLED}（已成功验证），否则抛
   :class:`CredentialNotFoundError`（404）或 :class:`PassportStateTransitionError`
   （409）——具体取决于失败原因。给定凭证 → 创建为 ACTIVE；不给 → DRAFT
   （Req 3 AC2）。

7. **policy 校验始终走 `validate_policy_dsl`**：模板模式的策略由
   `build_policy_from_template` 调用 `validate_policy_dsl` 校验；自定义
   `policy_dict` 模式直接调 `validate_policy_dsl`；两条路径都会在合并 / 校验
   阶段做 `allowed_symbols` 小写归一化（Req 4 AC3）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.state_machine import (
    PASSPORT_TRANSITIONS,
    assert_transition,
)
from app.models import AgentAction, AgentPassport, ApiCredential
from app.models.enums import (
    ActionState,
    AuditEventType,
    CredentialState,
    PassportState,
)
from app.services.audit_writer import (
    ACTOR_TYPE_SYSTEM,
    ACTOR_TYPE_USER,
    write_audit_event,
)
from app.services.capability_envelope import (
    PolicyTemplate,
    build_policy_from_template,
)
from app.services.credentials import CredentialNotFoundError
from app.services.policy_validator import (
    InvalidPolicyError,
    validate_policy_dsl,
)

# logger 不会输出策略原文（policy 不算敏感但通常较大，写日志会噪音）；
# 仅记录 passport_id / state / version / trace_id 等元数据。
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 业务异常
# ---------------------------------------------------------------------------
class PassportNotFoundError(LookupError):
    """Passport 不存在或不属于当前用户（用于 router 转 404）。

    安全考虑：「不属于本人」与「不存在」对外一律返回 404，避免
    通过对比 404 / 403 推测他人 passport_id 的存在性。

    Attributes
    ----------
    passport_id : uuid.UUID
        请求中携带的 passport_id（用于错误响应 details）。
    """

    def __init__(self, passport_id: uuid.UUID) -> None:
        self.passport_id = passport_id
        super().__init__(f"passport {passport_id} not found")


class PassportStateTransitionError(ValueError):
    """Passport 状态转换的业务侧前置条件未满足。

    与 :class:`IllegalStateTransition` 区别
    -----------------------------------------
    - :class:`IllegalStateTransition` 表达「转换不在状态机表中」（结构错）；
      由 ``assert_transition`` 抛。
    - :class:`PassportStateTransitionError` 表达「转换在状态机表中，但
      业务前置条件不满足」（语义错），例如：
        * update_policy 要求 ACTIVE / PAUSED，否则不允许变更（DRAFT 没意义、
          REVOKED/EXPIRED 是终态不能改）。
        * 关联的凭证 state 不在 {READ_ONLY, TRADE_ENABLED}。

    继承 ``ValueError`` 让上层既能精确捕获，也能宽松 ``except ValueError``。
    最终在 :mod:`app.core.errors` 映射为 409。
    """

    def __init__(self, message: str, *, code: str = "PASSPORT_STATE_INVALID") -> None:
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _now() -> datetime:
    """统一的"当前时间"生成函数，便于测试 monkeypatch。"""
    return datetime.now(UTC)


def _get_owned_passport(
    db: Session,
    passport_id: uuid.UUID,
    user_id: uuid.UUID,
) -> AgentPassport:
    """按主键查 passport + 校验所有者。

    "不存在 / 不属于本人" 都映射为 :class:`PassportNotFoundError`，
    最终统一返回 404，避免泄露存在性信息。
    """
    passport = db.get(AgentPassport, passport_id)
    if passport is None or passport.user_id != user_id:
        raise PassportNotFoundError(passport_id)
    return passport


def _get_owned_credential(
    db: Session,
    credential_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ApiCredential:
    """按主键查凭证 + 校验所有者（不含软删除项）。

    与 :func:`app.services.credentials._get_owned_credential` 行为一致；
    本模块内联一份是为了避免跨服务循环依赖（credentials 不应依赖 passports）。
    """
    cred = db.get(ApiCredential, credential_id)
    if cred is None or cred.user_id != user_id or cred.deleted_at is not None:
        raise CredentialNotFoundError(credential_id)
    return cred


# ---------------------------------------------------------------------------
# 业务函数
# ---------------------------------------------------------------------------
def create_passport(
    db: Session,
    *,
    user_id: uuid.UUID,
    name: str,
    agent_type: str,
    api_credential_id: uuid.UUID | None = None,
    policy_dict: dict[str, Any] | None = None,
    template_name: PolicyTemplate | str | None = None,
    overrides: dict[str, Any] | None = None,
    trace_id: uuid.UUID | None = None,
) -> AgentPassport:
    """创建一份 Passport（Req 3 AC1,2 / Req 4 AC1,6）。

    流程
    ----
    1. 策略来源互斥校验：``policy_dict`` 与 ``template_name`` 必须二选一。
    2. 构造 Policy：模板模式走
       :func:`app.services.capability_envelope.build_policy_from_template`；
       自定义模式直接 :func:`validate_policy_dsl`。两条路径都会做 schema +
       业务规则校验（含 withdraw=False / 小写归一化）。
    3. 凭证关联校验：``api_credential_id`` 给定时必须属本人且 state ∈
       {READ_ONLY, TRADE_ENABLED}。
    4. 创建 ORM 行：state=ACTIVE（关联凭证）或 DRAFT（无凭证），version=1。
    5. 写 PASSPORT_CREATED 审计事件。
    6. 返回（不 commit；commit 由路由层执行）。

    Parameters
    ----------
    db : Session
        当前请求会话。
    user_id : uuid.UUID
        Passport 拥有者。
    name, agent_type : str
        展示字段。
    api_credential_id : uuid.UUID | None, default None
        关联凭证 ID。给定 → 创建为 ACTIVE；不给 → DRAFT。
    policy_dict : dict | None, default None
        完整 PolicyDSLv0 dict；与 ``template_name`` 互斥。
    template_name : PolicyTemplate | str | None, default None
        内置模板枚举或字符串值；与 ``policy_dict`` 互斥。
    overrides : dict | None, default None
        模板模式下的顶层节级覆盖；与 ``policy_dict`` 一起给会被拒。
    trace_id : uuid.UUID | None, default None
        请求级 trace_id；为 None 时自行生成一枚。

    Returns
    -------
    AgentPassport
        已 ``flush`` 的 ORM 行（``id`` / ``state`` / ``version`` 已分配）。

    Raises
    ------
    ValueError
        ``policy_dict`` 与 ``template_name`` 都给或都不给；或 ``policy_dict``
        与 ``overrides`` 同时给（``overrides`` 仅模板模式有意义）。
    InvalidPolicyError
        Policy DSL v0 校验失败。
    CredentialNotFoundError
        ``api_credential_id`` 不存在 / 不属本人 / 已软删除。
    PassportStateTransitionError
        关联凭证 state 不在 {READ_ONLY, TRADE_ENABLED}。
    """
    if trace_id is None:
        trace_id = uuid.uuid4()

    # ---- 1. 策略来源互斥（防御性，schema 已校验过一次） ----
    has_dict = policy_dict is not None
    has_template = template_name is not None
    if has_dict and has_template:
        raise ValueError(
            "policy_dict and template_name are mutually exclusive; "
            "provide exactly one"
        )
    if not has_dict and not has_template:
        raise ValueError(
            "either policy_dict or template_name must be provided"
        )
    if has_dict and overrides is not None:
        raise ValueError(
            "overrides is only valid with template_name"
        )

    # ---- 2. 构造 Policy（含校验 + 归一化） ----
    if has_template:
        # 接受字符串值，方便内部调用；statically 由 PolicyTemplate 枚举校验。
        if isinstance(template_name, str) and not isinstance(template_name, PolicyTemplate):
            try:
                template_name = PolicyTemplate(template_name)
            except ValueError as exc:
                raise InvalidPolicyError(
                    f"unknown template_name: {template_name!r}",
                    errors=[
                        {
                            "path": "template_name",
                            "message": f"{template_name!r} not in "
                            f"{[t.value for t in PolicyTemplate]}",
                            "validator": "enum",
                        }
                    ],
                ) from exc
        # build_policy_from_template 内部已调 validate_policy_dsl
        policy = build_policy_from_template(template_name, overrides=overrides)
    else:
        # 自定义策略；直接 validator 走 schema + business rules
        assert policy_dict is not None  # mypy 已被分支收窄但显式断言更稳
        policy = validate_policy_dsl(policy_dict)

    # 持久化形态：dict（已归一化）。Pydantic 的 model_dump 把嵌套 BaseModel 压平。
    policy_json = policy.model_dump()

    # ---- 3. 凭证关联校验 ----
    initial_state = PassportState.DRAFT
    if api_credential_id is not None:
        cred = _get_owned_credential(db, api_credential_id, user_id)
        if cred.state not in (
            CredentialState.READ_ONLY,
            CredentialState.TRADE_ENABLED,
        ):
            # 业务前置条件错（凭证未通过验证 / 已 INVALID 等）→ 409
            raise PassportStateTransitionError(
                f"api_credential {api_credential_id} state {cred.state!r} "
                f"is not eligible to back a passport (required: READ_ONLY or TRADE_ENABLED)",
                code="CREDENTIAL_NOT_ELIGIBLE",
            )
        initial_state = PassportState.ACTIVE

    # ---- 4. 创建 ORM 行 ----
    passport = AgentPassport(
        user_id=user_id,
        api_credential_id=api_credential_id,
        name=name,
        agent_type=agent_type,
        state=initial_state,
        version=1,
        policy_json=policy_json,
        reputation_score=50,  # PRD §8 默认值；显式写出便于阅读
    )
    db.add(passport)
    db.flush()  # 让 passport.id 立即可读

    # ---- 5. 审计事件 ----
    write_audit_event(
        db,
        event_type=AuditEventType.PASSPORT_CREATED,
        user_id=user_id,
        passport_id=passport.id,
        actor_type=ACTOR_TYPE_USER,
        actor_id=str(user_id),
        trace_id=trace_id,
        event_data={
            "passport_id": str(passport.id),
            "name": name,
            "agent_type": agent_type,
            "state": initial_state,
            "version": 1,
            "trace_id": str(trace_id),
            "template_name": (
                template_name.value if isinstance(template_name, PolicyTemplate) else None
            ),
            "has_credential": api_credential_id is not None,
        },
    )

    logger.info(
        "passport created",
        extra={
            "passport_id": str(passport.id),
            "user_id": str(user_id),
            "state": initial_state,
            "version": 1,
        },
    )

    return passport


def update_passport_policy(
    db: Session,
    *,
    passport_id: uuid.UUID,
    user_id: uuid.UUID,
    new_policy_dict: dict[str, Any],
    trace_id: uuid.UUID | None = None,
) -> AgentPassport:
    """更新 Passport 的策略并递增 version（Req 3 AC3 / Req 4 AC1）。

    流程
    ----
    1. 找 Passport（属本人，否则 404）。
    2. 业务前置：state 必须是 ACTIVE 或 PAUSED——
       - DRAFT：还没绑定凭证、策略尚未生效，应通过 ``create`` 重建而非补丁；
       - REVOKED / EXPIRED / DELETED：终态，不允许任何变更。
       不满足抛 :class:`IllegalStateTransition`（虽然 state 不变，但语义上
       是「禁止 policy 编辑」的状态机判定，复用同一异常类便于路由层映射 409）。
    3. ``validate_policy_dsl`` 严格校验新策略。
    4. ``version += 1``，``policy_json = new_policy.model_dump()``。
    5. 写 PASSPORT_POLICY_UPDATED 审计事件，含 old / new version。

    Notes
    -----
    更新策略时 **不**改变 ``state``（仍是 ACTIVE 或 PAUSED）；下次审批阶段
    若发现 ``policy_version_at_planning`` 与当前 version 不一致，会触发重裁决
    （Req 8 AC9 / 任务 11）。

    Returns
    -------
    AgentPassport
        已 ``flush`` 的 ORM 行；version 已 +1。
    """
    if trace_id is None:
        trace_id = uuid.uuid4()

    passport = _get_owned_passport(db, passport_id, user_id)

    # 业务前置：仅 ACTIVE / PAUSED 允许编辑 policy
    # （PRD/design.md 没硬性规定，但「DRAFT 直接编辑不写凭证」语义模糊；
    # 终态变更明显违反一致性。这里用 IllegalStateTransition 是因为：
    # 状态机角度看，policy 更新不改变 state，但「在错误 state 下做 policy
    # 操作」语义等价于「不允许的状态转换」。）
    if passport.state not in (PassportState.ACTIVE, PassportState.PAUSED):
        from app.core.state_machine import IllegalStateTransition

        # 用 "policy_update" 作为 target 让错误信息可读：
        # "[passport] illegal state transition: 'DRAFT' -> 'policy_update'"
        raise IllegalStateTransition(
            current=passport.state,
            target="policy_update",
            machine_name="passport",
        )

    # 校验新策略（schema + business rules + symbol 归一化）
    new_policy = validate_policy_dsl(new_policy_dict)
    new_policy_json = new_policy.model_dump()

    # 原子更新
    old_version = passport.version
    passport.version = old_version + 1
    passport.policy_json = new_policy_json
    db.flush()

    write_audit_event(
        db,
        event_type=AuditEventType.PASSPORT_POLICY_UPDATED,
        user_id=user_id,
        passport_id=passport.id,
        actor_type=ACTOR_TYPE_USER,
        actor_id=str(user_id),
        trace_id=trace_id,
        event_data={
            "passport_id": str(passport.id),
            "old_version": old_version,
            "new_version": passport.version,
            "trace_id": str(trace_id),
        },
    )

    logger.info(
        "passport policy updated",
        extra={
            "passport_id": str(passport.id),
            "user_id": str(user_id),
            "old_version": old_version,
            "new_version": passport.version,
        },
    )

    return passport


def pause_passport(
    db: Session,
    *,
    passport_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: uuid.UUID | None = None,
) -> AgentPassport:
    """ACTIVE → PAUSED（Req 3 AC4）。

    用 :func:`assert_transition` 把守状态机；非法转换抛
    :class:`IllegalStateTransition`（→ 409）。

    Returns
    -------
    AgentPassport
        已 flush 的 ORM 行；state=PAUSED。
    """
    if trace_id is None:
        trace_id = uuid.uuid4()

    passport = _get_owned_passport(db, passport_id, user_id)

    assert_transition(
        passport.state,
        PassportState.PAUSED,
        PASSPORT_TRANSITIONS,
        machine_name="passport",
    )

    passport.state = PassportState.PAUSED
    db.flush()

    write_audit_event(
        db,
        event_type=AuditEventType.PASSPORT_PAUSED,
        user_id=user_id,
        passport_id=passport.id,
        actor_type=ACTOR_TYPE_USER,
        actor_id=str(user_id),
        trace_id=trace_id,
        event_data={
            "passport_id": str(passport.id),
            "version": passport.version,
            "trace_id": str(trace_id),
        },
    )

    logger.info(
        "passport paused",
        extra={"passport_id": str(passport.id), "user_id": str(user_id)},
    )
    return passport


def resume_passport(
    db: Session,
    *,
    passport_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: uuid.UUID | None = None,
) -> AgentPassport:
    """PAUSED → ACTIVE（PRD §7.1 PASSPORT_TRANSITIONS）。

    PRD §14 审计事件列表未显式列出 PASSPORT_RESUMED；任务 5.3 在
    :class:`AuditEventType` 中补齐这枚常量，让 PAUSED → ACTIVE 也有可
    审计的事件类型，与 PASSPORT_PAUSED 形成对称。

    Returns
    -------
    AgentPassport
        已 flush 的 ORM 行；state=ACTIVE。
    """
    if trace_id is None:
        trace_id = uuid.uuid4()

    passport = _get_owned_passport(db, passport_id, user_id)

    assert_transition(
        passport.state,
        PassportState.ACTIVE,
        PASSPORT_TRANSITIONS,
        machine_name="passport",
    )

    passport.state = PassportState.ACTIVE
    db.flush()

    write_audit_event(
        db,
        event_type=AuditEventType.PASSPORT_RESUMED,
        user_id=user_id,
        passport_id=passport.id,
        actor_type=ACTOR_TYPE_USER,
        actor_id=str(user_id),
        trace_id=trace_id,
        event_data={
            "passport_id": str(passport.id),
            "version": passport.version,
            "trace_id": str(trace_id),
        },
    )

    logger.info(
        "passport resumed",
        extra={"passport_id": str(passport.id), "user_id": str(user_id)},
    )
    return passport


def revoke_passport(
    db: Session,
    *,
    passport_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: uuid.UUID | None = None,
) -> AgentPassport:
    """ACTIVE / PAUSED → REVOKED + 取消下属待审批 action（Req 3 AC5）。

    流程
    ----
    1. 找 Passport（属本人，否则 404）。
    2. ``assert_transition`` 校验 current → REVOKED 合法。
    3. 取消所有 ``state=APPROVAL_REQUIRED`` 的下属 action：
       - 走 ``ACTION_TRANSITIONS`` 的 ``APPROVAL_REQUIRED → CANCELLED`` 边
         （任务 5.3 新增）。
       - 每个被取消的 action 写一条 ``ACTION_CANCELLED`` 审计事件，便于
         审计重放显示「这个 action 因 passport 撤销被级联取消」。
       - 已 EXECUTED / EXECUTING / 其他状态的 action 不受影响（最终一致性
         边界，符合 Req 3 AC5 「APPROVAL_REQUIRED 状态的待处理操作」语义）。
    4. 设置 ``passport.state = REVOKED``。
    5. 写 ``PASSPORT_REVOKED`` 审计事件，含级联取消的 action_id 列表。

    Returns
    -------
    AgentPassport
        已 flush 的 ORM 行；state=REVOKED。

    Notes
    -----
    REVOKED 是终态——之后任何 pause/resume/policy_update 调用都会被
    :func:`assert_transition` 拒绝（Req 3 AC7）。
    """
    if trace_id is None:
        trace_id = uuid.uuid4()

    passport = _get_owned_passport(db, passport_id, user_id)

    assert_transition(
        passport.state,
        PassportState.REVOKED,
        PASSPORT_TRANSITIONS,
        machine_name="passport",
    )

    # 级联取消下属 APPROVAL_REQUIRED 状态的 action（Req 3 AC5）
    pending_stmt = (
        select(AgentAction)
        .where(AgentAction.passport_id == passport.id)
        .where(AgentAction.state == ActionState.APPROVAL_REQUIRED)
    )
    pending_actions = list(db.execute(pending_stmt).scalars().all())

    cancelled_action_ids: list[str] = []
    for action in pending_actions:
        # 每个 action 走自己的状态机：APPROVAL_REQUIRED → CANCELLED
        # （任务 2.3 ACTION_TRANSITIONS 在任务 5.3 补齐这条边）
        assert_transition(
            action.state,
            ActionState.CANCELLED,
            __import__(
                "app.core.state_machine", fromlist=["ACTION_TRANSITIONS"]
            ).ACTION_TRANSITIONS,
            machine_name="action",
        )
        action.state = ActionState.CANCELLED
        cancelled_action_ids.append(str(action.id))

        # 每个 action 单独写一条审计事件，便于 audit 重放界面在该 action 的
        # 时间线上显示「这次取消是因为 passport 被撤销」
        write_audit_event(
            db,
            event_type=AuditEventType.ACTION_CANCELLED,
            user_id=user_id,
            passport_id=passport.id,
            action_id=action.id,
            actor_type=ACTOR_TYPE_SYSTEM,
            actor_id="SYSTEM",
            trace_id=trace_id,
            event_data={
                "action_id": str(action.id),
                "passport_id": str(passport.id),
                "reason": "PASSPORT_REVOKED",
                "trace_id": str(trace_id),
            },
        )

    passport.state = PassportState.REVOKED
    db.flush()

    write_audit_event(
        db,
        event_type=AuditEventType.PASSPORT_REVOKED,
        user_id=user_id,
        passport_id=passport.id,
        actor_type=ACTOR_TYPE_USER,
        actor_id=str(user_id),
        trace_id=trace_id,
        event_data={
            "passport_id": str(passport.id),
            "version": passport.version,
            "trace_id": str(trace_id),
            "cancelled_action_ids": cancelled_action_ids,
            "cancelled_action_count": len(cancelled_action_ids),
        },
    )

    logger.info(
        "passport revoked",
        extra={
            "passport_id": str(passport.id),
            "user_id": str(user_id),
            "cancelled_action_count": len(cancelled_action_ids),
        },
    )

    return passport


def get_passport(
    db: Session,
    *,
    passport_id: uuid.UUID,
    user_id: uuid.UUID,
) -> AgentPassport:
    """按 ID 查询 Passport（属本人，否则 404）。

    本函数不过滤 state——REVOKED / EXPIRED 的 passport 仍可读，便于审计
    回顾。仅 ``DELETED``（在 :func:`list_passports` 中）会被过滤。
    """
    return _get_owned_passport(db, passport_id, user_id)


def list_passports(
    db: Session,
    *,
    user_id: uuid.UUID,
) -> list[AgentPassport]:
    """列出当前用户的所有 Passport（不返回 ``DELETED`` 状态）。

    ``DELETED`` 不是当前 PRD §7.1 PassportState 的活跃值
    （PASSPORT_TRANSITIONS 里 ``DRAFT → DELETED`` 是占位边，MVP 不主动用），
    这里防御性过滤；REVOKED / EXPIRED 仍可见，便于用户审计自己的历史 passport。

    返回顺序按 ``created_at DESC``（最新创建在前）。
    """
    stmt = (
        select(AgentPassport)
        .where(AgentPassport.user_id == user_id)
        .where(AgentPassport.state != PassportState.DELETED)
        .order_by(AgentPassport.created_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


__all__ = [
    "PassportNotFoundError",
    "PassportStateTransitionError",
    "create_passport",
    "get_passport",
    "list_passports",
    "pause_passport",
    "resume_passport",
    "revoke_passport",
    "update_passport_policy",
]
