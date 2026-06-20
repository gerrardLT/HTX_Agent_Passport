"""任务 5.3 Passport 服务层单元测试。

直接调用 :mod:`app.services.passports`，不经过 HTTP 层，聚焦：

- 创建路径（policy_dict / template_name / 互斥校验 / 凭证关联）
- 状态机集成（pause / resume / revoke + 终态终封闭性）
- 版本递增（每次 PATCH /policy 严格 +1）
- 撤销级联（取消下属 APPROVAL_REQUIRED action + 写每个 action 的审计事件）
- 跨用户访问统一 404
- 审计事件类型与 event_data 内容

复用 conftest 的 ``db_session`` fixture（事务级隔离）。

每个测试用例用 ``_make_user`` / ``_make_credential_trade_enabled`` 等本地 helper
快速构造前置数据，避免依赖 HTTP 路径——单元测试聚焦业务逻辑，速度优先。
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.core.state_machine import IllegalStateTransition
from app.models import (
    AgentAction,
    AgentPassport,
    ApiCredential,
    AuditEvent,
    User,
)
from app.models.enums import (
    ActionState,
    AuditEventType,
    CredentialState,
    PassportState,
)
from app.services.capability_envelope import (
    PolicyTemplate,
    TEMPLATE_SMALL_SPOT_EXECUTOR,
)
from app.services.credentials import CredentialNotFoundError
from app.services.passports import (
    PassportNotFoundError,
    PassportStateTransitionError,
    create_passport,
    get_passport,
    list_passports,
    pause_passport,
    resume_passport,
    revoke_passport,
    update_passport_policy,
)
from app.services.policy_validator import InvalidPolicyError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_user(
    db_session,
    wallet: str = "0xPASSPORT0000000000000000000000000000001",
) -> User:
    """快速建一个测试用户（事务级隔离 fixture 提供）。"""
    user = User(primary_wallet=wallet)
    db_session.add(user)
    db_session.flush()
    return user


def _make_credential(
    db_session,
    user: User,
    *,
    state: str = CredentialState.TRADE_ENABLED,
    label: str = "test-cred",
) -> ApiCredential:
    """快速建一条凭证；默认 TRADE_ENABLED，可被 Passport 关联。

    直接构造 ORM 行（绕过 :mod:`app.services.credentials` 的加密路径），
    单元测试聚焦 Passport 服务逻辑，凭证内容用 dummy 值。
    """
    cred = ApiCredential(
        user_id=user.id,
        provider="HTX",
        label=label,
        access_key_hash=f"hash-{uuid.uuid4().hex[:16]}",
        encrypted_access_key=b"x" * 40,  # bytes 占位，长度满足 NOT NULL
        encrypted_secret_key=b"y" * 40,
        encryption_algorithm="AES-256-GCM",
        permission_read=True,
        permission_trade=(state == CredentialState.TRADE_ENABLED),
        permission_withdraw=False,
        state=state,
    )
    db_session.add(cred)
    db_session.flush()
    return cred


def _valid_policy_dict() -> dict[str, Any]:
    """合法的 PolicyDSLv0 dict —— 基于 small_spot_executor 模板深拷贝。"""
    return copy.deepcopy(TEMPLATE_SMALL_SPOT_EXECUTOR)


# ---------------------------------------------------------------------------
# 1. create_passport — 基本路径
# ---------------------------------------------------------------------------
class TestCreatePassport:
    """创建路径覆盖：state 派生、模板模式、自定义 policy、互斥校验、凭证关联。"""

    def test_create_with_credential_yields_active_state(self, db_session) -> None:
        """关联已 TRADE_ENABLED 凭证 → state=ACTIVE（Req 3 AC2）。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)

        passport = create_passport(
            db_session,
            user_id=user.id,
            name="my-bot",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )

        assert passport.state == PassportState.ACTIVE
        assert passport.version == 1
        assert passport.api_credential_id == cred.id
        assert passport.user_id == user.id
        assert passport.reputation_score == 50
        # policy_json 持久化形态：dict（已归一化）
        assert passport.policy_json["version"] == "0.1"
        assert passport.policy_json["limits"]["allowed_symbols"] == [
            "btcusdt",
            "ethusdt",
        ]

    def test_create_without_credential_yields_draft_state(self, db_session) -> None:
        """不关联凭证 → state=DRAFT（Req 3 AC2）。"""
        user = _make_user(db_session)

        passport = create_passport(
            db_session,
            user_id=user.id,
            name="draft-bot",
            agent_type="trader",
            api_credential_id=None,
            policy_dict=_valid_policy_dict(),
        )

        assert passport.state == PassportState.DRAFT
        assert passport.version == 1
        assert passport.api_credential_id is None

    def test_create_with_template_name_path(self, db_session) -> None:
        """走 ``template_name`` 路径，policy 由 build_policy_from_template 生成。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)

        passport = create_passport(
            db_session,
            user_id=user.id,
            name="template-bot",
            agent_type="trader",
            api_credential_id=cred.id,
            template_name=PolicyTemplate.SMALL_SPOT_EXECUTOR,
        )

        # 模板字段值锚点（PRD §17 demo seed）
        assert passport.policy_json["limits"]["max_notional_usdt_per_order"] == 20
        assert passport.policy_json["limits"]["max_daily_notional_usdt"] == 100
        assert passport.policy_json["capabilities"]["place_order"] is True

    def test_create_with_template_name_string_value_accepted(self, db_session) -> None:
        """``template_name`` 接受字符串值，便于内部直接调用（demo seed 加载器）。"""
        user = _make_user(db_session)

        passport = create_passport(
            db_session,
            user_id=user.id,
            name="tmpl-str",
            agent_type="trader",
            template_name="readonly_researcher",  # 字符串而非枚举
        )
        assert passport.policy_json["capabilities"]["place_order"] is False
        assert passport.policy_json["capabilities"]["read_market"] is True

    def test_create_with_template_overrides_replaces_section(self, db_session) -> None:
        """``overrides`` 在模板模式下生效，整节替换 limits。"""
        user = _make_user(db_session)

        passport = create_passport(
            db_session,
            user_id=user.id,
            name="ovr-bot",
            agent_type="trader",
            template_name=PolicyTemplate.SMALL_SPOT_EXECUTOR,
            overrides={
                "limits": {
                    "allowed_symbols": ["solusdt"],
                    "max_notional_usdt_per_order": 5,
                    "max_daily_notional_usdt": 30,
                    "max_orders_per_day": 5,
                }
            },
        )
        assert passport.policy_json["limits"]["allowed_symbols"] == ["solusdt"]
        assert passport.policy_json["limits"]["max_notional_usdt_per_order"] == 5

    def test_create_with_both_policy_and_template_raises(self, db_session) -> None:
        """``policy_dict`` 与 ``template_name`` 都给 → ValueError（互斥）。"""
        user = _make_user(db_session)
        with pytest.raises(ValueError, match="mutually exclusive"):
            create_passport(
                db_session,
                user_id=user.id,
                name="both",
                agent_type="trader",
                policy_dict=_valid_policy_dict(),
                template_name=PolicyTemplate.SMALL_SPOT_EXECUTOR,
            )

    def test_create_with_neither_policy_nor_template_raises(self, db_session) -> None:
        """``policy_dict`` 与 ``template_name`` 都不给 → ValueError。"""
        user = _make_user(db_session)
        with pytest.raises(ValueError, match="must be provided"):
            create_passport(
                db_session,
                user_id=user.id,
                name="empty",
                agent_type="trader",
            )

    def test_create_with_overrides_but_no_template_raises(self, db_session) -> None:
        """``overrides`` 仅模板模式有意义；与 ``policy_dict`` 一起给应被拒。"""
        user = _make_user(db_session)
        with pytest.raises(ValueError, match="overrides"):
            create_passport(
                db_session,
                user_id=user.id,
                name="ovr-bad",
                agent_type="trader",
                policy_dict=_valid_policy_dict(),
                overrides={"limits": {}},
            )

    def test_create_with_invalid_policy_raises(self, db_session) -> None:
        """非法 policy（withdraw=true）→ InvalidPolicyError（→ 400）。"""
        user = _make_user(db_session)
        bad_policy = _valid_policy_dict()
        bad_policy["capabilities"]["withdraw"] = True

        with pytest.raises(InvalidPolicyError):
            create_passport(
                db_session,
                user_id=user.id,
                name="bad-policy",
                agent_type="trader",
                policy_dict=bad_policy,
            )

    def test_create_with_unknown_template_string_raises(self, db_session) -> None:
        """未知 ``template_name`` 字符串值 → InvalidPolicyError。"""
        user = _make_user(db_session)
        with pytest.raises(InvalidPolicyError):
            create_passport(
                db_session,
                user_id=user.id,
                name="unknown-tpl",
                agent_type="trader",
                template_name="not_a_real_template",
            )

    def test_create_with_credential_not_owned_raises(self, db_session) -> None:
        """关联凭证不属本人 → CredentialNotFoundError（→ 404）。"""
        owner = _make_user(db_session, wallet="0xOWNER")
        intruder = _make_user(db_session, wallet="0xINTRUDER")
        cred = _make_credential(db_session, owner)

        with pytest.raises(CredentialNotFoundError):
            create_passport(
                db_session,
                user_id=intruder.id,
                name="x",
                agent_type="trader",
                api_credential_id=cred.id,  # owner 的凭证
                policy_dict=_valid_policy_dict(),
            )

    def test_create_with_credential_not_validated_raises(self, db_session) -> None:
        """关联凭证 state=CREATED（未验证）→ PassportStateTransitionError。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user, state=CredentialState.CREATED)

        with pytest.raises(PassportStateTransitionError) as exc_info:
            create_passport(
                db_session,
                user_id=user.id,
                name="x",
                agent_type="trader",
                api_credential_id=cred.id,
                policy_dict=_valid_policy_dict(),
            )
        # code 字段被路由层用于 details，便于前端区分错误类型
        assert exc_info.value.code == "CREDENTIAL_NOT_ELIGIBLE"

    def test_create_with_credential_invalid_raises(self, db_session) -> None:
        """关联凭证 state=INVALID → PassportStateTransitionError。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user, state=CredentialState.INVALID)

        with pytest.raises(PassportStateTransitionError):
            create_passport(
                db_session,
                user_id=user.id,
                name="x",
                agent_type="trader",
                api_credential_id=cred.id,
                policy_dict=_valid_policy_dict(),
            )

    def test_create_with_read_only_credential_yields_active(self, db_session) -> None:
        """READ_ONLY 凭证也能激活 Passport（Req 2 AC3）。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user, state=CredentialState.READ_ONLY)

        passport = create_passport(
            db_session,
            user_id=user.id,
            name="readonly-bot",
            agent_type="researcher",
            api_credential_id=cred.id,
            template_name=PolicyTemplate.READONLY_RESEARCHER,
        )
        assert passport.state == PassportState.ACTIVE

    def test_create_writes_passport_created_audit(self, db_session) -> None:
        """写一条 PASSPORT_CREATED 审计事件，event_data 含核心字段。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)

        passport = create_passport(
            db_session,
            user_id=user.id,
            name="audit-bot",
            agent_type="trader",
            api_credential_id=cred.id,
            template_name=PolicyTemplate.SMALL_SPOT_EXECUTOR,
        )

        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.PASSPORT_CREATED
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        data = events[0].event_json["data"]
        assert data["passport_id"] == str(passport.id)
        assert data["state"] == PassportState.ACTIVE
        assert data["version"] == 1
        assert data["template_name"] == "small_spot_executor"
        assert data["has_credential"] is True


# ---------------------------------------------------------------------------
# 2. update_passport_policy — 版本递增 + 状态机门槛
# ---------------------------------------------------------------------------
class TestUpdatePassportPolicy:
    """版本递增 1→2→3 + 终态拒绝编辑。"""

    def test_version_increments_on_each_update(self, db_session) -> None:
        """连续 3 次 PATCH /policy → version 1 → 2 → 3 → 4（Req 3 AC3）。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="v",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        assert passport.version == 1

        for expected in (2, 3, 4):
            new_policy = _valid_policy_dict()
            # 每次微调一个字段，确保新策略是「真的不同」
            new_policy["limits"]["max_orders_per_day"] = expected
            updated = update_passport_policy(
                db_session,
                passport_id=passport.id,
                user_id=user.id,
                new_policy_dict=new_policy,
            )
            assert updated.version == expected
            assert updated.policy_json["limits"]["max_orders_per_day"] == expected

    def test_update_writes_audit_event_with_version_diff(self, db_session) -> None:
        """每次更新写一条 PASSPORT_POLICY_UPDATED 审计，含 old_version / new_version。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="audit-update",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        new_policy = _valid_policy_dict()
        new_policy["limits"]["max_orders_per_day"] = 7
        update_passport_policy(
            db_session,
            passport_id=passport.id,
            user_id=user.id,
            new_policy_dict=new_policy,
        )

        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.PASSPORT_POLICY_UPDATED
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        data = events[0].event_json["data"]
        assert data["old_version"] == 1
        assert data["new_version"] == 2

    def test_update_revoked_passport_raises(self, db_session) -> None:
        """REVOKED 是终态，不允许编辑 policy → IllegalStateTransition。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        revoke_passport(db_session, passport_id=passport.id, user_id=user.id)
        assert passport.state == PassportState.REVOKED

        with pytest.raises(IllegalStateTransition):
            update_passport_policy(
                db_session,
                passport_id=passport.id,
                user_id=user.id,
                new_policy_dict=_valid_policy_dict(),
            )

    def test_update_draft_passport_raises(self, db_session) -> None:
        """DRAFT 状态不允许 policy 编辑（应通过重新创建）。"""
        user = _make_user(db_session)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="draft",
            agent_type="trader",
            policy_dict=_valid_policy_dict(),
        )
        assert passport.state == PassportState.DRAFT

        with pytest.raises(IllegalStateTransition):
            update_passport_policy(
                db_session,
                passport_id=passport.id,
                user_id=user.id,
                new_policy_dict=_valid_policy_dict(),
            )

    def test_update_paused_passport_allowed(self, db_session) -> None:
        """PAUSED 状态允许编辑 policy（暂停期间也能改策略后再 resume）。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="paused-edit",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        pause_passport(db_session, passport_id=passport.id, user_id=user.id)
        assert passport.state == PassportState.PAUSED

        new_policy = _valid_policy_dict()
        new_policy["limits"]["max_orders_per_day"] = 3
        updated = update_passport_policy(
            db_session,
            passport_id=passport.id,
            user_id=user.id,
            new_policy_dict=new_policy,
        )
        assert updated.version == 2
        assert updated.state == PassportState.PAUSED  # state 不变

    def test_update_with_invalid_policy_raises(self, db_session) -> None:
        """新策略含 withdraw=true → InvalidPolicyError，version 不变。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )

        bad_policy = _valid_policy_dict()
        bad_policy["capabilities"]["withdraw"] = True

        with pytest.raises(InvalidPolicyError):
            update_passport_policy(
                db_session,
                passport_id=passport.id,
                user_id=user.id,
                new_policy_dict=bad_policy,
            )
        # 失败时 version 不递增
        db_session.refresh(passport)
        assert passport.version == 1


