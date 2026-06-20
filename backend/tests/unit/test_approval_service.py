"""审批服务单元测试（任务 11 / Req 8 / Property 6）。

覆盖矩阵
--------
1. **create_approval_request**：创建审批 + expires_at 正确 + 审计事件写入。
2. **双重审批 409**（Property 6）：同一 approval 不可被处理两次。
3. **惰性过期转 EXPIRED**：提交时发现已过期 → action 转 EXPIRED。
4. **主动扫描过期清理**：scan_expired_approvals 批量清理。
5. **Passport 撤销 409**：passport.state=REVOKED 时拒绝审批。
6. **Policy 版本变化重裁决**：version 变化 + REJECT → 拒绝。
7. **拒绝转 REJECTED_BY_USER**：approved=false → action 转 REJECTED_BY_USER。
8. **typed_confirmation 校验**：approved=true 但 confirmation != "APPROVE" → 400。
9. **成功批准**：正常流程 → action 转 APPROVED。
10. **可选钱包签名**：signature 字段正确存储。

注：G16 stale-price 重校验由 :mod:`tests.unit.test_stale_price_recheck` 单独覆盖；
本模块用 ``_fresh_market_snapshot`` autouse fixture 让 SEED_MARKET_DATA 的 ``as_of``
字段在测试期间保持新鲜，避免本模块每个 happy-path 都被 G16 误伤——本模块关心的是
审批语义而非 stale-price 检查。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.models import AgentAction, AgentPassport, Approval, AuditEvent, User
from app.models.enums import ActionState, AuditEventType, PassportState
from app.services.approval_service import (
    ActionNotInApprovalStateError,
    ApprovalAlreadyProcessedError,
    ApprovalExpiredError,
    ApprovalInvalidConfirmationError,
    ApprovalNotFoundError,
    ApprovalPassportRevokedError,
    DEFAULT_APPROVAL_EXPIRES_SECONDS,
    create_approval_request,
    scan_expired_approvals,
    submit_approval,
)


# ---------------------------------------------------------------------------
# Autouse fixture: 让 SEED_MARKET_DATA 在测试期间保持"新鲜"
# ---------------------------------------------------------------------------
# 修复 G16 后，``SEED_MARKET_DATA`` 含静态 ``as_of=2024-06-15``——
# ``submit_approval`` 在 ``approved=True`` 时会做 stale-price 重校验,
# 必然抛 ``MARKET_SNAPSHOT_STALE``。本模块的目的是测审批语义而非 G16 路径,
# 所以 monkeypatch 把 ``as_of`` 改成"测试当下时间"。
#
# G16 自身的覆盖在 :mod:`tests.unit.test_stale_price_recheck` 中单独完成。
@pytest.fixture(autouse=True)
def _fresh_market_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import htx_adapter as _htx

    fresh_now_iso = datetime.now(UTC).isoformat()
    fresh_data = {
        "btcusdt": {
            "last": 68000.0, "bid": 67999.0, "ask": 68001.0,
            "vol_24h": 1500.0, "as_of": fresh_now_iso,
        },
        "ethusdt": {
            "last": 3600.0, "bid": 3599.0, "ask": 3601.0,
            "vol_24h": 25000.0, "as_of": fresh_now_iso,
        },
    }
    monkeypatch.setattr(_htx, "SEED_MARKET_DATA", fresh_data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_user(db_session) -> User:
    user = User(primary_wallet=f"0xAPPROVAL{uuid.uuid4().hex[:30]}")
    db_session.add(user)
    db_session.flush()
    return user


def _make_passport(
    db_session,
    user: User,
    *,
    state: str = PassportState.ACTIVE,
    version: int = 1,
) -> AgentPassport:
    passport = AgentPassport(
        user_id=user.id,
        name="test-passport",
        agent_type="trader",
        state=state,
        version=version,
        policy_json={
            "version": "0.1",
            "capabilities": {"read_market": True, "place_order": True},
            "limits": {"allowed_symbols": ["btcusdt"]},
            "approval": {"required_for_trade": True},
            "blocked_actions": ["withdraw"],
        },
    )
    db_session.add(passport)
    db_session.flush()
    return passport


def _make_action(
    db_session,
    user: User,
    passport: AgentPassport,
    *,
    state: str = ActionState.APPROVAL_REQUIRED,
    policy_version_at_planning: int | None = 1,
) -> AgentAction:
    action = AgentAction(
        passport_id=passport.id,
        user_id=user.id,
        trace_id=uuid.uuid4(),
        natural_language_request="买入 0.01 BTC",
        state=state,
        approval_required=True,
        policy_version_at_planning=policy_version_at_planning,
        normalized_action_json={
            "type": "place_order",
            "symbol": "btcusdt",
            "side": "buy",
            "order_type": "market",
            "amount": 0.01,
            "amount_unit": "base",
            "max_notional_usdt": 680,
            "limit_price": None,
            "requires_user_approval": True,
            "rationale": "用户要求买入 BTC",
        },
    )
    db_session.add(action)
    db_session.flush()
    return action


def _make_approval(
    db_session,
    action: AgentAction,
    user: User,
    *,
    expires_at: datetime | None = None,
    approved: bool | None = None,
) -> Approval:
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(seconds=DEFAULT_APPROVAL_EXPIRES_SECONDS)
    approval = Approval(
        action_id=action.id,
        user_id=user.id,
        approval_type="typed_confirmation",
        approved=approved,
        expires_at=expires_at,
    )
    db_session.add(approval)
    db_session.flush()
    return approval


def _list_event_types(db_session, user_id: uuid.UUID) -> list[str]:
    events = (
        db_session.execute(
            select(AuditEvent)
            .where(AuditEvent.user_id == user_id)
            .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        )
        .scalars()
        .all()
    )
    return [e.event_type for e in events]


# ===========================================================================
# 1. create_approval_request
# ===========================================================================
class TestCreateApprovalRequest:
    """**Validates: Req 8** AC1, AC4。"""

    def test_creates_approval_with_correct_expires_at(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)

        before = datetime.now(UTC)
        approval = create_approval_request(
            db_session,
            action=action,
            user_id=user.id,
            passport_id=passport.id,
        )
        after = datetime.now(UTC)

        assert approval.id is not None
        assert approval.action_id == action.id
        assert approval.user_id == user.id
        assert approval.approved is None  # 待审批
        assert approval.approval_type == "typed_confirmation"
        # expires_at 在 [before + 300s, after + 300s] 范围内
        expected_min = before + timedelta(seconds=DEFAULT_APPROVAL_EXPIRES_SECONDS)
        expected_max = after + timedelta(seconds=DEFAULT_APPROVAL_EXPIRES_SECONDS)
        assert expected_min <= approval.expires_at <= expected_max

    def test_creates_approval_with_custom_expires(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)

        approval = create_approval_request(
            db_session,
            action=action,
            user_id=user.id,
            expires_seconds=60,
        )

        # 60s 过期
        expected_min = datetime.now(UTC) - timedelta(seconds=2)
        expected_max = datetime.now(UTC) + timedelta(seconds=62)
        assert expected_min + timedelta(seconds=60) <= approval.expires_at

    def test_writes_approval_requested_audit_event(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)

        create_approval_request(
            db_session,
            action=action,
            user_id=user.id,
            passport_id=passport.id,
        )

        events = _list_event_types(db_session, user.id)
        assert AuditEventType.APPROVAL_REQUESTED in events


# ===========================================================================
# 2. 双重审批 409（Property 6）
# ===========================================================================
class TestDoubleApprovalPrevention:
    """**Validates: Property 6**（双重审批防护）。"""

    def test_already_approved_raises_409(self, db_session) -> None:
        """同一 approval 已被批准 → 再次提交 → 409。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        # 创建一个已处理的 approval
        _make_approval(db_session, action, user, approved=True)

        with pytest.raises(ApprovalNotFoundError):
            # 没有 pending approval（approved IS NULL）
            submit_approval(
                db_session,
                action_id=action.id,
                user_id=user.id,
                approved=True,
                typed_confirmation="APPROVE",
            )

    def test_cannot_approve_twice_in_sequence(self, db_session) -> None:
        """第一次批准成功后，action 状态变为 APPROVED，第二次提交 → 状态不对。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        _make_approval(db_session, action, user)

        # 第一次批准
        submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=True,
            typed_confirmation="APPROVE",
        )

        # 第二次尝试 → action 已不在 APPROVAL_REQUIRED 状态
        with pytest.raises(ActionNotInApprovalStateError):
            submit_approval(
                db_session,
                action_id=action.id,
                user_id=user.id,
                approved=True,
                typed_confirmation="APPROVE",
            )


# ===========================================================================
# 3. 惰性过期转 EXPIRED
# ===========================================================================
class TestLazyExpiry:
    """**Validates: Req 8** AC4, AC5（惰性过期）。"""

    def test_expired_approval_transitions_action_to_expired(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        # 创建一个已过期的 approval
        _make_approval(
            db_session,
            action,
            user,
            expires_at=datetime.now(UTC) - timedelta(seconds=10),
        )

        with pytest.raises(ApprovalExpiredError):
            submit_approval(
                db_session,
                action_id=action.id,
                user_id=user.id,
                approved=True,
                typed_confirmation="APPROVE",
            )

        # action 应已转为 EXPIRED
        assert action.state == ActionState.EXPIRED

    def test_expired_writes_approval_expired_audit(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        _make_approval(
            db_session,
            action,
            user,
            expires_at=datetime.now(UTC) - timedelta(seconds=10),
        )

        with pytest.raises(ApprovalExpiredError):
            submit_approval(
                db_session,
                action_id=action.id,
                user_id=user.id,
                approved=True,
                typed_confirmation="APPROVE",
            )

        events = _list_event_types(db_session, user.id)
        assert AuditEventType.APPROVAL_EXPIRED in events


# ===========================================================================
# 4. 主动扫描过期清理
# ===========================================================================
class TestScanExpiredApprovals:
    """**Validates: Req 8** AC5（主动扫描）。"""

    def test_scan_expires_timed_out_actions(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)

        # 创建 2 个过期 action + 1 个未过期 action
        action1 = _make_action(db_session, user, passport)
        _make_approval(
            db_session, action1, user,
            expires_at=datetime.now(UTC) - timedelta(seconds=60),
        )

        action2 = _make_action(db_session, user, passport)
        _make_approval(
            db_session, action2, user,
            expires_at=datetime.now(UTC) - timedelta(seconds=30),
        )

        action3 = _make_action(db_session, user, passport)
        _make_approval(
            db_session, action3, user,
            expires_at=datetime.now(UTC) + timedelta(seconds=200),
        )

        count = scan_expired_approvals(db_session)

        assert count == 2
        assert action1.state == ActionState.EXPIRED
        assert action2.state == ActionState.EXPIRED
        assert action3.state == ActionState.APPROVAL_REQUIRED  # 未过期

    def test_scan_writes_audit_events(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        _make_approval(
            db_session, action, user,
            expires_at=datetime.now(UTC) - timedelta(seconds=10),
        )

        scan_expired_approvals(db_session)

        events = _list_event_types(db_session, user.id)
        assert AuditEventType.APPROVAL_EXPIRED in events

    def test_scan_skips_actions_without_pending_approval(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        # 没有创建 approval

        count = scan_expired_approvals(db_session)
        assert count == 0
        assert action.state == ActionState.APPROVAL_REQUIRED


# ===========================================================================
# 5. Passport 撤销 409
# ===========================================================================
class TestPassportRevocationCheck:
    """**Validates: Req 8**（passport 撤销时拒绝审批）。"""

    def test_revoked_passport_rejects_approval(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, state=PassportState.REVOKED)
        action = _make_action(db_session, user, passport)
        _make_approval(db_session, action, user)

        with pytest.raises(ApprovalPassportRevokedError):
            submit_approval(
                db_session,
                action_id=action.id,
                user_id=user.id,
                approved=True,
                typed_confirmation="APPROVE",
            )

        # action 应转为 CANCELLED
        assert action.state == ActionState.CANCELLED


# ===========================================================================
# 6. Policy 版本变化重裁决
# ===========================================================================
class TestPolicyVersionReadjudication:
    """**Validates: Req 8** AC9（policy 版本变化重裁决）。"""

    def test_version_change_with_reject_blocks_approval(self, db_session) -> None:
        """passport.version 变化 + 重裁决 REJECT → 拒绝。"""
        user = _make_user(db_session)
        # passport version=2，但 action 记录的是 version=1
        passport = _make_passport(db_session, user, version=2)
        # 修改 policy 使 place_order 不被允许
        passport.policy_json = {
            "version": "0.1",
            "capabilities": {"read_market": True, "place_order": False},
            "limits": {"allowed_symbols": ["btcusdt"]},
            "approval": {"required_for_trade": True},
            "blocked_actions": ["withdraw"],
        }
        db_session.flush()

        action = _make_action(
            db_session, user, passport, policy_version_at_planning=1
        )
        _make_approval(db_session, action, user)

        with pytest.raises(ApprovalPassportRevokedError, match="re-adjudication"):
            submit_approval(
                db_session,
                action_id=action.id,
                user_id=user.id,
                approved=True,
                typed_confirmation="APPROVE",
            )

        # 注意：SQLite 不支持 ARRAY 类型的 list 写入，所以这里只验证状态转换
        # 在 PostgreSQL 上 reason_codes 会正确写入
        assert action.state == ActionState.AUTO_REJECTED

    def test_same_version_no_readjudication(self, db_session) -> None:
        """passport.version == action.policy_version_at_planning → 不重裁决。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, version=1)
        action = _make_action(
            db_session, user, passport, policy_version_at_planning=1
        )
        _make_approval(db_session, action, user)

        result = submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=True,
            typed_confirmation="APPROVE",
        )

        assert result.state == ActionState.APPROVED


