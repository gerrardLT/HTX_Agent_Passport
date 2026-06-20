"""恢复管理器 + 循环检测器 + 检查点管理器（任务 14 / Req 14 / Req 18）。

实现方法论 §11 定义的 5 类失败恢复策略：

1. **工具执行失败**：幂等工具（getTicker / getAccountBalance）自动重试 1 次；
   非幂等工具（placeSpotOrder / cancelOrder）不重试，标记 EXECUTION_FAILED。
2. **模型幻觉**：标记 PLAN_INVALID + 写入 PLAN_HALLUCINATION 审计事件。
3. **部分完成中断**：通过 CheckpointManager 保存/恢复检查点，从最近状态继续。
4. **死循环检测**：LoopDetector 同工具同参数连续 3 次 → 强制中止。
5. **模型不可用**：仅记录 MODEL_UNAVAILABLE 审计事件（降级逻辑在 Planner 适配器）。

所有恢复动作写入对应审计事件，确保错误路径也有完整 trace（Req 14 AC6）。

设计依据
--------
- requirements.md Req 14 AC1-7
- requirements.md Req 18 AC6（循环检测单元测试覆盖）
- design.md「执行层：恢复管理器 + 循环检测器 + 检查点」
- 方法论 §11（错误恢复是主路径）
- 方法论 §11.4（检查点机制）
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.audit_chain import canonical_json
from app.models.enums import ActionState, AuditEventType
from app.services.audit_writer import ACTOR_TYPE_SYSTEM, AuditWriter
from app.services.htx_adapter import TOOL_METADATA


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------
@dataclass
class RetryDecision:
    """工具失败后的重试决策。

    Attributes
    ----------
    retry : bool
        是否应该重试。
    max_attempts : int
        最大重试次数（仅当 retry=True 时有意义）。
    """

    retry: bool
    max_attempts: int = 0


@dataclass
class Checkpoint:
    """检查点数据（Req 14 AC7）。

    包含：action_id / 已完成步骤列表 / 待执行步骤列表 /
    最近工具结果摘要 / 时间戳。

    Attributes
    ----------
    action_id : UUID
        关联的 action 主键。
    completed_steps : list[str]
        已完成的步骤标识列表。
    pending_steps : list[str]
        待执行的步骤标识列表。
    last_tool_results : list[dict[str, Any]]
        最近工具执行结果摘要。
    timestamp : datetime
        检查点创建时间（UTC）。
    """

    action_id: UUID
    completed_steps: list[str] = field(default_factory=list)
    pending_steps: list[str] = field(default_factory=list)
    last_tool_results: list[dict[str, Any]] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> dict[str, Any]:
        """序列化为 JSON 兼容字典（存入 action.checkpoint_json）。"""
        return {
            "action_id": str(self.action_id),
            "completed_steps": self.completed_steps,
            "pending_steps": self.pending_steps,
            "last_tool_results": self.last_tool_results,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Checkpoint:
        """从 JSON 字典反序列化为 Checkpoint 实例。"""
        return cls(
            action_id=UUID(data["action_id"]),
            completed_steps=data["completed_steps"],
            pending_steps=data["pending_steps"],
            last_tool_results=data["last_tool_results"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
        )


# ---------------------------------------------------------------------------
# 循环检测器
# ---------------------------------------------------------------------------
class LoopDetector:
    """方法论 §11: 同工具同参数连续 N 次 → 强制跳出（Req 14 AC4）。

    Parameters
    ----------
    max_repeat : int, default 3
        触发循环检测的连续重复阈值。
    """

    def __init__(self, max_repeat: int = 3) -> None:
        self.max_repeat = max_repeat
        self.history: list[tuple[str, str]] = []

    def record_and_check(self, tool_name: str, params: dict[str, Any]) -> bool:
        """记录一次工具调用并检测是否触发循环。

        Parameters
        ----------
        tool_name : str
            工具名称（如 "getTicker"）。
        params : dict[str, Any]
            工具调用参数。

        Returns
        -------
        bool
            True 表示检测到循环（同工具同参数连续 >= max_repeat 次）。
        """
        params_hash = hashlib.md5(
            canonical_json(params).encode("utf-8")
        ).hexdigest()
        self.history.append((tool_name, params_hash))

        if len(self.history) >= self.max_repeat:
            recent = self.history[-self.max_repeat:]
            if all(item == recent[0] for item in recent):
                return True
        return False

    def reset(self) -> None:
        """清空历史记录。"""
        self.history.clear()


# ---------------------------------------------------------------------------
# 恢复管理器
# ---------------------------------------------------------------------------
class RecoveryManager:
    """方法论 §11 错误恢复是主路径；Req 14 五类失败。

    Parameters
    ----------
    session : Session
        当前 SQLAlchemy 会话。
    audit_writer : AuditWriter | None
        审计写入器实例；为 None 时自动构造。
    """

    def __init__(
        self, session: Session, audit_writer: AuditWriter | None = None
    ) -> None:
        self.session = session
        self.audit_writer = audit_writer or AuditWriter(session)

    # ------------------------------------------------------------------
    # 1. 工具执行失败（Req 14 AC1）
    # ------------------------------------------------------------------
    def handle_tool_failure(
        self,
        action_id: UUID,
        user_id: UUID,
        tool: str,
        error: Exception,
        retryable: bool = False,
    ) -> RetryDecision:
        """工具执行失败：幂等工具重试 1 次；非幂等标记 EXECUTION_FAILED。

        Parameters
        ----------
        action_id : UUID
            关联的 action 主键。
        user_id : UUID
            用户主键（审计事件归属）。
        tool : str
            工具名称（如 "getTicker" / "placeSpotOrder"）。
        error : Exception
            工具执行异常。
        retryable : bool, default False
            调用方标记的可重试标志（如网络超时）。

        Returns
        -------
        RetryDecision
            retry=True 表示应重试（幂等 + retryable）；
            retry=False 表示不重试（已标记 EXECUTION_FAILED + 写入审计）。
        """
        meta = TOOL_METADATA.get(tool, {})
        if meta.get("idempotent") and retryable:
            return RetryDecision(retry=True, max_attempts=1)

        # 非幂等或不可重试 → 标记失败 + 审计
        self._update_action_state(action_id, ActionState.EXECUTION_FAILED)
        self.audit_writer.write(
            event_type=AuditEventType.EXECUTION_FAILED,
            user_id=user_id,
            action_id=action_id,
            actor_type=ACTOR_TYPE_SYSTEM,
            event_data={
                "tool": tool,
                "error": str(error),
                "retryable": retryable,
            },
        )
        return RetryDecision(retry=False)

    # ------------------------------------------------------------------
    # 2. 模型幻觉（Req 14 AC2）
    # ------------------------------------------------------------------
    def handle_hallucination(
        self,
        action_id: UUID,
        user_id: UUID,
        hallucinated_fields: list[str],
    ) -> None:
        """模型幻觉：PLAN_INVALID + PLAN_HALLUCINATION 审计。

        Parameters
        ----------
        action_id : UUID
            关联的 action 主键。
        user_id : UUID
            用户主键。
        hallucinated_fields : list[str]
            幻觉字段列表（如 ["symbol:FAKEUSDT", "price:99999"]）。
        """
        self._update_action_state(action_id, ActionState.PLAN_INVALID)
        self.audit_writer.write(
            event_type=AuditEventType.PLAN_HALLUCINATION,
            user_id=user_id,
            action_id=action_id,
            actor_type=ACTOR_TYPE_SYSTEM,
            event_data={"hallucinated_fields": hallucinated_fields},
        )

    # ------------------------------------------------------------------
    # 3. 死循环检测（Req 14 AC4）
    # ------------------------------------------------------------------
    def handle_loop_detected(
        self,
        action_id: UUID,
        user_id: UUID,
        tool: str,
        params: dict[str, Any],
        count: int,
    ) -> None:
        """死循环：FAILED + LOOP_DETECTED 审计。

        Parameters
        ----------
        action_id : UUID
            关联的 action 主键。
        user_id : UUID
            用户主键。
        tool : str
            触发循环的工具名称。
        params : dict[str, Any]
            触发循环的参数。
        count : int
            连续重复次数。
        """
        self._update_action_state(action_id, ActionState.FAILED)
        self.audit_writer.write(
            event_type=AuditEventType.LOOP_DETECTED,
            user_id=user_id,
            action_id=action_id,
            actor_type=ACTOR_TYPE_SYSTEM,
            event_data={
                "tool": tool,
                "params_summary": str(params)[:200],
                "count": count,
            },
        )

    # ------------------------------------------------------------------
    # 4. 模型不可用（Req 14 AC5）
    # ------------------------------------------------------------------
    def handle_model_unavailable(
        self, action_id: UUID, user_id: UUID
    ) -> None:
        """模型不可用：仅记录审计（降级逻辑在 Planner 适配器）。

        Parameters
        ----------
        action_id : UUID
            关联的 action 主键。
        user_id : UUID
            用户主键。
        """
        self.audit_writer.write(
            event_type=AuditEventType.MODEL_UNAVAILABLE,
            user_id=user_id,
            action_id=action_id,
            actor_type=ACTOR_TYPE_SYSTEM,
            event_data={
                "message": "B.AI service unavailable, degraded to mock planner",
            },
        )

    # ------------------------------------------------------------------
    # 5. 检查点管理（Req 14 AC3, AC7）
    # ------------------------------------------------------------------
    def save_checkpoint(self, action_id: UUID, checkpoint: Checkpoint) -> None:
        """保存检查点到 action.checkpoint_json。

        Parameters
        ----------
        action_id : UUID
            关联的 action 主键。
        checkpoint : Checkpoint
            检查点数据。
        """
        from app.models import AgentAction

        action = self.session.get(AgentAction, action_id)
        if action:
            action.checkpoint_json = checkpoint.to_json()
            self.session.flush()

    def restore_checkpoint(self, action_id: UUID) -> Checkpoint | None:
        """从 action.checkpoint_json 恢复检查点。

        Parameters
        ----------
        action_id : UUID
            关联的 action 主键。

        Returns
        -------
        Checkpoint | None
            恢复的检查点；无检查点时返回 None。
        """
        from app.models import AgentAction

        action = self.session.get(AgentAction, action_id)
        if action and action.checkpoint_json:
            return Checkpoint.from_json(action.checkpoint_json)
        return None

    # ------------------------------------------------------------------
    # 内部 helpers
    # ------------------------------------------------------------------
    def _update_action_state(self, action_id: UUID, new_state: str) -> None:
        """更新 action 状态。"""
        from app.models import AgentAction

        action = self.session.get(AgentAction, action_id)
        if action:
            action.state = new_state
            self.session.flush()


__all__ = [
    "Checkpoint",
    "LoopDetector",
    "RecoveryManager",
    "RetryDecision",
]
