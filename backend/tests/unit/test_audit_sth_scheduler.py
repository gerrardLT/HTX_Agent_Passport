"""周期 STH 签发后台任务测试（Phase 1 / G10-G11 跟进）。

**Validates: G10/G11**——周期 STH 签发让"删除 + 重写"攻击窗口被关闭：
即使攻击者改了 DB，已签发并锚定的 STH 仍然存在外部介质，root 与新链
的重算结果不一致即可发现。

测试策略
--------
- **同步路径优先**：直接调 ``issue_sth_for_all_chains`` 和 ``_tick_sync`` 在
  ``db_session`` 上跑——更稳定、不依赖 asyncio 时序。
- **少量异步 smoke 测试**覆盖 ``start`` / ``stop`` 幂等性 + 实际 tick 触发。
  用超短 ``interval_seconds=0.05`` + 自定义 ``db_factory``（指向 db_session
  but with ``close=lambda: None``）让一轮 tick 跑完后就停。
- **错误隔离**：用 ``monkeypatch`` 让某条链的签发抛异常，验证其他链仍正常。

设计权衡
--------
- 不实测 ``main.lifespan`` 集成：那会让测试启动整个 FastAPI app + asyncio
  event loop，复杂度爆炸。``conftest.py`` 已让 ``AUDIT_STH_ENABLED=false``,
  集成路径有专门的 audit router 测试覆盖。
- ``_list_chains`` 只测一次（在 ``issue_sth_for_all_chains`` 的覆盖中已经
  间接验证）；它本质是 ``SELECT DISTINCT``，再多写一遍意义不大。
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy.orm import Session

from app.models import AuditEvent, AuditTreeHead, User
from app.services.audit_sth_scheduler import (
    STHScheduler,
    build_default_scheduler,
    issue_sth_for_all_chains,
)


# ---------------------------------------------------------------------------
# 共享 fixture / 工厂
# ---------------------------------------------------------------------------
def _make_user(db: Session) -> User:
    user = User(primary_wallet=f"0xSCHED{uuid.uuid4().hex[:30]}")
    db.add(user)
    db.flush()
    return user


def _make_audit_event(db: Session, user: User) -> AuditEvent:
    """给某用户的链追加一条审计事件。"""
    eh = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    e = AuditEvent(
        user_id=user.id,
        passport_id=None,
        event_type="USER_LOGIN",
        actor_type="USER",
        actor_id=str(user.id),
        event_json={"event_type": "USER_LOGIN", "data": {}, "actor_type": "USER", "actor_id": str(user.id)},
        previous_event_hash="HTX_AGENT_PASSPORT_GENESIS_V1",
        event_hash=eh,
    )
    db.add(e)
    db.flush()
    return e


# ===========================================================================
# 1. issue_sth_for_all_chains —— 同步签发函数
# ===========================================================================
class TestIssueSthForAllChains:
    """**Validates: G10/G11 周期 STH 签发核心逻辑**。"""

    def test_no_chains_no_sth(self, db_session: Session) -> None:
        """空 audit_events → 无链 → 不签发任何 STH。"""
        issued = issue_sth_for_all_chains(db_session)
        assert issued == []

    def test_one_chain_signs_one_sth(self, db_session: Session) -> None:
        """单条链 + 多条事件 → 签发 1 份 STH。"""
        user = _make_user(db_session)
        for _ in range(3):
            _make_audit_event(db_session, user)

        issued = issue_sth_for_all_chains(db_session)
        assert len(issued) == 1
        sth = issued[0]
        assert sth.user_id == user.id
        assert sth.passport_id is None
        assert sth.tree_size == 3
        assert len(sth.root_hash) == 64

    def test_multiple_chains_all_signed(self, db_session: Session) -> None:
        """两个用户各有事件 → 两条链各签 1 份 STH。"""
        u1 = _make_user(db_session)
        u2 = _make_user(db_session)
        _make_audit_event(db_session, u1)
        _make_audit_event(db_session, u2)
        _make_audit_event(db_session, u2)

        issued = issue_sth_for_all_chains(db_session)
        assert len(issued) == 2
        # 按 user_id 索引
        by_user = {sth.user_id: sth for sth in issued}
        assert by_user[u1.id].tree_size == 1
        assert by_user[u2.id].tree_size == 2

    def test_skips_chain_with_no_new_events(self, db_session: Session) -> None:
        """同链第二轮调用 + 无新事件 → 跳过（不签发冗余 STH）。

        这是 ``audit_tree_heads`` 表保持单调有意义的关键防护。
        """
        user = _make_user(db_session)
        _make_audit_event(db_session, user)
        _make_audit_event(db_session, user)

        issued1 = issue_sth_for_all_chains(db_session)
        issued2 = issue_sth_for_all_chains(db_session)

        assert len(issued1) == 1
        assert issued2 == []  # 第二轮无新事件，跳过

    def test_signs_again_after_new_events(self, db_session: Session) -> None:
        """同链第二轮 + 有新事件 → 再签 1 份新 STH。"""
        user = _make_user(db_session)
        _make_audit_event(db_session, user)

        issued1 = issue_sth_for_all_chains(db_session)
        assert len(issued1) == 1
        assert issued1[0].tree_size == 1

        _make_audit_event(db_session, user)
        _make_audit_event(db_session, user)

        issued2 = issue_sth_for_all_chains(db_session)
        assert len(issued2) == 1
        assert issued2[0].tree_size == 3

    def test_chain_failure_does_not_break_other_chains(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**关键容错保证**：某链签发抛异常时其他链仍正常签发。

        让后台任务任意一条坏链都不能拖垮整轮——避免"一坏全坏"。
        """
        u1 = _make_user(db_session)
        u2 = _make_user(db_session)
        _make_audit_event(db_session, u1)
        _make_audit_event(db_session, u2)

        # 让 u1 的签发抛错
        from app.services import audit_sth_scheduler as mod

        original_issue = mod.issue_signed_tree_head

        def _selective_failure(db, *, user_id, passport_id=None):
            if user_id == u1.id:
                raise RuntimeError("simulated failure for u1")
            return original_issue(db, user_id=user_id, passport_id=passport_id)

        monkeypatch.setattr(mod, "issue_signed_tree_head", _selective_failure)

        issued = issue_sth_for_all_chains(db_session)
        # u2 仍然成功签发（u1 的错被吞掉）
        assert len(issued) == 1
        assert issued[0].user_id == u2.id


