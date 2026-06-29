"""AuditEvent 模型（PRD §8 ``audit_events`` / Req 11 审计哈希链）。

事件哈希链由 ``app.core.audit_chain``（任务 7.1）维护：
``event_hash = sha256(canonical_json(event_json) + previous_event_hash + created_at_iso)``。

注意：
- ``actor_id``：``Text`` 而非 UUID，允许 "SYSTEM" / "PLANNER" / "POLICY_ENGINE" 等非人类 actor（Req 11 AC5）。
- ``previous_event_hash``：首事件为 ``GENESIS_HASH`` 常量字符串，非 NULL；这里允许 NULL
  仅作迁移阶段兼容，应用层写入时永远填值。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.action import AgentAction
    from app.models.passport import AgentPassport
    from app.models.user import User


class AuditEvent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """append-only 审计事件（Req 11）。

    - ``event_type``：见 ``app.models.enums.AuditEventType``（Req 11 AC6）。
    - ``actor_type``：USER / SYSTEM / PLANNER / POLICY_ENGINE / EXECUTOR 等。
    - ``actor_id``：人类用户为 UUID 字符串；自动化 actor 为常量字符串。
    - ``event_json``：JSONB 结构化事件负载，用于审计重放。
    - ``previous_event_hash`` / ``event_hash``：哈希链字段（Req 11 AC1-4）。
    - ``trace_id``：与 ``agent_actions.trace_id`` / ``model_calls.trace_id`` 串联（Req 13 AC2）。
    """

    __tablename__ = "audit_events"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    passport_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_passports.id"),
        nullable=True,
    )
    action_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_actions.id"),
        nullable=True,
    )
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    previous_event_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # ---- 关系 ----
    user: Mapped[User] = relationship(back_populates="audit_events", lazy="select")
    passport: Mapped[AgentPassport | None] = relationship(
        back_populates="audit_events", lazy="select"
    )
    action: Mapped[AgentAction | None] = relationship(
        back_populates="audit_events", lazy="select"
    )
