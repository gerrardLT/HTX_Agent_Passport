"""任务 4.2 凭证管理路由集成测试。

通过 FastAPI ``TestClient`` 端到端验证：

- 路由 + Pydantic 序列化 + 服务层 + 数据库 + 审计事件 全链路
- 安全约束：响应永不含密钥明文 / 哈希；日志中也不含原文
- 错误映射：401 未授权 / 404 不属于本人 / 409 重复 / 409 状态冲突
- 状态机闭环：CREATE → VALIDATE → DELETE → 再次操作 404

复用 conftest 提供的 ``client`` / ``demo_user`` / ``auth_client`` fixtures
（已在 ``tests/conftest.py`` 中实现）。
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

# 测试需要直接读数据库做"加密入库"的存储侧断言
from app.core.vault import CredentialVault
from app.models import ApiCredential, AuditEvent
from app.models.enums import AuditEventType, CredentialState

# 测试常量：用容易识别的字符串便于 grep 断言
SAMPLE_ACCESS_KEY = "TEST-AK-1234567890"
SAMPLE_SECRET_KEY = "TEST-SK-1234567890-VERY-SECRET"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _create_via_api(
    auth_client: TestClient,
    *,
    label: str = "my-htx-key",
    access_key: str = SAMPLE_ACCESS_KEY,
    secret_key: str = SAMPLE_SECRET_KEY,
) -> dict:
    """快捷创建凭证；返回响应 body。"""
    resp = auth_client.post(
        "/api/credentials/htx",
        json={"label": label, "access_key": access_key, "secret_key": secret_key},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# 401 — 未授权
# ---------------------------------------------------------------------------
class TestUnauthorizedRequests:
    """所有路由都依赖 get_current_user；缺 token 一律 401。"""

    def test_create_without_token_returns_401(self, client: TestClient) -> None:
        resp = client.post(
            "/api/credentials/htx",
            json={"label": "x", "access_key": "x", "secret_key": "x"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHORIZED"

    def test_validate_without_token_returns_401(self, client: TestClient) -> None:
        resp = client.post(f"/api/credentials/{uuid.uuid4()}/validate")
        assert resp.status_code == 401

    def test_list_without_token_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/credentials")
        assert resp.status_code == 401

    def test_delete_without_token_returns_401(self, client: TestClient) -> None:
        resp = client.delete(f"/api/credentials/{uuid.uuid4()}")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/credentials/htx
# ---------------------------------------------------------------------------
class TestCreateCredentialEndpoint:
    """创建路径：201 + 响应不含密钥 + 数据库密文 + 审计事件。"""

    def test_create_returns_201_without_any_key_field_in_response(
        self, auth_client: TestClient
    ) -> None:
        """响应 status=201，body 不含 access_key / secret_key / 哈希等任何字段。

        把响应 body 序列化成字符串后，原始 access_key / secret_key 字面量
        都不应出现，保证 PRD §15 安全约束。
        """
        resp = auth_client.post(
            "/api/credentials/htx",
            json={
                "label": "demo-key",
                "access_key": SAMPLE_ACCESS_KEY,
                "secret_key": SAMPLE_SECRET_KEY,
            },
        )
        assert resp.status_code == 201
        body = resp.json()

        # 字段白名单：响应必须有的字段
        assert body["id"]
        assert body["provider"] == "HTX"
        assert body["label"] == "demo-key"
        assert body["state"] == CredentialState.CREATED
        assert body["permissions"] == {"read": False, "trade": False, "withdraw": False}
        assert body["last_validated_at"] is None
        assert body["deleted_at"] is None

        # 字段黑名单：响应 JSON 字符串里都不应出现密钥相关字段或值
        serialized = json.dumps(body)
        assert "access_key" not in serialized
        assert "secret_key" not in serialized
        # 重点：原始密钥字面量也不应出现（防止字段名变化但值仍泄露）
        assert SAMPLE_ACCESS_KEY not in serialized
        assert SAMPLE_SECRET_KEY not in serialized
        assert "access_key_hash" not in serialized
        assert "encrypted_access_key" not in serialized
        assert "encrypted_secret_key" not in serialized

    def test_create_persists_encrypted_secret_with_correct_hash(
        self, auth_client: TestClient, sqlite_engine
    ) -> None:
        """数据库行：access_key_hash = SHA-256(access_key)；
        encrypted_* 是 bytes 密文（不含原文）。"""
        body = _create_via_api(auth_client)
        cred_id = uuid.UUID(body["id"])

        from sqlalchemy.orm import Session

        with Session(sqlite_engine) as session:
            cred = session.get(ApiCredential, cred_id)
            assert cred is not None
            # access_key_hash 是 SHA-256（确定性可验证）
            expected_hash = CredentialVault.hash_access_key(SAMPLE_ACCESS_KEY)
            assert cred.access_key_hash == expected_hash
            # encrypted_secret_key / encrypted_access_key 是 bytes 密文
            assert isinstance(cred.encrypted_access_key, bytes)
            assert isinstance(cred.encrypted_secret_key, bytes)
            # 密文里不含明文（语义安全可见证）
            assert SAMPLE_ACCESS_KEY.encode() not in cred.encrypted_access_key
            assert SAMPLE_SECRET_KEY.encode() not in cred.encrypted_secret_key
            # encryption_algorithm 标注 AES-256-GCM
            assert cred.encryption_algorithm == "AES-256-GCM"
            # 软删除字段未设置
            assert cred.deleted_at is None

    def test_duplicate_create_returns_409_duplicate_credential(
        self, auth_client: TestClient
    ) -> None:
        """重复 POST 同一 access_key → 409 + ``code="DUPLICATE_CREDENTIAL"``。"""
        first = _create_via_api(auth_client)

        # 第二次同 access_key 必须 409
        resp = auth_client.post(
            "/api/credentials/htx",
            json={
                "label": "another-label",
                "access_key": SAMPLE_ACCESS_KEY,  # 同一 access_key
                "secret_key": "different-secret",
            },
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "DUPLICATE_CREDENTIAL"
        # details 包含已存在凭证 id，便于前端引导
        assert body["error"]["details"]["existing_credential_id"] == first["id"]

    def test_create_writes_audit_event(
        self, auth_client: TestClient, sqlite_engine
    ) -> None:
        body = _create_via_api(auth_client)
        from sqlalchemy.orm import Session

        with Session(sqlite_engine) as session:
            events = (
                session.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == AuditEventType.CREDENTIAL_CREATED
                    )
                )
                .scalars()
                .all()
            )
        assert len(events) == 1
        # event_data 含 credential_id 但不含密钥
        data = events[0].event_json["data"]
        assert data["credential_id"] == body["id"]
        assert SAMPLE_ACCESS_KEY not in json.dumps(data)
        assert SAMPLE_SECRET_KEY not in json.dumps(data)

    def test_extra_fields_in_request_rejected(self, auth_client: TestClient) -> None:
        """``ConfigDict(extra='forbid')`` 拒绝多余字段，包括试图设置 permission_withdraw。"""
        resp = auth_client.post(
            "/api/credentials/htx",
            json={
                "label": "x",
                "access_key": "ak",
                "secret_key": "sk",
                "permission_withdraw": True,  # 试图绕过 Req 15 AC6
            },
        )
        # pydantic 422 VALIDATION_ERROR
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/credentials/{id}/validate
# ---------------------------------------------------------------------------
class TestValidateCredentialEndpoint:
    def test_validate_returns_trade_enabled_with_withdraw_false(
        self, auth_client: TestClient, sqlite_engine
    ) -> None:
        """**核心安全测试**：mock 验证器返回 withdraw=true，
        响应里 permissions.withdraw 必须为 False，state=TRADE_ENABLED（Req 2 AC4）。"""
        created = _create_via_api(auth_client)

        resp = auth_client.post(f"/api/credentials/{created['id']}/validate")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == created["id"]
        assert body["state"] == CredentialState.TRADE_ENABLED
        assert body["permissions"] == {
            "read": True,
            "trade": True,
            "withdraw": False,  # 关键：硬覆盖
        }

        # 数据库侧也确认 last_validated_at 非空
        from sqlalchemy.orm import Session

        with Session(sqlite_engine) as session:
            cred = session.get(ApiCredential, uuid.UUID(created["id"]))
            assert cred is not None
            assert cred.last_validated_at is not None
            assert cred.permission_withdraw is False

    def test_validate_other_users_credential_returns_404(
        self, auth_client: TestClient, sqlite_engine, client: TestClient
    ) -> None:
        """跨用户访问 → 404，避免存在性侧信道。"""
        # demo_user A 通过 auth_client 创建凭证
        created = _create_via_api(auth_client)
        cred_id = created["id"]

        # 切换到另一个 user：用同一 client 但不同 wallet 登录拿新 token
        resp_b = client.post(
            "/api/auth/demo-login", json={"wallet": "0xOTHERUSER0000000000000000000000000000B"}
        )
        assert resp_b.status_code == 200
        token_b = resp_b.json()["token"]

        # 用 user B 的 token 尝试访问 user A 的凭证
        resp = client.post(
            f"/api/credentials/{cred_id}/validate",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_validate_nonexistent_credential_returns_404(
        self, auth_client: TestClient
    ) -> None:
        resp = auth_client.post(f"/api/credentials/{uuid.uuid4()}/validate")
        assert resp.status_code == 404

    def test_validate_writes_audit_event(
        self, auth_client: TestClient, sqlite_engine
    ) -> None:
        created = _create_via_api(auth_client)
        auth_client.post(f"/api/credentials/{created['id']}/validate")

        from sqlalchemy.orm import Session

        with Session(sqlite_engine) as session:
            events = (
                session.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == AuditEventType.CREDENTIAL_VALIDATED
                    )
                )
                .scalars()
                .all()
            )
        assert len(events) == 1
        data = events[0].event_json["data"]
        assert data["new_state"] == CredentialState.TRADE_ENABLED
        # 关键：审计事件显式记录 withdraw 被覆盖
        assert data["withdraw_overridden"] is True
        assert data["permissions"]["withdraw"] is False


# ---------------------------------------------------------------------------
# GET /api/credentials
# ---------------------------------------------------------------------------
class TestListCredentialsEndpoint:
    def test_list_returns_only_active_credentials(
        self, auth_client: TestClient
    ) -> None:
        """软删除后的凭证不应出现在列表里。"""
        c1 = _create_via_api(auth_client, label="active", access_key="ak-active")
        c2 = _create_via_api(auth_client, label="to-delete", access_key="ak-deleted")

        # 删除 c2
        del_resp = auth_client.delete(f"/api/credentials/{c2['id']}")
        assert del_resp.status_code == 200

        resp = auth_client.get("/api/credentials")
        assert resp.status_code == 200
        body = resp.json()
        ids = [c["id"] for c in body["credentials"]]
        assert c1["id"] in ids
        assert c2["id"] not in ids
        assert len(body["credentials"]) == 1

    def test_list_isolates_by_user(
        self, auth_client: TestClient, client: TestClient
    ) -> None:
        """user A 的列表里不出现 user B 的凭证。"""
        # user A（auth_client 已绑定）创建一条
        _create_via_api(auth_client, label="a-key", access_key="ak-A")

        # user B 登录创建一条
        resp_b = client.post(
            "/api/auth/demo-login", json={"wallet": "0xUSERB000000000000000000000000000000B"}
        )
        token_b = resp_b.json()["token"]
        client.post(
            "/api/credentials/htx",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"label": "b-key", "access_key": "ak-B", "secret_key": "sk-B"},
        )

        # user A 的列表只看到自己那条
        resp_a = auth_client.get("/api/credentials")
        body_a = resp_a.json()
        assert len(body_a["credentials"]) == 1
        assert body_a["credentials"][0]["label"] == "a-key"

        # user B 的列表只看到自己那条
        resp_b2 = client.get(
            "/api/credentials", headers={"Authorization": f"Bearer {token_b}"}
        )
        body_b = resp_b2.json()
        assert len(body_b["credentials"]) == 1
        assert body_b["credentials"][0]["label"] == "b-key"

    def test_list_response_does_not_contain_secrets(
        self, auth_client: TestClient
    ) -> None:
        _create_via_api(auth_client)
        resp = auth_client.get("/api/credentials")
        serialized = json.dumps(resp.json())
        assert SAMPLE_ACCESS_KEY not in serialized
        assert SAMPLE_SECRET_KEY not in serialized
        assert "secret_key" not in serialized
        assert "access_key" not in serialized


# ---------------------------------------------------------------------------
# DELETE /api/credentials/{id}
# ---------------------------------------------------------------------------
class TestDeleteCredentialEndpoint:
    def test_delete_returns_state_deleted_and_persists_soft_delete(
        self, auth_client: TestClient, sqlite_engine
    ) -> None:
        created = _create_via_api(auth_client)
        cred_id = uuid.UUID(created["id"])

        resp = auth_client.delete(f"/api/credentials/{created['id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == created["id"]
        assert body["state"] == CredentialState.DELETED
        assert body["deleted_at"] is not None

        # 数据库行依然存在（软删除而非物理删除）
        from sqlalchemy.orm import Session

        with Session(sqlite_engine) as session:
            row = session.get(ApiCredential, cred_id)
            assert row is not None
            assert row.state == CredentialState.DELETED
            assert row.deleted_at is not None
            # 加密数据保留
            assert row.encrypted_secret_key

    def test_delete_then_get_excludes_credential(self, auth_client: TestClient) -> None:
        created = _create_via_api(auth_client)
        auth_client.delete(f"/api/credentials/{created['id']}")
        resp = auth_client.get("/api/credentials")
        ids = [c["id"] for c in resp.json()["credentials"]]
        assert created["id"] not in ids

    def test_delete_already_deleted_returns_404_or_409(
        self, auth_client: TestClient
    ) -> None:
        """重复 DELETE：当前实现把"已软删除"视为 404（不属于活跃凭证视图）。

        测试任选其一稳定即可——这里我们的实现选择 404（_get_owned_credential
        把软删除项过滤掉），路由文档已说明这一点。
        """
        created = _create_via_api(auth_client)
        first = auth_client.delete(f"/api/credentials/{created['id']}")
        assert first.status_code == 200

        second = auth_client.delete(f"/api/credentials/{created['id']}")
        assert second.status_code == 404
        assert second.json()["error"]["code"] == "NOT_FOUND"

    def test_delete_writes_audit_event(
        self, auth_client: TestClient, sqlite_engine
    ) -> None:
        created = _create_via_api(auth_client)
        auth_client.delete(f"/api/credentials/{created['id']}")

        from sqlalchemy.orm import Session

        with Session(sqlite_engine) as session:
            events = (
                session.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == AuditEventType.CREDENTIAL_DELETED
                    )
                )
                .scalars()
                .all()
            )
        assert len(events) == 1
        assert events[0].event_json["data"]["credential_id"] == created["id"]


# ---------------------------------------------------------------------------
# 全链路：CREATE → VALIDATE → DELETE 串联 + 全程审计
# ---------------------------------------------------------------------------
class TestEndToEndCredentialLifecycle:
    def test_full_lifecycle_writes_three_audit_events(
        self, auth_client: TestClient, sqlite_engine
    ) -> None:
        created = _create_via_api(auth_client)
        auth_client.post(f"/api/credentials/{created['id']}/validate")
        auth_client.delete(f"/api/credentials/{created['id']}")

        from sqlalchemy.orm import Session

        with Session(sqlite_engine) as session:
            events = (
                session.execute(
                    select(AuditEvent).order_by(AuditEvent.created_at.asc())
                )
                .scalars()
                .all()
            )
        # USER_LOGIN（demo_user fixture 触发） + CREDENTIAL_CREATED + CREDENTIAL_VALIDATED + CREDENTIAL_DELETED
        types = [e.event_type for e in events]
        assert AuditEventType.CREDENTIAL_CREATED in types
        assert AuditEventType.CREDENTIAL_VALIDATED in types
        assert AuditEventType.CREDENTIAL_DELETED in types


# ---------------------------------------------------------------------------
# 日志 / 输出层面：原始密钥永不出现
# ---------------------------------------------------------------------------
class TestNoSecretLeakInLogs:
    """Req 15 AC1：日志中绝不能出现 access_key / secret_key 原文。"""

    def test_no_secret_leaks_in_stdout_or_logs(
        self,
        auth_client: TestClient,
        capsys: pytest.CaptureFixture,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """完整 lifecycle 跑一遍，扫描 stdout / stderr / log 不含原始密钥。"""
        # 让 caplog 捕获 INFO 及以上级别（服务层的 logger.info 调用）
        caplog.set_level(logging.DEBUG)

        unique_access_key = "AK-LEAK-CHECK-XYZ987"
        unique_secret_key = "SK-LEAK-CHECK-XYZ987-VERY-SECRET"

        # CREATE → VALIDATE → DELETE 完整链路
        created = auth_client.post(
            "/api/credentials/htx",
            json={
                "label": "leak-check",
                "access_key": unique_access_key,
                "secret_key": unique_secret_key,
            },
        ).json()
        auth_client.post(f"/api/credentials/{created['id']}/validate")
        auth_client.delete(f"/api/credentials/{created['id']}")

        # capsys 捕获 stdout/stderr；caplog 捕获 logging
        captured = capsys.readouterr()

        haystacks = [captured.out, captured.err, caplog.text]
        for h in haystacks:
            assert unique_access_key not in h, (
                f"原始 access_key 出现在输出中: {h[:200]!r}"
            )
            assert unique_secret_key not in h, (
                f"原始 secret_key 出现在输出中: {h[:200]!r}"
            )
