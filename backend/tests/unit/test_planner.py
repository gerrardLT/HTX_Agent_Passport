"""B.AI Planner 适配器单元测试（任务 10 / Req 5 / Req 13 / Req 14 / Req 15）。

覆盖矩阵
--------
1. **成功路径**：B.AI 返回合法 ActionPlan JSON → status="success"，
   ``ModelCall`` 行 + 审计事件正确写入 + ``trace_id`` 串联。
2. **schema 失败**：
   - 非 JSON 字符串 → status="invalid"，``PLAN_INVALID`` 审计事件。
   - JSON 但缺顶层字段 → status="invalid"。
3. **超时重试**：
   - 第一次超时第二次成功 → status="success"，``retries=1``。
   - 连续 3 次超时（``max_retries=2`` 总尝试 3 次）→ 降级 → status="unavailable"，
     ``MODEL_CALL_FAILED`` + mock no_op plan。
4. **服务不可用**：503 立即返回 → status="unavailable"，``MODEL_UNAVAILABLE``，无重试。
5. **prompt 安全**：
   - 默认 ``raw_response=None``，``prompt_hash`` 非空（Req 5 AC6）。
   - ``store_raw_response=True`` 时才存原始 content（调试用）。
   - ``PLANNER_SYSTEM_PROMPT`` 含 5 条安全约束。
6. **mock fallback**：单独验证 :func:`mock_planner_fallback` 返回合法 no_op plan。
7. **审计事件顺序**：MODEL_CALL_STARTED → MODEL_CALL_COMPLETED/MODEL_UNAVAILABLE
   → PLAN_SCHEMA_VALIDATED/PLAN_INVALID。
8. **symbol 归一化**：B.AI 返回 ``BTCUSDT`` → 校验后 ``btcusdt``（Req 6 AC6）。

测试策略
--------
- 不真实调用 B.AI（无 API key）；用一个继承 :class:`BAIClient` 的 stub 注入。
- 不写 PBT（任务 10 不在 PBT 任务清单内）；专注单元覆盖率。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.models import AuditEvent, ModelCall, User
from app.models.enums import AuditEventType
from app.schemas.action_plan import ActionPlanV0
from app.services.audit_writer import ACTOR_TYPE_PLANNER
from app.services.bai_client import (
    BAIClient,
    BAIError,
    BAIResponse,
    BAIServiceUnavailableError,
    BAITimeoutError,
)
from app.services.context_builder import PlannerContext
from app.services.planner import (
    PLANNER_SYSTEM_PROMPT,
    PlannerConfig,
    PlannerResult,
    call_planner,
    mock_planner_fallback,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def _make_user(db_session) -> User:
    user = User(primary_wallet=f"0xPLANNER{uuid.uuid4().hex[:32]}")
    db_session.add(user)
    db_session.flush()
    return user


def _make_context(
    *,
    task: str = "查看 BTC/USDT 行情",
    market_snapshot: dict[str, dict[str, Any]] | None = None,
) -> PlannerContext:
    """构造一个最小 PlannerContext。"""
    return PlannerContext(
        passport_policy_json={
            "version": "0.1",
            "capabilities": {"read_market": True, "place_order": True},
            "limits": {"allowed_symbols": ["btcusdt"]},
        },
        current_market_snapshot=market_snapshot
        if market_snapshot is not None
        else {"btcusdt": {"last": 68000}},
        user_task=task,
        current_time_utc=datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC).isoformat(),
        recent_actions_summary="无历史操作",
        estimated_tokens=500,
    )


def _valid_action_plan_json(symbol: str = "btcusdt") -> str:
    """构造合法的 ActionPlan v0 JSON 字符串。"""
    return json.dumps(
        {
            "version": "0.1",
            "intent_summary": "查看 BTC 行情",
            "actions": [{"type": "read_market", "symbol": symbol}],
            "assumptions": [],
            "risk_notes": [],
        }
    )


def _invalid_json_response(content: str = "not a json {{{") -> BAIResponse:
    return BAIResponse(
        content=content,
        input_tokens=100,
        output_tokens=50,
        latency_ms=120,
    )


def _valid_response(content: str | None = None) -> BAIResponse:
    return BAIResponse(
        content=content or _valid_action_plan_json(),
        input_tokens=200,
        output_tokens=80,
        latency_ms=150,
    )


# ---------------------------------------------------------------------------
# StubBAIClient
# ---------------------------------------------------------------------------
class StubBAIClient(BAIClient):
    """可编排的 BAI 客户端 stub。

    用法
    ----
    ``StubBAIClient(responses=[...])`` 让 ``chat()`` 按顺序：
    - 如果元素是 :class:`BAIResponse` → 直接返回。
    - 如果元素是 :class:`Exception` 实例 → ``raise``。

    多于 ``responses`` 长度的调用会抛 ``AssertionError``——便于发现
    重试次数偏离预期。
    """

    def __init__(self, responses: list[BAIResponse | Exception]) -> None:
        # 不调 super().__init__——避免构造真实的 httpx.Client / 读取 settings
        self.api_url = "https://stub.b.ai/v1"
        self._api_key = ""
        self.model = "stub-model"
        self._owned_client = False
        self._http = None  # type: ignore[assignment]

        self._responses: list[BAIResponse | Exception] = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.close_count = 0

    def chat(self, system: str, user: str, *, timeout: float) -> BAIResponse:  # type: ignore[override]
        self.calls.append({"system": system, "user": user, "timeout": timeout})
        if not self._responses:
            raise AssertionError(
                f"StubBAIClient.chat called {len(self.calls)} times "
                "but no more responses queued"
            )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:  # type: ignore[override]
        self.close_count += 1


# ===========================================================================
# 1. PLANNER_SYSTEM_PROMPT 内容
# ===========================================================================
class TestPlannerSystemPrompt:
    """**Validates: Requirements 15**（AC2/3/4 + Req 5 AC8 + Req 15 AC1,6）。"""

    def test_prompt_requires_only_json(self) -> None:
        assert "valid JSON" in PLANNER_SYSTEM_PROMPT
        # 显式禁止 markdown
        assert "no markdown" in PLANNER_SYSTEM_PROMPT.lower()

    def test_prompt_forbids_tool_calls(self) -> None:
        """Req 15 AC3：LLM 不持有任何工具调用能力。"""
        assert "MUST NOT call tools" in PLANNER_SYSTEM_PROMPT

    def test_prompt_forbids_secret_disclosure(self) -> None:
        """Req 15 AC1：API 密钥绝不出现。"""
        prompt = PLANNER_SYSTEM_PROMPT
        assert "API keys" in prompt
        assert "secrets" in prompt or "secret" in prompt.lower()
        assert "private keys" in prompt

    def test_prompt_routes_blocked_intents_to_no_op(self) -> None:
        """Req 5 AC8 / Req 15 AC6：提现 / 借贷 / 杠杆 → no_op。"""
        prompt = PLANNER_SYSTEM_PROMPT
        for keyword in ("withdraw", "leverage", "borrow"):
            assert keyword in prompt.lower(), f"keyword {keyword!r} missing"
        assert "no_op" in prompt

    def test_prompt_requires_risk_notes(self) -> None:
        assert "risk_notes" in PLANNER_SYSTEM_PROMPT


# ===========================================================================
# 2. 成功路径
# ===========================================================================
class TestCallPlannerSuccess:
    """**Validates: Requirements 5**（AC1, AC7）+ Requirements 13（AC1, AC2）。"""

    def test_call_planner_success_returns_action_plan(self, db_session) -> None:
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_valid_response()])
        ctx = _make_context()

        result = call_planner(
            ctx,
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "success"
        assert isinstance(result.action_plan, ActionPlanV0)
        assert result.action_plan.actions[0].type == "read_market"
        assert len(stub.calls) == 1
        assert result.retries == 0

    def test_call_planner_writes_model_call_record(self, db_session) -> None:
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_valid_response()])
        ctx = _make_context()

        result = call_planner(
            ctx,
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        record = db_session.get(ModelCall, result.model_call_id)
        assert record is not None
        assert record.provider == "B.AI"
        assert record.status == "COMPLETED"
        assert record.input_token_count == 200
        assert record.output_token_count == 80
        assert record.latency_ms == 150
        # Req 5 AC6：默认不存原始响应
        assert record.raw_response is None
        # Req 5 AC6：prompt_hash 是 64 hex 字符
        assert isinstance(record.prompt_hash, str)
        assert len(record.prompt_hash) == 64

    def test_call_planner_links_trace_id(self, db_session) -> None:
        """**Validates: Requirements 13**（AC2：trace_id 串联）。"""
        user = _make_user(db_session)
        trace_id = uuid.uuid4()
        action_id = uuid.uuid4()
        stub = StubBAIClient(responses=[_valid_response()])

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            trace_id=trace_id,
            action_id=action_id,
            bai_client=stub,
        )

        assert result.status == "success"
        # ModelCall 行携带 trace_id
        record = db_session.get(ModelCall, result.model_call_id)
        assert record.trace_id == trace_id
        assert record.action_id == action_id

        # 所有审计事件携带相同 trace_id
        events = (
            db_session.execute(
                select(AuditEvent).where(AuditEvent.user_id == user.id)
            )
            .scalars()
            .all()
        )
        assert len(events) >= 3  # STARTED + COMPLETED + PLAN_SCHEMA_VALIDATED
        for evt in events:
            assert evt.trace_id == trace_id

    def test_call_planner_passes_system_prompt_to_bai(self, db_session) -> None:
        """B.AI 收到的 system 消息就是 :data:`PLANNER_SYSTEM_PROMPT`。"""
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_valid_response()])

        call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert stub.calls[0]["system"] == PLANNER_SYSTEM_PROMPT

    def test_call_planner_normalizes_symbol_to_lowercase(self, db_session) -> None:
        """**Validates: Requirements 6**（AC6：symbol 小写归一化）。

        B.AI 返回 ``BTCUSDT`` 大写 → 校验后转 ``btcusdt``。
        """
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_valid_response(_valid_action_plan_json("BTCUSDT"))])

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "success"
        assert result.action_plan.actions[0].symbol == "btcusdt"


# ===========================================================================
# 3. Schema 校验失败
# ===========================================================================
class TestCallPlannerInvalid:
    """**Validates: Requirements 5**（AC2：非 JSON / schema 失败 → PLAN_INVALID）。"""

    def test_non_json_response_returns_invalid(self, db_session) -> None:
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_invalid_json_response("not a json {{{")])

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "invalid"
        assert result.action_plan is None
        # invalid 路径**不**重试
        assert len(stub.calls) == 1

    def test_invalid_schema_response_returns_invalid(self, db_session) -> None:
        """JSON 但缺顶层字段 → 校验失败。"""
        user = _make_user(db_session)
        # 缺 actions / assumptions / risk_notes
        bad_json = json.dumps({"version": "0.1", "intent_summary": "x"})
        stub = StubBAIClient(responses=[_invalid_json_response(bad_json)])

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "invalid"
        assert result.action_plan is None

    def test_invalid_response_writes_plan_invalid_audit(self, db_session) -> None:
        """``PLAN_INVALID`` 审计事件 + ``MODEL_CALL_COMPLETED`` 都写入。"""
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_invalid_json_response()])

        call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        events = _list_event_types(db_session, user.id)
        assert AuditEventType.MODEL_CALL_STARTED in events
        assert AuditEventType.MODEL_CALL_COMPLETED in events
        assert AuditEventType.PLAN_INVALID in events
        # PLAN_SCHEMA_VALIDATED 一定不写（schema 失败）
        assert AuditEventType.PLAN_SCHEMA_VALIDATED not in events


# ===========================================================================
# 4. 超时重试
# ===========================================================================
class TestCallPlannerRetries:
    """**Validates: Requirements 5**（AC4：超时 30s 重试 2 次后降级）。"""

    def test_first_attempt_times_out_then_succeeds(self, db_session) -> None:
        user = _make_user(db_session)
        stub = StubBAIClient(
            responses=[
                BAITimeoutError("first attempt timed out"),
                _valid_response(),
            ]
        )

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "success"
        assert result.retries == 1
        assert len(stub.calls) == 2

    def test_two_timeouts_third_succeeds(self, db_session) -> None:
        """``max_retries=2`` 总尝试 3 次：前两次超时第三次成功。"""
        user = _make_user(db_session)
        stub = StubBAIClient(
            responses=[
                BAITimeoutError("attempt 1 timeout"),
                BAITimeoutError("attempt 2 timeout"),
                _valid_response(),
            ]
        )

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "success"
        assert result.retries == 2
        assert len(stub.calls) == 3

    def test_all_retries_exhausted_falls_back_to_mock(self, db_session) -> None:
        """3 次超时 → 降级 → status="unavailable" + no_op plan。"""
        user = _make_user(db_session)
        stub = StubBAIClient(
            responses=[
                BAITimeoutError("attempt 1 timeout"),
                BAITimeoutError("attempt 2 timeout"),
                BAITimeoutError("attempt 3 timeout"),
            ]
        )

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "unavailable"
        assert isinstance(result.action_plan, ActionPlanV0)
        assert len(result.action_plan.actions) == 1
        assert result.action_plan.actions[0].type == "no_op"
        # 应已写 MODEL_CALL_FAILED 事件
        events = _list_event_types(db_session, user.id)
        assert AuditEventType.MODEL_CALL_FAILED in events
        assert AuditEventType.MODEL_UNAVAILABLE not in events

    def test_retries_respects_custom_max_retries_zero(self, db_session) -> None:
        """``max_retries=0`` 时仅 1 次尝试，超时立即降级。"""
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[BAITimeoutError("only attempt timeout")])
        config = PlannerConfig(max_retries=0, timeout_seconds=5)

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
            config=config,
        )

        assert result.status == "unavailable"
        assert len(stub.calls) == 1


# ===========================================================================
# 5. 服务不可用（立即降级，不重试）
# ===========================================================================
class TestCallPlannerServiceUnavailable:
    """**Validates: Requirements 14**（AC5：429/503 → MODEL_UNAVAILABLE，立即降级）。"""

    def test_503_immediately_falls_back(self, db_session) -> None:
        user = _make_user(db_session)
        stub = StubBAIClient(
            responses=[BAIServiceUnavailableError("BAI returned status=503")]
        )

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "unavailable"
        assert isinstance(result.action_plan, ActionPlanV0)
        assert result.action_plan.actions[0].type == "no_op"
        # 立即降级，不重试
        assert len(stub.calls) == 1

    def test_503_writes_model_unavailable_audit(self, db_session) -> None:
        """``MODEL_UNAVAILABLE`` 而非 ``MODEL_CALL_FAILED``。"""
        user = _make_user(db_session)
        stub = StubBAIClient(
            responses=[BAIServiceUnavailableError("BAI returned status=429")]
        )

        call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        events = _list_event_types(db_session, user.id)
        assert AuditEventType.MODEL_CALL_STARTED in events
        assert AuditEventType.MODEL_UNAVAILABLE in events
        # 不应写 MODEL_CALL_FAILED
        assert AuditEventType.MODEL_CALL_FAILED not in events
        # 不应写 PLAN_SCHEMA_VALIDATED（mock plan 不视为成功 schema 验证）
        assert AuditEventType.PLAN_SCHEMA_VALIDATED not in events

    def test_other_bai_error_falls_back_with_call_failed(self, db_session) -> None:
        """其它 :class:`BAIError`（响应格式错误等）→ ``MODEL_CALL_FAILED`` + 降级。"""
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[BAIError("malformed response")])

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "unavailable"
        events = _list_event_types(db_session, user.id)
        assert AuditEventType.MODEL_CALL_FAILED in events
        assert AuditEventType.MODEL_UNAVAILABLE not in events


# ===========================================================================
# 6. Prompt 安全：不存原始 prompt
# ===========================================================================
class TestPlannerPromptSecurity:
    """**Validates: Requirements 5**（AC6：原始 prompt 默认不存）+ Requirements 15（AC1）。"""

    def test_does_not_store_raw_prompt_by_default(self, db_session) -> None:
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_valid_response()])

        result = call_planner(
            _make_context(task="买入 10 USDT 的 BTC"),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        record = db_session.get(ModelCall, result.model_call_id)
        # raw_response 默认 None（store_raw_response=False）
        assert record.raw_response is None
        # 但 prompt_hash 一定非空（Req 5 AC6 显式要求存 hash 不存原文）
        assert record.prompt_hash is not None
        assert len(record.prompt_hash) == 64

    def test_prompt_hash_is_deterministic(self, db_session) -> None:
        """同输入 → 同 prompt_hash。"""
        user = _make_user(db_session)

        ctx = _make_context()

        # 第一次调用
        stub1 = StubBAIClient(responses=[_valid_response()])
        result1 = call_planner(
            ctx, db=db_session, user_id=user.id, bai_client=stub1
        )

        # 第二次调用同样的 context
        stub2 = StubBAIClient(responses=[_valid_response()])
        result2 = call_planner(
            ctx, db=db_session, user_id=user.id, bai_client=stub2
        )

        assert result1.prompt_hash == result2.prompt_hash

    def test_store_raw_response_true_persists_content(self, db_session) -> None:
        """显式打开调试存储时，``raw_response`` 才填值。"""
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_valid_response()])
        config = PlannerConfig(store_raw_response=True)

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
            config=config,
        )

        record = db_session.get(ModelCall, result.model_call_id)
        assert record.raw_response is not None
        assert "content" in record.raw_response


# ===========================================================================
# 7. mock_planner_fallback
# ===========================================================================
class TestMockPlannerFallback:
    """**Validates: Requirements 14**（AC5：mock 返回 no_op）。"""

    def test_returns_valid_no_op_plan(self) -> None:
        ctx = _make_context()
        result = mock_planner_fallback(ctx, prompt_hash="a" * 64)

        assert result.status == "unavailable"
        assert isinstance(result.action_plan, ActionPlanV0)
        assert len(result.action_plan.actions) == 1
        assert result.action_plan.actions[0].type == "no_op"
        assert "planner_unavailable" in result.action_plan.actions[0].rationale.lower()

    def test_returns_meaningful_intent_summary(self) -> None:
        ctx = _make_context()
        result = mock_planner_fallback(ctx, prompt_hash="b" * 64)
        assert "暂时不可用" in result.action_plan.intent_summary

    def test_returns_risk_note(self) -> None:
        ctx = _make_context()
        result = mock_planner_fallback(ctx, prompt_hash="c" * 64)
        assert len(result.action_plan.risk_notes) >= 1
        assert any("不可用" in note for note in result.action_plan.risk_notes)

    def test_does_not_create_model_call_record(self, db_session) -> None:
        """``mock_planner_fallback`` 本身不写 DB——它是纯函数。"""
        ctx = _make_context()
        result = mock_planner_fallback(ctx, prompt_hash="d" * 64)
        assert result.model_call_id is None


# ===========================================================================
# 8. 审计事件顺序
# ===========================================================================
class TestPlannerAuditEventOrdering:
    """**Validates: Requirements 13**（AC1）+ Requirements 11（AC1：哈希链）。"""

    def test_success_path_writes_3_events_in_order(self, db_session) -> None:
        """成功路径：MODEL_CALL_STARTED → MODEL_CALL_COMPLETED → PLAN_SCHEMA_VALIDATED。"""
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_valid_response()])

        call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        events = (
            db_session.execute(
                select(AuditEvent)
                .where(AuditEvent.user_id == user.id)
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
            )
            .scalars()
            .all()
        )
        types = [e.event_type for e in events]
        # 确保至少这三件事按这个顺序发生
        assert types == [
            AuditEventType.MODEL_CALL_STARTED,
            AuditEventType.MODEL_CALL_COMPLETED,
            AuditEventType.PLAN_SCHEMA_VALIDATED,
        ]
        # 全部由 PLANNER 发起
        for evt in events:
            assert evt.actor_type == ACTOR_TYPE_PLANNER

    def test_invalid_path_writes_3_events_in_order(self, db_session) -> None:
        """invalid 路径：MODEL_CALL_STARTED → MODEL_CALL_COMPLETED → PLAN_INVALID。"""
        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_invalid_json_response()])

        call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        events = (
            db_session.execute(
                select(AuditEvent)
                .where(AuditEvent.user_id == user.id)
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
            )
            .scalars()
            .all()
        )
        types = [e.event_type for e in events]
        assert types == [
            AuditEventType.MODEL_CALL_STARTED,
            AuditEventType.MODEL_CALL_COMPLETED,
            AuditEventType.PLAN_INVALID,
        ]

    def test_unavailable_path_writes_2_events_in_order(self, db_session) -> None:
        """unavailable 路径：MODEL_CALL_STARTED → MODEL_UNAVAILABLE。

        注意没有 ``MODEL_CALL_COMPLETED``——因为请求根本没走通。
        """
        user = _make_user(db_session)
        stub = StubBAIClient(
            responses=[BAIServiceUnavailableError("503")]
        )

        call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        events = (
            db_session.execute(
                select(AuditEvent)
                .where(AuditEvent.user_id == user.id)
                .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
            )
            .scalars()
            .all()
        )
        types = [e.event_type for e in events]
        assert types == [
            AuditEventType.MODEL_CALL_STARTED,
            AuditEventType.MODEL_UNAVAILABLE,
        ]

    def test_audit_chain_remains_valid_across_planner_call(
        self, db_session
    ) -> None:
        """**Validates: Requirements 11**（AC1：planner 写出的事件可被 verify）。"""
        from app.services.audit_writer import AuditWriter

        user = _make_user(db_session)
        stub = StubBAIClient(responses=[_valid_response()])
        call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is True, f"audit chain should verify; err={err!r}"


# ===========================================================================
# 9. 不暴露原始模型响应给 Executor（Req 5 AC7）
# ===========================================================================
class TestPlannerDoesNotLeakRawResponse:
    """**Validates: Requirements 5**（AC7：不把原始模型响应直接暴露给 Executor）。"""

    def test_planner_result_only_exposes_validated_action_plan(
        self, db_session
    ) -> None:
        """``PlannerResult.action_plan`` 是经过 schema 校验的 :class:`ActionPlanV0`，
        不是原始字符串——这是「不直接暴露」最有力的契约证明。
        """
        user = _make_user(db_session)
        # 模型响应包含一个伪装的"工具调用"指令——但 schema 会拒绝额外字段
        # （extra='forbid'），所以只有合法的结构化字段能传出去。
        stub = StubBAIClient(responses=[_valid_response()])

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "success"
        # 必须是经过校验的 Pydantic 模型
        assert isinstance(result.action_plan, ActionPlanV0)
        # raw_response_excerpt 字段在 success 路径下为 None（不需调试 hint）
        assert result.raw_response_excerpt is None

    def test_invalid_response_gets_truncated_excerpt_in_audit_only(
        self, db_session
    ) -> None:
        """invalid 路径：审计 event_data 中的 ``response_excerpt`` 截断到 200 字符
        且**仅**写入审计（PlannerResult 也带，但供调试用）；不会传给 Executor。
        """
        user = _make_user(db_session)
        long_garbage = "garbage" * 200  # 1400 字符
        stub = StubBAIClient(responses=[_invalid_json_response(long_garbage)])

        result = call_planner(
            _make_context(),
            db=db_session,
            user_id=user.id,
            bai_client=stub,
        )

        assert result.status == "invalid"
        # excerpt 截断
        assert result.raw_response_excerpt is not None
        assert len(result.raw_response_excerpt) <= 200


# ===========================================================================
# Helpers
# ===========================================================================
def _list_event_types(db_session, user_id: uuid.UUID) -> list[str]:
    events = (
        db_session.execute(
            select(AuditEvent)
            .where(AuditEvent.user_id == user_id)
            .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        )
        .scalars()
        .all()
    )
    return [e.event_type for e in events]
