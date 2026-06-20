"""审计 Merkle 树的 Signed Tree Head（修复 G10/G11）。

每条记录是审计日志在某一时刻的 RFC 6962 Signed Tree Head（STH）：
``(tree_size, root_hash, timestamp, signature)``。STH 是日志服务器对外做出的
密码学承诺：「截至此时，日志规模为 N，Merkle 根为 R」。一旦发布（如对外
锚定到外部存储 / 区块链 / 公开 git 仓库），日志服务器再也无法在不被发现的
情况下修改任何已 ≤ N 的事件。

为什么需要 STH？
---------------
仅有 root hash 还不足以防"删除+重写"——攻击者删除事件后重算的新 root 与
旧 root 不一致，但若旧 root 从未对外发布，攻击就无人发觉。STH 周期持久化
（且建议异地/链上锚定）后，"日志在某时刻的 root 是什么"成为不可抵赖的
公开事实，攻击窗口被关闭。

字段
----
- ``user_id`` / ``passport_id``：与 audit_events 同样按 ``(user_id, passport_id)``
  分链（passport_id=NULL 是用户级链）。每条独立链有自己的 STH 时间序列。
- ``tree_size``：本 STH 承诺的事件数（leaf count）。
- ``root_hash``：Merkle 根（hex 64）。
- ``signature``：服务签名（hex），便于第三方验证发布方身份。MVP 用 HMAC-SHA256
  + 服务私钥（见 audit_merkle_service.py）；生产可升级为 ECDSA / Ed25519。
- ``signed_at``：服务签发 STH 的时间（独立于 created_at，用于排序）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.passport import AgentPassport
    from app.models.user import User


class AuditTreeHead(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """审计 Merkle 树的 Signed Tree Head（RFC 6962 风格）。"""

    __tablename__ = "audit_tree_heads"

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
    tree_size: Mapped[int] = mapped_column(Integer, nullable=False)
    root_hash: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped["User"] = relationship(lazy="select")
    passport: Mapped["AgentPassport | None"] = relationship(lazy="select")
