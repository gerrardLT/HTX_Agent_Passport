"""任务 3 集成测试：``POST /api/auth/demo-login`` + 受保护路由 401 行为。

覆盖 Req 1 AC2、AC4、AC6、AC7：
- demo-login 懒创建用户 + 返回 JWT 与 user 摘要
- 同一钱包多次登录复用 user.id
- 写入 USER_LOGIN 审计事件（含 trace_id）
- 受保护路由：缺失 / 错乱 / 过期 token 一律 401
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.auth import create_access_token
from app.core.config import get_settings
from app.core.dependencies import get_current_user, get_db
from app.core.errors import register_exception_handlers
from app.models import AuditEvent, User
from app.models.enums import AuditEventType


# ---------------------------------------------------------------------------
# /api/auth/demo-login
# ---------------------------------------------------------------------------
def test_demo_login_creates_user_and_returns_token(client: TestClient) -> None:
    """POST 不带 body：使用 settings.DEMO_WALLET，懒创建一行 users 并返回 token。"""
    settings = get_settings()
    resp = client.post("/api/auth/demo-login")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert "token" in body and isinstance(body["token"], str) and body["token"]
    assert body["user"]["wallet"] == settings.DEMO_WALLET
    assert "id" in body["user"]


def test_demo_login_returns_existing_user(client: TestClient, sqlite_engine) -> None:
    """同一钱包二次登录复用 user.id（不重复建账）。"""
    first = client.post("/api/auth/demo-login").json()
    second = client.post("/api/auth/demo-login").json()
    assert first["user"]["id"] == second["user"]["id"]

    # 数据库里应只有 1 行 user
    from sqlalchemy.orm import Session

    with Session(sqlite_engine) as session:
        users = session.execute(select(User)).scalars().all()
        assert len(users) == 1


def test_demo_login_writes_audit_event(client: TestClient, sqlite_engine) -> None:
    """成功登录后 audit_events 表多一条 USER_LOGIN，包含 trace_id 与 wallet。"""
    settings = get_settings()
    resp = client.post("/api/auth/demo-login")
    assert resp.status_code == 200

    from sqlalchemy.orm import Session

    with Session(sqlite_engine) as session:
        events = session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.USER_LOGIN)
        ).scalars().all()
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "USER"
    assert event.event_json["data"]["wallet"] == settings.DEMO_WALLET
    # trace_id 同时落到列与 event_json.data
    assert event.trace_id is not None
    assert event.event_json["data"]["trace_id"] == str(event.trace_id)
    # 首事件 previous_event_hash 应为 GENESIS_HASH
    assert event.previous_event_hash == settings.GENESIS_HASH


def test_demo_login_with_custom_wallet(client: TestClient) -> None:
    """带 wallet 字段时使用自定义钱包；同时不影响默认钱包流程。"""
    custom = "0xCUSTOM000000000000000000000000000000BEEF"
    resp = client.post("/api/auth/demo-login", json={"wallet": custom})
    assert resp.status_code == 200
    assert resp.json()["user"]["wallet"] == custom


# ---------------------------------------------------------------------------
# 受保护路由 401 行为
# ---------------------------------------------------------------------------
def _build_protected_app(sqlite_engine) -> FastAPI:
    """构造一个只挂载 ``/test/me`` 受保护路由的最小 app，专用于 401 测试。"""
    from sqlalchemy.orm import sessionmaker

    from app.core.database import set_engine_for_testing
    from main import create_app

    set_engine_for_testing(sqlite_engine)
    app = create_app()

    SessionLocal = sessionmaker(  # noqa: N806 — SessionLocal 是 SQLAlchemy 习惯命名
        bind=sqlite_engine, autocommit=False, autoflush=False, expire_on_commit=False, future=True
    )

    def _override_get_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db

    @app.get("/test/me")
    def _me(user: Annotated[User, Depends(get_current_user)]):
        return {"id": str(user.id), "wallet": user.primary_wallet}

    register_exception_handlers(app)  # 已在 create_app 中注册，重复调用是幂等的
    return app


def test_protected_route_without_token_returns_401(sqlite_engine) -> None:
    app = _build_protected_app(sqlite_engine)
    with TestClient(app) as c:
        resp = c.get("/test/me")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert "trace_id" in body["error"]


def test_protected_route_with_invalid_token_returns_401(sqlite_engine) -> None:
    app = _build_protected_app(sqlite_engine)
    with TestClient(app) as c:
        resp = c.get(
            "/test/me",
            headers={"Authorization": "Bearer this-is-not-a-real-jwt"},
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_protected_route_with_malformed_authorization_header_returns_401(sqlite_engine) -> None:
    """``Authorization`` 不是 ``Bearer <token>`` 形式也应 401。"""
    app = _build_protected_app(sqlite_engine)
    with TestClient(app) as c:
        resp = c.get("/test/me", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401


def test_protected_route_with_expired_token_returns_401(client: TestClient, sqlite_engine) -> None:
    """先签一个过期 token，再访问受保护路由应返回 401。

    复用 ``client`` fixture 来执行一次成功登录确保用户存在，
    然后用 expires_delta=-1 重新签一个属于该用户的过期 token，
    最后用一个新挂载受保护路由的 app 实例发起请求。
    """
    # Step 1. 登录创建 user 并获取 user_id
    resp = client.post("/api/auth/demo-login")
    assert resp.status_code == 200
    user_id = resp.json()["user"]["id"]
    wallet = resp.json()["user"]["wallet"]

    # Step 2. 直接用核心库签一个已过期 token
    from uuid import UUID

    expired_token = create_access_token(
        user_id=UUID(user_id),
        wallet=wallet,
        expires_delta=timedelta(seconds=-1),
    )

    # Step 3. 用受保护路由 app 发请求
    app = _build_protected_app(sqlite_engine)
    with TestClient(app) as c:
        resp2 = c.get(
            "/test/me",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
    assert resp2.status_code == 401
    assert resp2.json()["error"]["code"] == "UNAUTHORIZED"


def test_protected_route_with_valid_token_returns_200(client: TestClient, sqlite_engine) -> None:
    """正向用例：登录拿到 token 后访问受保护路由应 200，并返回当前用户。"""
    login = client.post("/api/auth/demo-login").json()
    token = login["token"]

    app = _build_protected_app(sqlite_engine)
    with TestClient(app) as c:
        resp = c.get("/test/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["id"] == login["user"]["id"]
    assert resp.json()["wallet"] == login["user"]["wallet"]
