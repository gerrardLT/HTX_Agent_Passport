"""凭证使用限额检查（修复 G12 / Phase 3）。

提供 :func:`check_and_record_credential_use` —— 在 HTX adapter / 任何使用
凭证调外部 API 的路径上**调用前先校验**：

1. 凭证状态非 INVALID/REVOKED/DELETED
2. 凭证未显式过期（expires_at）
3. 当日（UTC 日边界）使用次数未达上限（max_uses_per_day）

通过则递增 ``current_uses_today`` + 更新 ``last_use_at`` 后放行；不通过抛
:class:`CredentialUsageError`（调用方应映射为 HTX_AUTH_FAILED）。

设计要点
--------
- **UTC 日边界自动重置**：检查 ``last_use_at`` 与 ``now`` 是否在同一 UTC
  日；不在则把 ``current_uses_today`` 重置为 0 后再判断。
- **不 commit 只 flush**：与项目其他 service 一致；调用方负责事务边界。
- **过期检查触发 state 转换**：``now > expires_at`` 时把 state 置为 INVALID
  并写审计事件——形成"过期 → 不可用"的状态机闭环。
- **服务函数纯增量逻辑**：不写 model_calls / 不发 HTX 请求；只做校验与计数。

设计依据：``docs/tech-research/07-...md`` §7.3.3 "凭证使用限额"路径。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import ApiCredential
from app.models.enums import AuditEventType, CredentialState
from app.services.audit_writer import (
    ACTOR_TYPE_SYSTEM,
    write_audit_event,
)

logger = logging.getLogger(__name__)


class CredentialUsageError(Exception):
    """凭证使用受限（过期 / 超限 / 状态非法）。

    Attributes
    ----------
    code : str
        机器可读错误码（``EXPIRED`` / ``DAILY_LIMIT_EXCEEDED`` / ``INVALID_STATE``）。
    message : str
        人类可读错误信息。
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# UTC 日边界判断
# ---------------------------------------------------------------------------
def _is_same_utc_day(a: datetime, b: datetime) -> bool:
    """判断 a 与 b 是否在同一 UTC 日（不依赖时区库，保持纯函数）。"""
    if a.tzinfo is None:
        a = a.replace(tzinfo=UTC)
    if b.tzinfo is None:
        b = b.replace(tzinfo=UTC)
    a_utc = a.astimezone(UTC)
    b_utc = b.astimezone(UTC)
    return (
        a_utc.year == b_utc.year
        and a_utc.month == b_utc.month
        and a_utc.day == b_utc.day
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def check_and_record_credential_use(
    db: Session,
    *,
    credential_id: UUID,
    now: datetime | None = None,
    trace_id: UUID | None = None,
) -> ApiCredential:
    """检查凭证可用性并记录一次使用。

    流程
    ----
    1. 加载 credential（不存在 → ``CredentialUsageError("INVALID_STATE")``）。
    2. 状态检查：state ∈ {READ_ONLY, TRADE_ENABLED, VALIDATING} 才可用。
    3. 过期检查：``now > expires_at`` 时置 state=INVALID + 写审计 + 抛错。
    4. 日边界自动重置：``last_use_at`` 与 ``now`` 不在同一 UTC 日 → 重置计数。
    5. 限额检查：``current_uses_today >= max_uses_per_day`` → 抛错。
    6. 记录：``current_uses_today += 1`` + ``last_use_at = now``。

    Parameters
    ----------
    db : Session
        当前会话；不 commit。
    credential_id : UUID
        要使用的凭证。
    now : datetime | None
        当前时间；默认 ``datetime.now(UTC)``。测试时显式传以保持确定性。
    trace_id : UUID | None
        请求级 trace_id；写审计事件时使用。

    Returns
    -------
    ApiCredential
        通过校验且已记录使用的凭证 ORM 对象（已 flush）。

    Raises
    ------
    CredentialUsageError
        凭证不可用；调用方应映射为 ``HTX_AUTH_FAILED``。
    """
    if now is None:
        now = datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    cred = db.get(ApiCredential, credential_id)
    if cred is None:
        raise CredentialUsageError(
            "INVALID_STATE",
            f"credential {credential_id} not found",
        )

    # ---- Step 2: state 检查 ----
    valid_states = {
        CredentialState.READ_ONLY,
        CredentialState.TRADE_ENABLED,
        CredentialState.VALIDATING,
    }
    if cred.state not in valid_states:
        raise CredentialUsageError(
            "INVALID_STATE",
            f"credential {credential_id} state={cred.state!r} is not usable",
        )
    if cred.deleted_at is not None:
        raise CredentialUsageError(
            "INVALID_STATE",
            f"credential {credential_id} is soft-deleted",
        )

    # ---- Step 3: 过期检查 ----
    if cred.expires_at is not None:
        expires_at = cred.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if now > expires_at:
            # 自动转 INVALID + 写审计（先于异常抛出，确保审计与状态都落盘）
            previous_state = cred.state
            cred.state = CredentialState.INVALID
            db.flush()
            write_audit_event(
                db,
                event_type=AuditEventType.CREDENTIAL_VALIDATED,
                user_id=cred.user_id,
                actor_type=ACTOR_TYPE_SYSTEM,
                actor_id=ACTOR_TYPE_SYSTEM,
                trace_id=trace_id,
                event_data={
                    "credential_id": str(cred.id),
                    "previous_state": previous_state,
                    "new_state": CredentialState.INVALID,
                    "reason": "EXPIRED",
                    "expires_at": expires_at.isoformat(),
                    "detected_at": now.isoformat(),
                },
            )
            raise CredentialUsageError(
                "EXPIRED",
                f"credential {credential_id} expired at {expires_at.isoformat()}",
            )

    # ---- Step 4: UTC 日边界自动重置 ----
    if cred.last_use_at is not None:
        last_use_at = cred.last_use_at
        if last_use_at.tzinfo is None:
            last_use_at = last_use_at.replace(tzinfo=UTC)
        if not _is_same_utc_day(last_use_at, now):
            # 跨日：重置计数器
            cred.current_uses_today = 0

    # ---- Step 5: 限额检查 ----
    if cred.max_uses_per_day is not None:
        if cred.current_uses_today >= cred.max_uses_per_day:
            raise CredentialUsageError(
                "DAILY_LIMIT_EXCEEDED",
                f"credential {credential_id} hit max_uses_per_day="
                f"{cred.max_uses_per_day}",
            )

    # ---- Step 6: 记录使用 ----
    cred.current_uses_today = (cred.current_uses_today or 0) + 1
    cred.last_use_at = now
    db.flush()
    return cred


__all__ = [
    "CredentialUsageError",
    "check_and_record_credential_use",
]
