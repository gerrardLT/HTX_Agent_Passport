"""S3 Object Lock STH 锚定 backend 测试（Phase 2.5）。

**Validates: docs/tech-research/05-production-upgrades.md §5.2**
—— 把本地 JSONL 锚定升级到 S3 Object Lock Compliance 模式，让"删除 +
重写全链"攻击窗口被关闭。

测试策略
--------
用 ``moto`` 库 mock S3 服务（无网络，纯进程内）：
- ``mock_aws`` decorator：所有 boto3 S3 调用都路由到内存模拟器。
- 桶启用 ``ObjectLockEnabled=True``（与生产配置同款）。
- 写入后用 ``head_object`` / ``get_object_lock_configuration`` 验证落地状态。

不测的部分
----------
- 真实 AWS 环境的 IAM / KMS 集成（要 AWS 账号）。
- 跨 region 复制（实测意义有限，AWS 自身保证）。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

# moto 的 mock_aws 是 5.x 主入口
moto = pytest.importorskip("moto")
import boto3
from moto import mock_aws

from app.core.config import get_settings
from app.models import AuditTreeHead
from app.services.audit_sth_anchor import (
    JsonLineFileAnchorBackend,
    NullAnchorBackend,
    S3ObjectLockAnchorBackend,
    build_sth_record,
    get_default_anchor_backend,
    is_duplicate_record,
)


# ---------------------------------------------------------------------------
# 共享工厂 / fixture
# ---------------------------------------------------------------------------
def _make_sth(
    *,
    user_id: uuid.UUID | None = None,
    passport_id: uuid.UUID | None = None,
    tree_size: int = 5,
    root_hash: str = "a" * 64,
    signature: str = "ed25519:" + "b" * 128,
    signed_at: datetime | None = None,
) -> AuditTreeHead:
    """构造未持久化但字段就绪的 STH，供 anchor 测试使用。"""
    return AuditTreeHead(
        user_id=user_id or uuid.uuid4(),
        passport_id=passport_id,
        tree_size=tree_size,
        root_hash=root_hash,
        signature=signature,
        signed_at=signed_at or datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC),
    )


@pytest.fixture()
def s3_with_object_lock():
    """给一个启用 Object Lock 的 mock S3 桶；yield (client, bucket_name)。"""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        bucket = "htx-passport-audit-anchor-test"
        client.create_bucket(
            Bucket=bucket,
            ObjectLockEnabledForBucket=True,
        )
        yield client, bucket


# ===========================================================================
# 1. 共享工具函数
# ===========================================================================
class TestSharedHelpers:
    def test_build_sth_record_complete_fields(self) -> None:
        """build_sth_record 输出字段完整 + 类型正确。"""
        sth = _make_sth(
            tree_size=42,
            root_hash="c" * 64,
            signature="hmac:" + "d" * 64,
        )
        record = build_sth_record(sth)
        assert set(record.keys()) == {
            "signed_at", "user_id", "passport_id",
            "tree_size", "root_hash", "signature",
        }
        assert record["tree_size"] == 42
        assert record["root_hash"] == "c" * 64
        assert record["signature"] == "hmac:" + "d" * 64

    def test_build_sth_record_passport_id_none(self) -> None:
        """passport_id=None 序列化为 JSON null（不是字符串 "None"）。"""
        sth = _make_sth(passport_id=None)
        record = build_sth_record(sth)
        assert record["passport_id"] is None

    def test_is_duplicate_record_quadruple_match(self) -> None:
        """四元组完全相同 → 重复。"""
        last = {
            "user_id": "u1", "passport_id": None,
            "tree_size": 5, "root_hash": "a" * 64,
            "signed_at": "2026-01-01T00:00:00+00:00",
            "signature": "x",
        }
        new = {
            "user_id": "u1", "passport_id": None,
            "tree_size": 5, "root_hash": "a" * 64,
            "signed_at": "2026-05-31T12:00:00+00:00",  # 时间不同也算重复
            "signature": "y",  # 签名不同也算重复
        }
        assert is_duplicate_record(last, new) is True

    def test_is_duplicate_record_root_diff_not_dupe(self) -> None:
        last = {"user_id": "u1", "passport_id": None, "tree_size": 5, "root_hash": "a" * 64}
        new = {"user_id": "u1", "passport_id": None, "tree_size": 5, "root_hash": "b" * 64}
        assert is_duplicate_record(last, new) is False


# ===========================================================================
# 2. S3ObjectLockAnchorBackend 构造与配置
# ===========================================================================
class TestS3BackendConstruction:
    def test_empty_bucket_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty bucket"):
            S3ObjectLockAnchorBackend(bucket="")

    def test_negative_retention_raises(self) -> None:
        with pytest.raises(ValueError, match="retention_years"):
            S3ObjectLockAnchorBackend(bucket="x", retention_years=0)

    def test_accepts_injected_client(self) -> None:
        """**测试友好**：可注入 mock client，不强制初始化 boto3。"""
        fake_client = object()
        backend = S3ObjectLockAnchorBackend(
            bucket="x", s3_client=fake_client
        )
        assert backend._client is fake_client


# ===========================================================================
# 3. S3ObjectLockAnchorBackend 端到端写入（moto）
# ===========================================================================
class TestS3AnchorWrite:
    """**Validates: G10/G11 物理不可变性升级核心**——写入 S3 + retention 验证。"""

    def test_anchor_writes_object_with_object_lock(
        self, s3_with_object_lock: tuple[Any, str]
    ) -> None:
        """anchor 后 S3 应能 head_object + get_object_retention 返回 COMPLIANCE。"""
        client, bucket = s3_with_object_lock
        backend = S3ObjectLockAnchorBackend(
            bucket=bucket,
            retention_years=7,
            s3_client=client,
        )
        sth = _make_sth(tree_size=5, root_hash="a" * 64)

        ok = backend.anchor(sth)
        assert ok is True

        # 列出对象，确认 1 个 key 已写入
        resp = client.list_objects_v2(Bucket=bucket, Prefix="sth/")
        assert resp.get("KeyCount", 0) == 1

        # head_object 验证 retention 模式
        key = resp["Contents"][0]["Key"]
        head = client.head_object(Bucket=bucket, Key=key)
        assert head["ObjectLockMode"] == "COMPLIANCE"
        # retention 时间在 6.5-8 年范围内（7 年 ± 容差）
        retain_until = head["ObjectLockRetainUntilDate"]
        now = datetime.now(UTC)
        delta_days = (retain_until - now).days
        assert 365 * 6 <= delta_days <= 365 * 8

    def test_anchor_object_body_is_canonical_json(
        self, s3_with_object_lock: tuple[Any, str]
    ) -> None:
        """对象 body 是 canonical JSON——含全部 6 个字段，外部审计可直接消费。"""
        client, bucket = s3_with_object_lock
        backend = S3ObjectLockAnchorBackend(bucket=bucket, s3_client=client)
        sth = _make_sth(tree_size=10, root_hash="c" * 64)
        backend.anchor(sth)

        resp = client.list_objects_v2(Bucket=bucket, Prefix="sth/")
        key = resp["Contents"][0]["Key"]
        obj = client.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        assert obj["ContentType"] == "application/json"
        record = json.loads(body)
        assert record["tree_size"] == 10
        assert record["root_hash"] == "c" * 64

    def test_anchor_idempotent_second_call_skipped(
        self, s3_with_object_lock: tuple[Any, str]
    ) -> None:
        """同 STH 重复 anchor → 第二次返回 False；S3 中仍只有 1 个对象。"""
        client, bucket = s3_with_object_lock
        backend = S3ObjectLockAnchorBackend(bucket=bucket, s3_client=client)
        sth = _make_sth()

        ok1 = backend.anchor(sth)
        ok2 = backend.anchor(sth)
        assert ok1 is True
        assert ok2 is False

        resp = client.list_objects_v2(Bucket=bucket, Prefix="sth/")
        assert resp.get("KeyCount", 0) == 1

    def test_anchor_two_distinct_sths_two_keys(
        self, s3_with_object_lock: tuple[Any, str]
    ) -> None:
        """不同 tree_size → 不同 key → 都写入。"""
        client, bucket = s3_with_object_lock
        backend = S3ObjectLockAnchorBackend(bucket=bucket, s3_client=client)

        backend.anchor(_make_sth(tree_size=1, root_hash="a" * 64))
        backend.anchor(_make_sth(tree_size=2, root_hash="b" * 64))

        resp = client.list_objects_v2(Bucket=bucket, Prefix="sth/")
        assert resp.get("KeyCount", 0) == 2

    def test_key_format_includes_tree_size_zero_padded(
        self, s3_with_object_lock: tuple[Any, str]
    ) -> None:
        """tree_size 用 020d 零填充，让 S3 字典序 = 数值序。"""
        client, bucket = s3_with_object_lock
        backend = S3ObjectLockAnchorBackend(bucket=bucket, s3_client=client)
        backend.anchor(_make_sth(tree_size=42))

        resp = client.list_objects_v2(Bucket=bucket, Prefix="sth/")
        key = resp["Contents"][0]["Key"]
        # 类似 sth/<uid>/_root/00000000000000000042-2026-05-31T12-00-00+00-00.json
        assert "00000000000000000042" in key

    def test_passport_id_in_key_when_set(
        self, s3_with_object_lock: tuple[Any, str]
    ) -> None:
        """passport_id 不为 None 时进 key 路径；为 None 时用 _root 占位。"""
        client, bucket = s3_with_object_lock
        backend = S3ObjectLockAnchorBackend(bucket=bucket, s3_client=client)
        passport_uid = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

        backend.anchor(_make_sth(passport_id=passport_uid, tree_size=1))
        backend.anchor(_make_sth(passport_id=None, tree_size=2))

        resp = client.list_objects_v2(Bucket=bucket, Prefix="sth/")
        keys = [obj["Key"] for obj in resp["Contents"]]
        assert any(str(passport_uid) in k for k in keys)
        assert any("_root" in k for k in keys)


# ===========================================================================
# 4. 失败静默语义
# ===========================================================================
class TestS3FailureSwallowed:
    """boto3 异常一律吞错 + 返回 False，不向上抛——保护主路径。"""

    def test_put_failure_returns_false(self) -> None:
        """注入一个 put_object 总是抛 ClientError 的 fake client。"""

        class _FaultyClient:
            def head_object(self, **_: Any) -> Any:
                # 模拟 404 Not Found（key 不存在,允许 PUT）
                from botocore.exceptions import ClientError
                raise ClientError(
                    {"Error": {"Code": "404", "Message": "Not Found"}},
                    "HeadObject",
                )

            def put_object(self, **_: Any) -> Any:
                from botocore.exceptions import ClientError
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "Denied"}},
                    "PutObject",
                )

        backend = S3ObjectLockAnchorBackend(
            bucket="x", s3_client=_FaultyClient()
        )
        ok = backend.anchor(_make_sth())
        assert ok is False  # 不抛，安静返回


# ===========================================================================
# 5. 工厂 get_default_anchor_backend
# ===========================================================================
class TestDefaultAnchorBackendFactory:
    def test_default_jsonl_with_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """``BACKEND=jsonl`` + 路径 → JsonLineFileAnchorBackend。"""
        monkeypatch.setenv("AUDIT_STH_ANCHOR_BACKEND", "jsonl")
        monkeypatch.setenv("AUDIT_STH_ANCHOR_PATH", str(tmp_path / "sth.jsonl"))
        get_settings.cache_clear()
        try:
            backend = get_default_anchor_backend()
            assert isinstance(backend, JsonLineFileAnchorBackend)
        finally:
            get_settings.cache_clear()

    def test_null_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUDIT_STH_ANCHOR_BACKEND", "null")
        get_settings.cache_clear()
        try:
            backend = get_default_anchor_backend()
            assert isinstance(backend, NullAnchorBackend)
        finally:
            get_settings.cache_clear()

    def test_unknown_backend_falls_back_to_null(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未知 backend 名 → NullAnchorBackend（不抛错，让 scheduler 继续跑）。"""
        monkeypatch.setenv("AUDIT_STH_ANCHOR_BACKEND", "unknown-backend-name")
        get_settings.cache_clear()
        try:
            backend = get_default_anchor_backend()
            assert isinstance(backend, NullAnchorBackend)
        finally:
            get_settings.cache_clear()

    def test_s3_backend_without_bucket_falls_back_to_null(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BACKEND=s3`` 但 bucket 为空 → NullAnchorBackend + 错误日志。"""
        monkeypatch.setenv("AUDIT_STH_ANCHOR_BACKEND", "s3")
        monkeypatch.setenv("AUDIT_STH_ANCHOR_S3_BUCKET", "")
        get_settings.cache_clear()
        try:
            backend = get_default_anchor_backend()
            assert isinstance(backend, NullAnchorBackend)
        finally:
            get_settings.cache_clear()

    def test_s3_backend_with_full_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """全配置正确 → S3ObjectLockAnchorBackend 实例。

        本测试不实际调 AWS——boto3 client 构造不会发网络请求,
        只在调 ``anchor()`` 时才请求；构造路径走通即足够。
        """
        with mock_aws():
            monkeypatch.setenv("AUDIT_STH_ANCHOR_BACKEND", "s3")
            monkeypatch.setenv("AUDIT_STH_ANCHOR_S3_BUCKET", "test-bucket")
            monkeypatch.setenv("AUDIT_STH_ANCHOR_S3_RETENTION_YEARS", "10")
            monkeypatch.setenv("AUDIT_STH_ANCHOR_S3_REGION", "us-east-1")
            get_settings.cache_clear()
            try:
                backend = get_default_anchor_backend()
                assert isinstance(backend, S3ObjectLockAnchorBackend)
                assert backend.bucket == "test-bucket"
                assert backend.retention_years == 10
            finally:
                get_settings.cache_clear()
