"""声誉服务（任务 15 / Req 24）。

根据 action 最终结果更新 passport 的 reputation_score：
- EXECUTED → +2
- AUTO_REJECTED → -3
- REJECTED_BY_USER → -1
- EXECUTION_FAILED / FAILED → -5

分数范围 [0, 100]，更新后写入 REPUTATION_UPDATED 审计事件。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import AgentPassport
from app.models.enums import AuditEventType
from app.services.audit_writer import ACTOR_TYPE_SYSTEM, AuditWriter

# ---------------------------------------------------------------------------
# 声誉增减规则
# ---------------------------------------------------------------------------
REPUTATION_DELTAS: dict[str, int] = {
    "EXECUTED": +2,
    "AUTO_REJECTED": -3,
    "REJECTED_BY_USER": -1,
    "EXECUTION_FAILED": -5,
    "FAILED": -5,
}


# ---------------------------------------------------------------------------
# ReputationService
# ---------------------------------------------------------------------------
class ReputationService:
    """声誉服务（Req 24）。

    Parameters
    ----------
    session : Session
        当前请求的 SQLAlchemy 会话。
    audit_writer : AuditWriter | None
        审计写入器；为 None 时自动构造。
    """

    def __init__(
        self, session: Session, audit_writer: AuditWriter | None = None
    ) -> None:
        self.session = session
        self.audit_writer = audit_writer or AuditWriter(session)

    def update_reputation(self, passport_id: uuid.UUID, action_outcome: str) -> int:
        """根据 action 结果更新声誉分。返回新分数。

        Parameters
        ----------
        passport_id : uuid.UUID
            目标 passport 的主键。
        action_outcome : str
            action 最终状态（EXECUTED / AUTO_REJECTED / REJECTED_BY_USER /
            EXECUTION_FAILED / FAILED）。

        Returns
        -------
        int
            更新后的 reputation_score。若 outcome 不在规则表中则不变。
        """
        delta = REPUTATION_DELTAS.get(action_outcome, 0)
        if delta == 0:
            return self._get_current_score(passport_id)

        passport = self.session.get(AgentPassport, passport_id)
        if not passport:
            return 0

        old_score = passport.reputation_score
        new_score = max(0, min(100, old_score + delta))
        passport.reputation_score = new_score
        self.session.flush()

        # 写入 REPUTATION_UPDATED 审计事件（Req 24 AC4）
        self.audit_writer.write(
            event_type=AuditEventType.REPUTATION_UPDATED,
            user_id=passport.user_id,
            passport_id=passport_id,
            actor_type=ACTOR_TYPE_SYSTEM,
            actor_id="SYSTEM",
            event_data={
                "previous_score": old_score,
                "new_score": new_score,
                "delta": delta,
                "reason": action_outcome,
            },
        )
        return new_score

    def _get_current_score(self, passport_id: uuid.UUID) -> int:
        """获取当前声誉分。"""
        passport = self.session.get(AgentPassport, passport_id)
        return passport.reputation_score if passport else 0


__all__ = [
    "REPUTATION_DELTAS",
    "ReputationService",
]