# ---------------------------------------------------------------------------
# 3. pause / resume / revoke — 状态机集成
# ---------------------------------------------------------------------------
class TestPausePassport:
    def test_pause_active_passport(self, db_session) -> None:
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        paused = pause_passport(db_session, passport_id=passport.id, user_id=user.id)
        assert paused.state == PassportState.PAUSED

    def test_pause_writes_audit(self, db_session) -> None:
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        pause_passport(db_session, passport_id=passport.id, user_id=user.id)

        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.PASSPORT_PAUSED
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

    def test_pause_draft_passport_raises(self, db_session) -> None:
        """DRAFT → PAUSED 不在转换表中。"""
        user = _make_user(db_session)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="draft",
            agent_type="trader",
            policy_dict=_valid_policy_dict(),
        )
        with pytest.raises(IllegalStateTransition):
            pause_passport(db_session, passport_id=passport.id, user_id=user.id)


class TestResumePassport:
    def test_resume_paused_passport(self, db_session) -> None:
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        pause_passport(db_session, passport_id=passport.id, user_id=user.id)
        resumed = resume_passport(db_session, passport_id=passport.id, user_id=user.id)
        assert resumed.state == PassportState.ACTIVE

    def test_resume_writes_passport_resumed_audit(self, db_session) -> None:
        """新增的 PASSPORT_RESUMED 审计事件类型生效。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        pause_passport(db_session, passport_id=passport.id, user_id=user.id)
        resume_passport(db_session, passport_id=passport.id, user_id=user.id)

        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.PASSPORT_RESUMED
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

    def test_resume_active_passport_raises(self, db_session) -> None:
        """ACTIVE → ACTIVE 是自循环，状态机表不允许（Req 3 AC4）。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        with pytest.raises(IllegalStateTransition):
            resume_passport(db_session, passport_id=passport.id, user_id=user.id)


