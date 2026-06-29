"""pytest 共享 fixtures。

已实现：
- 任务 2.2  ``sqlite_engine``    —— 内存 SQLite 引擎，验证 ORM metadata 一致性  ✅
- 任务 2.2  ``db_session``       —— SQLAlchemy 测试会话（每用例独立事务回滚）  ✅
- 任务 3    ``client`` / ``api_client`` —— FastAPI TestClient，``get_db`` 注入 SQLite  ✅
- 任务 3    ``settings_overrides`` —— 把 JWT_SECRET 等敏感配置切到测试默认值  ✅

后续任务会在此追加：
- 任务 4.1  ``vault_master_key`` —— 32 字节随机主密钥
- 任务 4.2  ``demo_credential``  —— TRADE_ENABLED 沙箱凭证
- 任务 5.3  ``demo_passport``    —— small_spot_executor 模板护照
- 任务 9    ``mock_market``      —— 种子行情快照（btcusdt=68000, ethusdt=3600）
- 任务 10   ``mock_bai_client``  —— 可控制的 B.AI 适配器

任务 2.2 提供基于 in-memory SQLite 的 ``sqlite_engine`` 与 ``db_session`` fixture，
用于在没有 PostgreSQL 的 CI 环境验证 ORM metadata 与基础 CRUD（schema 完整性）。
真正的 Alembic 迁移测试需要 PostgreSQL，本地运行 ``docker compose up -d postgres``
后再执行 ``alembic upgrade head``。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.engine import Engine

# ---------------------------------------------------------------------------
# 1. SQLite 兼容层：把 PostgreSQL 专用类型映射为 SQLite 等价物
# ---------------------------------------------------------------------------
# SQLite 不支持 UUID / JSONB / ARRAY / BYTEA 原生类型；为了在 CI 跑通
# ``Base.metadata.create_all(engine)``，我们用 SQLAlchemy 的 ``with_variant``
# 思路（或直接对 dialect 注册 type adapter）让 metadata 在 SQLite 下回退到通用类型。
#
# 这里采用最小侵入的做法：在 fixture 创建 SQLite 引擎前，
# 给 PG 专用类型注册一个 ``compile`` 函数，让它们在 SQLite 方言下输出兼容 SQL。
# 仅在测试环境生效，不影响生产 PG 迁移。
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker


@compiles(UUID, "sqlite")
def _uuid_to_char(element, compiler, **kw):  # type: ignore[no-untyped-def]
    """SQLite 没有 UUID 列；用 CHAR(36) 存储字符串形式。"""
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def _jsonb_to_json(element, compiler, **kw):  # type: ignore[no-untyped-def]
    """SQLite 没有 JSONB；用普通 JSON（SQLite 3.9+ 支持 JSON 函数）。"""
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_to_json(element, compiler, **kw):  # type: ignore[no-untyped-def]
    """SQLite 没有 ARRAY；用 JSON 文本存储数组（应用层负责序列化）。"""
    return "JSON"


# ---------------------------------------------------------------------------
# 1b. SQLite ARRAY 绑定 / 取回处理器（仅测试环境）
# ---------------------------------------------------------------------------
# 仅 ``@compiles`` 改 DDL 还不够：``ARRAY(Text)`` 列（如 ``agent_actions.reason_codes``）
# 在 SQLite 下绑定一个 Python ``list`` 会抛
# ``sqlite3.ProgrammingError: type 'list' is not supported``——因为 PG ARRAY 的
# ``bind_processor`` 在非 PG 方言下返回 None（不做任何序列化），原始 list 直接
# 被丢给 sqlite3 驱动。
#
# 这里给 PG ``ARRAY`` 类型挂一层 **dialect-gated** 的 bind / result 处理器：
# - bind（写入）：dialect=sqlite 时把 ``list`` 序列化为 JSON 文本（与 ``@compiles``
#   的 ``JSON`` 列类型一致）；其它方言（生产 PG）走原生逻辑，不受影响。
# - result（读取）：dialect=sqlite 时把 JSON 文本反序列化回 ``list``；
#   None / 已是 list 的值原样返回。
#
# 这是**测试专用兼容层**——只在 SQLite 方言下生效，生产 PostgreSQL 路径
# （``dialect.name == "postgresql"``）完全走 SQLAlchemy 原生 ARRAY 处理器。
# 放在 conftest 顶部、``create_all`` 之前 patch 一次即可（幂等防御）。
if not getattr(ARRAY, "_htx_sqlite_shim_installed", False):
    import json as _json

    _orig_array_bind_processor = ARRAY.bind_processor
    _orig_array_result_processor = ARRAY.result_processor

    def _sqlite_array_bind_processor(self, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "sqlite":

            def process(value):  # type: ignore[no-untyped-def]
                if value is None:
                    return None
                return _json.dumps(list(value))

            return process
        return _orig_array_bind_processor(self, dialect)

    def _sqlite_array_result_processor(self, dialect, coltype):  # type: ignore[no-untyped-def]
        if dialect.name == "sqlite":

            def process(value):  # type: ignore[no-untyped-def]
                if value is None:
                    return None
                if isinstance(value, bytes | bytearray):
                    value = value.decode("utf-8")
                if isinstance(value, str):
                    return _json.loads(value)
                return value

            return process
        return _orig_array_result_processor(self, dialect, coltype)

    ARRAY.bind_processor = _sqlite_array_bind_processor  # type: ignore[method-assign]
    ARRAY.result_processor = _sqlite_array_result_processor  # type: ignore[method-assign]
    ARRAY._htx_sqlite_shim_installed = True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 2. SQLite 引擎与会话 fixtures
# ---------------------------------------------------------------------------
#: SQLite DDL 不兼容 PostgreSQL 的 server_default 函数调用表达式。
#: 在 ``create_all`` 之前临时替换元数据中不兼容的 server_default，
#: 测试结束后恢复原值（不影响生产 PostgreSQL 路径）。

from sqlalchemy.schema import ColumnDefault as _ColumnDefault


def _patch_metadata_for_sqlite(metadata):
    """遍历所有表，替换 PG 专用 server_default 为 SQLite 兼容版本。

    返回 ``list[tuple[Column, original_server_default, original_default]]`` 用于 teardown 恢复。
    """
    originals: list = []
    for table in metadata.tables.values():
        for col in table.columns:
            sd = col.server_default
            if sd is None:
                continue
            # 获取 DDL 文本
            try:
                sd_text = str(sd.arg)
            except Exception:
                continue
            if "gen_random_uuid" in sd_text.lower():
                originals.append((col, sd, col.default))
                col.server_default = None
                # 为 SQLite 添加 Python 侧 UUID 生成（替代 PG 的 server_default）
                # hex 格式（无连字符）与 PG UUID(as_uuid=True) 的 bind 格式一致
                col.default = _ColumnDefault(lambda ctx: uuid.uuid4())
            elif "now()" in sd_text.lower():
                originals.append((col, sd, col.default))
                col.server_default = None
                # SQLite 不支持 DEFAULT now()，改用 CURRENT_TIMESTAMP
                # 但 TextClause 会导致 mapper 布尔检查报错，所以用 SQL 文本
                col.default = _ColumnDefault(
                    lambda ctx: datetime.now(UTC)
                )
    return originals


def _restore_metadata(originals: list) -> None:
    """恢复被 patch 的 server_default 原值。"""
    for col, original_sd, original_default in originals:
        col.server_default = original_sd
        col.default = original_default


@pytest.fixture(scope="session")
def sqlite_engine() -> Iterator[Engine]:
    """会话级共享 in-memory SQLite 引擎。

    使用 ``shared cache`` URI 让同一进程多 fixture 看到相同 schema，
    避免每次连接都得重新 ``create_all``。
    """
    # ``check_same_thread=False`` 允许 pytest 的多线程收集与执行；
    # ``StaticPool`` 让多次 connect() 返回同一物理连接（in-memory DB 持久化）。
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    # ---- SQLite 兼容层 ----
    # 1) 注册 UDF（gen_random_uuid / now）以备某些查询场景需要。
    # 2) 在 create_all 之前 patch 元数据，移除 PG 专用的 server_default。
    #    测试中 ORM 对象都显式指定 id，所以去掉 server_default 不影响测试逻辑。
    @event.listens_for(engine, "connect")
    def _register_sqlite_funcs(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        try:
            dbapi_connection.create_function(
                "gen_random_uuid", 0, lambda: uuid.uuid4().hex
            )
            dbapi_connection.create_function(
                "now", 0, lambda: datetime.now(UTC).isoformat()
            )
        except Exception:
            pass

    # 延迟到此处导入：让 compiles 装饰器先注册，create_all 才能正确翻译。
    from app.models import Base

    # Patch 元数据：移除 PG 专用 server_default，测试结束后恢复
    originals = _patch_metadata_for_sqlite(Base.metadata)
    Base.metadata.create_all(engine)
    # 注意：不在 create_all 后立即恢复！测试期间保持 patched 状态，
    # 这样 INSERT 时 Python 侧的 UUID default 才能生效。
    try:
        yield engine
    finally:
        _restore_metadata(originals)
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def db_session(sqlite_engine: Engine) -> Iterator[Session]:
    """函数级会话；每个用例在独立事务中运行，结束后回滚保持隔离。"""
    connection = sqlite_engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# 3. 任务 3：FastAPI TestClient + 配置覆盖
# ---------------------------------------------------------------------------
# 思路：
# - 用 ``monkeypatch`` 在 session 级别把 ``JWT_SECRET`` 等敏感配置固定到测试值，
#   避免依赖开发者本地 ``.env``。
# - 用 ``client`` fixture 构造 FastAPI ``TestClient``，把 ``get_db`` 依赖
#   重定向到本进程内存 SQLite，让认证流程在零外部依赖下跑通。


@pytest.fixture(scope="session", autouse=True)
def _settings_overrides() -> Iterator[None]:
    """会话级配置覆盖：把 JWT_SECRET / DEMO_WALLET 等固定到测试常量。

    autouse=True 让所有用例自动生效；测试中拿到的 ``get_settings()``
    永远是这套确定值，不会被开发者本地 ``.env`` 干扰。
    """
    import os

    from app.core.config import get_settings

    overrides = {
        "JWT_SECRET": "unit-test-jwt-secret-DO-NOT-USE-IN-PROD",
        "JWT_ALGORITHM": "HS256",
        "JWT_EXPIRE_HOURS": "24",
        "DEMO_WALLET": "0xA11CE00000000000000000000000000000000001",
        # 测试用 SQLite，但 ``DATABASE_URL`` 仅作为占位（真正绑定通过
        # set_engine_for_testing 直接换 engine，不会用到这个串）。
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "GENESIS_HASH": "HTX_AGENT_PASSPORT_GENESIS_V1",
        "DEMO_MODE": "true",
        "DEMO_REAL_TRADE": "false",
        "DEMO_DISABLE_EXECUTION": "false",
        # 任务 4.1+：固定 32 字节 hex 主密钥，让 CredentialVault 在测试中可直接构造。
        # 全 0x42 模式便于失败时一眼识别。
        "VAULT_MASTER_KEY": "42" * 32,
        # Phase 1 G10/G11 跟进：默认禁用 STH 周期任务,避免 TestClient lifespan 起后台 task。
        # 单独的 scheduler 测试用例会自行注入 enabled=True 的 scheduler 实例。
        "AUDIT_STH_ENABLED": "false",
        "AUDIT_STH_ANCHOR_PATH": "",
    }
    saved: dict[str, str | None] = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        os.environ[k] = v
    get_settings.cache_clear()
    try:
        yield
    finally:
        for k, original in saved.items():
            if original is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = original
        get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_module_caches() -> Iterator[None]:
    """每个测试前清空模块级缓存（LLM 响应缓存 / 行情快照缓存）。

    这些缓存在生产环境提供 TTL 去重，但测试间共享会导致:
    - planner 测试中相同 prompt_hash 命中缓存，跳过 B.AI stub
    - execution_gateway 测试中行情快照被复用，stale 检测失效
    """
    # 清空 LLM 响应缓存
    from app.services.planner import _LLM_RESPONSE_CACHE
    _LLM_RESPONSE_CACHE.clear()

    # 清空行情快照缓存
    import app.services.execution_gateway as eg
    eg._MARKET_SNAPSHOT_CACHE = None
    eg._MARKET_SNAPSHOT_CACHED_AT = 0.0

    yield

    _LLM_RESPONSE_CACHE.clear()
    eg._MARKET_SNAPSHOT_CACHE = None
    eg._MARKET_SNAPSHOT_CACHED_AT = 0.0


@pytest.fixture()
def client(sqlite_engine: Engine) -> Iterator[TestClient]:
    """FastAPI TestClient，``get_db`` 被重定向到 in-memory SQLite。

    每个用例：
    1. 用 ``set_engine_for_testing(sqlite_engine)`` 让应用使用共享 SQLite。
    2. 通过 ``app.dependency_overrides`` 把 ``get_db`` 改为 yield 一个 SQLite session。
    3. 用例开始前清空所有表，保证测试隔离（``sqlite_engine`` 是 session 级共享）。
    4. 用例结束后清空所有表 + 清理 dependency overrides 与 engine 状态，
       避免 ``client`` 提交的行污染后续使用 ``db_session`` 的用例。
    """
    from fastapi.testclient import TestClient

    from app.core.database import (
        reset_engine_for_testing,
        set_engine_for_testing,
    )
    from app.core.dependencies import get_db
    from app.models import Base
    from main import create_app

    set_engine_for_testing(sqlite_engine)

    def _truncate_all() -> None:
        """清空所有表（保留 schema）。"""
        with sqlite_engine.begin() as conn:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())

    _truncate_all()

    app = create_app()

    SessionLocal = sessionmaker(
        bind=sqlite_engine, autocommit=False, autoflush=False, expire_on_commit=False, future=True
    )

    def _override_get_db() -> Iterator[Session]:
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        reset_engine_for_testing()
        # 关键：用例结束后再次清空表，避免 client 提交的行污染后续 db_session 用例
        _truncate_all()



# ---------------------------------------------------------------------------
# 4. 任务 4.2：演示用户 + 已注入 token 的 TestClient
# ---------------------------------------------------------------------------
# 集成测试常常需要"已登录用户"，让每个用例自己手写一遍 demo-login + 抽 token
# 既冗长又容易掩盖业务断言。这里抽出 ``demo_user`` / ``auth_client`` fixture
# 让凭证 / 护照 / Action 等路由测试直接拿"持 token 的 client"开干。


@pytest.fixture()
def demo_user(client) -> dict[str, str]:
    """演示已登录用户：执行一次 ``POST /api/auth/demo-login`` 拿到 token + user 信息。

    复用同一个 ``client`` fixture（已绑定 SQLite + 注入 get_db），
    保证后续受保护路由请求看到的就是这个 user。

    Returns
    -------
    dict[str, str]
        ``{"id": <user uuid>, "wallet": <wallet>, "token": <jwt>}``。
    """
    resp = client.post("/api/auth/demo-login")
    assert resp.status_code == 200, f"demo-login failed: {resp.text}"
    body = resp.json()
    return {
        "id": body["user"]["id"],
        "wallet": body["user"]["wallet"],
        "token": body["token"],
    }


@pytest.fixture()
def auth_client(client, demo_user):
    """已携带 ``Authorization: Bearer <token>`` 头的 TestClient。

    用法::

        def test_create_credential(auth_client):
            resp = auth_client.post("/api/credentials/htx", json={...})

    内部用 ``client.headers.update`` 把 token 钉死到客户端默认 headers，
    省去每个请求都手写 ``headers=...`` 的麻烦。
    """
    client.headers.update({"Authorization": f"Bearer {demo_user['token']}"})
    return client


# ---------------------------------------------------------------------------
# 5. 任务 5.3：demo_credential / demo_passport fixtures
# ---------------------------------------------------------------------------
# Passport 端点测试经常需要"已存在的 TRADE_ENABLED 凭证"和"已激活的 Passport"
# 作为前置；为避免每个测试自己重复创建，这里抽出共享 fixture。
# 任务 8 / 11 / 13（Action / Approval / Execution Gateway 测试）也可复用。


@pytest.fixture()
def demo_credential(auth_client) -> dict[str, str]:
    """演示凭证：CREATE → VALIDATE 后返回 ``{id, state}``，state=TRADE_ENABLED。

    复用 ``auth_client`` fixture 自动携带 token；走完整的 HTTP 路径（而非
    直接构造 ORM 行）能保证审计事件链完整、状态机走通——方便后续测试断言
    审计事件序列时对齐真实生产路径。
    """
    create_resp = auth_client.post(
        "/api/credentials/htx",
        json={
            "label": "demo-fixture-credential",
            "access_key": "FIXTURE-AK",
            "secret_key": "FIXTURE-SK",
        },
    )
    assert create_resp.status_code == 201, f"create failed: {create_resp.text}"
    cred_id = create_resp.json()["id"]

    validate_resp = auth_client.post(f"/api/credentials/{cred_id}/validate")
    assert validate_resp.status_code == 200, (
        f"validate failed: {validate_resp.text}"
    )
    body = validate_resp.json()
    assert body["state"] == "TRADE_ENABLED", (
        f"expected TRADE_ENABLED, got {body['state']!r}"
    )
    return {"id": cred_id, "state": body["state"]}


@pytest.fixture()
def demo_passport(auth_client, demo_credential) -> dict:
    """演示 Passport：基于 ``demo_credential`` + ``small_spot_executor`` 模板。

    返回值
    ------
    完整的 ``PassportResponse`` body（含 id / state / version / policy 等）。
    state=ACTIVE（因为关联了已验证凭证），version=1。

    基于 PRD §17 demo seed 选择 ``small_spot_executor`` 模板，与任务 19
    的 happy path / over_limit 场景一致——后续 Action / Approval / E2E
    测试可直接复用本 fixture 拿到「最贴近 demo 主线」的 Passport。
    """
    resp = auth_client.post(
        "/api/passports",
        json={
            "name": "demo-fixture-passport",
            "agent_type": "trader",
            "api_credential_id": demo_credential["id"],
            "template_name": "small_spot_executor",
        },
    )
    assert resp.status_code == 201, f"create passport failed: {resp.text}"
    body = resp.json()
    assert body["state"] == "ACTIVE"
    assert body["version"] == 1
    return body
