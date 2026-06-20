"""SQLAlchemy 引擎与 Session 工厂。

设计要点
--------
1. **延迟创建**：``_engine`` / ``_SessionLocal`` 在首次调用 ``get_engine()`` /
   ``get_sessionmaker()`` 时才创建，避免导入期就连数据库（不利于测试与脚本场景）。
2. **测试注入**：测试可通过 ``set_engine_for_testing()`` 临时替换底层 engine
   （任务 3 集成测试就用这个把 PostgreSQL 切到 in-memory SQLite）。
3. **同步会话**：本任务先实现 sync sessionmaker；后续任务若引入 async 会另起
   ``async_engine`` / ``AsyncSession``，不与本文件冲突。
4. **SQLite 兼容层**：当 ``DATABASE_URL`` 使用 ``sqlite`` 方言时，自动注册
   PostgreSQL 专有类型（UUID / JSONB / ARRAY）的 SQLite 编译器 + 绑定/结果处理器，
   并 patch 元数据中的 PG 专用 ``server_default``。本地开发无需安装 PostgreSQL。

对应：design.md「Data Models / API 设计」、Req 1（认证服务依赖数据库）。
"""

from __future__ import annotations

import json as _json
import uuid as _uuid
from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import ColumnDefault as _ColumnDefault
from sqlalchemy.sql import func as _sa_func

from app.core.config import get_settings

# ---- 模块级状态 ----
_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None
_sqlite_compat_installed: bool = False


# ---------------------------------------------------------------------------
# SQLite 兼容层：让 PostgreSQL 专有类型在 SQLite 方言下工作
# ---------------------------------------------------------------------------
def _install_sqlite_compat() -> None:
    """注册 UUID / JSONB / ARRAY 的 SQLite DDL 编译器 + ARRAY 绑定/结果处理器。

    幂等：多次调用只注册一次。仅在 dialect=sqlite 时生效，不影响 PostgreSQL。
    """
    global _sqlite_compat_installed
    if _sqlite_compat_installed:
        return

    @compiles(UUID, "sqlite")
    def _uuid_to_char(element, compiler, **kw):  # type: ignore[no-untyped-def]
        return "CHAR(36)"

    @compiles(JSONB, "sqlite")
    def _jsonb_to_json(element, compiler, **kw):  # type: ignore[no-untyped-def]
        return "JSON"

    @compiles(ARRAY, "sqlite")
    def _array_to_json(element, compiler, **kw):  # type: ignore[no-untyped-def]
        return "JSON"

    if not getattr(ARRAY, "_htx_sqlite_shim_installed", False):
        _orig_bind = ARRAY.bind_processor
        _orig_result = ARRAY.result_processor

        def _array_bind(self, dialect):  # type: ignore[no-untyped-def]
            if dialect.name == "sqlite":
                def process(value):  # type: ignore[no-untyped-def]
                    if value is None:
                        return None
                    return _json.dumps(list(value))
                return process
            return _orig_bind(self, dialect)

        def _array_result(self, dialect, coltype):  # type: ignore[no-untyped-def]
            if dialect.name == "sqlite":
                def process(value):  # type: ignore[no-untyped-def]
                    if value is None:
                        return None
                    if isinstance(value, (bytes, bytearray)):
                        value = value.decode("utf-8")
                    if isinstance(value, str):
                        return _json.loads(value)
                    return value
                return process
            return _orig_result(self, dialect, coltype)

        ARRAY.bind_processor = _array_bind  # type: ignore[method-assign]
        ARRAY.result_processor = _array_result  # type: ignore[method-assign]
        ARRAY._htx_sqlite_shim_installed = True  # type: ignore[attr-defined]

    _sqlite_compat_installed = True


def _patch_metadata_for_sqlite(metadata):
    """遍历所有表，替换 PG 专用 server_default 为 SQLite 兼容版本。

    返回 ``list[tuple[Column, original_server_default, original_default]]`` 用于 teardown 恢复。
    """
    originals = []
    for table in metadata.tables.values():
        for col in table.columns:
            sd = col.server_default
            if sd is None:
                continue
            try:
                sd_text = str(sd.arg)
            except Exception:
                continue
            if "gen_random_uuid" in sd_text.lower():
                originals.append((col, sd, col.default))
                col.server_default = None
                col.default = _ColumnDefault(lambda ctx: _uuid.uuid4())
            elif "now()" in sd_text.lower():
                originals.append((col, sd, col.default))
                col.server_default = None
                col.default = _ColumnDefault(lambda ctx: datetime.now(UTC))
    return originals


def _is_sqlite(url: str) -> bool:
    """判断 DATABASE_URL 是否使用 SQLite 方言。"""
    return "sqlite" in url.split("://")[0].lower()


# ---- SQLite 元数据 patch 状态 ----
_sqlite_originals: list | None = None


def _build_engine() -> Engine:
    """根据当前 ``Settings.DATABASE_URL`` 构造同步 engine。

    SQLite 方言时自动安装兼容层并建表。
    """
    global _sqlite_originals
    settings = get_settings()
    url = settings.DATABASE_URL

    if _is_sqlite(url):
        _install_sqlite_compat()
        engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False},
        )
        # SQLite: 自动建表
        from app.models import Base
        _sqlite_originals = _patch_metadata_for_sqlite(Base.metadata)
        Base.metadata.create_all(engine)
    else:
        engine = create_engine(
            url,
            future=True,
            pool_pre_ping=True,
        )
    return engine


def get_engine() -> Engine:
    """惰性返回当前进程使用的 engine。"""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    """惰性返回 sessionmaker。

    保留 ``expire_on_commit=False`` 让 ORM 对象在 commit 后仍可读取属性，
    简化路由层「commit → 立刻把对象字段返回到响应」的写法。
    """
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )
    return _SessionLocal


def get_db_session() -> Iterator[Session]:
    """FastAPI 依赖：每次请求一个 Session，请求结束自动 close。

    注意：本函数自身不做 ``commit``/``rollback``，由路由 / 服务层显式控制
    事务边界，便于把多步业务放进同一事务。
    """
    SessionLocal = get_sessionmaker()  # noqa: N806 — SessionLocal 是 SQLAlchemy 习惯命名
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def set_engine_for_testing(engine: Engine) -> None:
    """测试钩子：将模块级 ``_engine`` / ``_SessionLocal`` 替换为外部 engine。

    典型用法（``conftest.py``）::

        from app.core.database import set_engine_for_testing, reset_engine_for_testing

        @pytest.fixture(autouse=True)
        def _bind_sqlite(sqlite_engine):
            set_engine_for_testing(sqlite_engine)
            yield
            reset_engine_for_testing()
    """
    global _engine, _SessionLocal
    _engine = engine
    _SessionLocal = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


def reset_engine_for_testing() -> None:
    """清空模块级 engine / sessionmaker（测试 teardown 用）。"""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None


__all__ = [
    "get_db_session",
    "get_engine",
    "get_sessionmaker",
    "reset_engine_for_testing",
    "set_engine_for_testing",
]