class TestRevokePassport:
    """撤销 + 级联取消 APPROVAL_REQUIRED action（Req 3 AC5）。"""

    def test_revoke_active_passport(self, db_session) -> None:
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        revoked = revoke_passport(
            db_session, passport_id=passport.id, user_id=user.id
        )
        assert revoked.state == PassportState.REVOKED

    def test_revoke_paused_passport(self, db_session) -> None:
        """PAUSED → REVOKED 也合法（design.md PASSPORT_TRANSITIONS）。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        pause_passport(db_session, passport_id=passport.id, user_id=user.id)
        revoked = revoke_passport(
            db_session, passport_id=passport.id, user_id=user.id
        )
        assert revoked.state == PassportState.REVOKED

    def test_revoke_revoked_passport_raises(self, db_session) -> None:
        """REVOKED 是终态，再次 revoke → IllegalStateTransition（Req 3 AC7）。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        revoke_passport(db_session, passport_id=passport.id, user_id=user.id)
        with pytest.raises(IllegalStateTransition):
            revoke_passport(db_session, passport_id=passport.id, user_id=user.id)

    def test_revoke_cascades_cancel_pending_actions(self, db_session) -> None:
        """撤销时取消所有 APPROVAL_REQUIRED 状态的下属 action（Req 3 AC5）。

        构造场景：3 个 action（一个 APPROVAL_REQUIRED、一个 EXECUTED、一个 EXECUTING）。
        revoke 后只有 APPROVAL_REQUIRED 那个被转 CANCELLED；其他不受影响。
        """
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )

        # 直接构造 ORM 行（绕过完整 action 流程，本测试聚焦级联取消）
        pending_action = AgentAction(
            passport_id=passport.id,
            user_id=user.id,
            trace_id=uuid.uuid4(),
            natural_language_request="buy btc",
            state=ActionState.APPROVAL_REQUIRED,
            execution_mode="simulation",
        )
        executed_action = AgentAction(
            passport_id=passport.id,
            user_id=user.id,
            trace_id=uuid.uuid4(),
            natural_language_request="prior trade",
            state=ActionState.EXECUTED,  # 终态，不应被影响
            execution_mode="simulation",
        )
        executing_action = AgentAction(
            passport_id=passport.id,
            user_id=user.id,
            trace_id=uuid.uuid4(),
            natural_language_request="ongoing trade",
            state=ActionState.EXECUTING,  # 不在 APPROVAL_REQUIRED，不应被影响
            execution_mode="simulation",
        )
        db_session.add_all([pending_action, executed_action, executing_action])
        db_session.flush()

        # 撤销
        revoke_passport(db_session, passport_id=passport.id, user_id=user.id)

        # 重新加载断言
        db_session.refresh(pending_action)
        db_session.refresh(executed_action)
        db_session.refresh(executing_action)

        assert pending_action.state == ActionState.CANCELLED
        assert executed_action.state == ActionState.EXECUTED  # 不变
        assert executing_action.state == ActionState.EXECUTING  # 不变

    def test_revoke_writes_passport_revoked_audit_with_cancelled_ids(
        self, db_session
    ) -> None:
        """PASSPORT_REVOKED 审计事件含 cancelled_action_ids 列表。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )

        pending = AgentAction(
            passport_id=passport.id,
            user_id=user.id,
            trace_id=uuid.uuid4(),
            natural_language_request="x",
            state=ActionState.APPROVAL_REQUIRED,
            execution_mode="simulation",
        )
        db_session.add(pending)
        db_session.flush()

        revoke_passport(db_session, passport_id=passport.id, user_id=user.id)

        # PASSPORT_REVOKED 事件
        revoked_events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.PASSPORT_REVOKED
                )
            )
            .scalars()
            .all()
        )
        assert len(revoked_events) == 1
        data = revoked_events[0].event_json["data"]
        assert str(pending.id) in data["cancelled_action_ids"]
        assert data["cancelled_action_count"] == 1

        # ACTION_CANCELLED 事件（每个被取消的 action 各一条）
        cancelled_events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.ACTION_CANCELLED
                )
            )
            .scalars()
            .all()
        )
        assert len(cancelled_events) == 1
        cancelled_data = cancelled_events[0].event_json["data"]
        assert cancelled_data["action_id"] == str(pending.id)
        assert cancelled_data["reason"] == "PASSPORT_REVOKED"

    def test_revoke_with_no_pending_actions_does_not_write_action_cancelled(
        self, db_session
    ) -> None:
        """没有待审批 action 时 → 不写 ACTION_CANCELLED 审计；只写 PASSPORT_REVOKED。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        revoke_passport(db_session, passport_id=passport.id, user_id=user.id)

        cancelled_events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.ACTION_CANCELLED
                )
            )
            .scalars()
            .all()
        )
        assert len(cancelled_events) == 0


