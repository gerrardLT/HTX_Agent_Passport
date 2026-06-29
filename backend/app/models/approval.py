"""Approval 模型（PRD §8 ``approvals`` / Req 8 人机审批流）。

补全字段：
- ``expires_at``：审批过期时间（Req 8 AC4 + AC5），由 ``approval.expires_after_seconds``（默认 300s）计算。
- ``approved`` 允许 ``NULL``：表示「待审批」中间状态，避免与 ``approved=false``（拒绝）混淆，
  也用于支持 Property 6（双重审批检测：``approved is not None`` 即代表已处理）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.action import AgentAction
    from app.models.user import User


class Approval(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """人工审批记录（Req 8）。

    - ``approval_type``：默认 ``typed_confirmation``（Req 8 AC2 用户输入"APPROVE"）；
      可扩展为钱包签名等。
    - ``signed_payload``：可选钱包签名负载，仅当 approval_type 为 wallet_signature 时使用。
    - ``approved``：``true``=已批准 / ``false``=已拒绝 / ``NULL``=待审批（双重防护检测点）。
    - ``expires_at``：到期后惰性 + 主动扫描双重清理（Req 8 AC4,5）。
    """

    __tablename__ = "approvals"

    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_actions.id"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    approval_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'typed_confirmation'")
    )
    signed_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ---- 关系 ----
    action: Mapped[AgentAction] = relationship(
        back_populates="approvals", lazy="select"
    )
    user: Mapped[User] = relationship(back_populates="approvals", lazy="select")
