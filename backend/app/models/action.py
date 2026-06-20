"""AgentAction 模型（PRD §8 ``agent_actions`` / Req 5-9 + Req 14）。

补全字段（design.md「Data Models」）：
- ``trace_id``：贯穿一次用户请求的全链路标识（Req 13 AC2）。
- ``reason_codes``：Policy Engine 拒绝原因码数组（Req 7 AC10），TEXT[]。
- ``checkpoint_json``：检查点（Req 14 AC3,7），JSONB。
- ``policy_version_at_planning``：规划时刻的 passport.version；审批阶段对比触发重裁决（Req 8 AC9）。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.sqltypes import Boolean

from app.models.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin
from app.models.enums import ActionState

if TYPE_CHECKING:
    from app.models.approval import Approval
    from app.models.audit import AuditEvent
    from app.models.execution import ExecutionResult
    from app.models.model_call import ModelCall
    from app.models.passport import AgentPassport
    from app.models.user import User


class AgentAction(Base, UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin):
    """代理操作（Req 5-9 全流程的核心实体）。

    主要字段：
    - ``natural_language_request``：用户自然语言任务原文（Req 5 AC1）。
    - ``normalized_action_json``：归一化后的结构化动作（PolicyVerdict.normalized_action）。
    - ``state``：ActionState 状态机（design.md ACTION_TRANSITIONS）。
    - ``risk_verdict`` / ``risk_score`` / ``reason_codes``：Policy Engine 输出（Req 7 AC10）。
    - ``approval_required``：是否需要人工审批（Req 8）。
    - ``execution_mode``：simulation / real_read / real_trade（Req 9 AC3-5）。
    - ``policy_version_at_planning``：用于审批阶段检测策略变化（Req 8 AC9）。
    - ``checkpoint_json``：恢复管理器检查点（Req 14 AC3,7）。
    - ``trace_id``：与 audit_events.trace_id / model_calls.trace_id 串联（Req 13）。
    """

    __tablename__ = "agent_actions"

    passport_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_passports.id"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
    )
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    natural_language_request: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_action_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text(f"'{ActionState.REQUESTED}'")
    )
    risk_verdict: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason_codes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    approval_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    execution_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'simulation'")
    )
    policy_version_at_planning: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkpoint_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # ---- 关系 ----
    passport: Mapped["AgentPassport"] = relationship(
        back_populates="actions", lazy="select"
    )
    user: Mapped["User"] = relationship(back_populates="actions", lazy="select")
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="action", lazy="select"
    )
    execution_results: Mapped[list["ExecutionResult"]] = relationship(
        back_populates="action", lazy="select"
    )
    model_calls: Mapped[list["ModelCall"]] = relationship(
        back_populates="action", lazy="select"
    )
    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="action", lazy="select"
    )