# ---------------------------------------------------------------------------
# 4. get / list — 跨用户隔离
# ---------------------------------------------------------------------------
class TestGetAndListPassports:
    def test_get_other_users_passport_raises_not_found(self, db_session) -> None:
        """跨用户 get → PassportNotFoundError（→ 404）。"""
        owner = _make_user(db_session, wallet="0xOWNER")
        intruder = _make_user(db_session, wallet="0xINTRUDER")
        cred = _make_credential(db_session, owner)
        passport = create_passport(
            db_session,
            user_id=owner.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )

        with pytest.raises(PassportNotFoundError):
            get_passport(
                db_session, passport_id=passport.id, user_id=intruder.id
            )

    def test_list_isolates_by_user(self, db_session) -> None:
        u1 = _make_user(db_session, wallet="0xLISTU1")
        u2 = _make_user(db_session, wallet="0xLISTU2")
        c1 = _make_credential(db_session, u1, label="u1c")
        c2 = _make_credential(db_session, u2, label="u2c")

        p1 = create_passport(
            db_session,
            user_id=u1.id,
            name="u1p",
            agent_type="trader",
            api_credential_id=c1.id,
            policy_dict=_valid_policy_dict(),
        )
        create_passport(
            db_session,
            user_id=u2.id,
            name="u2p",
            agent_type="trader",
            api_credential_id=c2.id,
            policy_dict=_valid_policy_dict(),
        )

        u1_list = list_passports(db_session, user_id=u1.id)
        u1_ids = [p.id for p in u1_list]
        assert p1.id in u1_ids
        assert len(u1_list) == 1

    def test_list_includes_revoked_passports(self, db_session) -> None:
        """REVOKED 仍出现在列表里（便于审计回顾）；只过滤 DELETED。"""
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        revoke_passport(db_session, passport_id=passport.id, user_id=user.id)

        result = list_passports(db_session, user_id=user.id)
        assert passport.id in [p.id for p in result]


