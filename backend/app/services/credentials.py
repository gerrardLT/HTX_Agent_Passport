"""凭证管理业务逻辑（任务 4.2 / Req 2 / Req 15）。

本服务模块负责把"安全敏感"的密钥处理逻辑集中在一处：

1. **加密入库**（:func:`create_credential`）—— 调用
   :class:`app.core.vault.CredentialVault` 把 access_key / secret_key 加密为 BYTEA；
   原文不进 ORM、不进日志（Req 2 AC1 / Req 15 AC1）。
2. **重复检测** —— 用 ``access_key_hash``（SHA-256）做"同 user 同 access_key"判重；
   重复时抛 :class:`DuplicateCredentialError`（最终在 errors.py 映射为 409）。
3. **验证状态机**（:func:`validate_credential`）—— 走 CREDENTIAL_TRANSITIONS：
   当前状态 → VALIDATING → READ_ONLY / TRADE_ENABLED / INVALID。
4. **withdraw 强制为 false**（Req 2 AC4 / Req 15 AC6）—— 即便外部验证器返回 ``true``，
   本层也要硬覆盖为 ``false``，并在审计事件中显式记录这次"覆盖动作"，
   作为可审计的安全边界证据。
5. **软删除**（:func:`delete_credential`）—— 设置 ``state=DELETED`` + ``deleted_at=now()``；
   不物理删除加密数据（Req 2 AC6），保留可追溯性。
6. **审计事件** —— 每个写操作（CREATE / VALIDATE / DELETE）都通过
   :func:`app.services.audit_writer.write_audit_event` 写入对应类型的 audit_event；
   ``event_data`` 仅含 ``credential_id`` / ``label`` / ``trace_id`` 等非敏感字段
   （Req 2 AC7）。

服务层 **不直接 commit**——事务边界由路由层显式控制，方便把"业务写入 + 审计写入"
放进同一事务（Req 11 AC7：审计写入失败必须阻止业务转换）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.state_machine import (
    CREDENTIAL_TRANSITIONS,
    assert_transition,
)
from app.core.vault import CredentialVault
from app.core.envelope_vault import EnvelopeVault
from app.models import ApiCredential
from app.models.enums import AuditEventType, CredentialState
from app.services.audit_writer import (
    ACTOR_TYPE_SYSTEM,
    ACTOR_TYPE_USER,
    write_audit_event,
)

# logger 不会输出密钥相关字段；本模块所有 logger 调用都只引用 credential_id / state。
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 业务异常
# ---------------------------------------------------------------------------
class DuplicateCredentialError(ValueError):
    """同一 user 已存在等价 access_key 的活跃凭证（Req 2 AC2）。

    继承 ``ValueError`` 让上层既可精确捕获 ``DuplicateCredentialError``，
    也可宽松捕获 ``ValueError``。最终被 ``app.core.errors`` 注册的 handler
    映射为 HTTP 409 + ``code="DUPLICATE_CREDENTIAL"``。

    Attributes
    ----------
    user_id : uuid.UUID
        触发冲突的用户主键。
    access_key_hash : str
        发生冲突的 access_key 的 SHA-256 摘要（64 hex）。
    existing_credential_id : uuid.UUID
        已存在的同等价凭证主键（便于前端引导用户去那条凭证操作）。
    """

    def __init__(
        self,
        user_id: uuid.UUID,
        access_key_hash: str,
        existing_credential_id: uuid.UUID,
    ) -> None:
        self.user_id = user_id
        self.access_key_hash = access_key_hash
        self.existing_credential_id = existing_credential_id
        super().__init__(
            f"duplicate credential for user_id={user_id} "
            f"(existing credential_id={existing_credential_id})"
        )


class CredentialNotFoundError(LookupError):
    """凭证不存在或不属于当前用户（用于 router 转 404）。

    安全考虑：「不属于本人」与「不存在」对外一律返回 404，避免
    通过对比 404 / 403 推测他人凭证 ID 的存在性。
    """

    def __init__(self, credential_id: uuid.UUID) -> None:
        self.credential_id = credential_id
        super().__init__(f"credential {credential_id} not found")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _now() -> datetime:
    """统一的"当前时间"生成函数，便于测试 monkeypatch。"""
    return datetime.now(UTC)


def _get_vault() -> EnvelopeVault:
    """构造 :class:`EnvelopeVault`（信封加密，修复 G7/G8/G9）。

    - **新写入**：用 envelope encryption（per-record DEK + KEK 包裹），
      KEK 由 ``VAULT_KEY_PROVIDER`` 决定（local / aws-kms）。
    - **读取**：透明兼容旧的单层 CredentialVault 密文（迁移期已有 DB 行仍可解）。

    单独抽出便于单元测试注入 ``EnvelopeVault(key_provider=...)``。
    接口与旧 CredentialVault 一致（encrypt / decrypt / hash_access_key），
    调用方无需改动。
    """
    return EnvelopeVault()


def _find_active_credential_by_hash(
    db: Session, user_id: uuid.UUID, access_key_hash: str
) -> ApiCredential | None:
    """查找"同 user_id + 同 access_key_hash"且未软删除的活跃凭证。

    SQLAlchemy `where(ApiCredential.deleted_at.is_(None))` 显式过滤软删除行，
    这里不依赖部分索引（部分索引仅是物理优化，不影响查询语义）。
    """
    stmt = (
        select(ApiCredential)
        .where(ApiCredential.user_id == user_id)
        .where(ApiCredential.access_key_hash == access_key_hash)
        .where(ApiCredential.deleted_at.is_(None))
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def _get_owned_credential(
    db: Session, credential_id: uuid.UUID, user_id: uuid.UUID
) -> ApiCredential:
    """按主键查凭证 + 校验所有者。

    "不存在 / 不属于本人 / 已软删除" 都映射为 :class:`CredentialNotFoundError`，
    最终统一返回 404，避免泄露存在性信息。

    Notes
    -----
    软删除行也视为 "404"，因为已删除凭证对当前用户而言不再可见。
    若将来需要"已删除凭证审计页面"，可单独提供 ``include_deleted=True`` 路径。
    """
    cred = db.get(ApiCredential, credential_id)
    if cred is None or cred.user_id != user_id or cred.deleted_at is not None:
        raise CredentialNotFoundError(credential_id)
    return cred


# ---------------------------------------------------------------------------
# Mock 验证器（任务 12 之前的占位实现）
# ---------------------------------------------------------------------------
def _mock_validate(access_key: str, secret_key: str) -> dict[str, bool]:
    """Demo 模式的"假装验证"：恒返回 ``read=true / trade=true / withdraw=true``。

    设计目的
    --------
    - **演示流程闭环**：任务 4 阶段还没有真实的 HTX 适配器（任务 12 才实现），
      但本任务 4.2 必须能让用户从「创建凭证」走到「TRADE_ENABLED 状态」。
    - **验证 withdraw 强制覆盖逻辑**：故意返回 ``withdraw=true``，让
      :func:`validate_credential` 的"硬覆盖为 false"分支可以被集成测试稳定地命中
      （Req 2 AC4 / Req 15 AC6）。
    - **零外部依赖**：方法论 §11 要求演示韧性，本函数纯本地、确定性，永不抛网络异常。

    任务 12 实现真实 HTX 适配器后，:func:`validate_credential` 内部的调用点可以
    切换为新的 ``htx_adapter.validate(access_key, secret_key)``——本函数不需保留。

    Parameters
    ----------
    access_key, secret_key : str
        明文密钥。**本函数不做任何持久化、不打日志**——
        密钥仅用于将来真实验证时的签名计算，当前 demo 模式直接忽略入参。

    Returns
    -------
    dict[str, bool]
        三个 capability 的"原始"返回（**未经过本服务层的 withdraw 强制覆盖**）。
    """
    # 故意忽略入参：demo 模式不真正打 HTX。返回三个 true 让 withdraw 覆盖逻辑被命中。
    _ = access_key, secret_key
    return {"read": True, "trade": True, "withdraw": True}


def _real_validate(access_key: str, secret_key: str) -> dict[str, bool]:
    """真实 HTX 凭证验证：通过签名请求验证 API Key 有效性。

    验证策略
    --------
    调用 ``GET /v1/account/accounts``（轻量级只读端点），若签名被接受且
    返回 ``status=ok``，则凭证有效。根据返回的 account 类型判断权限：

    - 存在 ``type=spot, state=working`` 的 account → ``read=true, trade=true``
    - 仅存在 ``type=point`` 等非现货 account → ``read=true, trade=false``
    - 签名被拒（``api-signature-not-valid``）→ ``read=false, trade=false``

    withdraw 永远返回 ``false``——与 :func:`validate_credential` 的硬覆盖
    逻辑保持一致（Req 2 AC4 / Req 15 AC6）。

    Parameters
    ----------
    access_key, secret_key : str
        明文密钥，仅用于本次签名计算，不持久化、不打日志。

    Returns
    -------
    dict[str, bool]
        ``{read, trade, withdraw}``——withdraw 恒为 false。

    Raises
    ------
    Exception
        网络异常时抛出，让调用方区分"凭证无效"与"网络不可用"。
    """
    import hashlib
    import hmac
    import time
    from urllib.parse import urlencode

    import httpx

    settings = get_settings()
    api_url = settings.HTX_API_URL

    # 构造签名（复用 HTXAdapter._sign_request 的算法）
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    params = {
        "AccessKeyId": access_key,
        "SignatureMethod": "HmacSHA256",
        "SignatureVersion": "2",
        "Timestamp": timestamp,
    }
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    host = api_url.replace("https://", "").replace("http://", "")
    path = "/v1/account/accounts"
    payload = f"GET\n{host}\n{path}\n{sorted_params}"
    signature = hmac.new(
        secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    params["Signature"] = signature

    url = f"{api_url}{path}?{urlencode(params)}"

    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        logger.warning("HTX credential validation timed out")
        return {"read": False, "trade": False, "withdraw": False}
    except httpx.HTTPStatusError as exc:
        logger.warning("HTX credential validation HTTP %s", exc.response.status_code)
        return {"read": False, "trade": False, "withdraw": False}

    if data.get("status") != "ok":
        # 签名错误 = 凭证无效
        logger.info(
            "HTX credential validation failed: %s",
            data.get("err-code", "unknown"),
        )
        return {"read": False, "trade": False, "withdraw": False}

    # 检查 account 类型判断权限
    accounts = data.get("data", [])
    has_spot = any(
        a.get("type") == "spot" and a.get("state") == "working"
        for a in accounts
    )
    has_any = len(accounts) > 0

    return {
        "read": has_any,
        "trade": has_spot,
        "withdraw": False,  # 硬编码 false（Req 2 AC4）
    }


# ---------------------------------------------------------------------------
# 业务函数
# ---------------------------------------------------------------------------
def create_credential(
    db: Session,
    *,
    user_id: uuid.UUID,
    label: str,
    access_key: str,
    secret_key: str,
    trace_id: uuid.UUID | None = None,
) -> ApiCredential:
    """创建一条新凭证（state=CREATED）。

    流程
    ----
    1. 调用 :class:`CredentialVault` 加密 access_key / secret_key，并计算
       access_key_hash（SHA-256）。
    2. 同 user_id + access_key_hash 已存在活跃凭证 → 抛
       :class:`DuplicateCredentialError`（路由层映射 409）。
    3. 写入 ``api_credentials`` 行，``state=CREATED`` / 三权限默认 false /
       ``encryption_algorithm='AES-256-GCM'``。
    4. 写入 ``CREDENTIAL_CREATED`` 审计事件（actor_type=USER）；
       event_data 仅含 ``credential_id`` / ``label`` / ``trace_id``，
       绝不含密钥任何形态。

    Parameters
    ----------
    db : Session
        当前请求的会话；本函数仅 ``add()`` + ``flush()``，不 ``commit()``。
    user_id : uuid.UUID
        凭证拥有者。
    label : str
        凭证展示名。
    access_key, secret_key : str
        待加密的明文。
    trace_id : uuid.UUID | None
        请求级 trace_id（Req 13 AC2）；为 None 时函数自行生成一枚，
        便于无 trace 中间件场景也能保证审计事件可追踪。

    Returns
    -------
    ApiCredential
        已 ``flush`` 的 ORM 行（``id`` 已分配；``commit`` 由路由层执行）。

    Raises
    ------
    DuplicateCredentialError
        同 user_id + access_key_hash 已存在活跃凭证。
    """
    if trace_id is None:
        trace_id = uuid.uuid4()

    vault = _get_vault()
    access_key_hash = vault.hash_access_key(access_key)

    # 1. 重复检测必须放在加密之前？——其实先检查后加密更省 CPU；密钥仍只在内存中短暂停留。
    existing = _find_active_credential_by_hash(db, user_id, access_key_hash)
    if existing is not None:
        raise DuplicateCredentialError(
            user_id=user_id,
            access_key_hash=access_key_hash,
            existing_credential_id=existing.id,
        )

    # 2. 加密：明文只在本函数局部变量中存在
    encrypted_access_key = vault.encrypt(access_key)
    encrypted_secret_key = vault.encrypt(secret_key)

    credential = ApiCredential(
        user_id=user_id,
        provider="HTX",
        label=label,
        access_key_hash=access_key_hash,
        encrypted_access_key=encrypted_access_key,
        encrypted_secret_key=encrypted_secret_key,
        encryption_algorithm="AES-256-GCM",
        permission_read=False,
        permission_trade=False,
        permission_withdraw=False,
        state=CredentialState.CREATED,
    )
    db.add(credential)
    db.flush()  # 让 credential.id 立即可读

    # 3. 写审计事件（绝不含密钥字段）
    write_audit_event(
        db,
        event_type=AuditEventType.CREDENTIAL_CREATED,
        user_id=user_id,
        actor_type=ACTOR_TYPE_USER,
        actor_id=str(user_id),
        trace_id=trace_id,
        event_data={
            "credential_id": str(credential.id),
            "label": label,
            "trace_id": str(trace_id),
            "provider": "HTX",
        },
    )

    # 仅记录 credential_id，不记录任何密钥字段（Req 2 AC7 / Req 15 AC1）
    logger.info(
        "credential created",
        extra={"credential_id": str(credential.id), "user_id": str(user_id)},
    )

    return credential


def validate_credential(
    db: Session,
    *,
    credential_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: uuid.UUID | None = None,
) -> ApiCredential:
    """触发一次凭证验证，写入新状态。

    流程
    ----
    1. 加载凭证并校验所有者（``CredentialNotFoundError`` → 404）。
    2. 校验当前状态可转 VALIDATING（``IllegalStateTransition`` → 409）。
       REVOKED / DELETED 终态都会被这里直接拒绝。
    3. 调用 mock 验证器（demo 模式）或真实验证器（非 demo，任务 12）；
       获取 ``{read, trade, withdraw}`` 原始返回。
    4. **强制 withdraw=false**（Req 2 AC4 / Req 15 AC6），即便外部返回 true。
       检测到 withdraw 被覆盖时，在审计事件 event_data 里显式记录
       ``withdraw_overridden=True``，作为安全边界的可追溯证据。
    5. 根据 read/trade 推导终态：
       - read=true 且 trade=true → ``TRADE_ENABLED``
       - 仅 read=true            → ``READ_ONLY``
       - 都没有                  → ``INVALID``
    6. 更新 ``last_validated_at = now()``。
    7. 写入 ``CREDENTIAL_VALIDATED`` 审计事件（actor_type=SYSTEM，
       因为验证由系统自动执行）。

    Parameters
    ----------
    db : Session
        当前请求会话；不 commit。
    credential_id : uuid.UUID
        待验证凭证主键。
    user_id : uuid.UUID
        请求者，必须与凭证 owner 匹配，否则 404。
    trace_id : uuid.UUID | None
        请求级 trace_id；为 None 时本函数自行生成。

    Returns
    -------
    ApiCredential
        已 ``flush`` 的 ORM 行；状态已更新。

    Raises
    ------
    CredentialNotFoundError
        凭证不存在或不属于本人或已被软删除。
    IllegalStateTransition
        当前状态不允许转为 VALIDATING（如已 REVOKED / DELETED）。
    """
    if trace_id is None:
        trace_id = uuid.uuid4()

    credential = _get_owned_credential(db, credential_id, user_id)

    # 步骤 2：状态机校验。注意"先尝试转 VALIDATING、再判断结果"。
    # 实际 ORM 字段更新在第 6 步统一完成；这里只做合法性断言。
    assert_transition(
        credential.state,
        CredentialState.VALIDATING,
        CREDENTIAL_TRANSITIONS,
        machine_name="credential",
    )

    # 步骤 3：调用验证器（demo / 真实）。
    settings = get_settings()
    if settings.DEMO_MODE:
        raw = _mock_validate(
            vault_decrypt_or_pass(credential.encrypted_access_key),
            vault_decrypt_or_pass(credential.encrypted_secret_key),
        )
    else:
        raw = _real_validate(
            vault_decrypt_or_pass(credential.encrypted_access_key),
            vault_decrypt_or_pass(credential.encrypted_secret_key),
        )

    # 步骤 4：硬覆盖 withdraw（Req 2 AC4 / Req 15 AC6）
    raw_withdraw = bool(raw.get("withdraw", False))
    permission_read = bool(raw.get("read", False))
    permission_trade = bool(raw.get("trade", False))
    permission_withdraw = False  # 硬编码 false，无视外部返回
    withdraw_overridden = raw_withdraw is True  # 仅当外部主动声明 true 才记录覆盖

    # 步骤 5：推导终态
    if permission_trade:
        # trade 蕴含 read 能力；即便外部漏传 read=true 也按 trade-enabled 处理
        new_state = CredentialState.TRADE_ENABLED
    elif permission_read:
        new_state = CredentialState.READ_ONLY
    else:
        new_state = CredentialState.INVALID

    # 步骤 5b：再次断言"VALIDATING → new_state"合法。
    # （CREDENTIAL_TRANSITIONS 已显式声明 VALIDATING → READ_ONLY/TRADE_ENABLED/INVALID 全部合法。）
    assert_transition(
        CredentialState.VALIDATING,
        new_state,
        CREDENTIAL_TRANSITIONS,
        machine_name="credential",
    )

    # 步骤 6：原子更新 ORM 字段
    credential.permission_read = permission_read
    credential.permission_trade = permission_trade
    credential.permission_withdraw = permission_withdraw
    credential.state = new_state
    credential.last_validated_at = _now()
    db.flush()

    # 步骤 7：审计事件——event_data 不含密钥任何字段
    write_audit_event(
        db,
        event_type=AuditEventType.CREDENTIAL_VALIDATED,
        user_id=user_id,
        actor_type=ACTOR_TYPE_SYSTEM,
        actor_id="SYSTEM",
        trace_id=trace_id,
        event_data={
            "credential_id": str(credential.id),
            "label": credential.label,
            "trace_id": str(trace_id),
            "new_state": new_state,
            "permissions": {
                "read": permission_read,
                "trade": permission_trade,
                "withdraw": permission_withdraw,
            },
            "withdraw_overridden": withdraw_overridden,
        },
    )

    logger.info(
        "credential validated",
        extra={
            "credential_id": str(credential.id),
            "user_id": str(user_id),
            "new_state": new_state,
            "withdraw_overridden": withdraw_overridden,
        },
    )

    return credential


def list_credentials(db: Session, *, user_id: uuid.UUID) -> list[ApiCredential]:
    """列出当前用户的所有"未软删除"凭证。

    显式过滤 ``deleted_at IS NULL``——即便部分索引未建好（理论上 PG
    迁移会建好），查询语义也独立保证软删除项不出现在列表里（Req 2 AC6）。

    返回顺序按 ``created_at DESC``（最新创建在前），便于前端默认展示。
    """
    stmt = (
        select(ApiCredential)
        .where(ApiCredential.user_id == user_id)
        .where(ApiCredential.deleted_at.is_(None))
        .order_by(ApiCredential.created_at.desc())
    )
    return list(db.execute(stmt).scalars().all())


def delete_credential(
    db: Session,
    *,
    credential_id: uuid.UUID,
    user_id: uuid.UUID,
    trace_id: uuid.UUID | None = None,
) -> ApiCredential:
    """软删除凭证。

    流程
    ----
    1. 加载凭证并校验所有者（已软删除直接 404）。
    2. 状态机校验 ``current → DELETED``：
       - CREATED / INVALID / READ_ONLY / TRADE_ENABLED 均允许直接软删除。
       - REVOKED 已是终态，``DELETED`` 不在其转换集合中 → :class:`IllegalStateTransition`。
       - VALIDATING 中的凭证也不允许删除（避免删除掉一个正在验证的瞬时态）。
    3. 设置 ``state=DELETED`` + ``deleted_at=now()``，**不物理删除加密数据**（Req 2 AC6）。
    4. 写入 ``CREDENTIAL_DELETED`` 审计事件。

    Parameters
    ----------
    db : Session
    credential_id : uuid.UUID
    user_id : uuid.UUID
    trace_id : uuid.UUID | None

    Returns
    -------
    ApiCredential
        已软删除的 ORM 行。

    Raises
    ------
    CredentialNotFoundError
    IllegalStateTransition
        当前状态不允许转 DELETED。
    """
    if trace_id is None:
        trace_id = uuid.uuid4()

    credential = _get_owned_credential(db, credential_id, user_id)

    # 显式状态机断言：CREATED / INVALID / READ_ONLY / TRADE_ENABLED 都允许；
    # REVOKED / VALIDATING 会在这里抛 IllegalStateTransition（最终 → 409）
    assert_transition(
        credential.state,
        CredentialState.DELETED,
        CREDENTIAL_TRANSITIONS,
        machine_name="credential",
    )

    credential.state = CredentialState.DELETED
    credential.deleted_at = _now()
    db.flush()

    write_audit_event(
        db,
        event_type=AuditEventType.CREDENTIAL_DELETED,
        user_id=user_id,
        actor_type=ACTOR_TYPE_USER,
        actor_id=str(user_id),
        trace_id=trace_id,
        event_data={
            "credential_id": str(credential.id),
            "label": credential.label,
            "trace_id": str(trace_id),
        },
    )

    logger.info(
        "credential soft-deleted",
        extra={"credential_id": str(credential.id), "user_id": str(user_id)},
    )

    return credential


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def vault_decrypt_or_pass(encrypted: bytes) -> str:
    """解密 BYTEA 列；解密失败时抛 :class:`app.core.vault.DecryptionError`。

    设计成模块级函数而非 :func:`validate_credential` 内部嵌套定义，
    便于单元测试 monkeypatch。

    Notes
    -----
    返回的明文**只在调用方栈帧中存活**：``validate_credential`` 拿到明文后
    立即把它传给验证器，之后局部变量被 GC，不会进 ORM 也不会进日志。
    """
    return _get_vault().decrypt(encrypted)


__all__ = [
    "CredentialNotFoundError",
    "DuplicateCredentialError",
    "create_credential",
    "delete_credential",
    "list_credentials",
    "validate_credential",
]
