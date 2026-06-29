"""周期 STH 签发后台任务（Phase 1 / G10-G11 跟进）。

职责
----
1. 定时（默认 5 分钟）扫描所有 (user_id, passport_id) 唯一对，
   对每条审计链调一次 :func:`issue_signed_tree_head`。
2. **只在 tree_size 比上次签发时多时**才签发新 STH——避免无新事件时连续生成
   相同 root 的冗余 STH（让 ``audit_tree_heads`` 表成为单调有意义的时间序列）。
3. 写入后立即调 :func:`anchor_sth_to_file` 把 STH 追加到外部锚定文件。
4. 任何单链失败不影响其他链；任何一轮失败不影响下一轮。

为什么不用 APScheduler？
------------------------
- 减少新依赖（hackathon scope 越小越好）；
- ``asyncio.create_task`` + ``asyncio.sleep`` 已经能完美解决"周期性后台任务"
  这个最简形态。APScheduler 的优势在 cron 表达式 / 持久化 jobs / 集群 lock，
  这里都用不上。
- 测试可注入超短间隔（``AUDIT_STH_INTERVAL_SECONDS=0.05``）让单测跑 1-2 轮就停。

为什么 db_factory 注入而非全局 SessionLocal？
---------------------------------------------
scheduler 的生命周期与 HTTP request 的 ``Session`` **不同**——它在后台长跑，
每轮自己开/关事务。直接持有 ``SessionLocal`` 会让"测试场景注入 in-memory SQLite"
变得困难。改成依赖注入一个 ``Callable[[], Session]`` 工厂，scheduler 内部
``with closing(db_factory()) as db: ...`` 自管事务边界，测试就能传 lambda。

事务边界
--------
每一轮迭代里：scheduler 自己开 Session、调 ``issue_signed_tree_head``（只 flush）、
``db.commit()`` 提交、``db.close()`` 关闭——典型的"短事务每轮 commit"模型，
不污染其他 session。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import AuditEvent, AuditTreeHead
from app.services.audit_merkle_service import (
    get_latest_sth,
    issue_signed_tree_head,
)
from app.services.audit_sth_anchor import (
    AnchorBackend,
    NullAnchorBackend,
    get_default_anchor_backend,
)

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

#: Session 工厂签名——参数无、返回新 Session（调用方负责 close）。
SessionFactory = Callable[[], Session]


def _list_chains(db: Session) -> list[tuple[UUID, UUID | None]]:
    """扫描 ``audit_events`` 找出所有不重复的 (user_id, passport_id) 组合。

    用 ``DISTINCT`` 直接交给 DB 处理，避免在 Python 层 dedupe 大表。
    返回顺序无要求——scheduler 内部按返回顺序逐个签发。
    """
    rows = db.execute(
        select(AuditEvent.user_id, AuditEvent.passport_id).distinct()
    ).all()
    return [(row[0], row[1]) for row in rows]


def issue_sth_for_all_chains(db: Session) -> list[AuditTreeHead]:
    """对所有 (user_id, passport_id) 链各签发一份 STH（如果有新事件）。

    流程
    ----
    1. 用 ``SELECT DISTINCT user_id, passport_id FROM audit_events`` 找出所有链。
    2. 对每条链：
       - 查 :func:`get_latest_sth` 拿到上次的 ``tree_size``；
       - 数当前链的事件数；
       - **仅当当前 > 上次** 时调 :func:`issue_signed_tree_head` 签发新 STH。
    3. 收集所有新签发的 STH 返回。
    4. 单链失败（DB 异常）记 logger.error 后**继续其他链**，不让一坏全坏。

    Parameters
    ----------
    db : Session
        当前轮使用的 SQLAlchemy 会话。**调用方负责 commit / close**——
        scheduler 在 :meth:`STHScheduler._tick` 里负责事务边界。

    Returns
    -------
    list[AuditTreeHead]
        本轮真正签发的 STH 列表（已 flush 但**未 commit**）。
        无变化的链不会出现在返回值中。
    """
    issued: list[AuditTreeHead] = []
    chains = _list_chains(db)

    for user_id, passport_id in chains:
        try:
            # 拉当前事件数
            current_size = db.execute(
                select(AuditEvent)
                .where(AuditEvent.user_id == user_id)
                .where(
                    AuditEvent.passport_id.is_(None)
                    if passport_id is None
                    else AuditEvent.passport_id == passport_id
                )
            ).all()
            current_count = len(current_size)

            latest = get_latest_sth(
                db, user_id=user_id, passport_id=passport_id
            )
            last_size = latest.tree_size if latest is not None else -1
            # last_size = -1 让"从未签过"也走签发分支（current_count >= 0 总成立）

            if current_count <= last_size:
                # 没有新事件——跳过，避免在 audit_tree_heads 里堆冗余的"同 root"行
                continue

            sth = issue_signed_tree_head(
                db, user_id=user_id, passport_id=passport_id
            )
            issued.append(sth)

        except Exception:  # noqa: BLE001 — 后台任务必须吞所有错继续
            logger.exception(
                "STH issuance failed for chain user=%s passport=%s",
                user_id, passport_id,
            )
            # 不 raise，继续下一条链
            continue

    return issued


# ---------------------------------------------------------------------------
# Scheduler 类
# ---------------------------------------------------------------------------
class STHScheduler:
    """周期 STH 签发的后台 task。

    生命周期
    --------
    - :meth:`start` 创建 ``asyncio.Task`` + 内部 ``stop_event``。多次 ``start``
      幂等：第二次以后看到已有 task 直接返回，不再起新的。
    - :meth:`stop` 设 ``stop_event``，等待 ``task`` 结束（``await task``）。
      多次 ``stop`` 也幂等：第二次以后看到没有 task 直接返回。

    使用示例
    --------
    在 FastAPI lifespan 里::

        from app.services.audit_sth_scheduler import STHScheduler
        from app.core.database import get_sessionmaker

        scheduler = STHScheduler(
            db_factory=lambda: get_sessionmaker()(),
            interval_seconds=settings.AUDIT_STH_INTERVAL_SECONDS,
            anchor_path=settings.AUDIT_STH_ANCHOR_PATH,
        )
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    在测试里::

        scheduler = STHScheduler(
            db_factory=lambda: my_fixture_session,  # 或一个 wrapper
            interval_seconds=0.05,
            anchor_path="",
        )
        await scheduler.start()
        await asyncio.sleep(0.2)  # 让它跑 3-4 轮
        await scheduler.stop()
    """

    def __init__(
        self,
        *,
        db_factory: SessionFactory,
        interval_seconds: float,
        anchor_path: str = "",
        anchor_backend: AnchorBackend | None = None,
    ) -> None:
        """初始化 scheduler。

        Parameters
        ----------
        db_factory : SessionFactory
            每轮 tick 调用一次，开新 SQLAlchemy session。
        interval_seconds : float
            tick 间隔（秒）；测试可注入 0.05 让快速跑 1-2 轮。
        anchor_path : str, default ""
            **向后兼容**字段：若提供且未传 ``anchor_backend``，使用
            :class:`JsonLineFileAnchorBackend(anchor_path)`。新代码建议
            直接传 ``anchor_backend``。
        anchor_backend : AnchorBackend | None, default None
            锚定后端实例。优先级最高；为 None 时按 ``anchor_path`` 兜底，
            再为空则用 :class:`NullAnchorBackend`。
        """
        self._db_factory = db_factory
        self._interval = max(0.001, float(interval_seconds))  # 防御 0 / 负值
        self._anchor_path = anchor_path  # 保留供日志展示
        if anchor_backend is not None:
            self._anchor_backend: AnchorBackend = anchor_backend
        elif anchor_path:
            from app.services.audit_sth_anchor import JsonLineFileAnchorBackend
            self._anchor_backend = JsonLineFileAnchorBackend(anchor_path)
        else:
            self._anchor_backend = NullAnchorBackend()
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        """当前是否有 task 在跑。"""
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动后台循环。已在跑时直接 no-op（幂等）。"""
        if self.is_running:
            logger.debug("STHScheduler.start ignored (already running)")
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="sth-scheduler")
        logger.info(
            "STH scheduler started (interval=%.3fs, anchor=%s)",
            self._interval, self._anchor_path or "<disabled>",
        )

    # ------------------------------------------------------------------
    async def stop(self) -> None:
        """停止后台循环。已停 / 从未启动时直接 no-op（幂等）。

        步骤：``stop_event.set()`` → 等 task 自己跑完一轮（最多 ``interval`` 秒）→
        清理引用。stop 不会强行 cancel——让 ``_tick`` 完成当前轮的 DB commit
        后正常退出，避免半提交事务。
        """
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            # 容忍 task 已结束（_run 自然退出后再调 stop）
            await asyncio.wait_for(self._task, timeout=self._interval * 2 + 1.0)
        except TimeoutError:
            logger.warning("STH scheduler stop timed out, cancelling task")
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        finally:
            self._task = None
            self._stop_event = None
            logger.info("STH scheduler stopped")

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        """主循环：跑 ``_tick`` → ``sleep(interval)`` 直到 ``stop_event`` 被设。

        用 ``stop_event.wait`` + ``timeout=interval`` 实现"可中断的 sleep"——
        ``stop()`` 设 event 后立即从 sleep 醒来，避免等满 interval 才退出。
        """
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                # 单轮异常已经在 _tick / issue_sth_for_all_chains 里被捕获了；
                # 这里再兜一层，理论上不会触发——但万一未来逻辑改动留了漏网之鱼,
                # 不能让后台 task 整个挂掉。
                logger.exception("STH scheduler tick failed unexpectedly")

            # 可中断的 sleep
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval,
                )
                # 走到这里 = stop_event 被设 → 退出循环
                return
            except TimeoutError:
                # 正常的"时间到，跑下一轮"
                continue

    # ------------------------------------------------------------------
    async def _tick(self) -> None:
        """执行一轮签发。

        把 DB 操作放到 :meth:`asyncio.to_thread`——SQLAlchemy 同步 ORM 在
        async 上下文里调 ``flush`` / ``commit`` 会阻塞 event loop；用线程池
        offload 让其他 async 任务（HTTP / WebSocket）不被卡。
        """
        await asyncio.to_thread(self._tick_sync)

    # ------------------------------------------------------------------
    def _tick_sync(self) -> None:
        """同步版 tick：开 Session、签发、commit、关闭、anchor。"""
        db = self._db_factory()
        try:
            issued = issue_sth_for_all_chains(db)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("STH scheduler tick rolled back")
            return
        finally:
            db.close()

        # commit 之后再 anchor——避免"已锚定但 DB 回滚"的不一致情况。
        for sth in issued:
            try:
                self._anchor_backend.anchor(sth)
            except Exception:  # noqa: BLE001
                # backend.anchor 自身已吞错；这里再兜一层防御
                logger.exception("STH anchor unexpectedly raised")


# ---------------------------------------------------------------------------
# 工厂便利函数
# ---------------------------------------------------------------------------
def build_default_scheduler() -> STHScheduler:
    """从全局 settings 构造一个标准 scheduler。

    供 :func:`main.create_app` 的 lifespan 调用——读取
    ``AUDIT_STH_INTERVAL_SECONDS`` / ``AUDIT_STH_ANCHOR_BACKEND`` 等配置,
    并把 ``app.core.database.get_sessionmaker`` 包装成 db_factory。

    锚定 backend 通过 :func:`get_default_anchor_backend` 工厂选择，
    支持 jsonl / s3 / null 三种后端配置切换无需改业务代码。

    测试可以**绕过**这个函数，直接 ``STHScheduler(...)`` 注入 mock。
    """
    from app.core.database import get_sessionmaker

    settings = get_settings()

    def _factory() -> Session:
        SessionLocal = get_sessionmaker()  # noqa: N806
        return SessionLocal()

    return STHScheduler(
        db_factory=_factory,
        interval_seconds=settings.AUDIT_STH_INTERVAL_SECONDS,
        anchor_backend=get_default_anchor_backend(),
    )


__all__ = [
    "STHScheduler",
    "build_default_scheduler",
    "issue_sth_for_all_chains",
]