# ---------------------------------------------------------------------------
# 5. 跨用户访问统一 404（pause / resume / revoke / update）
# ---------------------------------------------------------------------------
class TestCrossUserAccessReturnsNotFound:
    """跨用户访问所有 mutation 端点都映射为 PassportNotFoundError。"""

    @pytest.fixture()
    def setup(self, db_session):
        owner = _make_user(db_session, wallet="0xOWNER")
        intruder = _make_user(db_session, wallet="0xINTRUDER")
        cred = _make_credential(db_session, owner)
        passport = create_passport(
            db_session,
            user_id=owner.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        return {"owner": owner, "intruder": intruder, "passport": passport}

    def test_intruder_cannot_pause(self, db_session, setup) -> None:
        with pytest.raises(PassportNotFoundError):
            pause_passport(
                db_session,
                passport_id=setup["passport"].id,
                user_id=setup["intruder"].id,
            )

    def test_intruder_cannot_resume(self, db_session, setup) -> None:
        # 让 owner 先 pause，再 intruder resume 测试
        pause_passport(
            db_session,
            passport_id=setup["passport"].id,
            user_id=setup["owner"].id,
        )
        with pytest.raises(PassportNotFoundError):
            resume_passport(
                db_session,
                passport_id=setup["passport"].id,
                user_id=setup["intruder"].id,
            )

    def test_intruder_cannot_revoke(self, db_session, setup) -> None:
        with pytest.raises(PassportNotFoundError):
            revoke_passport(
                db_session,
                passport_id=setup["passport"].id,
                user_id=setup["intruder"].id,
            )

    def test_intruder_cannot_update_policy(self, db_session, setup) -> None:
        with pytest.raises(PassportNotFoundError):
            update_passport_policy(
                db_session,
                passport_id=setup["passport"].id,
                user_id=setup["intruder"].id,
                new_policy_dict=_valid_policy_dict(),
            )


# ---------------------------------------------------------------------------
# 6. ORM 持久化层断言（双向一致性 — 数据库行确实变了）
# ---------------------------------------------------------------------------
class TestOrmPersistence:
    """额外验证 ORM 行的字段在 db_session 中确实落盘。"""

    def test_revoked_passport_persisted_in_db(self, db_session) -> None:
        user = _make_user(db_session)
        cred = _make_credential(db_session, user)
        passport = create_passport(
            db_session,
            user_id=user.id,
            name="x",
            agent_type="trader",
            api_credential_id=cred.id,
            policy_dict=_valid_policy_dict(),
        )
        revoke_passport(db_session, passport_id=passport.id, user_id=user.id)

        # 重新查询确认状态已落盘
        fresh = db_session.get(AgentPassport, passport.id)
        assert fresh is not None
        assert fresh.state == PassportState.REVOKED
