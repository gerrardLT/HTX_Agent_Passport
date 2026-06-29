"""Stale-price 重校验单元测试（修复 G16 / Req 16 AC2 / HITL 头号失败模式）。

覆盖矩阵
--------
**纯函数 9 场景**（`check_market_snapshot_freshness_and_slippage`）：

1. ``test_fresh_snapshot_within_slippage_passes`` —— 新鲜 + 偏差 < 阈值 → ok=True
2. ``test_stale_snapshot_blocks_even_when_price_unchanged`` —— 过期 → 阻断
   ``MARKET_SNAPSHOT_STALE``（严格策略：过期就阻断，无论价格是否变了）
3. ``test_slippage_exceeds_threshold_blocks`` —— limit_price 偏离 last 超
   ``max_slippage_bps`` → 阻断 ``MARKET_SLIPPAGE_EXCEEDED``
4. ``test_max_slippage_not_configured_skips_slippage_check`` —— 未配置
   ``max_slippage_bps`` → 跳过 slippage（仅时效检查）
5. ``test_read_market_action_skipped`` —— ``action.type=read_market`` → ok=True
6. ``test_cancel_order_action_skipped`` —— ``action.type=cancel_order`` → ok=True
7. ``test_market_order_skips_slippage`` —— ``limit_price=None`` → 跳过 slippage
8. ``test_snapshot_without_as_of_skips_freshness`` —— snapshot 无 ``as_of``
   → 保守跳过时效，slippage 仍生效
9. ``test_symbol_not_in_snapshot_skipped`` —— 由 PLAN_HALLUCINATION 处理

**集成 2 场景**：

10. ``test_execution_gateway_blocks_stale_price`` —— 执行网关重裁决拦截过期价
11. ``test_approval_service_blocks_stale_price`` —— 审批服务重裁决拦截过期价
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import AgentAction, AgentPassport, Approval, AuditEvent, User
from app.models.enums import ActionState, AuditEventType, PassportState
from app.services.approval_service import (
    DEFAULT_APPROVAL_EXPIRES_SECONDS,
    MarketSlippageExceededError,
    submit_approval,
)
from app.services.execution_gateway import (
    ConflictError,
    ExecutionGateway,
    ExecutionGatewayConfig,
)
from app.services.htx_adapter import HTXAdapter
from app.services.simulation_engine import SimulationEngine
from app.services.stale_price_check import (
    DEFAULT_SNAPSHOT_FRESHNESS_SECONDS,
    StalePriceCheckResult,
    check_market_snapshot_freshness_and_slippage,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures（与现有测试风格一致）
# ---------------------------------------------------------------------------
#: 固定的"snapshot 抓取时间"——所有纯函数测试都以它为基准构造 now，
#: 避免依赖系统当前时间（与 Property 1 PBT 习惯一致）。
_AS_OF_BASE = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


def _policy_with_slippage(max_slippage_bps: int | None = 50) -> dict[str, Any]:
    """构造一个带 ``max_slippage_bps`` 配置的 policy（其余字段最小化）。

    ``max_slippage_bps=None`` 时不写该字段，模拟"未配置"场景。
    """
    limits: dict[str, Any] = {
        "allowed_symbols": ["btcusdt", "ethusdt"],
        "max_notional_usdt_per_order": 100.0,
        "max_daily_notional_usdt": 1000.0,
        "max_orders_per_day": 100,
    }
    if max_slippage_bps is not None:
        limits["max_slippage_bps"] = max_slippage_bps
    return {
        "version": "0.1",
        "capabilities": {
            "read_market": True,
            "read_account": True,
            "place_order": True,
            "cancel_order": True,
            "withdraw": False,
        },
        "limits": limits,
        "approval": {
            "required_for_trade": True,
            "required_for_policy_change": True,
            "expires_after_seconds": 300,
        },
        "blocked_actions": ["withdraw"],
    }


def _action_place_order(
    *,
    symbol: str = "btcusdt",
    limit_price: float | None = 68000.0,
) -> dict[str, Any]:
    return {
        "type": "place_order",
        "symbol": symbol,
        "side": "buy",
        "order_type": "limit",
        "amount": 0.001,
        "amount_unit": "base",
        "max_notional_usdt": 70.0,
        "limit_price": limit_price,
        "requires_user_approval": True,
        "rationale": "test",
    }


def _snapshot(
    *,
    symbol: str = "btcusdt",
    last: float = 68000.0,
    as_of: str | datetime | None = _AS_OF_BASE.isoformat(),
) -> dict[str, dict[str, Any]]:
    """构造单 symbol snapshot；as_of=None 时省略字段。"""
    entry: dict[str, Any] = {"last": last, "bid": last - 1, "ask": last + 1}
    if as_of is not None:
        entry["as_of"] = as_of
    return {symbol: entry}


# ===========================================================================
# 1. 纯函数：正常通过
# ===========================================================================
class TestFreshSnapshotPasses:
    """**Validates: Req 16 AC2** + G16（正常路径）。"""

    def test_fresh_snapshot_within_slippage_passes(self) -> None:
        """新鲜 snapshot + limit_price 与 last 偏差 < max_slippage_bps → ok=True。"""
        # now 比 as_of 晚 30 秒（< 60s 阈值）
        now = _AS_OF_BASE + timedelta(seconds=30)
        # limit_price=68001，last=68000 → deviation ~ 1.5 bps < 50 bps
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=68001.0),
            policy=_policy_with_slippage(max_slippage_bps=50),
            market_snapshot=_snapshot(last=68000.0),
            now=now,
        )

        assert isinstance(result, StalePriceCheckResult)
        assert result.ok is True
        assert result.reason_code is None
        # detail 包含计算用到的关键字段，便于审计 / 调试
        assert result.detail["symbol"] == "btcusdt"
        assert result.detail["limit_price"] == 68001.0
        assert result.detail["snapshot_last"] == 68000.0
        assert result.detail["deviation_bps"] < 50

    def test_exactly_at_freshness_boundary_passes(self) -> None:
        """now - as_of == 60s 边界值 → 不阻断（严格 > 才阻断）。"""
        now = _AS_OF_BASE + timedelta(seconds=DEFAULT_SNAPSHOT_FRESHNESS_SECONDS)
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(),
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(),
            now=now,
        )
        assert result.ok is True


# ===========================================================================
# 2. 纯函数：snapshot 过期
# ===========================================================================
class TestStaleSnapshotBlocks:
    """**Validates: G16** 严格策略——过期就阻断,无论价格是否实际变了。"""

    def test_stale_snapshot_blocks_even_when_price_unchanged(self) -> None:
        """now - as_of > 60s + limit_price=last → 阻断 MARKET_SNAPSHOT_STALE。"""
        # now 比 as_of 晚 120 秒（>> 60s）；价格保持 68000 不变
        now = _AS_OF_BASE + timedelta(seconds=120)
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=68000.0),
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(last=68000.0),
            now=now,
        )

        assert result.ok is False
        assert result.reason_code == "MARKET_SNAPSHOT_STALE"
        assert result.detail["snapshot_age_seconds"] == 120.0
        assert result.detail["freshness_threshold_seconds"] == DEFAULT_SNAPSHOT_FRESHNESS_SECONDS
        assert result.detail["symbol"] == "btcusdt"

    def test_custom_freshness_threshold(self) -> None:
        """调用方放宽阈值至 300s 后，120s 旧的 snapshot 通过。"""
        now = _AS_OF_BASE + timedelta(seconds=120)
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(),
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(),
            now=now,
            snapshot_freshness_seconds=300,
        )
        assert result.ok is True


# ===========================================================================
# 3. 纯函数：slippage 超限
# ===========================================================================
class TestSlippageExceeds:
    """**Validates: Req 16 AC2** ——价格偏离 max_slippage_bps 阻断。"""

    def test_slippage_exceeds_threshold_blocks(self) -> None:
        """新鲜 snapshot + 偏离 last 100 bps（> 50 bps 阈值）→ 阻断。"""
        now = _AS_OF_BASE + timedelta(seconds=10)
        # last=68000，limit=68680 → 偏离 = 680/68000 = 100 bps > 50
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=68680.0),
            policy=_policy_with_slippage(max_slippage_bps=50),
            market_snapshot=_snapshot(last=68000.0),
            now=now,
        )

        assert result.ok is False
        assert result.reason_code == "MARKET_SLIPPAGE_EXCEEDED"
        assert result.detail["deviation_bps"] == pytest.approx(100.0, rel=1e-6)
        assert result.detail["threshold_bps"] == 50
        assert result.detail["limit_price"] == 68680.0
        assert result.detail["snapshot_last"] == 68000.0

    def test_slippage_below_threshold_passes(self) -> None:
        """偏离 30 bps < 50 阈值 → 通过。"""
        now = _AS_OF_BASE + timedelta(seconds=10)
        # last=68000，limit=68000 * 1.003 = 68204 → ~30 bps
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=68204.0),
            policy=_policy_with_slippage(max_slippage_bps=50),
            market_snapshot=_snapshot(last=68000.0),
            now=now,
        )
        assert result.ok is True
        assert result.detail["deviation_bps"] < 50

    def test_slippage_buy_low_blocks(self) -> None:
        """买价低于 last 也算偏离（abs 比较，不分方向）。"""
        now = _AS_OF_BASE + timedelta(seconds=10)
        # last=68000, limit=67320 → 1000 bps 低 → 仍超 50
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=67320.0),
            policy=_policy_with_slippage(max_slippage_bps=50),
            market_snapshot=_snapshot(last=68000.0),
            now=now,
        )
        assert result.ok is False
        assert result.reason_code == "MARKET_SLIPPAGE_EXCEEDED"


# ===========================================================================
# 4. 纯函数：未配置 max_slippage_bps
# ===========================================================================
class TestMaxSlippageNotConfigured:
    """**Validates: Req 16 AC2** ——未配置时跳过 slippage 检查（默认放宽）。"""

    def test_max_slippage_not_configured_skips_slippage_check(self) -> None:
        """policy 不配 max_slippage_bps → 即使 limit_price 离谱也过 slippage。"""
        now = _AS_OF_BASE + timedelta(seconds=10)
        # 离谱价：limit=10000 (vs last=68000) → 但 max_slippage_bps 没配
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=10000.0),
            policy=_policy_with_slippage(max_slippage_bps=None),
            market_snapshot=_snapshot(last=68000.0),
            now=now,
        )
        # 时效通过 + slippage 跳过 → ok=True
        assert result.ok is True
        assert "max_slippage_bps_not_configured" in result.detail.get("skipped", "")

    def test_no_config_with_stale_snapshot_still_blocks(self) -> None:
        """没有 max_slippage_bps 但 snapshot 过期 → 仍因时效阻断。"""
        now = _AS_OF_BASE + timedelta(seconds=120)
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(),
            policy=_policy_with_slippage(max_slippage_bps=None),
            market_snapshot=_snapshot(),
            now=now,
        )
        assert result.ok is False
        assert result.reason_code == "MARKET_SNAPSHOT_STALE"


# ===========================================================================
# 5. 纯函数：非 place_order 跳过
# ===========================================================================
class TestNonPlaceOrderSkipped:
    """**Validates: G16** ——只对 place_order 生效。"""

    def test_read_market_action_skipped(self) -> None:
        """read_market 直接 ok=True，跳过所有检查。"""
        now = _AS_OF_BASE + timedelta(seconds=99999)  # 极端过期
        result = check_market_snapshot_freshness_and_slippage(
            action={"type": "read_market", "symbol": "btcusdt"},
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(),
            now=now,
        )
        assert result.ok is True
        assert result.detail.get("skipped") == "action_type_not_price_checked"

    def test_cancel_order_action_skipped(self) -> None:
        """cancel_order 引用已存在订单，与当前价无关。"""
        now = _AS_OF_BASE + timedelta(seconds=99999)
        result = check_market_snapshot_freshness_and_slippage(
            action={"type": "cancel_order", "symbol": "btcusdt", "order_id": "xyz"},
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(),
            now=now,
        )
        assert result.ok is True

    def test_no_op_action_skipped(self) -> None:
        """no_op 什么都不做，跳过。"""
        result = check_market_snapshot_freshness_and_slippage(
            action={"type": "no_op", "rationale": "降级"},
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(),
            now=_AS_OF_BASE,
        )
        assert result.ok is True


# ===========================================================================
# 6. 纯函数：market 单跳过 slippage
# ===========================================================================
class TestMarketOrderSkipsSlippage:
    """**Validates: G16** ——market 单不在此处控制滑点。"""

    def test_market_order_skips_slippage(self) -> None:
        """limit_price=None → slippage 检查跳过；时效仍生效。"""
        now = _AS_OF_BASE + timedelta(seconds=10)
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=None),
            policy=_policy_with_slippage(max_slippage_bps=10),
            market_snapshot=_snapshot(last=68000.0),
            now=now,
        )
        assert result.ok is True
        assert "limit_price_unset" in result.detail.get("skipped", "")

    def test_zero_limit_price_skips_slippage(self) -> None:
        """limit_price=0 视为未指定（与 None 同语义）。"""
        now = _AS_OF_BASE + timedelta(seconds=10)
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=0.0),
            policy=_policy_with_slippage(max_slippage_bps=10),
            market_snapshot=_snapshot(),
            now=now,
        )
        assert result.ok is True

    def test_market_order_with_stale_snapshot_still_blocks(self) -> None:
        """market 单 + snapshot 过期 → 仍因时效阻断（与 limit 单一致）。"""
        now = _AS_OF_BASE + timedelta(seconds=120)
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=None),
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(),
            now=now,
        )
        assert result.ok is False
        assert result.reason_code == "MARKET_SNAPSHOT_STALE"


# ===========================================================================
# 7. 纯函数：snapshot 无 as_of
# ===========================================================================
class TestSnapshotWithoutAsOf:
    """**Validates: G16** 保守策略——无 as_of 时跳过时效检查，但 slippage 仍生效。"""

    def test_snapshot_without_as_of_skips_freshness(self) -> None:
        """无 as_of + 偏差小 → ok=True（时效跳过）。"""
        now = _AS_OF_BASE + timedelta(seconds=99999)
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=68001.0),
            policy=_policy_with_slippage(max_slippage_bps=50),
            market_snapshot=_snapshot(last=68000.0, as_of=None),
            now=now,
        )
        assert result.ok is True

    def test_snapshot_without_as_of_still_enforces_slippage(self) -> None:
        """无 as_of 但 slippage 配置存在 + 偏差大 → 仍阻断。"""
        now = _AS_OF_BASE
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(limit_price=68680.0),
            policy=_policy_with_slippage(max_slippage_bps=50),
            market_snapshot=_snapshot(last=68000.0, as_of=None),
            now=now,
        )
        assert result.ok is False
        assert result.reason_code == "MARKET_SLIPPAGE_EXCEEDED"

    def test_invalid_as_of_string_treated_as_missing(self) -> None:
        """无法解析的 as_of 字符串视为缺失（保守跳过时效）。"""
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(),
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(as_of="not-a-date"),
            now=_AS_OF_BASE + timedelta(seconds=99999),
        )
        # 时效跳过；slippage（同价）也通过
        assert result.ok is True

    def test_as_of_with_z_suffix_supported(self) -> None:
        """ISO 8601 ``Z`` 后缀（UTC）可被正确解析。"""
        now = _AS_OF_BASE + timedelta(seconds=120)
        # as_of 用 ``Z`` 后缀（与 ``+00:00`` 等价）
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(),
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(as_of="2024-06-15T12:00:00Z"),
            now=now,
        )
        assert result.ok is False
        assert result.reason_code == "MARKET_SNAPSHOT_STALE"

    def test_as_of_as_datetime_object_supported(self) -> None:
        """as_of 直接是 datetime 实例时也能工作。"""
        now = _AS_OF_BASE + timedelta(seconds=120)
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(),
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(as_of=_AS_OF_BASE),
            now=now,
        )
        assert result.ok is False
        assert result.reason_code == "MARKET_SNAPSHOT_STALE"


# ===========================================================================
# 8. 纯函数：snapshot 缺该 symbol
# ===========================================================================
class TestSymbolNotInSnapshot:
    """**Validates: G16** ——symbol 不存在交给 PLAN_HALLUCINATION 处理，本模块跳过。"""

    def test_symbol_not_in_snapshot_skipped(self) -> None:
        """snapshot 没有 ``xyzusdt`` → ok=True（PLAN_HALLUCINATION 由 policy_engine 拦）。"""
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(symbol="xyzusdt"),
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(symbol="btcusdt"),
            now=_AS_OF_BASE,
        )
        assert result.ok is True
        assert result.detail.get("skipped") == "symbol_not_in_snapshot"


# ===========================================================================
# 9. 纯函数：naive datetime 防御
# ===========================================================================
class TestNaiveDatetimeDefence:
    """**Validates: G16** ——naive now 视为 UTC（与 audit_writer 同语义）。"""

    def test_naive_now_treated_as_utc(self) -> None:
        """now 不带 tzinfo → 当作 UTC，不报错。"""
        now_naive = datetime(2024, 6, 15, 12, 2, 0)  # 比 as_of 晚 2 分钟
        result = check_market_snapshot_freshness_and_slippage(
            action=_action_place_order(),
            policy=_policy_with_slippage(),
            market_snapshot=_snapshot(),
            now=now_naive,
        )
        # 120s > 60s → 阻断
        assert result.ok is False
        assert result.reason_code == "MARKET_SNAPSHOT_STALE"


# ===========================================================================
# 10. 集成：执行网关
# ===========================================================================
def _make_user(session: Session) -> User:
    user = User(
        id=uuid.uuid4(),
        primary_wallet=f"0xSTALE{uuid.uuid4().hex[:30]}",
        role="user",
    )
    session.add(user)
    session.flush()
    return user


def _make_passport_with_slippage(
    session: Session,
    user: User,
    *,
    max_slippage_bps: int = 50,
    state: str = PassportState.ACTIVE,
    version: int = 1,
) -> AgentPassport:
    """创建带 slippage 限制的 passport。"""
    passport = AgentPassport(
        id=uuid.uuid4(),
        user_id=user.id,
        name="stale-test-passport",
        agent_type="trader",
        state=state,
        version=version,
        policy_json=_policy_with_slippage(max_slippage_bps=max_slippage_bps),
        reputation_score=50,
    )
    session.add(passport)
    session.flush()
    return passport


def _make_action_for_execution(
    session: Session,
    user: User,
    passport: AgentPassport,
    *,
    state: str = ActionState.APPROVED,
    limit_price: float = 68000.0,
    policy_version_at_planning: int | None = 1,
) -> AgentAction:
    action = AgentAction(
        id=uuid.uuid4(),
        passport_id=passport.id,
        user_id=user.id,
        trace_id=uuid.uuid4(),
        natural_language_request="买入 0.001 BTC",
        normalized_action_json=_action_place_order(limit_price=limit_price),
        state=state,
        execution_mode="simulation",
        approval_required=True,
        policy_version_at_planning=policy_version_at_planning,
    )
    session.add(action)
    session.flush()
    return action


def _make_gateway(session: Session) -> ExecutionGateway:
    return ExecutionGateway(
        session=session,
        sim_engine=SimulationEngine(),
        htx_adapter=HTXAdapter(mode="mock"),
        config=ExecutionGatewayConfig(),
    )


class TestExecutionGatewayIntegration:
    """**Validates: G16 集成**——执行网关在重裁决阶段拦截 stale price。"""

    @pytest.mark.asyncio
    async def test_execution_gateway_blocks_when_limit_price_far_from_seed(
        self, db_session: Session, monkeypatch
    ) -> None:
        """偏离 SEED last 价 ~1764 bps >> 50 → 阻断 MARKET_SLIPPAGE_EXCEEDED。

        因 SEED_MARKET_DATA.as_of 是 2024-06-15 静态时间，gateway 先撞到
        MARKET_SNAPSHOT_STALE。为了精准验证 slippage 路径，monkeypatch
        ``_get_market_snapshot`` 返回带"当前时间"的 snapshot，让时效通过、
        slippage 单独触发。
        """
        user = _make_user(db_session)
        passport = _make_passport_with_slippage(
            db_session, user, max_slippage_bps=50
        )
        action = _make_action_for_execution(
            db_session, user, passport, limit_price=80000.0
        )
        gateway = _make_gateway(db_session)

        # 时效新鲜的快照——last=68000，limit=80000 → ~1764 bps > 50
        fresh_snapshot = {
            "btcusdt": {
                "last": 68000.0,
                "bid": 67999.0,
                "ask": 68001.0,
                "as_of": datetime.now(UTC).isoformat(),
            },
            "ethusdt": {
                "last": 3600.0,
                "bid": 3599.0,
                "ask": 3601.0,
                "as_of": datetime.now(UTC).isoformat(),
            },
        }
        monkeypatch.setattr(
            gateway, "_get_market_snapshot", lambda: fresh_snapshot
        )

        with pytest.raises(ConflictError) as exc_info:
            await gateway.execute(action.id)
        assert exc_info.value.code == "MARKET_SLIPPAGE_EXCEEDED"

        # 审计事件 MARKET_SLIPPAGE_DETECTED 已写入
        events = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.action_id == action.id,
                AuditEvent.event_type == AuditEventType.MARKET_SLIPPAGE_DETECTED,
            )
            .all()
        )
        assert len(events) == 1
        ed = events[0].event_json["data"]
        assert ed["reason_code"] == "MARKET_SLIPPAGE_EXCEEDED"
        assert ed["threshold_bps"] == 50
        assert ed["snapshot_last"] == 68000.0
        assert ed["limit_price"] == 80000.0

    @pytest.mark.asyncio
    async def test_execution_gateway_blocks_stale_seed_snapshot(
        self, db_session: Session, monkeypatch
    ) -> None:
        """SEED_MARKET_DATA.as_of=2024-06-15 → 现在远超 60s → MARKET_SNAPSHOT_STALE。

        这其实是 G16 在生产中希望发生的行为：种子数据时间永远过期，开发者必须
        接入实时数据才能成交（强制刷新），杜绝“按种子数据成交”的资金风险。
        """
        from app.services.execution_gateway import ExecutionGateway
        from app.services.htx_adapter import SEED_MARKET_DATA
        monkeypatch.setattr(
            ExecutionGateway, "_get_market_snapshot",
            lambda self: SEED_MARKET_DATA,
        )
        user = _make_user(db_session)
        passport = _make_passport_with_slippage(
            db_session, user, max_slippage_bps=500
        )
        action = _make_action_for_execution(
            db_session, user, passport, limit_price=68001.0
        )
        gateway = _make_gateway(db_session)

        with pytest.raises(ConflictError) as exc_info:
            await gateway.execute(action.id)
        assert exc_info.value.code == "MARKET_SNAPSHOT_STALE"

        events = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.action_id == action.id,
                AuditEvent.event_type == AuditEventType.MARKET_SLIPPAGE_DETECTED,
            )
            .all()
        )
        assert len(events) == 1
        assert events[0].event_json["data"]["reason_code"] == "MARKET_SNAPSHOT_STALE"

    @pytest.mark.asyncio
    async def test_execution_gateway_passes_with_fresh_snapshot_and_close_price(
        self, db_session: Session, monkeypatch
    ) -> None:
        """新鲜 snapshot + limit 接近 last → 通过执行（验证非阻断路径不破坏）。"""
        user = _make_user(db_session)
        passport = _make_passport_with_slippage(
            db_session, user, max_slippage_bps=50
        )
        action = _make_action_for_execution(
            db_session, user, passport, limit_price=68001.0
        )
        gateway = _make_gateway(db_session)

        # 时效新鲜 + 偏差 ~1.5 bps < 50 阈值
        fresh_snapshot = {
            "btcusdt": {
                "last": 68000.0,
                "bid": 67999.0,
                "ask": 68001.0,
                "as_of": datetime.now(UTC).isoformat(),
            },
            "ethusdt": {
                "last": 3600.0,
                "bid": 3599.0,
                "ask": 3601.0,
                "as_of": datetime.now(UTC).isoformat(),
            },
        }
        monkeypatch.setattr(
            gateway, "_get_market_snapshot", lambda: fresh_snapshot
        )

        result = await gateway.execute(action.id)
        # 正常完成：状态 EXECUTED，结果 SUCCESS
        assert result.status == "SUCCESS"
        db_session.refresh(action)
        assert action.state == ActionState.EXECUTED
        # 没有 MARKET_SLIPPAGE_DETECTED 事件
        events = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.action_id == action.id,
                AuditEvent.event_type == AuditEventType.MARKET_SLIPPAGE_DETECTED,
            )
            .all()
        )
        assert len(events) == 0


# ===========================================================================
# 11. 集成：审批服务
# ===========================================================================
class TestApprovalServiceIntegration:
    """**Validates: G16 集成**——审批 submit_approval 拦截 stale price。"""

    def _make_approval(
        self,
        db_session: Session,
        action: AgentAction,
        user: User,
        *,
        expires_at: datetime | None = None,
    ) -> Approval:
        if expires_at is None:
            expires_at = datetime.now(UTC) + timedelta(
                seconds=DEFAULT_APPROVAL_EXPIRES_SECONDS
            )
        approval = Approval(
            action_id=action.id,
            user_id=user.id,
            approval_type="typed_confirmation",
            approved=None,
            expires_at=expires_at,
        )
        db_session.add(approval)
        db_session.flush()
        return approval

    def test_approval_blocks_stale_price(self, db_session: Session, monkeypatch) -> None:
        """SEED_MARKET_DATA.as_of 已远过期 → 审批被 MarketSlippageExceededError 阻断。"""
        from app.services.htx_adapter import SEED_MARKET_DATA
        monkeypatch.setattr(
            "app.services.htx_adapter.get_fresh_seed_market_data",
            lambda: SEED_MARKET_DATA,
        )
        user = _make_user(db_session)
        passport = _make_passport_with_slippage(
            db_session, user, max_slippage_bps=50
        )
        # action 在 APPROVAL_REQUIRED；同 version 走不到 Step 7 重裁决
        action = AgentAction(
            id=uuid.uuid4(),
            passport_id=passport.id,
            user_id=user.id,
            trace_id=uuid.uuid4(),
            natural_language_request="买入 0.001 BTC",
            normalized_action_json=_action_place_order(limit_price=68000.0),
            state=ActionState.APPROVAL_REQUIRED,
            execution_mode="simulation",
            approval_required=True,
            policy_version_at_planning=1,
        )
        db_session.add(action)
        db_session.flush()

        self._make_approval(db_session, action, user)

        with pytest.raises(MarketSlippageExceededError) as exc_info:
            submit_approval(
                db_session,
                action_id=action.id,
                user_id=user.id,
                approved=True,
                typed_confirmation="APPROVE",
            )
        # SEED as_of=2024-06-15 → 现在 >> 60s → MARKET_SNAPSHOT_STALE
        assert exc_info.value.reason_code == "MARKET_SNAPSHOT_STALE"

        # action 被推到 REJECTED_BY_USER
        db_session.refresh(action)
        assert action.state == ActionState.REJECTED_BY_USER

        # 审计事件 MARKET_SLIPPAGE_DETECTED 已写入
        events = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.action_id == action.id,
                AuditEvent.event_type == AuditEventType.MARKET_SLIPPAGE_DETECTED,
            )
            .all()
        )
        assert len(events) == 1
        assert events[0].event_json["data"]["reason_code"] == "MARKET_SNAPSHOT_STALE"

    def test_approval_reject_path_does_not_check_stale_price(
        self, db_session: Session
    ) -> None:
        """approved=False（拒绝路径）不检查价格——直接走 REJECTED_BY_USER。"""
        user = _make_user(db_session)
        passport = _make_passport_with_slippage(
            db_session, user, max_slippage_bps=50
        )
        action = AgentAction(
            id=uuid.uuid4(),
            passport_id=passport.id,
            user_id=user.id,
            trace_id=uuid.uuid4(),
            natural_language_request="买入 0.001 BTC",
            normalized_action_json=_action_place_order(limit_price=68000.0),
            state=ActionState.APPROVAL_REQUIRED,
            execution_mode="simulation",
            approval_required=True,
            policy_version_at_planning=1,
        )
        db_session.add(action)
        db_session.flush()

        self._make_approval(db_session, action, user)

        # 拒绝不抛 MarketSlippageExceededError
        result = submit_approval(
            db_session,
            action_id=action.id,
            user_id=user.id,
            approved=False,
            typed_confirmation="REJECT",
        )
        assert result.state == ActionState.REJECTED_BY_USER
        # 没有 MARKET_SLIPPAGE_DETECTED 事件
        events = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.action_id == action.id,
                AuditEvent.event_type == AuditEventType.MARKET_SLIPPAGE_DETECTED,
            )
            .all()
        )
        assert len(events) == 0
