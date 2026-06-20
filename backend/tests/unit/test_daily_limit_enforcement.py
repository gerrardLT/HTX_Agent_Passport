"""日限额强制 + 幂等防护测试（修复 G13/G14/G15）。

**Validates: Requirements 7**（AC6 日累计限额、AC7 每日订单数）。

背景：修复前 evaluate_policy 的日限额逻辑因调用方恒传空 DailyActionHistory
而**从未生效**。本套件验证：
1. aggregate_daily_history 正确聚合当日「在途 + 已执行」写操作。
2. 执行网关在执行前用真实聚合做重裁决，累计超限时拒绝（DAILY_LIMIT_EXCEEDED）。
3. 每日订单数超限时拒绝（DAILY_ORDER_COUNT_EXCEEDED）。
4. 幂等防护：同一 action 已有 SUCCESS 执行结果时拒绝重复执行（ALREADY_EXECUTED）。
5. 被拒/过期/取消的 action 不占用日额度。

注：SQLite 测试环境串行执行，无法真正制造并发竞态；本套件验证"额度被正确
强制"这一**功能正确性**（修复前该功能完全失效）。并发原子性由 passport 行锁
（aggregate_daily_history_for_update 的 SELECT ... FOR UPDATE）在 PostgreSQL
生产环境保证，见 daily_history.py 文档。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import AgentAction, AgentPassport, ExecutionResult, User
from app.models.enums import ActionState
from app.services.daily_history import aggregate_daily_history
from app.services.execution_gateway import (
    ConflictError,
    ExecutionGateway,
    ExecutionGatewayConfig,
)
from app.services.htx_adapter import HTXAdapter
from app.services.simulation_engine import SimulationEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_user(session: Session) -> User:
    user = User(primary_wallet=f"0xDAILY{uuid.uuid4().hex[:30]}")
    session.add(user)
    session.flush()
    return user


def _make_passport(
    session: Session,
    user: User,
    *,
    max_daily_notional: float = 100.0,
    max_orders_per_day: int = 10,
) -> AgentPassport:
    passport = AgentPassport(
        user_id=user.id,
        name="daily-limit-passport",
        agent_type="trader",
        state="ACTIVE",
        version=1,
        policy_json={
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
                "max_daily_notional_usdt": max_daily_notional,
                "max_orders_per_day": max_orders_per_day,
            },
            "approval": {"required_for_trade": True, "expires_after_seconds": 300},
            "blocked_actions": ["withdraw", "borrow", "margin", "transfer_out"],
        },
        reputation_score=50,
    )
    session.add(passport)
    session.flush()
    return passport


def _make_action(
    session: Session,
    user: User,
    passport: AgentPassport,
    *,
    state: str = ActionState.EXECUTED,
    notional: float = 10.0,
    atype: str = "place_order",
    created_at: datetime | None = None,
) -> AgentAction:
    normalized = {
        "type": atype,
        "symbol": "btcusdt",
        "side": "buy",
        "order_type": "limit",
        "amount": 0.0001,
        "amount_unit": "base",
        "max_notional_usdt": notional,
        "limit_price": 67000.0,
    }
    action = AgentAction(
        passport_id=passport.id,
        user_id=user.id,
        trace_id=uuid.uuid4(),
        natural_language_request="buy BTC",
        normalized_action_json=normalized,
        state=state,
        execution_mode="simulation",
        approval_required=True,
        policy_version_at_planning=1,
    )
    session.add(action)
    session.flush()
    if created_at is not None:
        # 直接覆盖 created_at（绕过 server_default）用于"昨天"场景
        action.created_at = created_at
        session.flush()
    return action


def _make_gateway(session: Session) -> ExecutionGateway:
    """G16 修复后，gateway 默认用 SEED_MARKET_DATA（含静态 as_of=2024-06-15）
    会被 stale-price 检查拦截。本辅助函数注入"实时" snapshot，让本套件继续
    专注测试 G13/G14/G15。
    """
    gateway = ExecutionGateway(
        session=session,
        sim_engine=SimulationEngine(),
        htx_adapter=HTXAdapter(mode="mock"),
        config=ExecutionGatewayConfig(),
    )
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


# ===========================================================================
# 1. aggregate_daily_history 聚合正确性
# ===========================================================================
class TestAggregateDailyHistory:
    """**Validates: Requirements 7**（AC6/AC7 聚合口径）。"""

    def test_empty_when_no_actions(self, db_session: Session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        h = aggregate_daily_history(db_session, passport_id=passport.id)
        assert h.total_notional_today_utc == 0.0
        assert h.order_count_today_utc == 0

    def test_sums_executed_place_orders(self, db_session: Session) -> None:
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        _make_action(db_session, user, passport, state=ActionState.EXECUTED, notional=10.0)
        _make_action(db_session, user, passport, state=ActionState.EXECUTED, notional=15.0)

        h = aggregate_daily_history(db_session, passport_id=passport.id)
        assert h.total_notional_today_utc == 25.0
        assert h.order_count_today_utc == 2

    def test_counts_inflight_actions(self, db_session: Session) -> None:
        """在途（APPROVED/EXECUTING/待审批）也计入，防止并发审批超限。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        _make_action(db_session, user, passport, state=ActionState.APPROVED, notional=10.0)
        _make_action(db_session, user, passport, state=ActionState.APPROVAL_REQUIRED, notional=10.0)
        _make_action(db_session, user, passport, state=ActionState.EXECUTING, notional=10.0)

        h = aggregate_daily_history(db_session, passport_id=passport.id)
        assert h.total_notional_today_utc == 30.0
        assert h.order_count_today_utc == 3

    def test_excludes_rejected_and_terminal_failures(self, db_session: Session) -> None:
        """被拒/过期/取消/失败不占额度。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        _make_action(db_session, user, passport, state=ActionState.AUTO_REJECTED, notional=50.0)
        _make_action(db_session, user, passport, state=ActionState.REJECTED_BY_USER, notional=50.0)
        _make_action(db_session, user, passport, state=ActionState.EXPIRED, notional=50.0)
        _make_action(db_session, user, passport, state=ActionState.CANCELLED, notional=50.0)
        _make_action(db_session, user, passport, state=ActionState.EXECUTION_FAILED, notional=50.0)

        h = aggregate_daily_history(db_session, passport_id=passport.id)
        assert h.total_notional_today_utc == 0.0
        assert h.order_count_today_utc == 0

    def test_excludes_action_id(self, db_session: Session) -> None:
        """exclude_action_id 排除当前正在裁决的 action，避免双重计数。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        a1 = _make_action(db_session, user, passport, state=ActionState.APPROVED, notional=10.0)
        _make_action(db_session, user, passport, state=ActionState.APPROVED, notional=15.0)

        h = aggregate_daily_history(
            db_session, passport_id=passport.id, exclude_action_id=a1.id
        )
        assert h.total_notional_today_utc == 15.0
        assert h.order_count_today_utc == 1

    def test_only_counts_today_utc(self, db_session: Session) -> None:
        """昨天的 action 不计入今天的累计（UTC 日边界）。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        yesterday = datetime.now(UTC) - timedelta(days=1)
        _make_action(
            db_session, user, passport,
            state=ActionState.EXECUTED, notional=50.0, created_at=yesterday,
        )
        _make_action(db_session, user, passport, state=ActionState.EXECUTED, notional=10.0)

        h = aggregate_daily_history(db_session, passport_id=passport.id)
        assert h.total_notional_today_utc == 10.0
        assert h.order_count_today_utc == 1

    def test_cancel_order_counts_order_but_not_notional(self, db_session: Session) -> None:
        """cancel_order 计入订单数但不加 notional。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        _make_action(
            db_session, user, passport,
            state=ActionState.EXECUTED, atype="cancel_order", notional=0.0,
        )
        h = aggregate_daily_history(db_session, passport_id=passport.id)
        assert h.total_notional_today_utc == 0.0
        assert h.order_count_today_utc == 1


