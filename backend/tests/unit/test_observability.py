"""任务 15 可观测性服务单元测试。

覆盖维度（Req 13 AC1-6）：
1. 决策日志记录
2. 执行日志记录
3. 质量信号记录
4. trace_id 串联过滤
5. 所有日志包含 trace_id
6. token 消耗正常 / 异常 / 样本不足
"""

from __future__ import annotations

import uuid

import pytest

from app.services.observability import (
    DecisionLog,
    ExecutionLog,
    ObservabilityService,
    QualitySignal,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def svc() -> ObservabilityService:
    return ObservabilityService()


@pytest.fixture()
def trace_id() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# 决策日志
# ---------------------------------------------------------------------------
class TestLogDecision:
    def test_log_decision_stores_entry(self, svc: ObservabilityService, trace_id: uuid.UUID):
        """记录决策日志后可通过 get_all_logs 获取。"""
        decision = DecisionLog(
            route_type="rule_router",
            capability_envelope={"read_market": True, "place_order": False},
            model_choice="mock_planner",
        )
        svc.log_decision(trace_id, decision)

        logs = svc.get_all_logs()
        assert len(logs) == 1
        entry = logs[0]
        assert entry["type"] == "decision"
        assert entry["trace_id"] == str(trace_id)
        assert entry["route_type"] == "rule_router"
        assert entry["capability_envelope"] == {"read_market": True, "place_order": False}
        assert entry["model_choice"] == "mock_planner"
        assert "timestamp" in entry


# ---------------------------------------------------------------------------
# 执行日志
# ---------------------------------------------------------------------------
class TestLogExecution:
    def test_log_execution_stores_entry(self, svc: ObservabilityService, trace_id: uuid.UUID):
        """记录执行日志后可通过 get_all_logs 获取。"""
        execution = ExecutionLog(
            tool_name="getTicker",
            params_summary="symbol=btcusdt",
            latency_ms=120,
            retry_count=1,
            batch_id="batch-001",
            concurrent=True,
        )
        svc.log_execution(trace_id, execution)

        logs = svc.get_all_logs()
        assert len(logs) == 1
        entry = logs[0]
        assert entry["type"] == "execution"
        assert entry["trace_id"] == str(trace_id)
        assert entry["tool_name"] == "getTicker"
        assert entry["params_summary"] == "symbol=btcusdt"
        assert entry["latency_ms"] == 120
        assert entry["retry_count"] == 1
        assert entry["batch_id"] == "batch-001"
        assert entry["concurrent"] is True
        assert "timestamp" in entry


# ---------------------------------------------------------------------------
# 质量信号
# ---------------------------------------------------------------------------
class TestLogQualitySignal:
    def test_log_quality_signal_stores_entry(self, svc: ObservabilityService, trace_id: uuid.UUID):
        """记录质量信号后可通过 get_all_logs 获取。"""
        signal = QualitySignal(
            policy_reject_rate=0.15,
            planner_retry_count=2,
            hallucination_detected=True,
        )
        svc.log_quality_signal(trace_id, signal)

        logs = svc.get_all_logs()
        assert len(logs) == 1
        entry = logs[0]
        assert entry["type"] == "quality"
        assert entry["trace_id"] == str(trace_id)
        assert entry["policy_reject_rate"] == 0.15
        assert entry["planner_retry_count"] == 2
        assert entry["hallucination_detected"] is True
        assert "timestamp" in entry


# ---------------------------------------------------------------------------
# trace_id 串联
# ---------------------------------------------------------------------------
class TestTraceFiltering:
    def test_get_logs_by_trace_filters_correctly(self, svc: ObservabilityService):
        """按 trace_id 过滤只返回对应日志。"""
        trace_a = uuid.uuid4()
        trace_b = uuid.uuid4()

        svc.log_decision(trace_a, DecisionLog("rule_router", {}, "mock_planner"))
        svc.log_execution(trace_b, ExecutionLog("getTicker", "sym=btc", 50))
        svc.log_quality_signal(trace_a, QualitySignal(0.1, 0, False))

        logs_a = svc.get_logs_by_trace(trace_a)
        logs_b = svc.get_logs_by_trace(trace_b)

        assert len(logs_a) == 2
        assert len(logs_b) == 1
        assert all(log["trace_id"] == str(trace_a) for log in logs_a)
        assert logs_b[0]["trace_id"] == str(trace_b)

    def test_all_logs_contain_trace_id(self, svc: ObservabilityService):
        """所有日志条目都包含 trace_id 字段。"""
        tid = uuid.uuid4()
        svc.log_decision(tid, DecisionLog("llm_router", {"cap": True}, "bai_planner"))
        svc.log_execution(tid, ExecutionLog("placeSpotOrder", "amount=10", 200))
        svc.log_quality_signal(tid, QualitySignal())

        for log in svc.get_all_logs():
            assert "trace_id" in log
            assert log["trace_id"] == str(tid)


# ---------------------------------------------------------------------------
# Token 消耗 & 成本异常
# ---------------------------------------------------------------------------
class TestTokenUsage:
    def test_record_token_usage_normal(self, svc: ObservabilityService):
        """正常消耗不触发告警。"""
        tid = uuid.uuid4()
        # 先积累足够样本
        svc.record_token_usage(tid, 100, 100)  # total=200
        svc.record_token_usage(tid, 100, 100)  # total=200
        # 第三次正常范围（avg=200, 300 < 200*2=400）
        result = svc.record_token_usage(tid, 150, 150)  # total=300
        assert result is False

    def test_record_token_usage_anomaly(self, svc: ObservabilityService):
        """超 200% 均值触发 COST_ANOMALY 告警。"""
        tid = uuid.uuid4()
        # 积累样本：均值 = 200
        svc.record_token_usage(tid, 100, 100)  # total=200
        svc.record_token_usage(tid, 100, 100)  # total=200
        # 第三次超过 200% 均值（avg=200, 500 > 400）
        result = svc.record_token_usage(tid, 300, 200)  # total=500
        assert result is True

    def test_record_token_usage_insufficient_samples(self, svc: ObservabilityService):
        """样本不足（< 3 条）不触发告警。"""
        tid = uuid.uuid4()
        # 只有 1 条记录
        result = svc.record_token_usage(tid, 100, 100)
        assert result is False
        # 只有 2 条记录
        result = svc.record_token_usage(tid, 10000, 10000)
        assert result is False
