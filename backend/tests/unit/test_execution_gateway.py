"""执行网关单元测试（任务 13 / Req 9 + Req 15 + Property 7）。

覆盖：
1. test_execute_approved_action_simulation_succeeds：APPROVED + simulation → EXECUTED
2. test_execute_auto_approved_action_succeeds：AUTO_APPROVED → EXECUTED
3. test_execute_non_approved_action_returns_409：REQUESTED/PLANNING 等状态 → 409
4. test_execute_re_verdict_reject_blocks：重裁决返回 REJECT → 409 + EXECUTION_BLOCKED_BY_RECHECK 审计
5. test_execute_real_trade_without_env_flag_returns_403：real_trade + DEMO_REAL_TRADE=false → 403
6. test_execute_with_kill_switch_returns_409：DEMO_DISABLE_EXECUTION=true → 拒绝
7. test_execute_writes_audit_events：成功执行后有 EXECUTION_STARTED + EXECUTION_COMPLETED 审计
8. test_execute_writes_execution_result_record：execution_results 表有记录
9. test_execute_updates_reputation：成功后 reputation_score 增加
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.models import AgentAction, AgentPassport, AuditEvent, ExecutionResult, User
from app.models.enums import ActionState, AuditEventType
from app.services.execution_gateway import (
    ConflictError,
    ExecutionGateway,
    ExecutionGatewayConfig,
    ForbiddenError,
)
from app.services.htx_adapter import HTXAdapter
from app.services.simulation_engine import SimulationEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_user(session: Session) -> User:
    """创建测试用户。"""
    user = User(
        id=uuid.uuid4(),
        primary_wallet="0xTEST_WALLET_001",
        role="user",
    )
    session.add(user)
    session.flush()
    return user


def _make_passport(
    session: Session,
    user: User,
    *,
    policy_json: dict | None = None,
    reputation_score: int = 50,
) -> AgentPassport:
    """创建测试 passport（ACTIVE + small_spot_executor 策略）。"""
    default_policy = {
        "version": "0.1",
        "capabilities": {
            "read_market": True,
            "read_account": True,
            "place_order": True,
            "cancel_order": True,
            "withdraw": False,
        },
        "limits": {
            "allowed_symbols": ["btcusdt", "ethusdt"],
            "max_notional_usdt_per_order": 20.0,
            "max_daily_notional_usdt": 100.0,
            "max_orders_per_day": 10,
        },
        "approval": {
            "required_for_trade": True,
            "expires_after_seconds": 300,
        },
        "blocked_actions": ["withdraw", "borrow", "margin", "transfer_out"],
    }
    passport = AgentPassport(
        id=uuid.uuid4(),
        user_id=user.id,
        name="test-passport",
        agent_type="trader",
        state="ACTIVE",
        version=1,
        policy_json=policy_json or default_policy,
        reputation_score=reputation_score,
    )
    session.add(passport)
    session.flush()
    return passport


def _make_action(
    session: Session,
    user: User,
    passport: AgentPassport,
    *,
    state: str = ActionState.APPROVED,
    execution_mode: str = "simulation",
    normalized_action_json: dict | None = None,
) -> AgentAction:
    """创建测试 action。"""
    default_normalized = {
        "type": "place_order",
        "symbol": "btcusdt",
        "side": "buy",
        "order_type": "limit",
        "amount": 0.0001,
        "amount_unit": "base",
        "max_notional_usdt": 10.0,
        "limit_price": 67000.0,
    }
    action = AgentAction(
        id=uuid.uuid4(),
        passport_id=passport.id,
        user_id=user.id,
        trace_id=uuid.uuid4(),
        natural_language_request="buy 10 USDT of BTC",
        normalized_action_json=normalized_action_json or default_normalized,
        state=state,
        execution_mode=execution_mode,
        approval_required=True,
    )
    session.add(action)
    session.flush()
    return action


def _make_gateway(
    session: Session,
    *,
    demo_real_trade_enabled: bool = False,
    demo_disable_execution: bool = False,
) -> ExecutionGateway:
    """创建 ExecutionGateway 实例。

    G16 修复后，``_get_market_snapshot`` 默认返回 ``SEED_MARKET_DATA``
    （静态 ``as_of=2024-06-15``）会被 stale-price 检查拦截。本辅助
    函数把 snapshot 替换为"当前时间"的新鲜 snapshot——绝大多数现存
    测试不关心 stale-price 路径，应当通过；那些专门测试 stale-price
    的用例在 ``test_stale_price_recheck.py`` 单独覆盖。
    """
    config = ExecutionGatewayConfig(
        demo_real_trade_enabled=demo_real_trade_enabled,
        demo_disable_execution=demo_disable_execution,
    )
    sim_engine = SimulationEngine()
    htx_adapter = HTXAdapter(mode="mock")
    gateway = ExecutionGateway(
        session=session,
        sim_engine=sim_engine,
        htx_adapter=htx_adapter,
        config=config,
    )
    # 注入"实时" snapshot：用 datetime.now(UTC) 当作 as_of，让既有测试
    # 不被 G16 stale-price 检查误伤。注意：limit_price 可能与 last 价
    # 偏离很大，但这些测试 policy 都不配 max_slippage_bps，slippage 会跳过。
    _now_iso = datetime.now(UTC).isoformat()
    gateway._get_market_snapshot = lambda: {  # type: ignore[method-assign]
        "btcusdt": {
            "last": 68000.0, "bid": 67999.0, "ask": 68001.0,
            "vol_24h": 1500.0, "as_of": _now_iso,
        },
        "ethusdt": {
            "last": 3600.0, "bid": 3599.0, "ask": 3601.0,
            "vol_24h": 25000.0, "as_of": _now_iso,
        },
    }
    return gateway


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestExecutionGateway:
    """执行网关核心功能测试（Property 7）。"""

    @pytest.mark.asyncio
    async def test_execute_approved_action_simulation_succeeds(
        self, db_session: Session
    ) -> None:
        """APPROVED + simulation → EXECUTED，返回 ExecutionResult。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(
            db_session, user, passport, state=ActionState.APPROVED
        )
        gateway = _make_gateway(db_session)

        result = await gateway.execute(action.id)

        assert isinstance(result, ExecutionResult)
        assert result.mode == "simulation"
        assert result.status == "SUCCESS"
        assert result.action_id == action.id
        # action 状态应变为 EXECUTED
        db_session.refresh(action)
        assert action.state == ActionState.EXECUTED

    @pytest.mark.asyncio
    async def test_execute_auto_approved_action_succeeds(
        self, db_session: Session
    ) -> None:
        """AUTO_APPROVED → EXECUTED。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        # AUTO_APPROVED 通常用于 read_market/read_account
        normalized = {
            "type": "read_market",
            "symbol": "btcusdt",
        }
        action = _make_action(
            db_session,
            user,
            passport,
            state=ActionState.AUTO_APPROVED,
            execution_mode="simulation",
            normalized_action_json=normalized,
        )
        gateway = _make_gateway(db_session)

        result = await gateway.execute(action.id)

        assert isinstance(result, ExecutionResult)
        assert result.status == "SUCCESS"
        db_session.refresh(action)
        assert action.state == ActionState.EXECUTED

    @pytest.mark.asyncio
    async def test_execute_non_approved_action_returns_409(
        self, db_session: Session
    ) -> None:
        """非 APPROVED/AUTO_APPROVED 状态 → ConflictError 409。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        gateway = _make_gateway(db_session)

        non_approved_states = [
            ActionState.REQUESTED,
            ActionState.PLANNING,
            ActionState.PLAN_VALIDATED,
            ActionState.RISK_CHECKING,
            ActionState.APPROVAL_REQUIRED,
            ActionState.REJECTED_BY_USER,
            ActionState.EXECUTED,
        ]

        for state in non_approved_states:
            action = _make_action(
                db_session, user, passport, state=state
            )
            with pytest.raises(ConflictError) as exc_info:
                await gateway.execute(action.id)
            assert exc_info.value.code == "ACTION_NOT_APPROVED"

    @pytest.mark.asyncio
    async def test_execute_re_verdict_reject_blocks(
        self, db_session: Session
    ) -> None:
        """重裁决返回 REJECT → 409 + EXECUTION_BLOCKED_BY_RECHECK 审计事件。"""
        user = _make_user(db_session)
        # 创建一个策略不允许 place_order 的 passport（capability 关闭）
        restrictive_policy = {
            "version": "0.1",
            "capabilities": {
                "read_market": True,
                "read_account": True,
                "place_order": False,  # 不允许下单
                "cancel_order": False,
                "withdraw": False,
            },
            "limits": {
                "allowed_symbols": ["btcusdt"],
                "max_notional_usdt_per_order": 20.0,
                "max_daily_notional_usdt": 100.0,
                "max_orders_per_day": 10,
            },
            "approval": {
                "required_for_trade": True,
                "expires_after_seconds": 300,
            },
            "blocked_actions": ["withdraw", "borrow", "margin", "transfer_out"],
        }
        passport = _make_passport(db_session, user, policy_json=restrictive_policy)
        # action 已被审批（可能是策略变更前审批的），但重裁决会 REJECT
        action = _make_action(
            db_session, user, passport, state=ActionState.APPROVED
        )
        gateway = _make_gateway(db_session)

        with pytest.raises(ConflictError) as exc_info:
            await gateway.execute(action.id)
        assert exc_info.value.code == "POLICY_RECHECK_REJECT"

        # 验证写入了 EXECUTION_BLOCKED_BY_RECHECK 审计事件
        audit_events = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.action_id == action.id,
                AuditEvent.event_type == AuditEventType.EXECUTION_BLOCKED_BY_RECHECK,
            )
            .all()
        )
        assert len(audit_events) == 1
        assert "CAPABILITY_NOT_GRANTED" in audit_events[0].event_json["data"]["reason_codes"]

    @pytest.mark.asyncio
    async def test_execute_real_trade_without_env_flag_returns_403(
        self, db_session: Session
    ) -> None:
        """real_trade + DEMO_REAL_TRADE=false → ForbiddenError 403。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(
            db_session, user, passport,
            state=ActionState.APPROVED,
            execution_mode="real_trade",
        )
        gateway = _make_gateway(db_session, demo_real_trade_enabled=False)

        with pytest.raises(ForbiddenError) as exc_info:
            await gateway.execute(action.id)
        assert exc_info.value.code == "REAL_TRADE_DISABLED"

    @pytest.mark.asyncio
    async def test_execute_with_kill_switch_returns_409(
        self, db_session: Session
    ) -> None:
        """DEMO_DISABLE_EXECUTION=true → ConflictError 409。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(
            db_session, user, passport, state=ActionState.APPROVED
        )
        gateway = _make_gateway(db_session, demo_disable_execution=True)

        with pytest.raises(ConflictError) as exc_info:
            await gateway.execute(action.id)
        assert exc_info.value.code == "EXECUTION_DISABLED"

    @pytest.mark.asyncio
    async def test_execute_writes_audit_events(
        self, db_session: Session
    ) -> None:
        """成功执行后有 EXECUTION_STARTED + EXECUTION_COMPLETED 审计事件。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(
            db_session, user, passport, state=ActionState.APPROVED
        )
        gateway = _make_gateway(db_session)

        await gateway.execute(action.id)

        # 查询该 action 的审计事件
        audit_events = (
            db_session.query(AuditEvent)
            .filter(AuditEvent.action_id == action.id)
            .order_by(AuditEvent.created_at.asc())
            .all()
        )
        event_types = [e.event_type for e in audit_events]

        assert AuditEventType.EXECUTION_STARTED in event_types
        assert AuditEventType.EXECUTION_COMPLETED in event_types
        # STARTED 应在 COMPLETED 之前
        started_idx = event_types.index(AuditEventType.EXECUTION_STARTED)
        completed_idx = event_types.index(AuditEventType.EXECUTION_COMPLETED)
        assert started_idx < completed_idx

    @pytest.mark.asyncio
    async def test_execute_writes_execution_result_record(
        self, db_session: Session
    ) -> None:
        """execution_results 表有记录。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(
            db_session, user, passport, state=ActionState.APPROVED
        )
        gateway = _make_gateway(db_session)

        result = await gateway.execute(action.id)

        # 从数据库查询
        db_results = (
            db_session.query(ExecutionResult)
            .filter(ExecutionResult.action_id == action.id)
            .all()
        )
        assert len(db_results) == 1
        assert db_results[0].id == result.id
        assert db_results[0].mode == "simulation"
        assert db_results[0].status == "SUCCESS"
        assert db_results[0].provider == "HTX"
        assert db_results[0].request_payload is not None
        assert db_results[0].response_payload is not None
        # response_payload 应包含 order_id
        assert "order_id" in db_results[0].response_payload

    @pytest.mark.asyncio
    async def test_execute_updates_reputation(
        self, db_session: Session
    ) -> None:
        """成功后 reputation_score 增加。"""
        user = _make_user(db_session)
        initial_score = 50
        passport = _make_passport(
            db_session, user, reputation_score=initial_score
        )
        action = _make_action(
            db_session, user, passport, state=ActionState.APPROVED
        )
        gateway = _make_gateway(db_session)

        await gateway.execute(action.id)

        db_session.refresh(passport)
        assert passport.reputation_score > initial_score

        # 验证写入了 REPUTATION_UPDATED 审计事件
        rep_events = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.passport_id == passport.id,
                AuditEvent.event_type == AuditEventType.REPUTATION_UPDATED,
            )
            .all()
        )
        assert len(rep_events) == 1
