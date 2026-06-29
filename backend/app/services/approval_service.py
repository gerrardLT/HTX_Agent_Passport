"""审批服务（任务 11 / Req 8）。

实现 design.md「审批层」的核心业务逻辑：

- :func:`create_approval_request` —— 创建审批请求（设置 expires_at）。
- :func:`submit_approval` —— 提交审批（批准 / 拒绝）。
- :func:`scan_expired_approvals` —— 后台周期扫描过期审批。

设计依据
--------
- Req 8 AC1：place_order / cancel_order 需人工审批。
- Req 8 AC2：typed_confirmation="APPROVE" 校验。
- Req 8 AC3：审批摘要包含 symbol / side / amount / max_notional / risk_notes。
- Req 8 AC4：审批过期时间（默认 300s）。
- Req 8 AC5：过期后惰性 + 主动扫描双重清理。
- Req 8 AC9：policy 版本变化时重裁决。
- Property 6：双重审批防护（同一 approval 不可被处理两次）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentAction, Approval
from app.models.enums import ActionState, AuditEventType, PassportState
from app.models.passport import AgentPassport
from app.services.audit_writer import (
    ACTOR_TYPE_SYSTEM,
    ACTOR_TYPE_USER,
    write_audit_event,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 审批默认过期时间（秒）—— Req 8 AC4
DEFAULT_APPROVAL_EXPIRES_SECONDS: int = 300


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class ApprovalError(Exception):
    """审批服务基类异常。"""


class ApprovalNotFoundError(ApprovalError):
    """审批记录不存在。"""


class ApprovalAlreadyProcessedError(ApprovalError):
    """审批已被处理（Property 6：双重审批防护）。"""


class ApprovalExpiredError(ApprovalError):
    """审批已过期。"""


class ApprovalPassportRevokedError(ApprovalError):
    """关联 passport 已被撤销。"""


class ApprovalInvalidConfirmationError(ApprovalError):
    """typed_confirmation 校验失败。"""


class MarketSlippageExceededError(ApprovalError):
    """审批延迟后 market snapshot 重校验失败（修复 G16 / Req 16 AC2）。

    触发原因有两类（由 :mod:`app.services.stale_price_check` 区分）：

    - ``MARKET_SNAPSHOT_STALE``：snapshot 时效超过阈值（默认 60s），
      无法判定当前市场状态，保守阻断；
    - ``MARKET_SLIPPAGE_EXCEEDED``：``limit_price`` 与 snapshot 的 ``last``
      偏差超过 ``policy.limits.max_slippage_bps``，价格已显著漂移。

    语义：要求用户**重新发起 action**——保守起见不自动按新价审批。
    action 状态会被推进到 ``REJECTED_BY_USER``，与"用户主动取消"等价；
    审计写 :data:`AuditEventType.MARKET_SLIPPAGE_DETECTED`。
    """

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = reason_code
        self.message = message or reason_code
        super().__init__(self.message)


class ActionNotInApprovalStateError(ApprovalError):
    """Action 不在 APPROVAL_REQUIRED 状态。"""


# ---------------------------------------------------------------------------
# create_approval_request
# ---------------------------------------------------------------------------
def create_approval_request(
    db: Session,
    *,
    action: AgentAction,
    user_id: UUID,
    passport_id: UUID | None = None,
    trace_id: UUID | None = None,
    expires_seconds: int = DEFAULT_APPROVAL_EXPIRES_SECONDS,
) -> Approval:
    """创建审批请求（Req 8 AC1 / AC4）。

    前置条件：action.state 已被调用方转为 APPROVAL_REQUIRED。

    Parameters
    ----------
    db : Session
        当前会话（不 commit）。
    action : AgentAction
        需要审批的 action（state 应为 APPROVAL_REQUIRED）。
    user_id : UUID
        审批归属用户。
    passport_id : UUID | None
        关联 passport（审计事件用）。
    trace_id : UUID | None
        请求级 trace_id。
    expires_seconds : int
        过期时间（秒），默认 300s。

    Returns
    -------
    Approval
        已 flush 的审批记录。
    """
    if trace_id is None:
        trace_id = uuid.uuid4()

    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=expires_seconds)

    approval = Approval(
        action_id=action.id,
        user_id=user_id,
        approval_type="typed_confirmation",
        approved=None,  # 待审批
        expires_at=expires_at,
    )
    db.add(approval)
    db.flush()

    # 写审计事件
    write_audit_event(
        db,
        event_type=AuditEventType.APPROVAL_REQUESTED,
        user_id=user_id,
        actor_type=ACTOR_TYPE_SYSTEM,
        actor_id=ACTOR_TYPE_SYSTEM,
        passport_id=passport_id,
        action_id=action.id,
        trace_id=trace_id,
        event_data={
            "approval_id": str(approval.id),
            "expires_at": expires_at.isoformat(),
            "expires_seconds": expires_seconds,
            "trace_id": str(trace_id),
        },
    )

    return approval


# ---------------------------------------------------------------------------
# submit_approval
# ---------------------------------------------------------------------------
def submit_approval(
    db: Session,
    *,
    action_id: UUID,
    user_id: UUID,
    approved: bool,
    typed_confirmation: str,
    signature: str | None = None,
    trace_id: UUID | None = None,
) -> AgentAction:
    """提交审批决定（Req 8 AC2 / Property 6）。

    流程
    ----
    1. 查找 action + 校验状态为 APPROVAL_REQUIRED。
    2. 查找关联的 pending approval（approved IS NULL）。
    3. Property 6：双重审批防护——若 approval.approved 非 NULL → 409。
    4. 惰性过期检查——若 now > expires_at → 转 EXPIRED。
    5. Passport 撤销检查——若 passport.state == REVOKED → 409。
    6. typed_confirmation 校验——approved=true 时必须为 "APPROVE"。
    7. Policy 版本变化重裁决——若 passport.version != action.policy_version_at_planning
       → 重新调用 policy engine，若 REJECT → 拒绝。
    8. 更新 approval + action 状态。
    9. 写审计事件。

    Parameters
    ----------
    action_id : UUID
        目标 action。
    user_id : UUID
        提交审批的用户。
    approved : bool
        True=批准 / False=拒绝。
    typed_confirmation : str
        approved=True 时必须为 "APPROVE"。
    signature : str | None
        可选钱包签名。
    trace_id : UUID | None
        请求级 trace_id。

    Returns
    -------
    AgentAction
        更新后的 action。

    Raises
    ------
    ActionNotInApprovalStateError
        Action 不在 APPROVAL_REQUIRED 状态。
    ApprovalNotFoundError
        找不到 pending approval。
    ApprovalAlreadyProcessedError
        审批已被处理（Property 6）。
    ApprovalExpiredError
        审批已过期。
    ApprovalPassportRevokedError
        Passport 已被撤销 / policy 版本变化重裁决 REJECT。
    ApprovalInvalidConfirmationError
        typed_confirmation 校验失败。
    MarketSlippageExceededError
        Snapshot 过期或 ``limit_price`` 偏离 ``last`` 超 ``max_slippage_bps``
        （修复 G16 / Req 16 AC2）。
    """
    if trace_id is None:
        trace_id = uuid.uuid4()

    # ---- Step 1: 查找 action + IDOR 防护 ----
    action = db.get(AgentAction, action_id)
    if action is None:
        raise ApprovalNotFoundError(f"action {action_id} not found")

    # IDOR 防护：验证 action 归属当前用户（跨用户访问统一 404）
    if action.user_id != user_id:
        raise ApprovalNotFoundError(f"action {action_id} not found")

    if action.state != ActionState.APPROVAL_REQUIRED:
        raise ActionNotInApprovalStateError(
            f"action {action_id} state is {action.state!r}, expected APPROVAL_REQUIRED"
        )

    # ---- Step 2: 查找 pending approval ----
    approval = (
        db.execute(
            select(Approval)
            .where(Approval.action_id == action_id)
            .where(Approval.approved.is_(None))
            .order_by(Approval.created_at.desc())
        )
        .scalars()
        .first()
    )
    if approval is None:
        raise ApprovalNotFoundError(
            f"no pending approval for action {action_id}"
        )

    # ---- Step 3: Property 6 双重审批防护 ----
    if approval.approved is not None:
        raise ApprovalAlreadyProcessedError(
            f"approval {approval.id} already processed (approved={approval.approved})"
        )

    # ---- Step 4: 惰性过期检查 ----
    now = datetime.now(UTC)
    # SQLite 兼容：expires_at 从 SQLite 取回时可能是 naive datetime
    expires_at = approval.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if now > expires_at:
        # 惰性转 EXPIRED
        approval.approved = False
        action.state = ActionState.EXPIRED
        db.flush()
        write_audit_event(
            db,
            event_type=AuditEventType.APPROVAL_EXPIRED,
            user_id=user_id,
            actor_type=ACTOR_TYPE_SYSTEM,
            actor_id=ACTOR_TYPE_SYSTEM,
            passport_id=action.passport_id,
            action_id=action_id,
            trace_id=trace_id,
            event_data={
                "approval_id": str(approval.id),
                "expired_at": approval.expires_at.isoformat(),
                "detected_at": now.isoformat(),
                "trace_id": str(trace_id),
            },
        )
        raise ApprovalExpiredError(
            f"approval {approval.id} expired at {approval.expires_at.isoformat()}"
        )

    # ---- Step 5: Passport 撤销检查 ----
    passport = db.get(AgentPassport, action.passport_id)
    if passport is not None and passport.state == PassportState.REVOKED:
        # 级联取消
        action.state = ActionState.CANCELLED
        approval.approved = False
        db.flush()
        write_audit_event(
            db,
            event_type=AuditEventType.APPROVAL_SUBMITTED,
            user_id=user_id,
            actor_type=ACTOR_TYPE_USER,
            actor_id=str(user_id),
            passport_id=action.passport_id,
            action_id=action_id,
            trace_id=trace_id,
            event_data={
                "approval_id": str(approval.id),
                "approved": False,
                "reason": "PASSPORT_REVOKED",
                "trace_id": str(trace_id),
            },
        )
        raise ApprovalPassportRevokedError(
            f"passport {action.passport_id} is REVOKED"
        )

    # ---- Step 6: typed_confirmation 校验 ----
    if approved and typed_confirmation != "APPROVE":
        raise ApprovalInvalidConfirmationError(
            "typed_confirmation must be 'APPROVE' when approved=true"
        )

    # ---- Step 7: Policy 版本变化重裁决 ----
    if (
        approved
        and passport is not None
        and action.policy_version_at_planning is not None
        and passport.version != action.policy_version_at_planning
    ):
        # 重裁决：调用 policy engine
        from app.core.config import get_settings
        from app.services.daily_history import aggregate_daily_history
        from app.services.htx_adapter import get_fresh_seed_market_data
        from app.services.policy_engine import (
            GlobalConfig,
            evaluate_policy,
        )

        settings = get_settings()
        global_config = GlobalConfig(
            demo_disable_execution=settings.DEMO_DISABLE_EXECUTION,
            # G18：审批重裁决时同样传声誉分,让 auto_approval_thresholds 一致生效。
            passport_reputation_score=passport.reputation_score,
            # G2：与执行网关一致，从配置读取信息流追踪开关。
            enforce_market_provenance=bool(
                getattr(settings, "ENFORCE_MARKET_PROVENANCE", False)
            ),
        )

        # 聚合当日真实累计（修复 G14：审批重裁决也要算日限额）。
        # 排除当前 action 自身，避免双重计数。
        daily_history = aggregate_daily_history(
            db,
            passport_id=passport.id,
            exclude_action_id=action.id,
        )

        # 用 action 的 normalized_action_json 重新裁决。
        # market_snapshot 用种子行情（与执行网关一致），避免空快照误触发
        # PLAN_HALLUCINATION（symbol 不在空 snapshot 内）。
        normalized_action = action.normalized_action_json or {}
        verdict = evaluate_policy(
            action=normalized_action,
            policy=passport.policy_json,
            daily_history=daily_history,
            market_snapshot=get_fresh_seed_market_data(),
            global_config=global_config,
            now=now,
        )

        if verdict.verdict == "REJECT":
            # 策略变化导致拒绝
            action.state = ActionState.AUTO_REJECTED
            approval.approved = False
            db.flush()
            write_audit_event(
                db,
                event_type=AuditEventType.APPROVAL_SUBMITTED,
                user_id=user_id,
                actor_type=ACTOR_TYPE_USER,
                actor_id=str(user_id),
                passport_id=action.passport_id,
                action_id=action_id,
                trace_id=trace_id,
                event_data={
                    "approval_id": str(approval.id),
                    "approved": False,
                    "reason": "POLICY_VERSION_CHANGED_REJECT",
                    "old_version": action.policy_version_at_planning,
                    "new_version": passport.version,
                    "reason_codes": list(verdict.reason_codes),
                    "trace_id": str(trace_id),
                },
            )
            raise ApprovalPassportRevokedError(
                f"policy version changed ({action.policy_version_at_planning} → "
                f"{passport.version}), re-adjudication result: REJECT"
            )

    # ---- Step 7b: stale-price 重校验（修复 G16 / Req 16 AC2）----
    # 仅在 approved=True 时检查——拒绝路径无需校验当前价格。
    # 与 policy 版本重裁决并列：policy 版本变化 → 拒绝；价格漂移 → 拒绝。
    # 失败时把 action 推到 REJECTED_BY_USER（保守语义：要求用户重新发起
    # 新 action 而非"自动按新价审批"），写 MARKET_SLIPPAGE_DETECTED 审计。
    if approved and passport is not None:
        from app.services.htx_adapter import get_fresh_seed_market_data as _get_fresh
        from app.services.stale_price_check import (
            check_market_snapshot_freshness_and_slippage,
        )

        normalized_action = action.normalized_action_json or {}
        stale_result = check_market_snapshot_freshness_and_slippage(
            action=normalized_action,
            policy=passport.policy_json,
            market_snapshot=_get_fresh(),
            now=now,
        )
        if not stale_result.ok:
            # 推进 action / approval 状态（先于异常 raise，确保审计与业务
            # 状态都落库；与 Step 7 的"REJECT 路径"保持一致）。
            action.state = ActionState.REJECTED_BY_USER
            approval.approved = False
            db.flush()

            # actor_type 用 USER：是用户提交审批触发的检查，与 Step 9 的
            # APPROVAL_SUBMITTED 写入风格保持一致。
            write_audit_event(
                db,
                event_type=AuditEventType.MARKET_SLIPPAGE_DETECTED,
                user_id=user_id,
                actor_type=ACTOR_TYPE_USER,
                actor_id=str(user_id),
                passport_id=action.passport_id,
                action_id=action_id,
                trace_id=trace_id,
                event_data={
                    "reason_code": stale_result.reason_code,
                    "trace_id": str(trace_id),
                    **stale_result.detail,
                },
            )
            raise MarketSlippageExceededError(
                stale_result.reason_code or "MARKET_SLIPPAGE_DETECTED",
                f"stale price check failed: {stale_result.reason_code} "
                f"{stale_result.detail}",
            )

    # ---- Step 8: 更新 approval + action 状态 ----
    approval.approved = approved
    if signature:
        approval.signed_payload = signature
        approval.approval_type = "wallet_signature"

    if approved:
        action.state = ActionState.APPROVED
    else:
        action.state = ActionState.REJECTED_BY_USER

    db.flush()

    # ---- Step 9: 写审计事件 ----
    write_audit_event(
        db,
        event_type=AuditEventType.APPROVAL_SUBMITTED,
        user_id=user_id,
        actor_type=ACTOR_TYPE_USER,
        actor_id=str(user_id),
        passport_id=action.passport_id,
        action_id=action_id,
        trace_id=trace_id,
        event_data={
            "approval_id": str(approval.id),
            "approved": approved,
            "typed_confirmation": typed_confirmation,
            "has_signature": signature is not None,
            "trace_id": str(trace_id),
        },
    )

    return action


# ---------------------------------------------------------------------------
# scan_expired_approvals
# ---------------------------------------------------------------------------
def scan_expired_approvals(db: Session) -> int:
    """后台周期扫描：将超时的 APPROVAL_REQUIRED action 转为 EXPIRED（Req 8 AC5）。

    扫描逻辑
    --------
    1. 查找所有 state=APPROVAL_REQUIRED 的 action。
    2. 对每个 action，查找其 pending approval（approved IS NULL）。
    3. 若 now > approval.expires_at → 转 action.state=EXPIRED + approval.approved=False。
    4. 写 APPROVAL_EXPIRED 审计事件。

    Returns
    -------
    int
        本次扫描清理的过期 action 数量。

    Notes
    -----
    - 本函数由后台调度器（如 APScheduler / asyncio.create_task）每 30s 调用一次。
    - 不 commit——调用方负责事务边界。
    - 使用 SYSTEM actor_type（非用户触发）。
    """
    now = datetime.now(UTC)
    expired_count = 0

    # 查找所有 APPROVAL_REQUIRED 状态的 action
    actions = (
        db.execute(
            select(AgentAction).where(
                AgentAction.state == ActionState.APPROVAL_REQUIRED
            )
        )
        .scalars()
        .all()
    )

    for action in actions:
        # 查找 pending approval
        approval = (
            db.execute(
                select(Approval)
                .where(Approval.action_id == action.id)
                .where(Approval.approved.is_(None))
                .order_by(Approval.created_at.desc())
            )
            .scalars()
            .first()
        )

        if approval is None:
            continue

        # SQLite 兼容：expires_at 从 SQLite 取回时可能是 naive datetime
        expires_at = approval.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        if now > expires_at:
            # 过期处理
            approval.approved = False
            action.state = ActionState.EXPIRED
            db.flush()

            write_audit_event(
                db,
                event_type=AuditEventType.APPROVAL_EXPIRED,
                user_id=action.user_id,
                actor_type=ACTOR_TYPE_SYSTEM,
                actor_id=ACTOR_TYPE_SYSTEM,
                passport_id=action.passport_id,
                action_id=action.id,
                trace_id=action.trace_id,
                event_data={
                    "approval_id": str(approval.id),
                    "expired_at": approval.expires_at.isoformat(),
                    "detected_at": now.isoformat(),
                    "scan_triggered": True,
                },
            )
            expired_count += 1

    return expired_count


__all__ = [
    "ActionNotInApprovalStateError",
    "ApprovalAlreadyProcessedError",
    "ApprovalError",
    "ApprovalExpiredError",
    "ApprovalInvalidConfirmationError",
    "ApprovalNotFoundError",
    "ApprovalPassportRevokedError",
    "DEFAULT_APPROVAL_EXPIRES_SECONDS",
    "MarketSlippageExceededError",
    "create_approval_request",
    "scan_expired_approvals",
    "submit_approval",
]
