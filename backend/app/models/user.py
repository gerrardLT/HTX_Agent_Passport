"""User 模型（PRD §8 ``users`` / design.md「Data Models」）。

Demo 模式下使用预设钱包地址登录（Req 1 AC2）；
``id`` 是后续凭证 / 护照 / 操作 / 审批 / 审计事件的所有者外键。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.action import AgentAction
    from app.models.approval import Approval
    from app.models.audit import AuditEvent
    from app.models.credential import ApiCredential
    from app.models.passport import AgentPassport


class User(Base, UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin):
    """演示账户用户。

    - ``primary_wallet``：演示钱包地址（如 ``0xA11CE...001``），允许 NULL 但有 UNIQUE 约束。
    - ``email``：可选邮箱。
    - ``role``：默认 ``user``；预留 ``admin`` 供后续权限扩展。
    """

    __tablename__ = "users"

    primary_wallet: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'user'"))

    # ---- 关系（lazy='select' 避免 N+1 全量预加载） ----
    credentials: Mapped[list["ApiCredential"]] = relationship(
        back_populates="user", lazy="select"
    )
    passports: Mapped[list["AgentPassport"]] = relationship(
        back_populates="user", lazy="select"
    )
    actions: Mapped[list["AgentAction"]] = relationship(
        back_populates="user", lazy="select"
    )
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="user", lazy="select"
    )
    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="user", lazy="select"
    )
