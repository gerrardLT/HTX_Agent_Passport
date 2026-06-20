"""凭证使用限额测试（G12 / Phase 3）。

**Validates: docs/tech-research/07-...md §7.3**
—— HTX API key 不支持动态短时凭证（交易所平台限制），但我们在使用维度
强制"每日次数上限 + 显式过期"——形成"每用一次都过一关"的零信任使用层。

测试覆盖
--------
1. 默认（全 NULL 字段）→ 无限制可用
2. 过期凭证 → state 自动转 INVALID + 抛 EXPIRED
3. 当日次数达上限 → 抛 DAILY_LIMIT_EXCEEDED
4. UTC 跨日自动重置计数器
5. 状态非法（INVALID / REVOKED / DELETED）→ 抛 INVALID_STATE
6. 未提供 expires_at → 永久可用
7. 软删除凭证 → 拒绝
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import ApiCredential, User
from app.models.enums import CredentialState
from app.services.credential_usage import (
    CredentialUsageError,
    check_and_record_credential_use,
)


# ---------------------------------------------------------------------------
# fixture：构造一个 TRADE_ENABLED 凭证
# ---------------------------------------------------------------------------
@pytest.fixture()
def credential(db_session: Session) -> ApiCredential:
    user = User(primary_wallet=f"0xCREDUSE{uuid.uuid4().hex[:24]}")
    db_session.add(user)
    db_session.flush()
    cred = ApiCredential(
        user_id=user.id,
        label="g12-test-cred",
        access_key_hash="hash_" + "0" * 59,
        encrypted_access_key=b"ENCRYPTED_AK",
        encrypted_secret_key=b"ENCRYPTED_SK",
        encryption_algorithm="AES-256-GCM",
        permission_read=True,
        permission_trade=True,
        permission_withdraw=False,
        state=CredentialState.TRADE_ENABLED,
    )
    db_session.add(cred)
    db_session.flush()
    return cred


# ===========================================================================
# 1. 默认无限制
# ===========================================================================
class TestUnlimitedByDefault:
    """默认全 NULL → 无限次可用，向后兼容。"""

    def test_no_limits_passes(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        assert credential.max_uses_per_day is None
        assert credential.expires_at is None
        cred = check_and_record_credential_use(
            db_session, credential_id=credential.id
        )
        assert cred.id == credential.id
        assert cred.current_uses_today == 1
        assert cred.last_use_at is not None

    def test_repeated_use_increments_counter(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        for _ in range(5):
            check_and_record_credential_use(db_session, credential_id=credential.id)
        assert credential.current_uses_today == 5


# ===========================================================================
# 2. 过期检查
# ===========================================================================
class TestExpiration:
    def test_expired_raises_and_invalidates_state(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        """过期 → 抛 EXPIRED + state 自动置 INVALID。"""
        credential.expires_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.flush()

        with pytest.raises(CredentialUsageError) as exc_info:
            check_and_record_credential_use(
                db_session,
                credential_id=credential.id,
                now=datetime(2026, 5, 31, tzinfo=UTC),
            )
        assert exc_info.value.code == "EXPIRED"

        # state 已转 INVALID
        db_session.refresh(credential)
        assert credential.state == CredentialState.INVALID

    def test_not_yet_expired_passes(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        credential.expires_at = datetime(2099, 1, 1, tzinfo=UTC)
        db_session.flush()
        check_and_record_credential_use(db_session, credential_id=credential.id)
        assert credential.current_uses_today == 1

    def test_expired_writes_audit_event(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        from app.models import AuditEvent
        from sqlalchemy import select

        credential.expires_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.flush()

        with pytest.raises(CredentialUsageError):
            check_and_record_credential_use(
                db_session,
                credential_id=credential.id,
                now=datetime(2026, 5, 31, tzinfo=UTC),
            )

        events = db_session.execute(
            select(AuditEvent).where(
                AuditEvent.user_id == credential.user_id
            )
        ).scalars().all()
        assert any(
            ev.event_json.get("data", {}).get("reason") == "EXPIRED"
            for ev in events
        ), "should write CREDENTIAL_VALIDATED audit event with reason=EXPIRED"


# ===========================================================================
# 3. 每日次数上限
# ===========================================================================
class TestDailyLimit:
    def test_at_limit_raises(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        credential.max_uses_per_day = 3
        credential.current_uses_today = 3
        credential.last_use_at = datetime(2026, 5, 31, 11, 0, tzinfo=UTC)
        db_session.flush()

        with pytest.raises(CredentialUsageError) as exc_info:
            check_and_record_credential_use(
                db_session,
                credential_id=credential.id,
                now=datetime(2026, 5, 31, 11, 30, tzinfo=UTC),
            )
        assert exc_info.value.code == "DAILY_LIMIT_EXCEEDED"

    def test_below_limit_passes(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        credential.max_uses_per_day = 5
        credential.current_uses_today = 4
        credential.last_use_at = datetime(2026, 5, 31, 11, 0, tzinfo=UTC)
        db_session.flush()

        check_and_record_credential_use(
            db_session,
            credential_id=credential.id,
            now=datetime(2026, 5, 31, 11, 30, tzinfo=UTC),
        )
        assert credential.current_uses_today == 5

    def test_first_use_after_zero_count(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        """全新 last_use_at=None 时，当日 uses=0；首次调用应放行。"""
        credential.max_uses_per_day = 1
        db_session.flush()
        check_and_record_credential_use(db_session, credential_id=credential.id)
        assert credential.current_uses_today == 1


# ===========================================================================
# 4. UTC 跨日自动重置
# ===========================================================================
class TestDailyReset:
    def test_cross_utc_day_resets_counter(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        """昨天达上限 → 今天首次使用计数归 0 后 +1 = 1。"""
        credential.max_uses_per_day = 3
        credential.current_uses_today = 3  # 昨日已达上限
        credential.last_use_at = datetime(2026, 5, 30, 23, 0, tzinfo=UTC)
        db_session.flush()

        # 今天首次使用
        check_and_record_credential_use(
            db_session,
            credential_id=credential.id,
            now=datetime(2026, 5, 31, 0, 30, tzinfo=UTC),
        )
        assert credential.current_uses_today == 1
        assert credential.last_use_at == datetime(2026, 5, 31, 0, 30, tzinfo=UTC)

    def test_same_utc_day_does_not_reset(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        """同 UTC 日内不重置；同日次数累加到上限即拒。"""
        credential.max_uses_per_day = 2
        credential.current_uses_today = 2
        credential.last_use_at = datetime(2026, 5, 31, 1, 0, tzinfo=UTC)
        db_session.flush()

        with pytest.raises(CredentialUsageError) as exc_info:
            check_and_record_credential_use(
                db_session,
                credential_id=credential.id,
                now=datetime(2026, 5, 31, 23, 59, tzinfo=UTC),
            )
        assert exc_info.value.code == "DAILY_LIMIT_EXCEEDED"


# ===========================================================================
# 5. 状态检查
# ===========================================================================
class TestStateChecks:
    @pytest.mark.parametrize(
        "bad_state",
        [
            CredentialState.INVALID,
            CredentialState.REVOKED,
            CredentialState.DELETED,
            CredentialState.CREATED,
        ],
    )
    def test_unusable_state_raises(
        self,
        db_session: Session,
        credential: ApiCredential,
        bad_state: str,
    ) -> None:
        credential.state = bad_state
        db_session.flush()
        with pytest.raises(CredentialUsageError) as exc_info:
            check_and_record_credential_use(
                db_session, credential_id=credential.id
            )
        assert exc_info.value.code == "INVALID_STATE"

    def test_soft_deleted_credential_raises(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        credential.deleted_at = datetime.now(UTC)
        db_session.flush()
        with pytest.raises(CredentialUsageError) as exc_info:
            check_and_record_credential_use(
                db_session, credential_id=credential.id
            )
        assert exc_info.value.code == "INVALID_STATE"

    def test_unknown_credential_raises(self, db_session: Session) -> None:
        with pytest.raises(CredentialUsageError) as exc_info:
            check_and_record_credential_use(
                db_session, credential_id=uuid.uuid4()
            )
        assert exc_info.value.code == "INVALID_STATE"

    def test_validating_state_allowed(
        self, db_session: Session, credential: ApiCredential
    ) -> None:
        """VALIDATING 状态也允许使用（getAccountBalance 校验路径需要）。"""
        credential.state = CredentialState.VALIDATING
        db_session.flush()
        check_and_record_credential_use(db_session, credential_id=credential.id)
        assert credential.current_uses_today == 1