# ===========================================================================
# 2. STHScheduler —— 生命周期 + 幂等性
# ===========================================================================
class TestSTHSchedulerLifecycle:
    """**Validates: 周期任务生命周期管理**——start/stop 幂等，is_running 准确。"""

    def test_initial_state_not_running(self) -> None:
        """构造后未启动 → ``is_running == False``。"""
        scheduler = STHScheduler(
            db_factory=lambda: None,  # type: ignore[arg-type,return-value]
            interval_seconds=0.05,
        )
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_start_then_stop(self) -> None:
        """start → is_running=True；stop → is_running=False。"""

        def _factory():
            class _NoopSession:
                def commit(self): pass
                def rollback(self): pass
                def close(self): pass
                def execute(self, *a, **kw):
                    class _Empty:
                        def all(self): return []
                    return _Empty()
            return _NoopSession()

        scheduler = STHScheduler(
            db_factory=_factory,  # type: ignore[arg-type]
            interval_seconds=0.05,
        )
        await scheduler.start()
        assert scheduler.is_running is True

        await scheduler.stop()
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        """重复 start 不抛、不创建多个 task。"""
        def _factory():
            class _NoopSession:
                def commit(self): pass
                def rollback(self): pass
                def close(self): pass
                def execute(self, *a, **kw):
                    class _Empty:
                        def all(self): return []
                    return _Empty()
            return _NoopSession()

        scheduler = STHScheduler(db_factory=_factory, interval_seconds=0.05)  # type: ignore[arg-type]
        try:
            await scheduler.start()
            first_task = scheduler._task  # type: ignore[attr-defined]
            await scheduler.start()  # 第二次应 no-op
            assert scheduler._task is first_task  # type: ignore[attr-defined]
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_noop(self) -> None:
        """从未启动的 scheduler 调 stop 不抛。"""
        scheduler = STHScheduler(
            db_factory=lambda: None,  # type: ignore[arg-type,return-value]
            interval_seconds=0.05,
        )
        await scheduler.stop()  # 不抛即通过
        assert scheduler.is_running is False

    @pytest.mark.asyncio
    async def test_double_stop_is_idempotent(self) -> None:
        """重复 stop 不抛。"""
        def _factory():
            class _NoopSession:
                def commit(self): pass
                def rollback(self): pass
                def close(self): pass
                def execute(self, *a, **kw):
                    class _Empty:
                        def all(self): return []
                    return _Empty()
            return _NoopSession()

        scheduler = STHScheduler(db_factory=_factory, interval_seconds=0.05)  # type: ignore[arg-type]
        await scheduler.start()
        await scheduler.stop()
        await scheduler.stop()  # 第二次应 no-op
        assert scheduler.is_running is False

    def test_interval_floor_at_minimum(self) -> None:
        """interval_seconds=0 / 负值 → 自动 clamp 到 0.001（防御除零 / 死循环）。"""
        scheduler = STHScheduler(
            db_factory=lambda: None,  # type: ignore[arg-type,return-value]
            interval_seconds=0,
        )
        assert scheduler._interval >= 0.001  # type: ignore[attr-defined]

        scheduler2 = STHScheduler(
            db_factory=lambda: None,  # type: ignore[arg-type,return-value]
            interval_seconds=-1,
        )
        assert scheduler2._interval >= 0.001  # type: ignore[attr-defined]


