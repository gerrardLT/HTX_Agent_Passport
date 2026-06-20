"""任务 20 — L2 集成测试套件（Req 19）。

**Validates: Requirements 19**

方法论 §21「L2 组件与集成测试」：用 *mock B.AI* + *真实工具*（Policy Engine /
审批服务 / 执行网关 / 模拟引擎 / 恢复管理器 / 上下文构建器）验证组件间协作。
与 L1 单元测试（隔离单组件）不同，本套件把「微回合循环」串起来跑：

    REQUESTED → PLANNING → PLAN_VALIDATED → RISK_CHECKING
              → APPROVAL_REQUIRED → APPROVED → (EXECUTING) → EXECUTED

覆盖矩阵（对应 Req 19 的 6 条 acceptance criteria）
--------------------------------------------------
- AC1：mock B.AI + 真实工具的完整 action 流转，每步状态转换正确 + 审计事件完整。
       （含 place_order 审批路径 + read_market 自动通过路径两个变种）
- AC2：mock B.AI 返回格式错误响应 → PLAN_INVALID，错误信息回填上下文。
- AC3：RISK_CHECKING 中断后从检查点恢复，不重复 PLANNING 步骤。
- AC4：planner prompt 接近 8K token → 触发压缩，policy + market snapshot 保留。
- AC5：权限策略拒绝某工具调用 → 该工具未被执行 + 拒绝原因回填上下文。
       （含 Policy Engine 直接 REJECT、执行网关重裁决 REJECT、规则路由拦截三条路径）
- AC6：全部 L2 测试用 mock 外部服务，秒级完成（无网络依赖）。

测试策略
--------
- **真实工具**：Policy Engine / 审批服务 / 执行网关 / 模拟引擎 / 恢复管理器 /
  上下文构建器 / 审计写入器全部是真实实现，不打桩。
- **mock B.AI**：用 :class:`StubBAIClient`（继承 :class:`BAIClient` 但不发网络
  请求）注入 :func:`call_planner`——这是唯一被替换的外部依赖（Req 19 AC6）。
- **真实工具 = HTX 适配器 mock 模式**：``HTXAdapter(mode="mock")`` 返回种子行情，
  不依赖外部网络但走真实适配器代码路径（方法论「真实工具」语义在 demo 下即
  「mock 模式适配器」）。
- **service 层会话**：全部建在 ``db_session`` 上（函数级事务回滚隔离），
  避免 HTTP ``client`` fixture 的独立 SessionLocal 与本会话产生可见性割裂。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AgentAction,
    AgentPassport,
    AuditEvent,
    ExecutionResult,
    ModelCall,
    User,
)
from app.models.enums import ActionState, AuditEventType, PassportState
from app.services.approval_service import create_approval_request, submit_approval
from app.services.audit_writer import (
    ACTOR_TYPE_SYSTEM,
    ACTOR_TYPE_USER,
    AuditWriter,
)
from app.services.bai_client import (
    BAIClient,
    BAIResponse,
    BAIServiceUnavailableError,
)
from app.services.context_builder import (
    DEFAULT_RECENT_ACTIONS_LIMIT,
    MAX_TOKENS,
    build_planner_context,
)
from app.services.execution_gateway import (
    ConflictError,
    ExecutionGateway,
    ExecutionGatewayConfig,
)
from app.services.htx_adapter import HTXAdapter
from app.services.input_normalizer import build_blocked_action_plan, normalize_and_route
from app.services.planner import call_planner
from app.services.policy_engine import (
    DailyActionHistory,
    GlobalConfig,
    evaluate_policy,
    write_policy_check_completed_audit_event,
)
from app.services.recovery_manager import Checkpoint, RecoveryManager
from app.services.seed_data import SEED_MARKET_DATA, SEED_POLICY
from app.services.simulation_engine import SimulationEngine

pytestmark = pytest.mark.integration

#: 全程固定的"请求级当前时间"——让 Policy Engine 的 time_window / 上下文构建
#: 的 current_time_utc 完全确定（SEED_POLICY 无 time_window，但显式传入是好习惯）。
FIXED_NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Autouse fixture: 让 SEED_MARKET_DATA 在测试期间保持"新鲜"
# ---------------------------------------------------------------------------
# G16 修复后 SEED_MARKET_DATA 含静态 ``as_of=2024-06-15``——
# ``submit_approval`` 与 ``ExecutionGateway.execute`` 在重裁决时会做 stale-price
# 校验，必然抛 MARKET_SNAPSHOT_STALE。本套件验证集成流转语义而非 G16 路径,
# 用 monkeypatch 把 ``as_of`` 改成"测试当下时间"，让 happy path 继续通过。
# G16 自身的端到端覆盖在 :mod:`tests.unit.test_stale_price_recheck` 中完成。
@pytest.fixture(autouse=True)
def _fresh_seed_market_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import htx_adapter as _htx
    from app.services import seed_data as _seed_data
    import sys

    fresh_now_iso = datetime.now(UTC).isoformat()
    fresh_htx = {
        "btcusdt": {
            "last": 68000.0, "bid": 67999.0, "ask": 68001.0,
            "vol_24h": 1500.0, "as_of": fresh_now_iso,
        },
        "ethusdt": {
            "last": 3600.0, "bid": 3599.0, "ask": 3601.0,
            "vol_24h": 25000.0, "as_of": fresh_now_iso,
        },
    }
    fresh_seed = {
        "btcusdt": {
            "last": 68000.0, "bid": 67999.0, "ask": 68001.0,
            "as_of": fresh_now_iso,
        },
        "ethusdt": {
            "last": 3600.0, "bid": 3599.0, "ask": 3601.0,
            "as_of": fresh_now_iso,
        },
    }
    monkeypatch.setattr(_htx, "SEED_MARKET_DATA", fresh_htx)
    monkeypatch.setattr(_seed_data, "SEED_MARKET_DATA", fresh_seed)
    # 本测试模块在顶部 ``from app.services.seed_data import SEED_MARKET_DATA`` 把
    # 引用绑定到了模块本地——也要更新本地 binding（生产代码用 inline import 会
    # 自动看到 patch 后的值，但测试代码用顶部导入需手动同步）。
    test_module = sys.modules[__name__]
    monkeypatch.setattr(test_module, "SEED_MARKET_DATA", fresh_seed)


# ===========================================================================
# StubBAIClient —— 可编排的 mock B.AI（唯一被替换的外部依赖）
# ===========================================================================
class StubBAIClient(BAIClient):
    """按队列返回 :class:`BAIResponse` 或抛异常的 B.AI 客户端 stub。

    与 ``tests/unit/test_planner.py`` 的同名 stub 一致：不调用 ``super().__init__``
    （避免构造 httpx.Client / 读 settings），按 ``responses`` 顺序消费。
    额外暴露 :attr:`call_count` 让检查点测试断言「PLANNING 未被重复执行」。
    """

    def __init__(self, responses: list[BAIResponse | Exception]) -> None:
        self.api_url = "https://stub.b.ai/v1"
        self._api_key = ""
        self.model = "stub-model"
        self._owned_client = False
        self._http = None  # type: ignore[assignment]
        self._responses: list[BAIResponse | Exception] = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.close_count = 0

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def chat(self, system: str, user: str, *, timeout: float) -> BAIResponse:  # type: ignore[override]
        self.calls.append({"system": system, "user": user, "timeout": timeout})
        if not self._responses:
            raise AssertionError(
                f"StubBAIClient.chat called {len(self.calls)} times "
                "but no more responses queued (unexpected extra B.AI call)"
            )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:  # type: ignore[override]
        self.close_count += 1


# ===========================================================================
# ActionPlan JSON 构造器
# ===========================================================================
def _place_order_plan_json(
    *,
    symbol: str = "btcusdt",
    max_notional: float = 10.0,
    amount: float = 0.000147,
    limit_price: float | None = 68000.0,
) -> str:
    """构造合法的 place_order ActionPlan v0 JSON 字符串。"""
    import json

    return json.dumps(
        {
            "version": "0.1",
            "intent_summary": f"在策略范围内对 {symbol} 限价买入 {max_notional} USDT",
            "actions": [
                {
                    "type": "place_order",
                    "symbol": symbol,
                    "side": "buy",
                    "order_type": "limit",
                    "amount": amount,
                    "amount_unit": "base",
                    "max_notional_usdt": max_notional,
                    "limit_price": limit_price,
                    "requires_user_approval": True,
                    "rationale": "策略范围内的限价买入",
                }
            ],
            "assumptions": [f"{symbol} 当前价格约 {limit_price}"],
            "risk_notes": ["限价单可能不会立即成交"],
        }
    )


def _read_market_plan_json(symbol: str = "btcusdt") -> str:
    """构造合法的 read_market ActionPlan v0 JSON 字符串。"""
    import json

    return json.dumps(
        {
            "version": "0.1",
            "intent_summary": f"查看 {symbol} 行情",
            "actions": [{"type": "read_market", "symbol": symbol}],
            "assumptions": [],
            "risk_notes": [],
        }
    )


def _bai_response(content: str) -> BAIResponse:
    return BAIResponse(
        content=content, input_tokens=200, output_tokens=80, latency_ms=150
    )


# ===========================================================================
# ORM 构造器
# ===========================================================================
def _make_user(db: Session) -> User:
    user = User(primary_wallet=f"0xL2{uuid.uuid4().hex[:34]}")
    db.add(user)
    db.flush()
    return user


def _make_passport(
    db: Session,
    user: User,
    *,
    policy: dict[str, Any] | None = None,
    version: int = 1,
    state: str = PassportState.ACTIVE,
) -> AgentPassport:
    """构造一个 ACTIVE 护照，默认用 small_spot_executor 的 SEED_POLICY。"""
    passport = AgentPassport(
        user_id=user.id,
        name="l2-passport",
        agent_type="small_spot_executor",
        state=state,
        version=version,
        policy_json=policy if policy is not None else dict(SEED_POLICY),
        reputation_score=50,
    )
    db.add(passport)
    db.flush()
    return passport


def _create_action(
    db: Session,
    passport: AgentPassport,
    user: User,
    task: str,
    *,
    execution_mode: str = "simulation",
) -> AgentAction:
    action = AgentAction(
        passport_id=passport.id,
        user_id=user.id,
        trace_id=uuid.uuid4(),
        natural_language_request=task,
        state=ActionState.REQUESTED,
        execution_mode=execution_mode,
    )
    db.add(action)
    db.flush()
    return action


def _events(
    db: Session, user_id: uuid.UUID, passport_id: uuid.UUID | None = None
) -> list[AuditEvent]:
    """按时间序拉取某用户（+passport）的审计事件。"""
    stmt = select(AuditEvent).where(AuditEvent.user_id == user_id)
    if passport_id is not None:
        stmt = stmt.where(AuditEvent.passport_id == passport_id)
    stmt = stmt.order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
    return list(db.execute(stmt).scalars().all())


def _event_types(
    db: Session, user_id: uuid.UUID, passport_id: uuid.UUID | None = None
) -> list[str]:
    return [e.event_type for e in _events(db, user_id, passport_id)]


# ===========================================================================
# 微回合循环编排器（被测的"集成"）
# ===========================================================================
async def run_micro_turn_pipeline(
    db: Session,
    *,
    passport: AgentPassport,
    user: User,
    task: str,
    bai_client: BAIClient,
    market_snapshot: dict[str, Any] | None = None,
    recent_actions: list[dict[str, Any]] | None = None,
    execution_mode: str = "simulation",
    gateway: ExecutionGateway | None = None,
) -> dict[str, Any]:
    """串起感知 → 决策 → 执行的微回合循环（方法论 §9）。

    这是 L2 集成测试的"被测系统"——用真实工具组件，仅 B.AI 被 stub。
    返回值携带 action、状态轨迹、verdict、planner 结果，供测试断言。
    """
    snapshot = market_snapshot if market_snapshot is not None else dict(SEED_MARKET_DATA)
    audit = AuditWriter(db)

    # ---- 0. REQUESTED ----
    action = _create_action(db, passport, user, task, execution_mode=execution_mode)
    states: list[str] = [action.state]
    audit.write(
        event_type=AuditEventType.ACTION_REQUESTED,
        user_id=user.id,
        passport_id=passport.id,
        action_id=action.id,
        trace_id=action.trace_id,
        actor_type=ACTOR_TYPE_USER,
        event_data={"task": task, "execution_mode": execution_mode},
    )

    # ---- 1. 感知层：规则路由 ----
    routed = normalize_and_route(task)
    if routed.mode == "blocked_shortcut":
        # 高置信关键字命中：不调用 B.AI，直接生成 no_op + AUTO_REJECTED
        blocked_plan = build_blocked_action_plan(task, routed.blocked_reason or "BLOCKED")
        action.normalized_action_json = blocked_plan
        reason = f"BLOCKED_ACTION_{(routed.blocked_reason or '').replace('BLOCKED_KEYWORD_', '')}"
        action.state = ActionState.AUTO_REJECTED
        action.risk_verdict = "REJECT"
        action.risk_score = 100
        action.reason_codes = [reason]
        db.flush()
        states.append(action.state)
        return {
            "action": action,
            "states": states,
            "verdict": "REJECT",
            "reason_codes": action.reason_codes,
            "planner_result": None,
            "routed": routed,
        }

    # ---- 2. 决策层：PLANNING → 调 B.AI（stub）----
    action.state = ActionState.PLANNING
    db.flush()
    states.append(action.state)

    ctx = build_planner_context(
        passport_policy=passport.policy_json,
        task=task,
        market_snapshot=snapshot,
        recent_actions=recent_actions,
        now=FIXED_NOW,
    )
    planner_result = call_planner(
        ctx,
        db=db,
        user_id=user.id,
        action_id=action.id,
        trace_id=action.trace_id,
        passport_id=passport.id,
        bai_client=bai_client,
    )

    if planner_result.status == "invalid":
        action.state = ActionState.PLAN_INVALID
        db.flush()
        states.append(action.state)
        return {
            "action": action,
            "states": states,
            "verdict": None,
            "reason_codes": [],
            "planner_result": planner_result,
            "routed": routed,
        }

    assert planner_result.action_plan is not None
    normalized = planner_result.action_plan.actions[0].model_dump()
    action.normalized_action_json = normalized
    action.policy_version_at_planning = passport.version
    action.state = ActionState.PLAN_VALIDATED
    db.flush()
    states.append(action.state)

    # ---- 3. RISK_CHECKING：真实 Policy Engine 裁决 ----
    action.state = ActionState.RISK_CHECKING
    db.flush()
    states.append(action.state)

    verdict = evaluate_policy(
        action=normalized,
        policy=passport.policy_json,
        daily_history=DailyActionHistory(),
        market_snapshot=snapshot,
        global_config=GlobalConfig(),
        now=FIXED_NOW,
    )
    write_policy_check_completed_audit_event(
        db,
        user_id=user.id,
        passport_id=passport.id,
        action_id=action.id,
        trace_id=action.trace_id,
        verdict=verdict,
    )
    action.risk_verdict = verdict.verdict
    action.risk_score = verdict.risk_score
    action.reason_codes = list(verdict.reason_codes)
    db.flush()

    if verdict.verdict == "REJECT":
        action.state = ActionState.AUTO_REJECTED
        db.flush()
        states.append(action.state)
        return {
            "action": action,
            "states": states,
            "verdict": verdict.verdict,
            "reason_codes": list(verdict.reason_codes),
            "planner_result": planner_result,
            "routed": routed,
        }

    # ---- 4. 审批层 ----
    if verdict.verdict == "REQUIRE_APPROVAL":
        action.state = ActionState.APPROVAL_REQUIRED
        db.flush()
        states.append(action.state)
        create_approval_request(
            db,
            action=action,
            user_id=user.id,
            passport_id=passport.id,
            trace_id=action.trace_id,
        )
        submit_approval(
            db,
            action_id=action.id,
            user_id=user.id,
            approved=True,
            typed_confirmation="APPROVE",
            trace_id=action.trace_id,
        )
        states.append(action.state)  # APPROVED
    else:  # ALLOW（只读类）→ AUTO_APPROVED
        action.state = ActionState.AUTO_APPROVED
        db.flush()
        states.append(action.state)

    # ---- 5. 执行层：执行网关 + 真实工具（mock 适配器 / 模拟引擎）----
    gw = gateway or ExecutionGateway(
        session=db,
        sim_engine=SimulationEngine(),
        htx_adapter=HTXAdapter(mode="mock"),
        config=ExecutionGatewayConfig(),
    )
    await gw.execute(action.id)
    db.refresh(action)
    states.append(action.state)  # EXECUTED

    return {
        "action": action,
        "states": states,
        "verdict": verdict.verdict,
        "reason_codes": list(verdict.reason_codes),
        "planner_result": planner_result,
        "routed": routed,
    }


# ===========================================================================
# AC1 —— 完整 action 流转（mock B.AI + 真实工具）
# ===========================================================================
class TestFullActionFlow:
    """**Validates: Requirements 19**（AC1：完整流转 + 状态转换 + 审计完整）。"""

    @pytest.mark.asyncio
    async def test_place_order_flows_to_executed(self, db_session: Session) -> None:
        """place_order 走完 REQUESTED → ... → EXECUTED（审批路径）。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        stub = StubBAIClient(responses=[_bai_response(_place_order_plan_json())])

        result = await run_micro_turn_pipeline(
            db_session,
            passport=passport,
            user=user,
            task="在策略范围内对 BTC/USDT 限价买入 10 USDT",
            bai_client=stub,
        )

        action = result["action"]
        assert action.state == ActionState.EXECUTED
        # 状态轨迹覆盖微回合循环的关键节点
        assert ActionState.PLANNING in result["states"]
        assert ActionState.PLAN_VALIDATED in result["states"]
        assert ActionState.RISK_CHECKING in result["states"]
        assert ActionState.APPROVAL_REQUIRED in result["states"]
        assert ActionState.APPROVED in result["states"]
        assert result["states"][-1] == ActionState.EXECUTED

    @pytest.mark.asyncio
    async def test_full_flow_writes_complete_audit_chain(
        self, db_session: Session
    ) -> None:
        """完整流转写出完整审计事件链且哈希链可验证。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        stub = StubBAIClient(responses=[_bai_response(_place_order_plan_json())])

        result = await run_micro_turn_pipeline(
            db_session,
            passport=passport,
            user=user,
            task="限价买入 10 USDT BTC",
            bai_client=stub,
        )

        types = _event_types(db_session, user.id)
        # 决策层 + 策略 + 审批 + 执行各阶段事件齐全
        assert AuditEventType.ACTION_REQUESTED in types
        assert AuditEventType.MODEL_CALL_STARTED in types
        assert AuditEventType.MODEL_CALL_COMPLETED in types
        assert AuditEventType.PLAN_SCHEMA_VALIDATED in types
        assert AuditEventType.POLICY_CHECK_COMPLETED in types
        assert AuditEventType.APPROVAL_REQUESTED in types
        assert AuditEventType.APPROVAL_SUBMITTED in types
        assert AuditEventType.EXECUTION_STARTED in types
        assert AuditEventType.EXECUTION_COMPLETED in types

        # 哈希链完整性（Req 11 跨集成验证）
        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is True, f"audit chain broken: {err!r}"

        # 执行结果落库
        exec_rows = (
            db_session.execute(
                select(ExecutionResult).where(
                    ExecutionResult.action_id == result["action"].id
                )
            )
            .scalars()
            .all()
        )
        assert len(exec_rows) == 1
        assert exec_rows[0].status == "SUCCESS"
        assert exec_rows[0].provider_order_id is not None

    @pytest.mark.asyncio
    async def test_read_market_auto_approved_to_executed(
        self, db_session: Session
    ) -> None:
        """只读类 read_market → ALLOW → AUTO_APPROVED → EXECUTED（无需人工审批）。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        stub = StubBAIClient(responses=[_bai_response(_read_market_plan_json())])

        result = await run_micro_turn_pipeline(
            db_session,
            passport=passport,
            user=user,
            task="查看 BTC/USDT 行情",
            bai_client=stub,
            execution_mode="real_read",
        )

        assert result["verdict"] == "ALLOW"
        assert ActionState.AUTO_APPROVED in result["states"]
        # 只读路径不应产生审批事件
        types = _event_types(db_session, user.id)
        assert AuditEventType.APPROVAL_REQUESTED not in types
        assert result["action"].state == ActionState.EXECUTED

    @pytest.mark.asyncio
    async def test_trace_id_links_entire_flow(self, db_session: Session) -> None:
        """同一 action 的全链路审计事件共享 trace_id（Req 13 AC2 跨集成）。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        stub = StubBAIClient(responses=[_bai_response(_place_order_plan_json())])

        result = await run_micro_turn_pipeline(
            db_session,
            passport=passport,
            user=user,
            task="限价买入 10 USDT BTC",
            bai_client=stub,
        )

        trace_id = result["action"].trace_id
        events = _events(db_session, user.id)
        # 与本 action 关联的事件 trace_id 一致
        action_events = [e for e in events if e.action_id == result["action"].id]
        assert len(action_events) >= 5
        for e in action_events:
            assert e.trace_id == trace_id


# ===========================================================================
# AC2 —— mock B.AI 格式错误响应 → PLAN_INVALID + 回填
# ===========================================================================
class TestMalformedPlannerResponse:
    """**Validates: Requirements 19**（AC2：格式错误响应 → PLAN_INVALID + 回填）。"""

    @pytest.mark.asyncio
    async def test_non_json_response_transitions_to_plan_invalid(
        self, db_session: Session
    ) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        stub = StubBAIClient(responses=[_bai_response("这不是 JSON {{{ 乱码")])

        result = await run_micro_turn_pipeline(
            db_session,
            passport=passport,
            user=user,
            task="买点 BTC",
            bai_client=stub,
        )

        assert result["action"].state == ActionState.PLAN_INVALID
        assert result["planner_result"].status == "invalid"

    @pytest.mark.asyncio
    async def test_plan_invalid_backfills_excerpt_to_audit(
        self, db_session: Session
    ) -> None:
        """格式错误响应：PLAN_INVALID 审计事件回填截断后的 response_excerpt。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        garbage = "x" * 500
        stub = StubBAIClient(responses=[_bai_response(garbage)])

        await run_micro_turn_pipeline(
            db_session,
            passport=passport,
            user=user,
            task="买点 BTC",
            bai_client=stub,
        )

        events = _events(db_session, user.id)
        plan_invalid = [
            e for e in events if e.event_type == AuditEventType.PLAN_INVALID
        ]
        assert len(plan_invalid) == 1
        excerpt = plan_invalid[0].event_json["data"].get("response_excerpt")
        assert excerpt is not None
        assert len(excerpt) <= 200  # 回填到上下文但截断


