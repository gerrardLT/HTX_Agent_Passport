"""ExecutionResult 模型（PRD §8 ``execution_results`` / Req 9 执行网关）。

补全字段：
- ``model_call_id``：关联引发本次执行的 model_call（design.md 「Data Models」），
  便于审计重放跨表追溯「规划→裁决→审批→执行」链路。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.action import AgentAction
    from app.models.model_call import ModelCall


class ExecutionResult(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """执行网关产出的执行结果（Req 9 AC6）。

    - ``mode``：simulation / real_read / real_trade。
    - ``request_payload`` / ``response_payload``：调用 HTX（或 Simulation Engine）的入参与返回，
      JSONB 便于在审计重放界面展开。
    - ``provider_order_id``：交易所返回的 order_id（仿真模式为确定性 fake_id）。
    - ``status``：成功 / 失败 / 部分成交等业务状态字符串。
    - ``model_call_id``：可选外键，关联触发本次执行的 model_call。
    """

    __tablename__ = "execution_results"

    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_actions.id"),
        nullable=False,
    )
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_calls.id"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'HTX'")
    )
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    # ---- 关系 ----
    action: Mapped[AgentAction] = relationship(
        back_populates="execution_results", lazy="select"
    )
    model_call: Mapped[ModelCall | None] = relationship(
        back_populates="execution_results", lazy="select"
    )
