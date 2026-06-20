"""Action 详情 + 审计路由集成测试（Phase 1 / G10-G11 跟进）。

覆盖 ``/api/actions/{id}`` 与 ``/api/actions/{id}/audit`` 两个端点：

- 返回当前用户拥有的 action（跨用户访问 → 404）
- 审计事件按 ``created_at`` 升序
- 字段形状与前端 ``ActionDetail`` / ``AuditEvent`` 类型对齐
- 无 token → 401
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models import AgentAction, AgentPassport, AuditEvent
from app.models.enums import ActionState, PassportState


def _seed_action_with_events(
    sqlite_engine: Engine,
    user_id: uuid.UUID,
    *,
    event_count: int = 3,
) -> tuple[uuid.UUID, uuid.UUID]:
    """直接构造一个 ``AgentPassport`` + ``AgentAction`` + N 条审计事件,
    返回 ``(action_id, trace_id)``。

    AgentAction 要求非空 ``passport_id``，所以同步创建一个最小护照。
    护照的 ``user_id`` 与 action / events 一致——保证鉴权过滤路径一致。
    """
    SessionLocal = sessionmaker(bind=sqlite_engine, expire_on_commit=False, future=True)
    with SessionLocal() as db:
        passport = AgentPassport(
            user_id=user_id,
            name=f"audit-test-passport-{uuid.uuid4().hex[:8]}",
            agent_type="trader",
            state=PassportState.ACTIVE,
            version=1,
            policy_json={
                "version": "0.1",
                "capabilities": {
                    "read_market": True, "read_account": True,
                    "place_order": True, "cancel_order": True, "withdraw": False,
                },
                "limits": {
                    "allowed_symbols": ["btcusdt"],
                    "max_notional_usdt_per_order": 20,
                    "max_daily_notional_usdt": 100,
                    "max_orders_per_day": 10,
                },
                "approval": {
                    "required_for_trade": True,
                    "required_for_policy_change": True,
                },
                "blocked_actions": [],
            },
            reputation_score=50,
        )
        db.add(passport)
        db.flush()

        action = AgentAction(
            user_id=user_id,
            passport_id=passport.id,
            trace_id=uuid.uuid4(),
            natural_language_request="test action for audit router tests",
            state=ActionState.REQUESTED,
            execution_mode="simulation",
        )
        db.add(action)
        db.flush()

        for _ in range(event_count):
            eh = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            ev = AuditEvent(
                user_id=user_id,
                passport_id=passport.id,
                action_id=action.id,
                trace_id=action.trace_id,
                event_type="ACTION_REQUESTED",
                actor_type="USER",
                actor_id=str(user_id),
                event_json={
                    "event_type": "ACTION_REQUESTED",
                    "actor_type": "USER",
                    "actor_id": str(user_id),
                    "data": {},
                },
                previous_event_hash="HTX_AGENT_PASSPORT_GENESIS_V1",
                event_hash=eh,
            )
            db.add(ev)

        db.commit()
        return action.id, action.trace_id


# ===========================================================================
# 1. GET /api/actions/{id}
# ===========================================================================
class TestGetActionById:
    def test_returns_action_detail(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """返回 action 详情，字段与前端 ``ActionDetail`` 类型对齐。"""
        user_id = uuid.UUID(demo_user["id"])
        action_id, trace_id = _seed_action_with_events(
            sqlite_engine, user_id, event_count=2
        )

        resp = auth_client.get(f"/api/actions/{action_id}")
        assert resp.status_code == 200
        body = resp.json()

        # 关键字段齐全
        assert body["id"] == str(action_id)
        assert body["user_id"] == str(user_id)
        assert body["trace_id"] == str(trace_id)
        assert body["state"] == ActionState.REQUESTED
        assert body["execution_mode"] == "simulation"
        assert "natural_language_request" in body
        assert "reason_codes" in body
        assert "execution_result" in body
        assert body["execution_result"] is None  # 还没执行

    def test_other_users_action_returns_404(
        self,
        client: TestClient,
        sqlite_engine: Engine,
    ) -> None:
        """**鉴权隔离**：访问别人的 action_id → 404。"""
        login_a = client.post(
            "/api/auth/demo-login",
            json={"wallet": "0xACTAAA000000000000000000000000000000AAA"},
        ).json()
        login_b = client.post(
            "/api/auth/demo-login",
            json={"wallet": "0xACTBBB000000000000000000000000000000BBB"},
        ).json()
        user_b_id = uuid.UUID(login_b["user"]["id"])

        b_action_id, _ = _seed_action_with_events(sqlite_engine, user_b_id)

        client.headers.update({"Authorization": f"Bearer {login_a['token']}"})
        resp = client.get(f"/api/actions/{b_action_id}")
        assert resp.status_code == 404

    def test_unknown_action_returns_404(
        self, auth_client: TestClient
    ) -> None:
        fake_id = uuid.uuid4()
        resp = auth_client.get(f"/api/actions/{fake_id}")
        assert resp.status_code == 404

    def test_unauthenticated_returns_401(self, client: TestClient) -> None:
        resp = client.get(f"/api/actions/{uuid.uuid4()}")
        assert resp.status_code == 401


# ===========================================================================
# 2. GET /api/actions/{id}/audit
# ===========================================================================
class TestGetActionAuditEvents:
    def test_returns_events_in_chronological_order(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """审计事件按 created_at 升序返回，前端时间线可直接渲染。"""
        user_id = uuid.UUID(demo_user["id"])
        action_id, trace_id = _seed_action_with_events(
            sqlite_engine, user_id, event_count=4
        )

        resp = auth_client.get(f"/api/actions/{action_id}/audit")
        assert resp.status_code == 200
        body = resp.json()

        assert body["action_id"] == str(action_id)
        assert body["trace_id"] == str(trace_id)
        assert body["count"] == 4
        assert len(body["events"]) == 4

        timestamps = [e["created_at"] for e in body["events"]]
        assert timestamps == sorted(timestamps)

        # 每个事件含全部前端预期字段
        ev = body["events"][0]
        assert {
            "id", "event_type", "actor_type", "actor_id", "event_json",
            "event_hash", "previous_event_hash", "trace_id", "created_at",
        } <= ev.keys()

    def test_other_users_action_audit_returns_404(
        self,
        client: TestClient,
        sqlite_engine: Engine,
    ) -> None:
        """跨用户访问 audit 也是 404。"""
        login_a = client.post(
            "/api/auth/demo-login",
            json={"wallet": "0xAUDAAA000000000000000000000000000000AAA"},
        ).json()
        login_b = client.post(
            "/api/auth/demo-login",
            json={"wallet": "0xAUDBBB000000000000000000000000000000BBB"},
        ).json()
        user_b_id = uuid.UUID(login_b["user"]["id"])

        b_action_id, _ = _seed_action_with_events(sqlite_engine, user_b_id)

        client.headers.update({"Authorization": f"Bearer {login_a['token']}"})
        resp = client.get(f"/api/actions/{b_action_id}/audit")
        assert resp.status_code == 404

    def test_action_with_no_events_returns_empty(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """action 存在但无审计事件 → 200 + 空列表。"""
        user_id = uuid.UUID(demo_user["id"])
        action_id, _ = _seed_action_with_events(
            sqlite_engine, user_id, event_count=0
        )

        resp = auth_client.get(f"/api/actions/{action_id}/audit")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["events"] == []

    def test_unknown_action_returns_404(
        self, auth_client: TestClient
    ) -> None:
        fake_id = uuid.uuid4()
        resp = auth_client.get(f"/api/actions/{fake_id}/audit")
        assert resp.status_code == 404

    def test_audit_unauthenticated_returns_401(
        self, client: TestClient
    ) -> None:
        resp = client.get(f"/api/actions/{uuid.uuid4()}/audit")
        assert resp.status_code == 401
