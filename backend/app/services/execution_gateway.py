"""执行网关（任务 13 / Req 9 + Req 15 + Property 7）。

唯一调用 HTX 私有端点的组件。设计要点：

1. 仅接受 APPROVED / AUTO_APPROVED 状态的 action_id（Property 7）
2. 执行前重新裁决（re-read policy + evaluate_policy）
3. 模式分发：simulation → SimulationEngine / real_read → HTXAdapter.get_ticker / real_trade → 检查 DEMO_REAL_TRADE
4. DEMO_DISABLE_EXECUTION=true → 全局拒绝
5. 无公共路由可传原始载荷；写入 execution_result + EXECUTION_STARTED/COMPLETED/FAILED 审计事件
6. 成功后更新声誉 reputation_score

异常：
- ConflictError (409)：action 状态不合法 / 重裁决 REJECT / kill switch
- ForbiddenError (403)：real_trade 未启用
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentAction, AgentPassport, ExecutionResult
from app.models.enums import ActionState, AuditEventType
from app.services.audit_writer import (
    ACTOR_TYPE_EXECUTOR,
    AuditWriter,
)
from app.services.htx_adapter import HTXAdapter
from app.services.daily_history import aggregate_daily_history_for_update
from app.services.policy_engine import (
    GlobalConfig,
    evaluate_policy,
)
from app.services.simulation_engine import SimulationEngine
from app.services.stale_price_check import (
    check_market_snapshot_freshness_and_slippage,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 行情快照缓存（修复：每次调用都生成新种子数据）
# ---------------------------------------------------------------------------
#: 模块级行情快照缓存。TTL 内复用同一份快照，避免每次重裁决都生成新 as_of
#: 导致 stale_price_check 无法检测真实过期。
_MARKET_SNAPSHOT_CACHE: dict[str, dict[str, Any]] | None = None
_MARKET_SNAPSHOT_CACHED_AT: float = 0.0
_MARKET_SNAPSHOT_TTL_SECONDS: float = 30.0  # 30s TTL


# ---------------------------------------------------------------------------
# 模块级辅助函数
# ---------------------------------------------------------------------------
def _get_enforce_market_provenance() -> bool:
    """从全局 settings 读取 G2 信息流追踪开关。

    放在模块级而非 ExecutionGateway 实例上，让 :class:`ExecutionGateway`
    构造签名保持稳定（不需要每次注入新参数）；切到生产环境时只需配
    ``ENFORCE_MARKET_PROVENANCE=true`` 即可对所有调用方生效。
    """
    from app.core.config import get_settings

    return bool(getattr(get_settings(), "ENFORCE_MARKET_PROVENANCE", False))


def _is_cedar_shadow_enabled() -> bool:
    """从全局 settings 读取 Cedar shadow evaluator 开关（G1）。"""
    from app.core.config import get_settings

    return bool(getattr(get_settings(), "CEDAR_SHADOW_ENABLED", False))


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class ConflictError(Exception):
    """409 状态冲突。"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(f"[409] {code}: {message}")