# ===========================================================================
# 2. 执行网关强制日累计限额（修复 G13/G14）
# ===========================================================================
class TestExecutionGatewayDailyLimit:
    """**Validates: Requirements 7**（AC6：执行前重裁决强制日累计限额）。"""

    @pytest.mark.asyncio
    async def test_daily_notional_limit_enforced_at_execution(
        self, db_session: Session
    ) -> None:
        """已用 95/100，再来一笔 10 → 累计 105 超限 → 执行被拒。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, max_daily_notional=100.0)
        # 既有当日已执行累计 95
        for _ in range(5):
            _make_action(db_session, user, passport, state=ActionState.EXECUTED, notional=19.0)
        # 当前要执行的一笔 10 → 95+10=105 > 100
        target = _make_action(
            db_session, user, passport, state=ActionState.APPROVED, notional=10.0
        )

        gw = _make_gateway(db_session)
        with pytest.raises(ConflictError) as exc:
            await gw.execute(target.id)
        assert exc.value.code == "POLICY_RECHECK_REJECT"
        assert "DAILY_LIMIT_EXCEEDED" in exc.value.message

    @pytest.mark.asyncio
    async def test_within_daily_limit_executes(self, db_session: Session) -> None:
        """已用 50/100，再来一笔 10 → 累计 60 未超限 → 执行成功。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, max_daily_notional=100.0)
        for _ in range(5):
            _make_action(db_session, user, passport, state=ActionState.EXECUTED, notional=10.0)
        target = _make_action(
            db_session, user, passport, state=ActionState.APPROVED, notional=10.0
        )

        gw = _make_gateway(db_session)
        result = await gw.execute(target.id)
        assert result.status == "SUCCESS"
        db_session.refresh(target)
        assert target.state == ActionState.EXECUTED

    @pytest.mark.asyncio
    async def test_daily_order_count_limit_enforced(self, db_session: Session) -> None:
        """已有 10 单（达上限 10），再执行第 11 单 → 拒绝。"""
        user = _make_user(db_session)
        passport = _make_passport(
            db_session, user, max_daily_notional=10000.0, max_orders_per_day=10
        )
        for _ in range(10):
            _make_action(db_session, user, passport, state=ActionState.EXECUTED, notional=1.0)
        target = _make_action(
            db_session, user, passport, state=ActionState.APPROVED, notional=1.0
        )

        gw = _make_gateway(db_session)
        with pytest.raises(ConflictError) as exc:
            await gw.execute(target.id)
        assert exc.value.code == "POLICY_RECHECK_REJECT"
        assert "DAILY_ORDER_COUNT_EXCEEDED" in exc.value.message

    @pytest.mark.asyncio
    async def test_sequential_orders_accumulate_until_limit(
        self, db_session: Session
    ) -> None:
        """串行执行多笔，累计达限后续笔被拒——验证额度真实累加。

        日限额 100，每笔 20：前 5 笔成功（100），第 6 笔被拒。
        这是修复前完全失效的核心场景。
        """
        user = _make_user(db_session)
        passport = _make_passport(db_session, user, max_daily_notional=100.0)
        gw = _make_gateway(db_session)

        executed = 0
        rejected = 0
        for _ in range(6):
            action = _make_action(
                db_session, user, passport, state=ActionState.APPROVED, notional=20.0
            )
            try:
                await gw.execute(action.id)
                executed += 1
            except ConflictError as exc:
                assert exc.code == "POLICY_RECHECK_REJECT"
                assert "DAILY_LIMIT_EXCEEDED" in exc.message
                rejected += 1

        # 5 笔 × 20 = 100（恰好达限），第 6 笔被拒
        assert executed == 5
        assert rejected == 1


# ===========================================================================
# 3. 幂等防护（修复 G15）
# ===========================================================================
class TestExecutionIdempotency:
    """**Validates: Requirements 9**（防止 action 重复执行）。"""

    @pytest.mark.asyncio
    async def test_already_executed_action_rejected(
        self, db_session: Session
    ) -> None:
        """已有 SUCCESS 执行结果的 action → 再次执行被拒（ALREADY_EXECUTED）。"""
        user = _make_user(db_session)
        passport = _make_passport(db_session, user)
        action = _make_action(
            db_session, user, passport, state=ActionState.APPROVED, notional=10.0
        )
        # 预置一条 SUCCESS 执行结果（模拟已执行过）
        db_session.add(
            ExecutionResult(
                action_id=action.id,
                provider="HTX",
                mode="simulation",
                request_payload={},
                response_payload={},
                provider_order_id="sim-existing",
                status="SUCCESS",
            )
        )
        db_session.flush()

        gw = _make_gateway(db_session)
        with pytest.raises(ConflictError) as exc:
            await gw.execute(action.id)
        assert exc.value.code == "ALREADY_EXECUTED"