# ===========================================================================
# 7. 拒绝转 REJECTED_BY_USER
# ===========================================================================
class TestRejection:
    """**Validates: Req 8**（用户拒绝）。"""

    def test_reject_transitions_to_rejected_by_user(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        _make_approval(db_session, action, user)

        result = submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=False,
            typed_confirmation="REJECT",  # 拒绝时 confirmation 可为任意值
        )

        assert result.state == ActionState.REJECTED_BY_USER

    def test_reject_writes_approval_submitted_audit(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        _make_approval(db_session, action, user)

        submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=False,
            typed_confirmation="NO",
        )

        events = _list_event_types(db_session, user.id)
        assert AuditEventType.APPROVAL_SUBMITTED in events


# ===========================================================================
# 8. typed_confirmation 校验
# ===========================================================================
class TestTypedConfirmation:
    """**Validates: Req 8** AC2。"""

    def test_approve_without_correct_confirmation_raises(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        _make_approval(db_session, action, user)

        with pytest.raises(ApprovalInvalidConfirmationError):
            submit_approval(
                db_session,
                action_id=action.id,
                user_id=user.id,
                approved=True,
                typed_confirmation="approve",  # 小写不行
            )

    def test_approve_with_empty_confirmation_raises(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        _make_approval(db_session, action, user)

        with pytest.raises(ApprovalInvalidConfirmationError):
            submit_approval(
                db_session,
                action_id=action.id,
                user_id=user.id,
                approved=True,
                typed_confirmation="",
            )

    def test_reject_with_any_confirmation_is_ok(self, db_session) -> None:
        """拒绝时 typed_confirmation 不做校验。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        _make_approval(db_session, action, user)

        result = submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=False,
            typed_confirmation="whatever",
        )

        assert result.state == ActionState.REJECTED_BY_USER


# ===========================================================================
# 9. 成功批准
# ===========================================================================
class TestSuccessfulApproval:
    """**Validates: Req 8** AC1-3。"""

    def test_approve_transitions_to_approved(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        _make_approval(db_session, action, user)

        result = submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=True,
            typed_confirmation="APPROVE",
        )

        assert result.state == ActionState.APPROVED
        assert result.id == action.id

    def test_approve_marks_approval_record(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        approval = _make_approval(db_session, action, user)

        submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=True,
            typed_confirmation="APPROVE",
        )

        assert approval.approved is True

    def test_approve_writes_approval_submitted_audit(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        _make_approval(db_session, action, user)

        submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=True,
            typed_confirmation="APPROVE",
        )

        events = _list_event_types(db_session, user.id)
        assert AuditEventType.APPROVAL_SUBMITTED in events


# ===========================================================================
# 10. 可选钱包签名
# ===========================================================================
class TestWalletSignature:
    """**Validates: Req 8** AC2（可选钱包签名）。"""

    def test_signature_stored_on_approval(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        approval = _make_approval(db_session, action, user)

        submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=True,
            typed_confirmation="APPROVE",
            signature="0xdeadbeef1234567890",
        )

        assert approval.signed_payload == "0xdeadbeef1234567890"
        assert approval.approval_type == "wallet_signature"

    def test_no_signature_keeps_typed_confirmation_type(self, db_session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(db_session, user, passport)
        approval = _make_approval(db_session, action, user)

        submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=True,
            typed_confirmation="APPROVE",
        )

        assert approval.approval_type == "typed_confirmation"
        assert approval.signed_payload is None