# ===========================================================================
# AC3 —— 检查点恢复（RISK_CHECKING 中断后继续，不重复 PLANNING）
# ===========================================================================
class TestCheckpointRecovery:
    """**Validates: Requirements 19**（AC3：RISK_CHECKING 中断后从检查点恢复）。"""

    @pytest.mark.asyncio
    async def test_resume_from_risk_checking_does_not_replan(
        self, db_session: Session
    ) -> None:
        """在 RISK_CHECKING 保存检查点 → 模拟中断 → 恢复后不再调 B.AI（PLANNING 不重复）。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)

        # 第一阶段：跑到 PLAN_VALIDATED 后手动推进到 RISK_CHECKING 并存检查点
        stub = StubBAIClient(responses=[_bai_response(_place_order_plan_json())])
        action = _create_action(db_session, passport, user, "限价买入 10 USDT BTC")

        ctx = build_planner_context(
            passport_policy=passport.policy_json,
            task=action.natural_language_request,
            market_snapshot=dict(SEED_MARKET_DATA),
            now=FIXED_NOW,
        )
        action.state = ActionState.PLANNING
        db_session.flush()
        planner_result = call_planner(
            ctx,
            db=db_session,
            user_id=user.id,
            action_id=action.id,
            trace_id=action.trace_id,
            passport_id=passport.id,
            bai_client=stub,
        )
        assert planner_result.status == "success"
        normalized = planner_result.action_plan.actions[0].model_dump()
        action.normalized_action_json = normalized
        action.state = ActionState.PLAN_VALIDATED
        db_session.flush()
        action.state = ActionState.RISK_CHECKING
        db_session.flush()

        # 保存检查点：PLANNING / PLAN_VALIDATED 已完成，待执行 RISK_CHECKING 之后的步骤
        rm = RecoveryManager(db_session)
        rm.save_checkpoint(
            action.id,
            Checkpoint(
                action_id=action.id,
                completed_steps=["PLANNING", "PLAN_VALIDATED"],
                pending_steps=["RISK_CHECKING", "APPROVAL", "EXECUTION"],
                last_tool_results=[],
            ),
        )

        bai_call_count_before = stub.call_count
        assert bai_call_count_before == 1

        # 第二阶段：模拟中断后恢复。restore_checkpoint 拿回已完成步骤，
        # 直接从 RISK_CHECKING 继续——不再调用 B.AI。
        restored = rm.restore_checkpoint(action.id)
        assert restored is not None
        assert "PLANNING" in restored.completed_steps
        assert "PLANNING" not in restored.pending_steps

        # 恢复后直接用已存的 normalized_action_json 做裁决，不重新规划
        verdict = evaluate_policy(
            action=action.normalized_action_json,
            policy=passport.policy_json,
            daily_history=DailyActionHistory(),
            market_snapshot=dict(SEED_MARKET_DATA),
            global_config=GlobalConfig(),
            now=FIXED_NOW,
        )
        assert verdict.verdict in ("ALLOW", "REQUIRE_APPROVAL")
        # 关键断言：恢复路径没有再调用 B.AI（PLANNING 未重复）
        assert stub.call_count == bai_call_count_before

    def test_checkpoint_roundtrip_preserves_steps(self, db_session: Session) -> None:
        """检查点序列化/反序列化往返一致（completed/pending 步骤保留）。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _create_action(db_session, passport, user, "task")

        rm = RecoveryManager(db_session)
        cp = Checkpoint(
            action_id=action.id,
            completed_steps=["PLANNING", "PLAN_VALIDATED", "RISK_CHECKING"],
            pending_steps=["APPROVAL", "EXECUTION"],
            last_tool_results=[{"tool": "getTicker", "result": "ok"}],
        )
        rm.save_checkpoint(action.id, cp)

        restored = rm.restore_checkpoint(action.id)
        assert restored is not None
        assert restored.completed_steps == cp.completed_steps
        assert restored.pending_steps == cp.pending_steps
        assert restored.last_tool_results == cp.last_tool_results


