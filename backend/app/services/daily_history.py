"""当日累计聚合（修复 G13/G14：日限额从未真正生效 + TOCTOU 竞态）。

背景
----
``app.services.policy_engine.evaluate_policy`` 接收一个 :class:`DailyActionHistory`
快照来做 ``max_daily_notional_usdt`` / ``max_orders_per_day`` 裁决。但在修复前，
所有调用方都传入空的 ``DailyActionHistory()``（值恒为 0），导致这两个日级限额
**从未真正生效**。本模块提供从 DB 真实聚合当日累计的能力。

聚合口径（Req 4 AC5 / Req 7 AC6-7）
----------------------------------
- **日边界**：UTC 00:00:00 切日（``created_at >= 当日 UTC 0 点``）。
- **计入范围**：当日所有「已执行 + 在途（已审批/执行中/待审批）」的写操作
  （place_order / cancel_order）。在途也计入是为了防止"并发审批多个单累计
  超限"——这是 Req 4 AC5「已执行 + 待执行的累计」的语义。
- **notional**：累加 ``normalized_action_json.max_notional_usdt``（仅 place_order）。
- **order_count**：place_order + cancel_order 的条数。

TOCTOU 安全
-----------
:func:`aggregate_daily_history_for_update` 在调用前对 passport 行加
``SELECT ... FOR UPDATE`` 锁（PostgreSQL），把"聚合-裁决-写入"串行化到同一
passport 上，消除并发请求各自读到旧累计、各自放行的竞态（OWASP Business
Logic / CWE-367）。SQLite（测试）不支持行锁，``with_for_update`` 会被忽略，
但测试默认串行执行，不影响正确性验证。

设计为独立模块而非塞进 policy_engine：policy_engine 保持纯函数（无 I/O），
本模块负责"有 I/O 的聚合"，职责分离便于 PBT 与并发测试。
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentAction, AgentPassport
from app.models.enums import ActionState
from app.services.policy_engine import DailyActionHistory

#: 计入当日累计的写操作类型（与 policy_engine._WRITE_ACTION_TYPES 一致）。
_WRITE_ACTION_TYPES: frozenset[str] = frozenset({"place_order", "cancel_order"})

#: 「在途 + 已完成」中应计入日累计的 action 状态。
#: 已拒绝/过期/取消/失败的不占用额度。
_COUNTED_STATES: frozenset[str] = frozenset(
    {
        ActionState.APPROVAL_REQUIRED,
        ActionState.AUTO_APPROVED,
        ActionState.APPROVED,
        ActionState.EXECUTING,
        ActionState.EXECUTED,
    }
)


def _utc_day_start(now: datetime | None = None) -> datetime:
    """返回当前 UTC 日的 00:00:00（带 tzinfo=UTC）。"""
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    current = current.astimezone(UTC)
    return datetime.combine(current.date(), time.min, tzinfo=UTC)


def aggregate_daily_history(
    db: Session,
    *,
    passport_id: UUID,
    now: datetime | None = None,
    exclude_action_id: UUID | None = None,
) -> DailyActionHistory:
    """聚合某 passport 当日（UTC）的累计 notional 与订单数。

    Parameters
    ----------
    db : Session
        当前会话。
    passport_id : UUID
        目标 passport。
    now : datetime | None
        当前时间（用于确定 UTC 日边界）；None 时取 ``datetime.now(UTC)``。
    exclude_action_id : UUID | None
        要排除的 action（通常是"当前正在裁决的这一笔"——避免把自己算进
        既有累计里造成双重计数）。

    Returns
    -------
    DailyActionHistory
        ``total_notional_today_utc`` + ``order_count_today_utc``。
    """
    day_start = _utc_day_start(now)

    stmt = (
        select(AgentAction)
        .where(AgentAction.passport_id == passport_id)
        .where(AgentAction.created_at >= day_start)
        .where(AgentAction.state.in_(tuple(_COUNTED_STATES)))
    )
    if exclude_action_id is not None:
        stmt = stmt.where(AgentAction.id != exclude_action_id)

    actions = db.execute(stmt).scalars().all()

    total_notional = 0.0
    order_count = 0
    for action in actions:
        normalized = action.normalized_action_json or {}
        atype = normalized.get("type")
        if atype not in _WRITE_ACTION_TYPES:
            continue
        order_count += 1
        if atype == "place_order":
            try:
                total_notional += float(normalized.get("max_notional_usdt", 0) or 0)
            except (TypeError, ValueError):
                # 脏数据不应让聚合崩溃；按 0 计但保留订单计数
                pass

    return DailyActionHistory(
        total_notional_today_utc=total_notional,
        order_count_today_utc=order_count,
    )


def aggregate_daily_history_for_update(
    db: Session,
    *,
    passport_id: UUID,
    now: datetime | None = None,
    exclude_action_id: UUID | None = None,
) -> DailyActionHistory:
    """带 passport 行锁的聚合（TOCTOU 安全版本）。

    在聚合前对 passport 行执行 ``SELECT ... FOR UPDATE``，把同一 passport 上
    的"聚合-裁决-写入"串行化。调用方必须在一个**未提交的事务**内调用本函数，
    并在同一事务内完成后续的状态写入与 commit，锁才会持续到 commit/rollback。

    Notes
    -----
    - PostgreSQL：行锁生效，并发请求阻塞等待。
    - SQLite（测试）：``with_for_update`` 被静默忽略，但测试串行执行不受影响。
    """
    # 对 passport 行加锁——锁的是"额度归属主体"，而非逐 action 行。
    db.execute(
        select(AgentPassport.id)
        .where(AgentPassport.id == passport_id)
        .with_for_update()
    ).first()

    return aggregate_daily_history(
        db,
        passport_id=passport_id,
        now=now,
        exclude_action_id=exclude_action_id,
    )


__all__ = [
    "aggregate_daily_history",
    "aggregate_daily_history_for_update",
]
