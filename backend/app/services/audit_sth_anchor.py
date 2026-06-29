"""STH 外部锚定模块（Phase 1 / Phase 2.5）。

职责
----
把周期签发的 :class:`AuditTreeHead` 追加到外部介质（不可被 DBA 篡改的载体），
作为"删除 + 重写全链"攻击的最后一道密码学锁。设计为可插拔后端：

- :class:`JsonLineFileAnchorBackend`（默认 / dev / CI）—— 本地 JSONL 文件。
- :class:`S3ObjectLockAnchorBackend`（生产推荐）—— AWS S3 + Object Lock
  Compliance 模式。Compliance 模式下连 root 账号也无法在 retention 期内
  删除或覆盖对象——这是比 DB 行锁强得多的物理不可变性保证。

未来可扩展：
- ``GitAppendOnlyAnchorBackend``：commit 到 GitHub/Gitea 的 protected branch。
- ``OpenTimestampsAnchorBackend``：聚合多 STH 的 root 写到 Bitcoin OP_RETURN。

抽象层 :class:`AnchorBackend`（ABC）确保业务调用方（``audit_sth_scheduler`` /
audit 路由）感知不到具体后端，配置切换即可上线新介质。

向后兼容
--------
- 原 :func:`anchor_sth_to_file` 函数保留，内部委托给 :class:`JsonLineFileAnchorBackend`。
- 全部既有调用点（scheduler / audit 路由 ``POST /sth/issue``）保持原签名,
  逐步迁移到 :func:`get_default_anchor_backend` 工厂。

幂等 + 失败静默
---------------
- 同一 STH（user_id, passport_id, tree_size, root_hash）四元组重复 anchor → 跳过。
- 任何 I/O / 网络 / 权限异常一律 ``logger.error`` 后返回 ``False``，
  **不向上抛**——锚定是辅助证据，不能拖垮主路径（scheduler / API 调用）。

设计依据：``docs/tech-research/05-production-upgrades.md`` §5.2。
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models import AuditTreeHead

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 共享：把 STH 序列化为 JSON dict（所有 backend 共用）
# ---------------------------------------------------------------------------
def build_sth_record(sth: AuditTreeHead) -> dict[str, Any]:
    """把 :class:`AuditTreeHead` 序列化为 backend 通用的 dict。

    所有 backend 调用本函数生成 payload，保证锚定到不同介质（文件 / S3 / git）
    时**字段格式完全一致**——外部审计员可对任意来源做交叉对账。
    """
    return {
        "signed_at": sth.signed_at.isoformat(),
        "user_id": str(sth.user_id),
        "passport_id": (
            str(sth.passport_id) if sth.passport_id is not None else None
        ),
        "tree_size": int(sth.tree_size),
        "root_hash": sth.root_hash,
        "signature": sth.signature,
    }


def is_duplicate_record(
    last: dict[str, Any] | None, new: dict[str, Any]
) -> bool:
    """判断新 STH 是否与最后一条锚定记录是同一份。

    比较 ``(user_id, passport_id, tree_size, root_hash)`` 四元组。``signed_at``
    与 ``signature`` 不参与比较——同 root_hash 不可能由 HMAC/Ed25519 之外的
    输入伪造（密钥保密的前提下）。
    """
    if last is None:
        return False
    return (
        last.get("user_id") == new["user_id"]
        and last.get("passport_id") == new["passport_id"]
        and last.get("tree_size") == new["tree_size"]
        and last.get("root_hash") == new["root_hash"]
    )


# ---------------------------------------------------------------------------
# AnchorBackend ABC
# ---------------------------------------------------------------------------
class AnchorBackend(ABC):
    """STH 锚定后端抽象接口。

    实现方负责：
    1. 持久化 STH 到外部介质（不可被 DBA 篡改）。
    2. 失败时**吞错** + 写日志 + 返回 ``False``——绝不向上抛影响主路径。
    3. 幂等检查：同 ``(user_id, passport_id, tree_size, root_hash)`` 四元组
       重复 anchor 应跳过（避免堆积冗余记录）。

    本接口不限定 anchor 是同步还是异步——目前所有实现都是同步阻塞，scheduler
    在线程池内调用。
    """

    @abstractmethod
    def anchor(self, sth: AuditTreeHead) -> bool:
        """把单条 STH 写到外部介质。

        Returns
        -------
        bool
            ``True``：写入成功（新 anchor 落地）。
            ``False``：未写入（重复 / 失败 / 未配置）。
        """
        ...


class NullAnchorBackend(AnchorBackend):
    """空操作 backend——锚定未配置时使用，永远返回 False。

    比"判 anchor_path 是否为空"更优雅：调用方 always 调 ``backend.anchor()``,
    backend 自身决定行为。配合 :func:`get_default_anchor_backend` 让"未配置时
    跳过"成为 backend 自己的责任。
    """

    def anchor(self, sth: AuditTreeHead) -> bool:  # noqa: ARG002 — interface
        return False


# ---------------------------------------------------------------------------
# JsonLineFileAnchorBackend —— 默认 / dev / CI
# ---------------------------------------------------------------------------
class JsonLineFileAnchorBackend(AnchorBackend):
    """本地 JSONL 文件 backend（dev / CI / 单机部署）。

    每条 STH 作为一行 JSON 追加到文件末尾。unix 工具友好（``tail -f`` /
    ``grep`` / ``jq -c`` 直接消费）。父目录不存在时自动创建。

    威胁模型限制
    -------------
    本 backend 不能防御**同主机攻击者**改写文件——任何能写文件的进程都能改。
    生产部署应：
    1. 把锚定文件放在只读挂载点 / append-only 文件系统（如 ext4 chattr +a）；
    2. 或换用 :class:`S3ObjectLockAnchorBackend`（推荐）。
    """

    def __init__(self, anchor_path: str | Path) -> None:
        self.anchor_path = Path(anchor_path) if anchor_path else None

    def anchor(self, sth: AuditTreeHead) -> bool:
        if not self.anchor_path:
            return False
        path = self.anchor_path
        new_record = build_sth_record(sth)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            last = self._read_last_record(path)
            if is_duplicate_record(last, new_record):
                logger.debug(
                    "STH anchor skip (duplicate): user=%s passport=%s size=%d",
                    new_record["user_id"], new_record["passport_id"],
                    new_record["tree_size"],
                )
                return False

            line = json.dumps(new_record, ensure_ascii=False, sort_keys=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
            logger.info(
                "STH anchored to %s: user=%s passport=%s size=%d",
                path, new_record["user_id"], new_record["passport_id"],
                new_record["tree_size"],
            )
            return True
        except (OSError, ValueError, TypeError) as exc:
            logger.error(
                "STH JSONL anchor write failed for %s: %s (sth_id=%s)",
                self.anchor_path, exc, sth.id,
            )
            return False

    @staticmethod
    def _read_last_record(path: Path) -> dict[str, Any] | None:
        """读取 JSONL 文件最后一行；不存在 / 空 / 解析失败 → None。"""
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("failed to read STH anchor file %s: %s", path, exc)
            return None
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return None
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            logger.warning(
                "malformed last line in STH anchor file %s: %s", path, exc
            )
            return None


# ---------------------------------------------------------------------------
# S3ObjectLockAnchorBackend —— 生产推荐
# ---------------------------------------------------------------------------
class S3ObjectLockAnchorBackend(AnchorBackend):
    """AWS S3 + Object Lock Compliance 模式的 STH 锚定（生产推荐）。

    每条 STH 写为一个独立 S3 对象，key 格式：

        sth/{user_id}/{passport_id_or_root}/{tree_size:020d}-{signed_at_iso}.json

    桶必须**预先**启用 ``ObjectLockEnabled`` + Versioning。Compliance 模式下
    连 root 账号也不能在 retention 期内删除或覆盖对象——这是比 DB 行锁
    强得多的物理不可变性保证。

    幂等
    ----
    S3 PUT 同 key 默认覆盖；为保证幂等，写之前用 ``head_object`` 查 key 是否
    存在，存在则跳过。Object Lock Compliance 模式下覆盖请求会被服务端拒绝
    （return 403 AccessDenied with "object is locked"），所以即便 head_object
    查询有竞态也不会破坏锚定文件——这是双重保险。

    幂等 key 设计：``tree_size:020d`` 让不同 size 的 STH 排序友好；同 size
    若重复签发（理论不会），用 signed_at_iso 区分。

    错误处理
    --------
    任何 boto3 异常（NoCredentialsError / EndpointConnectionError / ClientError）
    一律捕获 + ``logger.error`` + 返回 ``False``。**不抛异常**，与
    JsonLineFileAnchorBackend 的"失败静默"语义对齐。

    依赖
    ----
    需要 ``boto3``。运行时若未安装，构造时抛 ``ImportError``——让用户在选择
    切换 backend 时立即得到反馈，而非延迟到第一次 anchor 调用。
    """

    def __init__(
        self,
        bucket: str,
        retention_years: int = 7,
        key_prefix: str = "sth/",
        region_name: str | None = None,
        endpoint_url: str | None = None,
        s3_client: Any = None,
    ) -> None:
        """初始化 S3 backend。

        Parameters
        ----------
        bucket : str
            S3 桶名（必须预先启用 Object Lock）。
        retention_years : int, default 7
            Compliance 模式 retention 期（年）。SEC 17a-4 / SOC 2 常见 7 年；
            金融监管常见 10-15 年。
        key_prefix : str, default "sth/"
            S3 key 前缀；多 backend 共用一个桶时用以隔离（如 audit / sth）。
        region_name : str | None
            AWS region；None 时从环境变量 / IAM 角色 / `~/.aws/config` 读。
        endpoint_url : str | None
            S3 兼容服务端点（MinIO / LocalStack / 测试用 moto）；生产留空。
        s3_client : Any, default None
            **测试注入用**：传入预置好的 boto3 client；生产路径走默认。
        """
        if not bucket:
            raise ValueError("S3ObjectLockAnchorBackend requires non-empty bucket")
        if retention_years < 1:
            raise ValueError("retention_years must be >= 1")

        self.bucket = bucket
        self.retention_years = retention_years
        self.key_prefix = key_prefix.rstrip("/") + "/" if key_prefix else ""

        if s3_client is not None:
            self._client = s3_client
        else:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError(
                    "boto3 is required for S3ObjectLockAnchorBackend; "
                    "install with `pip install boto3`"
                ) from exc
            self._client = boto3.client(
                "s3",
                region_name=region_name,
                endpoint_url=endpoint_url,
            )

    def _build_key(self, sth: AuditTreeHead) -> str:
        """构造 S3 对象 key——按 user_id / passport（or root）/ tree_size 分层。"""
        passport_part = (
            str(sth.passport_id) if sth.passport_id is not None else "_root"
        )
        # tree_size:020d 让字典序 = 数值序，方便审计员 list-objects 看链增长
        signed_iso = sth.signed_at.isoformat().replace(":", "-")
        return (
            f"{self.key_prefix}"
            f"{sth.user_id}/"
            f"{passport_part}/"
            f"{sth.tree_size:020d}-{signed_iso}.json"
        )

    def _key_exists(self, key: str) -> bool:
        """head_object 探测 key 是否已存在；用于幂等检查。"""
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:  # noqa: BLE001 — boto3 ClientError 系列
            # 404 是正常的"key 不存在"信号；其他错误向上吞但记日志
            err_code = getattr(getattr(exc, "response", {}), "get", lambda *_: {})(
                "Error", {}
            ).get("Code", "")
            if err_code in ("404", "NoSuchKey", "NotFound"):
                return False
            # 其他错误也按"不存在"处理，让 PUT 路径继续；PUT 失败再报错
            logger.debug(
                "head_object inconclusive for s3://%s/%s: %s",
                self.bucket, key, exc,
            )
            return False

    def anchor(self, sth: AuditTreeHead) -> bool:
        record = build_sth_record(sth)
        key = self._build_key(sth)

        # 幂等：key 已存在直接跳过
        if self._key_exists(key):
            logger.debug(
                "STH S3 anchor skip (duplicate key): s3://%s/%s",
                self.bucket, key,
            )
            return False

        try:
            body = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
            retention_until = datetime.now(UTC).replace(
                microsecond=0
            ) + _years(self.retention_years)
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=retention_until,
            )
            logger.info(
                "STH anchored to s3://%s/%s (retention until %s)",
                self.bucket, key, retention_until.isoformat(),
            )
            return True
        except Exception as exc:  # noqa: BLE001 — boto3 抛 ClientError 系列
            logger.error(
                "STH S3 anchor write failed for s3://%s/%s: %s (sth_id=%s)",
                self.bucket, key, exc, sth.id,
            )
            return False


def _years(n: int) -> _TimedeltaYears:
    """返回 n 年的 timedelta-like 值——用 Python 标准库逼近 365 天/年。

    精确"年"在 calendar 层有闰年问题；锚定 retention 不需要纳秒级精度，
    用 365 天/年逼近完全够（生产 retention 通常 7-15 年误差 < 4 天）。
    """
    from datetime import timedelta

    return timedelta(days=365 * n)


# 类型 alias 以让函数返回类型注解可读
_TimedeltaYears = Any


# ---------------------------------------------------------------------------
# 工厂：按 settings 选择 backend
# ---------------------------------------------------------------------------
def get_default_anchor_backend() -> AnchorBackend:
    """从全局 settings 构造默认 backend。

    选择规则
    --------
    - ``AUDIT_STH_ANCHOR_BACKEND=jsonl``（默认）→ :class:`JsonLineFileAnchorBackend`
      （指向 ``AUDIT_STH_ANCHOR_PATH``；为空时实质 noop）。
    - ``AUDIT_STH_ANCHOR_BACKEND=s3`` → :class:`S3ObjectLockAnchorBackend`
      （要求 ``AUDIT_STH_ANCHOR_S3_BUCKET`` 等配置）。
    - ``AUDIT_STH_ANCHOR_BACKEND=null`` → :class:`NullAnchorBackend`（显式禁用锚定）。

    供 :class:`STHScheduler` 在 ``_tick_sync`` 内调用一次拿到 backend 实例。
    """
    from app.core.config import get_settings

    settings = get_settings()
    backend_kind = (
        getattr(settings, "AUDIT_STH_ANCHOR_BACKEND", "") or "jsonl"
    ).lower()

    if backend_kind == "null":
        return NullAnchorBackend()
    if backend_kind == "jsonl":
        return JsonLineFileAnchorBackend(settings.AUDIT_STH_ANCHOR_PATH)
    if backend_kind == "s3":
        bucket = getattr(settings, "AUDIT_STH_ANCHOR_S3_BUCKET", "") or ""
        if not bucket:
            logger.error(
                "AUDIT_STH_ANCHOR_BACKEND=s3 but AUDIT_STH_ANCHOR_S3_BUCKET is empty; "
                "falling back to NullAnchorBackend"
            )
            return NullAnchorBackend()
        try:
            return S3ObjectLockAnchorBackend(
                bucket=bucket,
                retention_years=int(
                    getattr(settings, "AUDIT_STH_ANCHOR_S3_RETENTION_YEARS", 7) or 7
                ),
                key_prefix=getattr(
                    settings, "AUDIT_STH_ANCHOR_S3_KEY_PREFIX", "sth/"
                ),
                region_name=getattr(
                    settings, "AUDIT_STH_ANCHOR_S3_REGION", ""
                ) or None,
                endpoint_url=getattr(
                    settings, "AUDIT_STH_ANCHOR_S3_ENDPOINT", ""
                ) or None,
            )
        except Exception as exc:  # noqa: BLE001 — backend 构造失败也不抛
            logger.error(
                "failed to construct S3ObjectLockAnchorBackend: %s; "
                "falling back to NullAnchorBackend",
                exc,
            )
            return NullAnchorBackend()

    logger.warning(
        "unknown AUDIT_STH_ANCHOR_BACKEND=%r; falling back to NullAnchorBackend",
        backend_kind,
    )
    return NullAnchorBackend()


# ---------------------------------------------------------------------------
# 向后兼容：原 anchor_sth_to_file 函数保留
# ---------------------------------------------------------------------------
def anchor_sth_to_file(sth: AuditTreeHead, anchor_path: str | Path) -> bool:
    """**向后兼容入口**——委托给 :class:`JsonLineFileAnchorBackend`。

    现有调用方（``audit_sth_scheduler._tick_sync`` / audit 路由 ``POST /sth/issue``）
    仍调用此函数；新代码建议用 :func:`get_default_anchor_backend`。
    """
    if not anchor_path:
        return False
    backend = JsonLineFileAnchorBackend(anchor_path)
    return backend.anchor(sth)


__all__ = [
    "AnchorBackend",
    "JsonLineFileAnchorBackend",
    "NullAnchorBackend",
    "S3ObjectLockAnchorBackend",
    "anchor_sth_to_file",
    "build_sth_record",
    "get_default_anchor_backend",
    "is_duplicate_record",
]