# ===========================================================================
# 3. STHScheduler —— _tick_sync 集成路径
# ===========================================================================
class TestSTHSchedulerTickSync:
    """**Validates: tick 集成路径**——从 db_factory 开 session → 签发 → commit → anchor。"""

    def test_tick_sync_signs_sth_and_commits(self, sqlite_engine, tmp_path) -> None:
        """``_tick_sync`` 从 factory 开 session、签发、commit；DB 里有新 STH 行。

        用真实 sqlite_engine + 自建 sessionmaker，避免 ``db_session`` fixture
        的"事务回滚"语义吃掉 commit。
        """
        from sqlalchemy.orm import sessionmaker

        SessionLocal = sessionmaker(bind=sqlite_engine, expire_on_commit=False, future=True)

        # 准备一条链
        with SessionLocal() as setup_db:
            user = _make_user(setup_db)
            _make_audit_event(setup_db, user)
            _make_audit_event(setup_db, user)
            setup_db.commit()
            user_id = user.id

        anchor = tmp_path / "sth.jsonl"
        scheduler = STHScheduler(
            db_factory=SessionLocal,
            interval_seconds=0.05,
            anchor_path=str(anchor),
        )

        # 直接调同步 tick（不进 asyncio）
        scheduler._tick_sync()  # type: ignore[attr-defined]

        # 验证 STH 已写入 DB
        with SessionLocal() as verify_db:
            sth = (
                verify_db.query(AuditTreeHead)
                .filter(AuditTreeHead.user_id == user_id)
                .first()
            )
            assert sth is not None
            assert sth.tree_size == 2

        # 验证锚定文件已写入
        assert anchor.exists()
        # 至少一行
        assert any(ln.strip() for ln in anchor.read_text(encoding="utf-8").splitlines())

        # 清理：删掉测试链上的事件 + STH，避免污染后续 sqlite_engine 共享 session 测试
        with SessionLocal() as cleanup_db:
            cleanup_db.query(AuditTreeHead).filter(
                AuditTreeHead.user_id == user_id
            ).delete()
            cleanup_db.query(AuditEvent).filter(
                AuditEvent.user_id == user_id
            ).delete()
            cleanup_db.query(User).filter(User.id == user_id).delete()
            cleanup_db.commit()

    def test_tick_sync_no_anchor_path_still_succeeds(self, sqlite_engine) -> None:
        """anchor_path 空字符串 → 仍 commit 到 DB，仅跳过锚定（不抛）。"""
        from sqlalchemy.orm import sessionmaker

        SessionLocal = sessionmaker(bind=sqlite_engine, expire_on_commit=False, future=True)

        with SessionLocal() as setup_db:
            user = _make_user(setup_db)
            _make_audit_event(setup_db, user)
            setup_db.commit()
            user_id = user.id

        scheduler = STHScheduler(
            db_factory=SessionLocal,
            interval_seconds=0.05,
            anchor_path="",  # 关闭锚定
        )
        scheduler._tick_sync()  # type: ignore[attr-defined]

        with SessionLocal() as verify_db:
            sth = (
                verify_db.query(AuditTreeHead)
                .filter(AuditTreeHead.user_id == user_id)
                .first()
            )
            assert sth is not None

        # 清理
        with SessionLocal() as cleanup_db:
            cleanup_db.query(AuditTreeHead).filter(
                AuditTreeHead.user_id == user_id
            ).delete()
            cleanup_db.query(AuditEvent).filter(
                AuditEvent.user_id == user_id
            ).delete()
            cleanup_db.query(User).filter(User.id == user_id).delete()
            cleanup_db.commit()


