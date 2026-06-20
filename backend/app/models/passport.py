"""AgentPassport 模型（PRD §8 ``agent_passports`` / Req 3 + Req 4）。

代理护照即方法论中的「能力包」（Capability Envelope）持久化形态。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin
from app.models.enums import PassportState

if TYPE_CHECKING:
    from app.models.action import AgentAction
    from app.models.audit import AuditEvent
    from app.models.credential import ApiCredential
    from app.models.user import User


class AgentPassport(Base, UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin):
    """代理护照 / 能力包（Req 3 + Req 4）。

    字段说明：
    - ``policy_json``：Policy DSL v0（capabilities / limits / approval / blocked_actions）；
      JSONB 便于按字段查询与索引。
    - ``version``：每次 PATCH /policy 时递增（Req 3 AC3）；审批阶段
      ``policy_version_at_planning`` 与之比较，触发重裁决（Req 8 AC9）。
    - ``state``：DRAFT / ACTIVE / PAUSED / REVOKED / EXPIRED / DELETED
      （design.md PASSPORT_TRANSITIONS / Req 3 AC2,4,5,7,8）。
    - ``reputation_score``：声誉分（Req 24），由声誉服务在 action 终态时增减。
    - ``api_credential_id`` 允许 NULL：DRAFT 阶段可能尚未绑定凭证（Req 3 AC2）。
    """

    __tablename__ = "agent_passports"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    api_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("api_credentials.id"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_type: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text(f"'{PassportState.DRAFT}'")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reputation_score: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("50")
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- 关系 ----
    user: Mapped["User"] = relationship(back_populates="passports", lazy="select")
    api_credential: Mapped["ApiCredential | None"] = relationship(
        back_populates="passports", lazy="select"
    )
    actions: Mapped[list["AgentAction"]] = relationship(
        back_populates="passport", lazy="select"
    )
    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="passport", lazy="select"
    )