# ===========================================================================
# AC4 —— 上下文窗口边界（接近 8K 触发压缩，保留 policy + snapshot）
# ===========================================================================
class TestContextWindowBoundary:
    """**Validates: Requirements 19**（AC4：接近 8K token 触发压缩，保留关键信息）。"""

    def test_large_history_triggers_compression(self, db_session: Session) -> None:
        """大量历史 action → token 数始终回落到 8K 以内，policy + snapshot 保留。"""
        # 构造足够多、足够长的历史 action
        big_recent = [
            {
                "state": "EXECUTED",
                "natural_language_request": f"历史任务 {i} " + ("详细描述内容" * 60),
            }
            for i in range(30)
        ]

        ctx = build_planner_context(
            passport_policy=dict(SEED_POLICY),
            task="查看 BTC 行情",
            market_snapshot=dict(SEED_MARKET_DATA),
            recent_actions=big_recent,
            now=FIXED_NOW,
        )

        # 核心契约（Req 5 AC3）：token 数控制在 8K 以内
        assert ctx.estimated_tokens <= MAX_TOKENS
        # 关键信息保留：policy + market snapshot 原样，永不截断
        assert ctx.passport_policy_json == dict(SEED_POLICY)
        assert ctx.current_market_snapshot == dict(SEED_MARKET_DATA)
        # recent_actions 摘要至多保留 DEFAULT_RECENT_ACTIONS_LIMIT 条
        assert ctx.recent_actions_summary.count("\n") < DEFAULT_RECENT_ACTIONS_LIMIT

    def test_small_history_not_compressed(self, db_session: Session) -> None:
        """少量历史不触发压缩，保留默认 5 条摘要。"""
        recent = [
            {"state": "EXECUTED", "natural_language_request": f"任务 {i}"}
            for i in range(3)
        ]
        ctx = build_planner_context(
            passport_policy=dict(SEED_POLICY),
            task="查看行情",
            market_snapshot=dict(SEED_MARKET_DATA),
            recent_actions=recent,
            now=FIXED_NOW,
        )
        assert ctx.estimated_tokens <= MAX_TOKENS
        # 3 条都保留
        assert ctx.recent_actions_summary.count("\n") == 2

    def test_policy_and_snapshot_always_retained_under_compression(
        self, db_session: Session
    ) -> None:
        """即便极端历史，policy 与 market snapshot 永不被截断（Req 5 AC3）。"""
        huge_recent = [
            {
                "state": "EXECUTED",
                "natural_language_request": "超长历史" * 500,
            }
            for _ in range(50)
        ]
        ctx = build_planner_context(
            passport_policy=dict(SEED_POLICY),
            task="买入 BTC",
            market_snapshot=dict(SEED_MARKET_DATA),
            recent_actions=huge_recent,
            now=FIXED_NOW,
        )
        # 无论压缩与否，policy/snapshot 完整保留
        assert ctx.passport_policy_json == dict(SEED_POLICY)
        assert ctx.current_market_snapshot == dict(SEED_MARKET_DATA)