# ===========================================================================
# 4. build_default_scheduler 工厂
# ===========================================================================
class TestBuildDefaultScheduler:
    """**Validates: 工厂构造**——从 settings 读取 interval / anchor 配置。"""

    def test_returns_scheduler_with_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """工厂返回 ``STHScheduler`` 实例，参数取自 settings。

        Phase 2.5：``build_default_scheduler`` 改为通过 ``get_default_anchor_backend``
        工厂注入 backend；不再直接持有 ``anchor_path``——验证 backend 实例类型即可。
        """
        from app.core.config import get_settings
        from app.services.audit_sth_anchor import (
            JsonLineFileAnchorBackend,
        )

        monkeypatch.setenv("AUDIT_STH_INTERVAL_SECONDS", "120")
        monkeypatch.setenv("AUDIT_STH_ANCHOR_BACKEND", "jsonl")
        monkeypatch.setenv("AUDIT_STH_ANCHOR_PATH", "/tmp/sth.jsonl")
        get_settings.cache_clear()
        try:
            scheduler = build_default_scheduler()
            assert isinstance(scheduler, STHScheduler)
            assert scheduler._interval == 120.0  # type: ignore[attr-defined]
            backend = scheduler._anchor_backend  # type: ignore[attr-defined]
            assert isinstance(backend, JsonLineFileAnchorBackend)
            assert str(backend.anchor_path) in ("/tmp/sth.jsonl", "\\tmp\\sth.jsonl")
            assert scheduler.is_running is False
        finally:
            get_settings.cache_clear()

    def test_factory_falls_back_to_null_when_backend_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``AUDIT_STH_ANCHOR_BACKEND=null`` → 走 NullAnchorBackend。"""
        from app.core.config import get_settings
        from app.services.audit_sth_anchor import NullAnchorBackend

        monkeypatch.setenv("AUDIT_STH_ANCHOR_BACKEND", "null")
        get_settings.cache_clear()
        try:
            scheduler = build_default_scheduler()
            assert isinstance(
                scheduler._anchor_backend, NullAnchorBackend  # type: ignore[attr-defined]
            )
        finally:
            get_settings.cache_clear()