class ForbiddenError(Exception):
    """403 权限不足。"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        self.message = message or code
        super().__init__(f"[403] {code}: {message}")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExecutionGatewayConfig:
    """执行网关运行时配置。

    Attributes
    ----------
    demo_real_trade_enabled : bool
        环境变量 DEMO_REAL_TRADE=true 时为 True；否则 real_trade 模式被禁止。
    demo_disable_execution : bool
        环境变量 DEMO_DISABLE_EXECUTION=true 时为 True；全局拒绝所有执行。
    """

    demo_real_trade_enabled: bool = False
    demo_disable_execution: bool = False


# ---------------------------------------------------------------------------
# 执行网关
# ---------------------------------------------------------------------------
class ExecutionGateway:
    """执行网关（Req 9）：唯一调用 HTX 私有端点的组件。

    Parameters
    ----------
    session : Session
        SQLAlchemy 会话（事务由调用方管理）。
    sim_engine : SimulationEngine
        模拟引擎实例（simulation 模式）。
    htx_adapter : HTXAdapter
        HTX 适配器实例（real_read / real_trade 模式）。
    config : ExecutionGatewayConfig
        运行时配置（环境变量开关）。
    """

    #: 允许执行的 action 状态集合（Property 7）
    _EXECUTABLE_STATES: frozenset[str] = frozenset(
        {ActionState.APPROVED, ActionState.AUTO_APPROVED}
    )

    def __init__(
        self,
        session: Session,
        sim_engine: SimulationEngine,
        htx_adapter: HTXAdapter,
        config: ExecutionGatewayConfig,
    ) -> None:
        self.session = session
        self.sim_engine = sim_engine
        self.htx_adapter = htx_adapter
        self.config = config
        self.audit_writer = AuditWriter(session)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def execute(self, action_id: UUID) -> ExecutionResult:
        """执行已审批的 action（Req 9 全流程）。

        Steps:
        1. 加载 action，验证 state ∈ {APPROVED, AUTO_APPROVED}
        2. 检查 DEMO_DISABLE_EXECUTION kill switch
        3. 加载 passport，重新裁决（re-read policy + evaluate_policy）
        4. 模式分发（simulation / real_read / real_trade）
        5. 写入 execution_result 记录
        6. 写入 EXECUTION_STARTED + EXECUTION_COMPLETED 审计事件
        7. 更新声誉 reputation_score
        8. 更新 action state → EXECUTED

        Raises
        ------
        ConflictError
            action 状态不合法 / 重裁决 REJECT / kill switch 启用。
        ForbiddenError
            real_trade 模式未启用（DEMO_REAL_TRADE != true）。
        """
        # Step 1: 加载 action + 状态校验
        action = self.session.get(AgentAction, action_id)
        if action is None:
            raise ConflictError("ACTION_NOT_FOUND", f"action {action_id} not found")

        if action.state not in self._EXECUTABLE_STATES:
            raise ConflictError(
                "ACTION_NOT_APPROVED",
                f"action state is {action.state!r}, expected APPROVED or AUTO_APPROVED",
            )

        # Step 2: 全局 kill switch
        if self.config.demo_disable_execution:
            raise ConflictError(
                "EXECUTION_DISABLED",
                "DEMO_DISABLE_EXECUTION is enabled, all executions are blocked",
            )

        # Step 3: 加载 passport + 重新裁决
        passport = self.session.get(AgentPassport, action.passport_id)
        if passport is None:
            raise ConflictError("PASSPORT_NOT_FOUND", "associated passport not found")

        # Step 3a: 幂等防护（修复 G15）——若该 action 已有成功执行结果，拒绝重复执行。
        # action_id 是天然幂等键：一个 action 只应被执行一次。并发/重试到达时，
        # 先到者把 action 推进到 EXECUTED（Step 8），后到者在 Step 1 的状态校验
        # 处即被 ACTION_NOT_APPROVED 拦下。这里再加一道 execution_results 存在性
        # 检查，防御"状态尚未落库但已有执行结果"的极端窗口。
        existing_success = (
            self.session.execute(
                select(ExecutionResult)
                .where(ExecutionResult.action_id == action_id)
                .where(ExecutionResult.status == "SUCCESS")
            )
            .scalars()
            .first()
        )
        if existing_success is not None:
            raise ConflictError(
                "ALREADY_EXECUTED",
                f"action {action_id} already has a successful execution result",
            )

        # Step 3b: 聚合当日真实累计（修复 G13/G14）——带 passport 行锁，
        # 把"聚合-裁决-写入"串行化，消除并发击穿日限额的 TOCTOU 竞态。
        # 排除当前 action 自身，避免把自己算进既有累计造成双重计数。
        daily_history = aggregate_daily_history_for_update(
            self.session,
            passport_id=passport.id,
            exclude_action_id=action_id,
        )

        normalized_action = action.normalized_action_json or {}

        # Step 3c: stale-price 重校验（修复 G16 / Req 16 AC2）——审批延迟后
        # 市场可能已变。若 snapshot 过期或 limit_price 偏离当前 last 超
        # max_slippage_bps，阻断执行并写 MARKET_SLIPPAGE_DETECTED 审计事件,
        # 让用户重新发起 action（保守策略：不自动重裁决，避免"用户授权 X 价
        # 我自动按 Y 价成交"）。
        # market_snapshot 在这里只取一次：本步骤与紧随其后的 evaluate_policy
        # 共用同一份 snapshot，保证两者看到的"市场状态"一致。
        market_snapshot = self._get_market_snapshot()
        stale_result = check_market_snapshot_freshness_and_slippage(
            action=normalized_action,
            policy=passport.policy_json,
            market_snapshot=market_snapshot,
            now=datetime.now(UTC),
        )
        if not stale_result.ok:
            self.audit_writer.write(
                event_type=AuditEventType.MARKET_SLIPPAGE_DETECTED,
                user_id=action.user_id,
                passport_id=passport.id,
                action_id=action_id,
                trace_id=action.trace_id,
                actor_type=ACTOR_TYPE_EXECUTOR,
                actor_id=ACTOR_TYPE_EXECUTOR,
                event_data={
                    "reason_code": stale_result.reason_code,
                    **stale_result.detail,
                },
            )
            raise ConflictError(
                stale_result.reason_code or "MARKET_SLIPPAGE_DETECTED",
                f"stale price check failed: {stale_result.reason_code} "
                f"{stale_result.detail}",
            )

        re_verdict = evaluate_policy(
            action=normalized_action,
            policy=passport.policy_json,
            daily_history=daily_history,
            market_snapshot=market_snapshot,
            global_config=GlobalConfig(
                demo_disable_execution=self.config.demo_disable_execution,
                # G18：传 passport 当前声誉分给 Policy Engine,让
                # auto_approval_thresholds.min_reputation_score 检查能生效。
                passport_reputation_score=passport.reputation_score,
                # G2 信息流追踪：从全局配置读取开关，启用后 market data
                # 的 provenance 字段必须在白名单内才放行 place_order。
                # SEED_MARKET_DATA 已带 ``provenance="seed"``,与本检查兼容。
                enforce_market_provenance=_get_enforce_market_provenance(),
            ),
        )

        if re_verdict.verdict == "REJECT":
            # 写入 EXECUTION_BLOCKED_BY_RECHECK 审计事件
            self.audit_writer.write(
                event_type=AuditEventType.EXECUTION_BLOCKED_BY_RECHECK,
                user_id=action.user_id,
                passport_id=passport.id,
                action_id=action_id,
                trace_id=action.trace_id,
                actor_type=ACTOR_TYPE_EXECUTOR,
                actor_id=ACTOR_TYPE_EXECUTOR,
                event_data={
                    "reason_codes": list(re_verdict.reason_codes),
                    "risk_score": re_verdict.risk_score,
                },
            )
            raise ConflictError(
                "POLICY_RECHECK_REJECT",
                f"re-verdict rejected: {list(re_verdict.reason_codes)}",
            )

        # G1 Cedar shadow（Phase 2 PoC）：与主裁决器并行跑 Cedar 评估,
        # 差异只写日志,不影响主路径决定。30 天 0 差异后再考虑切换。
        if _is_cedar_shadow_enabled():
            self._run_cedar_shadow(
                action=normalized_action,
                policy=passport.policy_json,
                kill_switch=self.config.demo_disable_execution,
                main_verdict=re_verdict.verdict,
                main_reason_codes=re_verdict.reason_codes,
            )

        # 写入 EXECUTION_STARTED 审计事件
        self.audit_writer.write(
            event_type=AuditEventType.EXECUTION_STARTED,
            user_id=action.user_id,
            passport_id=passport.id,
            action_id=action_id,
            trace_id=action.trace_id,
            actor_type=ACTOR_TYPE_EXECUTOR,
            actor_id=ACTOR_TYPE_EXECUTOR,
            event_data={"execution_mode": action.execution_mode},
        )

        # Step 4: 模式分发
        try:
            result_data = await self._dispatch_execution(action)
        except Exception as exc:
            # 执行失败：写入 EXECUTION_FAILED 审计事件
            self.audit_writer.write(
                event_type=AuditEventType.EXECUTION_FAILED,
                user_id=action.user_id,
                passport_id=passport.id,
                action_id=action_id,
                trace_id=action.trace_id,
                actor_type=ACTOR_TYPE_EXECUTOR,
                actor_id=ACTOR_TYPE_EXECUTOR,
                event_data={"error": str(exc)},
            )
            action.state = ActionState.EXECUTION_FAILED
            self.session.flush()
            raise

        # Step 5: 写入 execution_result 记录
        exec_result = ExecutionResult(
            action_id=action_id,
            provider="HTX",
            mode=action.execution_mode,
            request_payload=normalized_action,
            response_payload=result_data,
            provider_order_id=result_data.get("order_id"),
            status="SUCCESS",
        )
        self.session.add(exec_result)
        self.session.flush()

        # Step 6: 写入 EXECUTION_COMPLETED 审计事件
        self.audit_writer.write(
            event_type=AuditEventType.EXECUTION_COMPLETED,
            user_id=action.user_id,
            passport_id=passport.id,
            action_id=action_id,
            trace_id=action.trace_id,
            actor_type=ACTOR_TYPE_EXECUTOR,
            actor_id=ACTOR_TYPE_EXECUTOR,
            event_data={
                "execution_mode": action.execution_mode,
                "provider_order_id": result_data.get("order_id"),
                "status": "SUCCESS",
            },
        )

        # Step 7: 更新声誉
        self._update_reputation(passport, delta=+2)

        # Step 8: 更新 action state → EXECUTED
        action.state = ActionState.EXECUTED
        self.session.flush()

        return exec_result

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    async def _dispatch_execution(self, action: AgentAction) -> dict[str, Any]:
        """根据 execution_mode 分发执行。

        Returns
        -------
        dict[str, Any]
            执行结果的 dict 表示（用于写入 response_payload）。
        """
        normalized = action.normalized_action_json or {}
        mode = action.execution_mode

        # no_op：不调用任何工具/交易所，直接返回空操作结果。
        # no_op 经 Policy Engine 走 ALLOW（什么也不做），常见于规划器降级
        # （B.AI 不可用 → mock no_op plan）或规则路由拦截后的安全中止。
        if normalized.get("type") == "no_op":
            return {
                "order_id": None,
                "symbol": None,
                "status": "NO_OP",
                "rationale": normalized.get("rationale", ""),
            }

        if mode == "simulation":
            sim_result = self.sim_engine.execute(normalized)
            return {
                "order_id": sim_result.order_id,
                "symbol": sim_result.symbol,
                "side": sim_result.side,
                "order_type": sim_result.order_type,
                "amount": sim_result.amount,
                "price": sim_result.price,
                "status": sim_result.status,
                "filled_amount": sim_result.filled_amount,
                "filled_price": sim_result.filled_price,
            }

        elif mode == "real_read":
            symbol = normalized.get("symbol", "btcusdt")
            ticker = await self.htx_adapter.get_ticker(symbol)
            return {
                "symbol": ticker.symbol,
                "last": ticker.last,
                "bid": ticker.bid,
                "ask": ticker.ask,
                "vol_24h": ticker.vol_24h,
            }

        elif mode == "real_trade":
            if not self.config.demo_real_trade_enabled:
                raise ForbiddenError(
                    "REAL_TRADE_DISABLED",
                    "DEMO_REAL_TRADE is not enabled; real_trade mode is forbidden",
                )
            # 真实交易调用（MVP 阶段 HTXAdapter 会抛 NotImplemented）
            symbol = normalized.get("symbol", "btcusdt")
            side = normalized.get("side", "buy")
            order_type = normalized.get("order_type", "limit")
            amount = float(normalized.get("amount", 0))
            price = normalized.get("limit_price")
            order_result = await self.htx_adapter.place_spot_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                amount=amount,
                price=price,
            )
            return {
                "order_id": order_result.order_id,
                "symbol": order_result.symbol,
                "side": order_result.side,
                "status": order_result.status,
                "filled_amount": order_result.filled_amount,
                "filled_price": order_result.filled_price,
            }

        else:
            raise ConflictError(
                "INVALID_EXECUTION_MODE",
                f"unknown execution_mode: {mode!r}",
            )

    def _get_market_snapshot(self) -> dict[str, dict[str, Any]]:
        """获取市场快照用于重裁决（带 TTL 缓存）。

        缓存策略
        --------
        - 模块级缓存 ``_MARKET_SNAPSHOT_CACHE``，TTL 默认 30s。
        - 缓存命中时直接返回，不生成新 ``as_of``，让 stale_price_check
          能在快照真正过期时检测到。
        - 生产环境应替换为 HTX 实时行情或 Redis 缓存。

        G2 信息流追踪：各 entry 已带 ``provenance="seed"``——若后续接入
        实时 HTX，``get_ticker`` 路径返回的条目应标 ``provenance="htx_real"``,
        缓存层返回 ``"htx_cached"``。
        """
        global _MARKET_SNAPSHOT_CACHE, _MARKET_SNAPSHOT_CACHED_AT

        import time
        now = time.monotonic()
        if (
            _MARKET_SNAPSHOT_CACHE is not None
            and (now - _MARKET_SNAPSHOT_CACHED_AT) < _MARKET_SNAPSHOT_TTL_SECONDS
        ):
            return _MARKET_SNAPSHOT_CACHE

        from app.services.htx_adapter import get_fresh_seed_market_data

        snapshot = get_fresh_seed_market_data()
        _MARKET_SNAPSHOT_CACHE = snapshot
        _MARKET_SNAPSHOT_CACHED_AT = now
        return snapshot

    def _run_cedar_shadow(
        self,
        *,
        action: dict[str, Any],
        policy: dict[str, Any],
        kill_switch: bool,
        main_verdict: str,
        main_reason_codes: tuple[str, ...],
    ) -> None:
        """运行 Cedar 影子评估并记日志（G1 / Phase 2 PoC）。

        永不抛异常——任何 Cedar 错误都吞为日志,不影响主路径业务流。
        30 天观察期内的差异统计依据。
        """
        try:
            from app.services.policy_engine_cedar import (
                log_cedar_shadow_difference,
                shadow_evaluate,
            )

            cedar_result = shadow_evaluate(
                action=action,
                policy=policy,
                kill_switch=kill_switch,
            )
            log_cedar_shadow_difference(
                cedar_result=cedar_result,
                main_verdict=main_verdict,
                main_reason_codes=main_reason_codes,
                action=action,
            )
        except Exception:  # noqa: BLE001 — shadow 永不阻断
            logger.exception("CEDAR_SHADOW_UNEXPECTED_ERROR")

    def _update_reputation(self, passport: AgentPassport, delta: int) -> None:
        """更新 passport 声誉分（Req 24）。

        Parameters
        ----------
        passport : AgentPassport
            目标 passport ORM 实例。
        delta : int
            增减值（正数增加，负数减少）。
        """
        new_score = max(0, min(100, passport.reputation_score + delta))
        passport.reputation_score = new_score
        self.session.flush()

        # 写入 REPUTATION_UPDATED 审计事件
        self.audit_writer.write(
            event_type=AuditEventType.REPUTATION_UPDATED,
            user_id=passport.user_id,
            passport_id=passport.id,
            actor_type=ACTOR_TYPE_EXECUTOR,
            actor_id=ACTOR_TYPE_EXECUTOR,
            event_data={
                "previous_score": passport.reputation_score - delta,
                "new_score": new_score,
                "delta": delta,
                "reason": "EXECUTED",
            },
        )


__all__ = [
    "ConflictError",
    "ExecutionGateway",
    "ExecutionGatewayConfig",
    "ForbiddenError",
]