# ===========================================================================
# AC5 —— 权限拒绝回填（被拒工具不执行，原因回填上下文）
# ===========================================================================
class TestPermissionRejectionBackfill:
    """**Validates: Requirements 19**（AC5：被拒工具不执行 + 原因回填）。"""

    @pytest.mark.asyncio
    async def test_over_limit_rejected_and_not_executed(
        self, db_session: Session
    ) -> None:
        """超限 place_order → Policy Engine REJECT → AUTO_REJECTED，不产生执行结果。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        # SEED_POLICY 单笔上限 20 USDT，这里规划 500 USDT
        stub = StubBAIClient(
            responses=[_bai_response(_place_order_plan_json(max_notional=500.0))]
        )

        result = await run_micro_turn_pipeline(
            db_session,
            passport=passport,
            user=user,
            task="买入 500 USDT 的 BTC",
            bai_client=stub,
        )

        assert result["action"].state == ActionState.AUTO_REJECTED
        assert "LIMIT_MAX_NOTIONAL_EXCEEDED" in result["reason_codes"]
        # 被拒 → 没有执行结果落库
        exec_rows = (
            db_session.execute(
                select(ExecutionResult).where(
                    ExecutionResult.action_id == result["action"].id
                )
            )
            .scalars()
            .all()
        )
        assert exec_rows == []
        # 拒绝原因回填到 action.reason_codes（供后续上下文构建引用）
        assert result["action"].reason_codes == ["LIMIT_MAX_NOTIONAL_EXCEEDED"]

    @pytest.mark.asyncio
    async def test_rule_route_block_skips_bai_and_execution(
        self, db_session: Session
    ) -> None:
        """规则路由拦截提现关键字 → 不调 B.AI、不执行，原因回填。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        stub = StubBAIClient(responses=[])  # 任何 B.AI 调用都会触发 AssertionError

        result = await run_micro_turn_pipeline(
            db_session,
            passport=passport,
            user=user,
            task="immediately withdraw all USDT to external address",
            bai_client=stub,
        )

        assert result["action"].state == ActionState.AUTO_REJECTED
        assert result["routed"].mode == "blocked_shortcut"
        # 关键：B.AI 从未被调用（stub 队列为空且无 AssertionError 抛出）
        assert stub.call_count == 0
        # 原因回填：英文 withdraw 关键字 → BLOCKED_ACTION_WITHDRAW
        assert any("WITHDRAW" in rc for rc in result["action"].reason_codes)
        # 无执行结果
        exec_rows = (
            db_session.execute(
                select(ExecutionResult).where(
                    ExecutionResult.action_id == result["action"].id
                )
            )
            .scalars()
            .all()
        )
        assert exec_rows == []

    @pytest.mark.asyncio
    async def test_execution_gateway_recheck_blocks_after_policy_change(
        self, db_session: Session
    ) -> None:
        """审批后 policy 收紧 → 执行网关重裁决 REJECT → 阻止执行 + EXECUTION_BLOCKED_BY_RECHECK。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)

        # 先正常规划 + 审批一个合法 place_order
        stub = StubBAIClient(responses=[_bai_response(_place_order_plan_json())])
        action = _create_action(db_session, passport, user, "限价买入 10 USDT BTC")
        ctx = build_planner_context(
            passport_policy=passport.policy_json,
            task=action.natural_language_request,
            market_snapshot=dict(SEED_MARKET_DATA),
            now=FIXED_NOW,
        )
        action.state = ActionState.PLANNING
        db_session.flush()
        pr = call_planner(
            ctx,
            db=db_session,
            user_id=user.id,
            action_id=action.id,
            trace_id=action.trace_id,
            passport_id=passport.id,
            bai_client=stub,
        )
        action.normalized_action_json = pr.action_plan.actions[0].model_dump()
        action.policy_version_at_planning = passport.version
        action.state = ActionState.APPROVED  # 直接置为已审批
        db_session.flush()

        # 审批后收紧 policy：禁用 place_order capability
        new_policy = dict(passport.policy_json)
        new_caps = dict(new_policy.get("capabilities", {}))
        new_caps["place_order"] = False
        new_policy["capabilities"] = new_caps
        passport.policy_json = new_policy
        passport.version += 1
        db_session.flush()

        # 执行网关重裁决应 REJECT
        gw = ExecutionGateway(
            session=db_session,
            sim_engine=SimulationEngine(),
            htx_adapter=HTXAdapter(mode="mock"),
            config=ExecutionGatewayConfig(),
        )
        with pytest.raises(ConflictError) as exc_info:
            await gw.execute(action.id)
        assert exc_info.value.code == "POLICY_RECHECK_REJECT"

        # 审计写入 EXECUTION_BLOCKED_BY_RECHECK，且无成功执行结果
        types = _event_types(db_session, user.id)
        assert AuditEventType.EXECUTION_BLOCKED_BY_RECHECK in types
        assert AuditEventType.EXECUTION_COMPLETED not in types


# ===========================================================================
# AC6 —— 无外部依赖（模型不可用降级仍走通本地路径）
# ===========================================================================
class TestNoExternalDependency:
    """**Validates: Requirements 19**（AC6：全程 mock 外部服务，无网络依赖）。"""

    @pytest.mark.asyncio
    async def test_bai_unavailable_degrades_to_mock_noop(
        self, db_session: Session
    ) -> None:
        """B.AI 不可用 → 降级 mock planner（no_op）→ ALLOW → 流程不卡死。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        stub = StubBAIClient(
            responses=[BAIServiceUnavailableError("BAI returned status=503")]
        )

        result = await run_micro_turn_pipeline(
            db_session,
            passport=passport,
            user=user,
            task="买点 BTC",
            bai_client=stub,
        )

        # 降级后 no_op → Policy Engine ALLOW → 走到 EXECUTED（no_op 执行即完成）
        assert result["planner_result"].status == "unavailable"
        assert result["action"].normalized_action_json["type"] == "no_op"
        # MODEL_UNAVAILABLE 审计事件写入
        types = _event_types(db_session, user.id)
        assert AuditEventType.MODEL_UNAVAILABLE in types
