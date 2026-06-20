"""ApiCredential 模型（PRD §8 ``api_credentials`` / Req 2 凭证保险库）。

补全字段：
- ``encryption_algorithm``：默认 ``AES-256-GCM``，便于将来轮转算法时区分版本（design.md）。
- ``deleted_at``：软删除时间戳（Req 2 AC6），不物理删除以保留审计可追溯性。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.models.enums import CredentialState

if TYPE_CHECKING:
    from app.models.passport import AgentPassport
    from app.models.user import User


class ApiCredential(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """用户存放在保险库中的 HTX API 凭证（Req 2）。

    安全约束：
    - ``encrypted_secret_key`` / ``encrypted_access_key``：AES-256-GCM 密文（任务 4.1 写入）；
      明文绝不落库（Req 2 AC1 + Req 15 AC1）。
    - ``access_key_hash``：SHA-256(access_key)，用于重复检测（Req 2 AC2）。
    - ``permission_withdraw``：MVP 阶段强制为 ``false``（Req 2 AC4 / Req 15 AC6）。

    软删除：``deleted_at`` 非空表示已删除；联合部分索引
    ``WHERE deleted_at IS NULL`` 保证活跃凭证唯一（任务 2.2 创建）。
    """

    __tablename__ = "api_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'HTX'")
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    access_key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_access_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_secret_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_algorithm: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'AES-256-GCM'")
    )
    permission_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    permission_trade: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    permission_withdraw: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    ip_whitelist_detected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text(f"'{CredentialState.CREATED}'")
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- G12 凭证使用限额 / 短时化（Phase 3 / docs/tech-research/07-...md §7.3）----
    # HTX API key 不支持动态生成（与 Vault 能动态创建 PostgreSQL 用户的语义不同）；
    # 我们能做的"短时化"是在使用维度限额：每日次数上限 + 显式过期时间。
    #
    # 默认全 NULL = 无限制（向后兼容）；显式启用后 HTX adapter 每次签名前
    # 校验，超限抛 HTX_AUTH_FAILED + 把 state 置 INVALID（不可继续用）。
    max_uses_per_day: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="每日 HTX API 调用次数上限；NULL 表示无限制",
    )
    current_uses_today: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="当日（UTC）已用次数；按 UTC 0:00 重置",
    )
    last_use_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最近一次签名调用时间；用于审计 + 配合 last_validated_at 排查",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="凭证显式过期时间；NULL 表示永久；过期后 state→INVALID",
    )

    # ---- 关系 ----
    user: Mapped["User"] = relationship(back_populates="credentials", lazy="select")
    passports: Mapped[list["AgentPassport"]] = relationship(
        back_populates="api_credential", lazy="select"
    )
