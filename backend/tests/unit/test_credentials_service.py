"""任务 4.2 凭证服务层单元测试。

直接调用 :mod:`app.services.credentials` 的纯函数，不经过 HTTP 层，
聚焦"业务规则 + 安全约束"层面。

覆盖维度
--------
1. **加密存储** —— ``encrypted_secret_key`` / ``encrypted_access_key`` 在 ORM
   行里都是密文 bytes；只有用 ``CredentialVault.decrypt`` 才能还原原文。
2. **withdraw 强制 false**（Req 2 AC4 / Req 15 AC6）—— 即使 mock 验证器返回
   ``withdraw=true``，``permission_withdraw`` 仍硬覆盖为 false。
3. **重复检测**（Req 2 AC2）—— 同 user_id + 同 access_key → ``DuplicateCredentialError``；
   不同 access_key 或不同 user 不冲突。
4. **状态机防御**（Req 2 AC3 / 任务 2.3）—— REVOKED 凭证不能再 validate / delete。
5. **软删除**（Req 2 AC6）—— DELETE 后 ORM 行依然存在（数据库可见），仅 ``state``
   与 ``deleted_at`` 改变。
6. **审计事件** —— 每次写操作都生成对应类型的 audit_event；event_data 不含密钥字段。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.core.state_machine import IllegalStateTransition
from app.core.vault import CredentialVault
from app.models import ApiCredential, AuditEvent, User
from app.models.enums import AuditEventType, CredentialState
from app.services import credentials as svc
from app.services.credentials import (
    CredentialNotFoundError,
    DuplicateCredentialError,
    create_credential,
    delete_credential,
    list_credentials,
    validate_credential,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_user(db_session, wallet: str = "0xUNIT0000000000000000000000000000000000A") -> User:
    """快速建一个测试用户。conftest 的 ``db_session`` fixture 提供事务级隔离。"""
    user = User(primary_wallet=wallet)
    db_session.add(user)
    db_session.flush()
    return user


from app.core.envelope_vault import EnvelopeVault


def _vault() -> EnvelopeVault:
    """conftest 已注入 VAULT_MASTER_KEY，无参构造即可。

    凭证服务已升级为信封加密（EnvelopeVault），新写入是 envelope 格式，
    EnvelopeVault.decrypt 同时兼容旧单层格式。测试用同一实现做 round-trip。
    """
    return EnvelopeVault()


# ---------------------------------------------------------------------------
# 1. create_credential：加密 + 审计 + 重复检测
# ---------------------------------------------------------------------------
class TestCreateCredential:
    """创建路径覆盖：加密、access_key_hash、审计事件、重复检测。"""

    def test_creates_with_encrypted_secret_not_recoverable_without_vault(
        self, db_session
    ) -> None:
        """ORM 行里的 ``encrypted_secret_key`` 是 bytes 密文；
        非 vault.decrypt 的方式（直接 decode）无法读出原文。"""
        user = _make_user(db_session)
        access_key = "ACCESS-KEY-VAULT-TEST"
        secret_key = "SECRET-KEY-VAULT-TEST"

        cred = create_credential(
            db_session,
            user_id=user.id,
            label="my-key",
            access_key=access_key,
            secret_key=secret_key,
        )

        # 1. 密文是 bytes，含 12-byte nonce + ciphertext + 16-byte tag
        assert isinstance(cred.encrypted_access_key, bytes)
        assert isinstance(cred.encrypted_secret_key, bytes)
        # 长度下界：plaintext_utf8 + 12 nonce + 16 tag
        assert len(cred.encrypted_access_key) >= len(access_key.encode("utf-8")) + 28
        assert len(cred.encrypted_secret_key) >= len(secret_key.encode("utf-8")) + 28

        # 2. 密文里看不到原文（机密性）
        assert access_key.encode("utf-8") not in cred.encrypted_access_key
        assert secret_key.encode("utf-8") not in cred.encrypted_secret_key

        # 3. 用 vault.decrypt 能还原
        v = _vault()
        assert v.decrypt(cred.encrypted_access_key) == access_key
        assert v.decrypt(cred.encrypted_secret_key) == secret_key

        # 4. access_key_hash 是 SHA-256 64 hex
        assert cred.access_key_hash == v.hash_access_key(access_key)
        assert len(cred.access_key_hash) == 64

        # 5. 初始状态 + 三权限默认 false
        assert cred.state == CredentialState.CREATED
        assert cred.permission_read is False
        assert cred.permission_trade is False
        assert cred.permission_withdraw is False
        assert cred.encryption_algorithm == "AES-256-GCM"
        assert cred.deleted_at is None

    def test_writes_audit_event_without_secrets(self, db_session) -> None:
        """CREDENTIAL_CREATED 审计事件存在，event_data 不包含任何密钥相关字段。"""
        user = _make_user(db_session)
        access_key = "ACCESS-KEY-AUDIT"
        secret_key = "SECRET-KEY-AUDIT"

        cred = create_credential(
            db_session,
            user_id=user.id,
            label="audit-label",
            access_key=access_key,
            secret_key=secret_key,
        )

        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.CREDENTIAL_CREATED
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        event = events[0]
        # event_data 中只暴露 credential_id / label / trace_id / provider
        assert event.event_json["data"]["credential_id"] == str(cred.id)
        assert event.event_json["data"]["label"] == "audit-label"

        # 关键安全断言：审计 JSON 序列化字符串里不出现密钥
        import json

        serialized = json.dumps(event.event_json)
        assert access_key not in serialized
        assert secret_key not in serialized
        # 也不应出现 access_key_hash —— 哈希虽然是单向的，但属于内部识别字段
        assert cred.access_key_hash not in serialized

    def test_duplicate_same_user_same_access_key_raises(self, db_session) -> None:
        """同 user + 同 access_key → ``DuplicateCredentialError``。"""
        user = _make_user(db_session)
        access_key = "DUP-ACCESS-KEY"

        first = create_credential(
            db_session,
            user_id=user.id,
            label="first",
            access_key=access_key,
            secret_key="secret-1",
        )

        with pytest.raises(DuplicateCredentialError) as exc_info:
            create_credential(
                db_session,
                user_id=user.id,
                label="second-attempt",
                access_key=access_key,
                secret_key="secret-2",
            )

        # 异常字段携带"已存在凭证"的引用，便于 handler 转 409 时回填到 details
        assert exc_info.value.user_id == user.id
        assert exc_info.value.existing_credential_id == first.id
        assert exc_info.value.access_key_hash == _vault().hash_access_key(access_key)
        # 是 ValueError 子类（接口稳定性）
        assert isinstance(exc_info.value, ValueError)

    def test_duplicate_detection_excludes_soft_deleted(self, db_session) -> None:
        """已软删除的凭证不算占位——同 access_key 仍可重新创建。"""
        user = _make_user(db_session)
        access_key = "DUP-AFTER-DELETE"

        first = create_credential(
            db_session,
            user_id=user.id,
            label="first",
            access_key=access_key,
            secret_key="secret-1",
        )
        delete_credential(db_session, credential_id=first.id, user_id=user.id)

        # 再次 create 同 access_key → 应允许（软删除不阻塞）
        second = create_credential(
            db_session,
            user_id=user.id,
            label="reborn",
            access_key=access_key,
            secret_key="secret-1",
        )
        assert second.id != first.id

    def test_different_users_can_share_access_key(self, db_session) -> None:
        """不同 user 可以创建相同 access_key 的凭证（同 hash 但不同 user_id 不冲突）。"""
        u1 = _make_user(db_session, wallet="0xUSER1")
        u2 = _make_user(db_session, wallet="0xUSER2")
        ak = "SHARED-ACCESS-KEY"

        c1 = create_credential(
            db_session, user_id=u1.id, label="u1", access_key=ak, secret_key="s1"
        )
        # 不应抛 DuplicateCredentialError
        c2 = create_credential(
            db_session, user_id=u2.id, label="u2", access_key=ak, secret_key="s2"
        )
        assert c1.access_key_hash == c2.access_key_hash
        assert c1.user_id != c2.user_id


# ---------------------------------------------------------------------------
# 2. validate_credential：withdraw 覆盖 + 状态推导 + 状态机防御
# ---------------------------------------------------------------------------
class TestValidateCredential:
    """验证路径覆盖：withdraw 覆盖、READ_ONLY/TRADE_ENABLED/INVALID 推导、终态拒绝。"""

    def test_withdraw_forced_false_even_if_mock_returns_true(self, db_session) -> None:
        """**核心安全测试**：mock 验证器返回 ``withdraw=true``，
        服务层必须硬覆盖为 false（Req 2 AC4 / Req 15 AC6）。"""
        user = _make_user(db_session)
        cred = create_credential(
            db_session, user_id=user.id, label="x", access_key="ak", secret_key="sk"
        )

        # 默认 _mock_validate 已经返回 read=true / trade=true / withdraw=true
        validated = validate_credential(
            db_session, credential_id=cred.id, user_id=user.id
        )

        assert validated.permission_read is True
        assert validated.permission_trade is True
        # 关键断言：withdraw 必须为 False
        assert validated.permission_withdraw is False
        assert validated.state == CredentialState.TRADE_ENABLED
        assert validated.last_validated_at is not None
        assert isinstance(validated.last_validated_at, datetime)

    def test_audit_event_records_withdraw_override(self, db_session) -> None:
        """CREDENTIAL_VALIDATED 审计事件应包含 ``withdraw_overridden=True`` 标记，
        作为安全边界的可追溯证据。"""
        user = _make_user(db_session)
        cred = create_credential(
            db_session, user_id=user.id, label="x", access_key="ak", secret_key="sk"
        )
        validate_credential(db_session, credential_id=cred.id, user_id=user.id)

        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.CREDENTIAL_VALIDATED
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        data = events[0].event_json["data"]
        assert data["withdraw_overridden"] is True
        assert data["new_state"] == CredentialState.TRADE_ENABLED
        assert data["permissions"]["withdraw"] is False

    @pytest.mark.parametrize(
        ("mock_return", "expected_state"),
        [
            ({"read": True, "trade": True, "withdraw": False}, CredentialState.TRADE_ENABLED),
            ({"read": True, "trade": False, "withdraw": False}, CredentialState.READ_ONLY),
            ({"read": False, "trade": False, "withdraw": False}, CredentialState.INVALID),
            # 即便 trade=true 而 read=false（外部异常返回），也按 TRADE_ENABLED 处理
            ({"read": False, "trade": True, "withdraw": False}, CredentialState.TRADE_ENABLED),
        ],
    )
    def test_state_derivation_from_permissions(
        self, db_session, mock_return: dict, expected_state: str
    ) -> None:
        """根据 read/trade 推导终态。"""
        user = _make_user(db_session)
        cred = create_credential(
            db_session, user_id=user.id, label="x", access_key="ak", secret_key="sk"
        )

        with patch.object(svc, "_mock_validate", return_value=mock_return):
            validated = validate_credential(
                db_session, credential_id=cred.id, user_id=user.id
            )
        assert validated.state == expected_state

    def test_validate_revoked_credential_raises_illegal_transition(
        self, db_session
    ) -> None:
        """REVOKED 凭证调用 validate → :class:`IllegalStateTransition`。"""
        user = _make_user(db_session)
        cred = create_credential(
            db_session, user_id=user.id, label="x", access_key="ak", secret_key="sk"
        )
        # 直接把 ORM 状态改成 REVOKED，模拟"曾经的 active 凭证被 revoke 后的状态"
        cred.state = CredentialState.REVOKED
        db_session.flush()

        with pytest.raises(IllegalStateTransition) as exc_info:
            validate_credential(db_session, credential_id=cred.id, user_id=user.id)
        assert exc_info.value.current == CredentialState.REVOKED
        assert exc_info.value.target == CredentialState.VALIDATING

    def test_validate_other_users_credential_raises_not_found(
        self, db_session
    ) -> None:
        """跨用户访问 → 404（不区分"不存在"与"不属于本人"）。"""
        owner = _make_user(db_session, wallet="0xOWNER")
        intruder = _make_user(db_session, wallet="0xINTRUDER")
        cred = create_credential(
            db_session, user_id=owner.id, label="x", access_key="ak", secret_key="sk"
        )

        with pytest.raises(CredentialNotFoundError):
            validate_credential(db_session, credential_id=cred.id, user_id=intruder.id)


# ---------------------------------------------------------------------------
# 3. list_credentials：过滤软删除 / 按用户隔离
# ---------------------------------------------------------------------------
class TestListCredentials:
    def test_lists_only_active_for_user(self, db_session) -> None:
        u1 = _make_user(db_session, wallet="0xLIST1")
        u2 = _make_user(db_session, wallet="0xLIST2")

        c1 = create_credential(
            db_session, user_id=u1.id, label="active-1", access_key="ak-u1", secret_key="s"
        )
        c2 = create_credential(
            db_session, user_id=u1.id, label="to-delete", access_key="ak-u1-2", secret_key="s"
        )
        # u2 自己的凭证不应进入 u1 的列表
        create_credential(
            db_session, user_id=u2.id, label="other-user", access_key="ak-u2", secret_key="s"
        )
        delete_credential(db_session, credential_id=c2.id, user_id=u1.id)

        result = list_credentials(db_session, user_id=u1.id)
        ids = [c.id for c in result]
        assert c1.id in ids
        assert c2.id not in ids  # 软删除被过滤
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 4. delete_credential：软删除而非物理删除
# ---------------------------------------------------------------------------
class TestDeleteCredential:
    def test_soft_delete_keeps_row_with_state_deleted(self, db_session) -> None:
        user = _make_user(db_session)
        cred = create_credential(
            db_session, user_id=user.id, label="x", access_key="ak", secret_key="sk"
        )
        cred_id = cred.id

        delete_credential(db_session, credential_id=cred_id, user_id=user.id)

        # 直接从数据库捞这一行——必须依然存在，state=DELETED + deleted_at 非空
        # 用 db.get 但绕过过滤逻辑（_get_owned_credential 会把软删除当 404）
        row = db_session.get(ApiCredential, cred_id)
        assert row is not None
        assert row.state == CredentialState.DELETED
        assert row.deleted_at is not None
        # 加密数据保留（不物理删除）
        assert row.encrypted_access_key  # bytes 非空
        assert row.encrypted_secret_key

    def test_delete_already_deleted_returns_not_found(self, db_session) -> None:
        """重复 delete 应当返回 404（软删除项对当前用户已不可见）。"""
        user = _make_user(db_session)
        cred = create_credential(
            db_session, user_id=user.id, label="x", access_key="ak", secret_key="sk"
        )
        delete_credential(db_session, credential_id=cred.id, user_id=user.id)

        with pytest.raises(CredentialNotFoundError):
            delete_credential(db_session, credential_id=cred.id, user_id=user.id)

    def test_delete_writes_audit_event(self, db_session) -> None:
        user = _make_user(db_session)
        cred = create_credential(
            db_session, user_id=user.id, label="x", access_key="ak", secret_key="sk"
        )
        delete_credential(db_session, credential_id=cred.id, user_id=user.id)

        events = (
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.CREDENTIAL_DELETED
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].event_json["data"]["credential_id"] == str(cred.id)

    def test_delete_revoked_credential_raises_illegal_transition(
        self, db_session
    ) -> None:
        """REVOKED → DELETED 不在转换表里，必须拒绝。"""
        user = _make_user(db_session)
        cred = create_credential(
            db_session, user_id=user.id, label="x", access_key="ak", secret_key="sk"
        )
        cred.state = CredentialState.REVOKED
        db_session.flush()

        with pytest.raises(IllegalStateTransition):
            delete_credential(db_session, credential_id=cred.id, user_id=user.id)
