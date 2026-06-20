"""ModelCall 模型（PRD §8 ``model_calls`` / Req 13 模型调用日志）。

注意：``execution_results.model_call_id`` 引用本表，因此 ``model_calls`` 必须先于
``execution_results`` 建表（任务 2.2 在 Alembic 迁移中保证顺序）。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.action import AgentAction
    from app.models.execution import ExecutionResult


class ModelCall(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """B.AI Planner 调用记录（Req 13 AC1）。

    安全约束：
    - ``prompt_hash``：仅存 SHA-256 哈希，原始 prompt 默认不入库（Req 5 AC6）。
    - ``raw_response``：可选 JSONB；生产环境也可关闭以节省存储。

    可观测性：
    - ``trace_id``：与 ``agent_actions.trace_id`` / ``audit_events.trace_id`` 串联（Req 13 AC2）。
    - ``input_token_count`` / ``output_token_count`` / ``latency_ms``：质量信号与成本异常告警依据（Req 13 AC5,6）。
    """

    __tablename__ = "model_calls"

    action_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_actions.id"),
        nullable=True,
    )
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'B.AI'")
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_hash: Mapped[str] = mapped_column(Text, nullable=False)
    input_token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'STARTED'")
    )
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # ---- 关系 ----
    action: Mapped["AgentAction | None"] = relationship(
        back_populates="model_calls", lazy="select"
    )
    execution_results: Mapped[list["ExecutionResult"]] = relationship(
        back_populates="model_call", lazy="select"
    )
