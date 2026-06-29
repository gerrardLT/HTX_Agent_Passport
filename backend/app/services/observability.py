"""可观测性服务（任务 15 / Req 13 / 方法论 §15）。

聚合 4 类数据：
1. 决策日志（DecisionLog）：路由决策 + 能力包 + 模型选择
2. 执行日志（ExecutionLog）：工具名 / 参数 / 耗时 / 重试 / 批次
3. 质量信号（QualitySignal）：拦截率 / 重试率 / 幻觉率
4. trace 链路：request_id（trace_id）串联前三类

成本异常告警：单次 token 消耗 > 历史均值 200% 时标记 COST_ANOMALY（Req 13 AC6 / Req 23 AC6）。

MVP 阶段使用内存存储；后续可替换为 OpenTelemetry / Prometheus exporter。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class DecisionLog:
    """决策日志：路由决策 + 能力包 + 模型选择（Req 13 AC3）。"""

    route_type: str  # "rule_router" | "llm_router"
    capability_envelope: dict[str, Any]
    model_choice: str  # "bai_planner" | "mock_planner"


@dataclass
class ExecutionLog:
    """执行日志：工具名 / 参数 / 耗时 / 重试 / 批次（Req 13 AC4）。"""

    tool_name: str
    params_summary: str
    latency_ms: int
    retry_count: int = 0
    batch_id: str = ""
    concurrent: bool = False


@dataclass
class QualitySignal:
    """质量信号：拦截率 / 重试率 / 幻觉率（Req 13 AC5）。"""

    policy_reject_rate: float = 0.0
    planner_retry_count: int = 0
    hallucination_detected: bool = False


# ---------------------------------------------------------------------------
# ObservabilityService
# ---------------------------------------------------------------------------
class ObservabilityService:
    """可观测性服务（Req 13 / 方法论 §15）。

    职责：
    - 记录 4 类数据（决策 / 执行 / 质量 / trace）
    - 按 trace_id 串联所有日志
    - 检测成本异常（单次 token > 200% 均值 → COST_ANOMALY）
    """

    def __init__(self) -> None:
        self._logs: list[dict[str, Any]] = []  # 内存存储（MVP 阶段）
        self._token_history: list[int] = []  # token 消耗历史

    # ------------------------------------------------------------------
    # 决策日志
    # ------------------------------------------------------------------
    def log_decision(self, trace_id: uuid.UUID, decision: DecisionLog) -> None:
        """记录决策日志（Req 13 AC3）。"""
        entry: dict[str, Any] = {
            "type": "decision",
            "trace_id": str(trace_id),
            "route_type": decision.route_type,
            "capability_envelope": decision.capability_envelope,
            "model_choice": decision.model_choice,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._logs.append(entry)
        logger.info("decision_log", extra=entry)

    # ------------------------------------------------------------------
    # 执行日志
    # ------------------------------------------------------------------
    def log_execution(self, trace_id: uuid.UUID, execution: ExecutionLog) -> None:
        """记录执行日志（Req 13 AC4）。"""
        entry: dict[str, Any] = {
            "type": "execution",
            "trace_id": str(trace_id),
            "tool_name": execution.tool_name,
            "params_summary": execution.params_summary,
            "latency_ms": execution.latency_ms,
            "retry_count": execution.retry_count,
            "batch_id": execution.batch_id,
            "concurrent": execution.concurrent,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._logs.append(entry)
        logger.info("execution_log", extra=entry)

    # ------------------------------------------------------------------
    # 质量信号
    # ------------------------------------------------------------------
    def log_quality_signal(self, trace_id: uuid.UUID, signal: QualitySignal) -> None:
        """记录质量信号（Req 13 AC5）。"""
        entry: dict[str, Any] = {
            "type": "quality",
            "trace_id": str(trace_id),
            "policy_reject_rate": signal.policy_reject_rate,
            "planner_retry_count": signal.planner_retry_count,
            "hallucination_detected": signal.hallucination_detected,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._logs.append(entry)
        logger.info("quality_signal", extra=entry)

    # ------------------------------------------------------------------
    # Token 消耗 & 成本异常检测
    # ------------------------------------------------------------------
    def record_token_usage(
        self, trace_id: uuid.UUID, input_tokens: int, output_tokens: int
    ) -> bool:
        """记录 token 消耗并检测异常（> 200% 均值）。

        返回 True 表示异常（COST_ANOMALY），False 表示正常。
        样本不足（< 3 条历史）时不触发告警。
        """
        total = input_tokens + output_tokens
        self._token_history.append(total)

        if len(self._token_history) < 3:
            return False  # 样本不足，不判断

        # 计算当前条目之前的历史均值
        history_before = self._token_history[:-1]
        avg = sum(history_before) / len(history_before)

        if avg > 0 and total > avg * 2:
            logger.warning(
                "COST_ANOMALY",
                extra={
                    "trace_id": str(trace_id),
                    "total_tokens": total,
                    "avg": avg,
                },
            )
            return True

        return False

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_logs_by_trace(self, trace_id: uuid.UUID) -> list[dict[str, Any]]:
        """按 trace_id 获取所有日志（Req 13 AC2）。"""
        tid = str(trace_id)
        return [log for log in self._logs if log.get("trace_id") == tid]

    def get_all_logs(self) -> list[dict[str, Any]]:
        """获取所有日志。"""
        return list(self._logs)


__all__ = [
    "DecisionLog",
    "ExecutionLog",
    "ObservabilityService",
    "QualitySignal",
]
