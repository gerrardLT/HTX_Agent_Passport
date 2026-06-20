"""审计写入器 + 链验证（任务 7.2 / Req 11 AC1-7）。

本模块在 :mod:`app.core.audit_chain` 提供的"哈希计算内核"之上，
追加**两条与数据库交互的运行时能力**：

1. :class:`AuditWriter.write` —— 自动取链尾、计算 ``previous_event_hash`` /
   ``event_hash``、写入 ``audit_events`` 表；
2. :class:`AuditWriter.verify_chain_integrity` —— 从 genesis 起逐事件
   重算并比对，定位第一处不一致。

设计依据
--------
- requirements.md Req 11 AC1：``event_hash = sha256(canonical_json + prev + ts)``
- requirements.md Req 11 AC2：链首事件 ``previous_event_hash = GENESIS_HASH``
- requirements.md Req 11 AC4：``verify_chain_integrity`` 必须能从 genesis
  逐事件重算并与存储值比对，任何不一致返回失败。
- requirements.md Req 11 AC5：``actor_id`` 允许 "SYSTEM" / "PLANNER" 等非 UUID。
- requirements.md Req 11 AC7：审计写入失败必须阻止业务转换——
  本模块仅 ``add()`` + ``flush()``；commit 由路由层控制；写入异常
  统一抛 :class:`RuntimeError`，调用方在事务中捕获即可让业务回滚。
- design.md「反馈层：审计写入器 + 哈希链计算器」。

链分组策略
----------
按 ``(user_id, passport_id)`` 二元组分链：

- ``passport_id=None``：取最近一条 ``passport_id IS NULL`` 的事件作为链尾——
  典型场景是 USER_LOGIN / CREDENTIAL_* 等"用户级"事件。
- ``passport_id`` 非 None：取该 passport 下最近一条事件作为链尾——
  让"用户级链"与"每个 passport 独立链"互不污染，便于审计重放界面按 passport
  分组渲染（Req 12 AC2）。

这与 ``audit_stub`` 仅按 ``user_id`` 分链的早期行为相比是更精细的分链规则——
但仍向后兼容：``audit_stub`` 在演示阶段只有 USER_LOGIN 这种 passport_id=None
的事件参与同一链，新规则下它们仍属同一条 ``(user_id, NULL)`` 链。

对于一个 passport 的第一条事件，``previous_event_hash = GENESIS_HASH``——
即便此时该用户的 ``(user_id, NULL)`` 链上已有 USER_LOGIN，两条链互不引用。

并发与事务语义
--------------
- 函数仅 ``session.add()`` + ``session.flush()``；不 ``commit()``。
- 同一事务内连续 :meth:`AuditWriter.write` 多次时：
  ``flush()`` 会把第 N 条事件落到 SQL 但**不**提交；第 N+1 次查询链尾时
  ORM 已能看到第 N 条（同一会话内的未提交读），保证哈希链连续。
- 不同请求并发写同一链时，依赖路由层的事务隔离级别处理；演示阶段
  PostgreSQL 默认 READ COMMITTED 即可，极端并发竞态不在 P0 范围内。

测试入口：``backend/tests/unit/test_audit_writer.py``。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.audit_chain import (
    compute_event_hash,
    get_genesis_hash,
)
from app.models import AuditEvent

# ---------------------------------------------------------------------------
# Actor 类型常量（与 audit_stub 兼容；其他模块仅依赖这些字符串值）
# ---------------------------------------------------------------------------
#: 人类用户主动触发（登录 / 创建凭证 / 审批 / 撤销 passport 等）。
ACTOR_TYPE_USER = "USER"
#: 系统自动触发（凭证验证 / 级联取消 / 后台过期清理等）。
ACTOR_TYPE_SYSTEM = "SYSTEM"
#: B.AI 规划器触发（任务 10）。
ACTOR_TYPE_PLANNER = "PLANNER"
#: 策略引擎触发（任务 8）。
ACTOR_TYPE_POLICY_ENGINE = "POLICY_ENGINE"
#: 执行网关 / HTX 适配器触发（任务 13）。
ACTOR_TYPE_EXECUTOR = "EXECUTOR"


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class AuditWriteError(RuntimeError):
    """审计写入失败异常——调用方必须回滚业务事务（Req 11 AC7）。

    继承 ``RuntimeError`` 以便不被通用 ``except Exception`` 意外吞掉；
    调用方应在事务边界显式捕获此异常并回滚。
    """


# ---------------------------------------------------------------------------
# 时间戳归一化
# ---------------------------------------------------------------------------
def _stable_iso_utc(dt: datetime) -> str:
    """把 ``datetime`` 转为稳定的 ISO 8601 字符串（始终包含 ``+00:00`` 偏移）。

    为什么需要归一化？
    -------------------
    - **PostgreSQL** ``TIMESTAMPTZ`` 经 SQLAlchemy 取回时带 ``tzinfo=UTC``，
      ``isoformat()`` 输出 ``"...+00:00"``。
    - **SQLite** 没有带时区的时间类型，SQLAlchemy 取回时 ``tzinfo`` 被剥离，
      同一个时间点的 ``isoformat()`` 会变成 ``"..."``（无偏移）。

    没有归一化时，:meth:`AuditWriter.verify_chain_integrity` 在 SQLite 上
    总会失败：``write`` 阶段用带 ``+00:00`` 的 iso 算 hash，``verify`` 阶段
    用 SQLite 取回的不带偏移的 iso 算 hash，二者不等。

    归一化规则
    ----------
    - 已有 ``tzinfo`` → 转 UTC 后 ``isoformat()`` → 一定带 ``+00:00``。
    - ``tzinfo is None`` → 当作 UTC（与本项目所有 ``DateTime(timezone=True)``
      列的约定一致），打上 UTC tz 后 ``isoformat()``。

    这样 write 与 verify 在两个引擎下都产出 ``"...+00:00"``，跨语言金标准
    向量（``test_audit_chain.test_known_test_vector``）也无变化。
    """
    if dt.tzinfo is None:
        # 数据库列定义是 ``DateTime(timezone=True)``；
        # 任何 tzinfo=None 的值都视为 UTC（SQLite 剥离 tz 后的恢复路径）。
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# AuditWriter
# ---------------------------------------------------------------------------
class AuditWriter:
    """审计事件写入器（Req 11 AC1-7）。

    Parameters
    ----------
    session : Session
        当前请求 / 测试用例的 SQLAlchemy 会话。本类只调用 ``add()`` /
        ``flush()`` / ``execute()``，不 ``commit()``——事务边界由调用方
        控制，与 :mod:`app.services.credentials` / :mod:`app.services.passports`
        的写入风格一致（Req 11 AC7）。
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # write —— 主入口
    # ------------------------------------------------------------------
    def write(
        self,
        *,
        event_type: str,
        user_id: uuid.UUID,
        actor_type: str = ACTOR_TYPE_SYSTEM,
        actor_id: str | None = None,
        passport_id: uuid.UUID | None = None,
        action_id: uuid.UUID | None = None,
        trace_id: uuid.UUID | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """写入一条审计事件。

        流程
        ----
        1. 派生 ``actor_id``：未传时使用 ``str(user_id)``（人类用户场景）。
        2. 取链尾：按 ``(user_id, passport_id)`` 查最新一条 audit_event；
           没有则用 :func:`get_genesis_hash` 作 ``previous_event_hash``。
        3. 派生 ``created_at``：使用 ``datetime.now(UTC)``，``isoformat()``
           输出**含微秒**——保留微秒精度让攻击者更难伪造一条"看似合法"
           的事件（即使知道 prev_hash，也还得猜对 ``created_at``）。
        4. 构造 ``event_json``：固定结构 ``{event_type, actor_type, actor_id,
           data}``，再用 :func:`compute_event_hash` 算出 ``event_hash``。
        5. ``add()`` + ``flush()`` 让 ``id`` / ``created_at`` 立即可读，
           异常包装成 :class:`RuntimeError` 抛给上层（确保业务转换被阻止）。

        Parameters
        ----------
        event_type : str
            必须是 :class:`app.models.enums.AuditEventType` 中声明的常量。
            本函数不做白名单校验（运行期开销过高），由调用方保证。
        user_id : uuid.UUID
            事件归属的用户。
        actor_type : str, default ACTOR_TYPE_SYSTEM
            行动者类型；见模块顶部常量。
        actor_id : str | None, default None
            行动者标识；为 None 时用 ``str(user_id)``。允许非 UUID（Req 11 AC5），
            如 ``"SYSTEM"`` / ``"PLANNER"``。
        passport_id, action_id : uuid.UUID | None
            事件涉及的 passport / action 主键，作为外键写入；用于审计重放
            筛选与链分组。
        trace_id : uuid.UUID | None
            请求级 trace_id（Req 13 AC2）；建议每个 HTTP 请求生成一枚并
            贯穿全部审计事件 / 模型调用 / 工具执行日志。
        event_data : dict[str, Any] | None
            事件负载——会被作为 ``event_json["data"]`` 序列化。**禁止**
            包含密钥 / 私钥 / token 等敏感字段（Req 2 AC7 / Req 15 AC1）。

        Returns
        -------
        AuditEvent
            已 ``flush`` 的 ORM 行（``id`` / ``created_at`` 已分配；
            ``event_hash`` 已计算）。

        Raises
        ------
        AuditWriteError
            ``flush()`` 失败（例如外键约束违反）时包装成 AuditWriteError 抛出，
            让调用方捕获后回滚业务事务（Req 11 AC7）。原始 SQLAlchemy 异常
            通过 ``__cause__`` 保留，便于排查。
        """
        actor_id_str = actor_id if actor_id is not None else str(user_id)

        # 步骤 2：取链尾 + 计算 previous_hash
        previous_hash = self._get_previous_hash(
            user_id=user_id, passport_id=passport_id
        )

        # 步骤 3：created_at（含微秒）
        created_at = datetime.now(UTC)
        created_at_iso = _stable_iso_utc(created_at)

        # 步骤 4：构造 event_json + 计算 event_hash
        event_json: dict[str, Any] = {
            "event_type": event_type,
            "actor_type": actor_type,
            "actor_id": actor_id_str,
            "data": event_data or {},
        }
        event_hash = compute_event_hash(event_json, previous_hash, created_at_iso)

        audit = AuditEvent(
            user_id=user_id,
            passport_id=passport_id,
            action_id=action_id,
            trace_id=trace_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id_str,
            event_json=event_json,
            previous_event_hash=previous_hash,
            event_hash=event_hash,
            created_at=created_at,
        )

        # 步骤 5：add + flush；任何 SQL 失败包装成 RuntimeError
        try:
            self.session.add(audit)
            self.session.flush()
        except SQLAlchemyError as exc:
            # Req 11 AC7：审计写入失败必须阻止业务转换。
            # 抛 AuditWriteError 让上层 try/except 跳转到回滚分支；
            # 业务函数（service 层）通常也不再 commit，事务自动回滚。
            raise AuditWriteError(
                f"audit write failed for event_type={event_type!r}: {exc!s}"
            ) from exc

        # 仅记录元数据，不打印 event_data（可能体量较大或含业务上下文）
        logger.debug(
            "audit event written",
            extra={
                "audit_id": str(audit.id),
                "event_type": event_type,
                "user_id": str(user_id),
                "passport_id": str(passport_id) if passport_id else None,
                "trace_id": str(trace_id) if trace_id else None,
            },
        )
        return audit

    # ------------------------------------------------------------------
    # verify_chain_integrity
    # ------------------------------------------------------------------
    def verify_chain_integrity(
        self,
        user_id: uuid.UUID,
        passport_id: uuid.UUID | None = None,
    ) -> tuple[bool, str | None]:
        """从 genesis 起逐事件重算并与存储值比对（Req 11 AC4）。

        Parameters
        ----------
        user_id : uuid.UUID
            链所属用户。
        passport_id : uuid.UUID | None, default None
            链分组维度的次键。``None`` 代表"用户级链"（USER_LOGIN /
            CREDENTIAL_* 等无 passport 关联的事件）；非 None 代表"该
            passport 的独立链"。

        Returns
        -------
        tuple[bool, str | None]
            ``(ok, error_msg)``：
            - ``(True, None)`` —— 链完整，所有事件 hash 与 prev 都自洽。
              空链也视为完整（没有事件可被篡改）。
            - ``(False, "<msg>")`` —— 第一处不一致；``error_msg`` 包含：
              出错事件 ID、不一致字段（``previous_event_hash`` /
              ``event_hash``）、期望值 vs 实际值。

        Notes
        -----
        实现细节：
        - 用 ``ORDER BY created_at ASC, id ASC`` 取全链；微秒级 created_at
          在多数情况下足以决定顺序，``id`` 作为次键保证同微秒插入顺序稳定。
        - 重算 hash 时严格使用存储行的 ``event_json`` /
          ``previous_event_hash`` / ``created_at``，**不**信任存储的
          ``event_hash``——这是检测篡改的唯一方式。
        - 检测到第一处不一致即返回，不继续遍历——后续事件因 prev 错位
          会全部不一致，单独报告意义不大。
        """
        events = self._get_chain_events(user_id=user_id, passport_id=passport_id)

        expected_prev = get_genesis_hash()

        for idx, event in enumerate(events):
            # ---- 1. 校验 previous_event_hash 是否等于上一条的 event_hash（首事件用 genesis） ----
            if event.previous_event_hash != expected_prev:
                return (
                    False,
                    self._format_chain_error(
                        idx=idx,
                        event=event,
                        field="previous_event_hash",
                        expected=expected_prev,
                        actual=event.previous_event_hash or "<NULL>",
                    ),
                )

            # ---- 2. 用存储的 event_json + prev + created_at 重算 hash，与存储值比对 ----
            recomputed = compute_event_hash(
                event.event_json,
                event.previous_event_hash or expected_prev,
                _stable_iso_utc(event.created_at),
            )
            if recomputed != event.event_hash:
                return (
                    False,
                    self._format_chain_error(
                        idx=idx,
                        event=event,
                        field="event_hash",
                        expected=recomputed,
                        actual=event.event_hash,
                    ),
                )

            expected_prev = event.event_hash

        return True, None

    # ------------------------------------------------------------------
    # 内部 helpers
    # ------------------------------------------------------------------
    def _get_previous_hash(
        self, *, user_id: uuid.UUID, passport_id: uuid.UUID | None
    ) -> str:
        """取链尾事件的 ``event_hash``；空链返回 :func:`get_genesis_hash`。

        排序键：``created_at DESC, id DESC``——
        ``id`` 作为次键防止同微秒级 created_at 下顺序漂移。
        """
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.user_id == user_id)
            .order_by(desc(AuditEvent.created_at), desc(AuditEvent.id))
            .limit(1)
        )
        # passport_id 的 None 与非 None 走两个不同的 WHERE 分支
        # （SQL ``= NULL`` 永远是 NULL，必须用 ``IS NULL``）
        if passport_id is None:
            stmt = stmt.where(AuditEvent.passport_id.is_(None))
        else:
            stmt = stmt.where(AuditEvent.passport_id == passport_id)

        prev = self.session.execute(stmt).scalar_one_or_none()
        if prev is None:
            return get_genesis_hash()
        return prev.event_hash

    def _get_chain_events(
        self, *, user_id: uuid.UUID, passport_id: uuid.UUID | None
    ) -> list[AuditEvent]:
        """按时间序拉取整条链。"""
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.user_id == user_id)
            .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        )
        if passport_id is None:
            stmt = stmt.where(AuditEvent.passport_id.is_(None))
        else:
            stmt = stmt.where(AuditEvent.passport_id == passport_id)
        return list(self.session.execute(stmt).scalars().all())

    @staticmethod
    def _format_chain_error(
        *,
        idx: int,
        event: AuditEvent,
        field: str,
        expected: str,
        actual: str,
    ) -> str:
        """构造可读的链不一致错误消息。

        消息中包含事件主键 + 时间戳，便于 audit 重放界面 / 运维定位
        到具体哪条记录被篡改 / 删除 / 插入。
        """
        return (
            f"chain integrity violation at index {idx} "
            f"(audit_id={event.id}, event_type={event.event_type!r}, "
            f"created_at={_stable_iso_utc(event.created_at)}): "
            f"{field} mismatch — expected={expected!r}, actual={actual!r}"
        )


# ---------------------------------------------------------------------------
# 便捷函数（与 audit_stub.write_audit_event_stub 等价的替代品）
# ---------------------------------------------------------------------------
def write_audit_event(session: Session, **kwargs: Any) -> AuditEvent:
    """便捷函数：等价于 ``AuditWriter(session).write(**kwargs)``。

    迁移路径：现有调用 ``write_audit_event_stub(db, ...)`` 的代码可以无缝替换为
    ``write_audit_event(db, ...)``——参数列表完全兼容。
    """
    return AuditWriter(session).write(**kwargs)


__all__ = [
    "ACTOR_TYPE_EXECUTOR",
    "ACTOR_TYPE_PLANNER",
    "ACTOR_TYPE_POLICY_ENGINE",
    "ACTOR_TYPE_SYSTEM",
    "ACTOR_TYPE_USER",
    "AuditWriteError",
    "AuditWriter",
    "write_audit_event",
]
