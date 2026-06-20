"""审计 / STH / Inclusion Proof 路由集成测试（Phase 1 / G10-G11 跟进）。

通过 FastAPI ``TestClient`` 走完整 HTTP 路径，覆盖 ``/api/audit/*`` 的 5 个端点：

- ``GET /api/audit/events``                              事件列表（含强制 user_id 鉴权隔离）
- ``GET /api/audit/sth/latest``                          当前最新 STH（404/200）
- ``POST /api/audit/sth/issue``                          手动签发 + 锚定（201）
- ``GET /api/audit/events/{event_id}/inclusion``         inclusion proof + 客户端可独立验证
- ``GET /api/audit/sth/consistency``                     consistency proof（参数校验 / 正向）

关键不变量
----------
1. **鉴权隔离**：所有端点强制 ``user_id = current_user.id``——跨用户访问统一
   404 / 空集，避免存在性侧信道。
2. **inclusion proof 客户端可独立验证**：返回的 ``leaf_hash + index + proof +
   tree_size + root_hash`` 用 :func:`app.core.merkle.verify_inclusion_proof`
   验证应立即通过（端到端密码学正确性的最强证据）。
3. **POST /sth/issue 同时落 DB + 锚定**：tmp_path 注入 ``AUDIT_STH_ANCHOR_PATH``,
   验证 JSONL 行被追加。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.merkle import verify_inclusion_proof
from app.models import AuditEvent, AuditTreeHead


# ---------------------------------------------------------------------------
# 共享工厂：直接往 DB 塞审计事件（绕开整套业务流程，专注测路由）
# ---------------------------------------------------------------------------
def _seed_audit_events(
    sqlite_engine: Engine,
    user_id: uuid.UUID,
    *,
    count: int,
    passport_id: uuid.UUID | None = None,
    action_id: uuid.UUID | None = None,
) -> list[AuditEvent]:
    """直接给某用户插入 ``count`` 条审计事件，返回 ORM 列表。

    用同一个 ``sqlite_engine`` + 单独 sessionmaker——绕过 ``client`` fixture
    的依赖注入路径（那条路径通过 dependency_overrides 给 HTTP 请求用），
    避免与 HTTP 请求事务串扰。

    每条事件的 ``event_hash`` 用唯一随机值；这够 Merkle 树测试用——审计
    路由不验证 prev_hash 链完整性（那是 audit_writer 的职责，已在其他测试
    覆盖）。
    """
    SessionLocal = sessionmaker(bind=sqlite_engine, expire_on_commit=False, future=True)
    events: list[AuditEvent] = []
    with SessionLocal() as db:
        for _ in range(count):
            eh = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            e = AuditEvent(
                user_id=user_id,
                passport_id=passport_id,
                action_id=action_id,
                event_type="USER_LOGIN",
                actor_type="USER",
                actor_id=str(user_id),
                event_json={
                    "event_type": "USER_LOGIN",
                    "actor_type": "USER",
                    "actor_id": str(user_id),
                    "data": {},
                },
                previous_event_hash="HTX_AGENT_PASSPORT_GENESIS_V1",
                event_hash=eh,
            )
            db.add(e)
            events.append(e)
        db.commit()
        # 重新加载，避免 detached
        for e in events:
            db.refresh(e)
    return events


# ===========================================================================
# 1. GET /api/audit/events —— 事件列表
# ===========================================================================
class TestListAuditEvents:
    """**Validates: Req 11 + G10/G11 鉴权隔离**。"""

    def test_no_filter_returns_400(self, auth_client: TestClient) -> None:
        """未提供任何过滤参数 → 400 BAD_REQUEST（避免未限定查询拉全表）。"""
        resp = auth_client.get("/api/audit/events")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["code"] == "BAD_REQUEST"

    def test_filter_by_action_id_returns_user_events(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """传 action_id → 返回该 action 下属于当前用户的事件。"""
        user_id = uuid.UUID(demo_user["id"])
        action_id = uuid.uuid4()
        _seed_audit_events(sqlite_engine, user_id, count=3, action_id=action_id)

        resp = auth_client.get(f"/api/audit/events?action_id={action_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 3
        assert len(body["events"]) == 3
        # 字段齐全（与前端 AuditEvent 类型对齐）
        ev = body["events"][0]
        assert {"id", "event_type", "actor_type", "actor_id", "event_json",
                "event_hash", "previous_event_hash", "trace_id", "created_at"} <= ev.keys()

    def test_filter_by_user_id_returns_only_self_events(
        self,
        client: TestClient,
        sqlite_engine: Engine,
    ) -> None:
        """**关键安全测试**：传他人 user_id → 实际只查自己的事件（强制鉴权隔离）。

        每次 demo-login 自身会写一条 USER_LOGIN 审计事件，所以 A/B 各登录后
        各自的链上已经有 1 条事件；再塞 2 条 → 各 3 条。本测试核心断言是
        "A 看不到 B 的任何 event_id"，而非精确数字。
        """
        # 1. 用户 A 登录
        login_a = client.post("/api/auth/demo-login", json={"wallet": "0xAAAA0000000000000000000000000000000000AA"}).json()
        user_a_id = uuid.UUID(login_a["user"]["id"])
        # 2. 用户 B 登录（独立钱包）
        login_b = client.post("/api/auth/demo-login", json={"wallet": "0xBBBB0000000000000000000000000000000000BB"}).json()
        user_b_id = uuid.UUID(login_b["user"]["id"])

        # 3. 给 A 和 B 各塞 2 条事件
        _seed_audit_events(sqlite_engine, user_a_id, count=2)
        b_events = _seed_audit_events(sqlite_engine, user_b_id, count=2)
        b_event_ids = {str(e.id) for e in b_events}

        # 4. 用 A 的 token 查询 B 的 user_id → 只返回 A 自己的事件
        client.headers.update({"Authorization": f"Bearer {login_a['token']}"})
        resp = client.get(f"/api/audit/events?user_id={user_b_id}")
        assert resp.status_code == 200
        body = resp.json()
        # A 的链至少有自己的 2 条 + USER_LOGIN
        assert body["count"] >= 2
        # **核心安全断言**：B 的任何 event_id 都不应出现在 A 的结果里
        returned_ids = {ev["id"] for ev in body["events"]}
        assert returned_ids.isdisjoint(b_event_ids), (
            "鉴权隔离失败：A 的查询结果包含了 B 的事件"
        )

    def test_results_ordered_by_created_at_asc(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """事件按 created_at 升序——前端时间线可直接渲染。"""
        user_id = uuid.UUID(demo_user["id"])
        action_id = uuid.uuid4()
        _seed_audit_events(sqlite_engine, user_id, count=4, action_id=action_id)

        resp = auth_client.get(f"/api/audit/events?action_id={action_id}")
        events = resp.json()["events"]
        timestamps = [e["created_at"] for e in events]
        assert timestamps == sorted(timestamps)

    def test_unauthenticated_returns_401(self, client: TestClient) -> None:
        resp = client.get("/api/audit/events?action_id=00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 401

    def test_limit_caps_results(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """limit 参数限制返回数。"""
        user_id = uuid.UUID(demo_user["id"])
        action_id = uuid.uuid4()
        _seed_audit_events(sqlite_engine, user_id, count=5, action_id=action_id)

        resp = auth_client.get(f"/api/audit/events?action_id={action_id}&limit=2")
        assert resp.status_code == 200
        assert resp.json()["count"] == 2


# ===========================================================================
# 2. GET /api/audit/sth/latest —— 最新 STH
# ===========================================================================
class TestGetLatestSth:
    """**Validates: G10/G11**——客户端可拉到当前的链承诺。"""

    def test_no_sth_returns_404(self, auth_client: TestClient) -> None:
        """无任何 STH → 404 NOT_FOUND。"""
        resp = auth_client.get("/api/audit/sth/latest")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_returns_latest_after_issue(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """先 POST /sth/issue 签发 → GET /sth/latest 能拿到。

        注：demo_user fixture 走 demo-login，自身会写一条 USER_LOGIN，所以
        当前链上已有 1 条；再塞 3 条 → 共 4 条。
        """
        user_id = uuid.UUID(demo_user["id"])
        _seed_audit_events(sqlite_engine, user_id, count=3)

        # 触发签发
        issue_resp = auth_client.post("/api/audit/sth/issue")
        assert issue_resp.status_code == 201
        issued = issue_resp.json()

        latest_resp = auth_client.get("/api/audit/sth/latest")
        assert latest_resp.status_code == 200
        latest = latest_resp.json()
        assert latest["root_hash"] == issued["root_hash"]
        assert latest["tree_size"] == issued["tree_size"]
        # 至少包含我们塞进去的 3 条
        assert latest["tree_size"] >= 3

    def test_only_returns_self_sth(
        self,
        client: TestClient,
        sqlite_engine: Engine,
    ) -> None:
        """**鉴权隔离**：用户 B 签发 STH 后，用户 A 看不到（404）。"""
        # 用户 A
        login_a = client.post("/api/auth/demo-login", json={"wallet": "0xAAA10000000000000000000000000000000000AA"}).json()
        # 用户 B
        login_b = client.post("/api/auth/demo-login", json={"wallet": "0xBBB10000000000000000000000000000000000BB"}).json()
        user_b_id = uuid.UUID(login_b["user"]["id"])

        _seed_audit_events(sqlite_engine, user_b_id, count=2)

        # B 签发 STH
        client.headers.update({"Authorization": f"Bearer {login_b['token']}"})
        issue_resp = client.post("/api/audit/sth/issue")
        assert issue_resp.status_code == 201

        # A 查询 → 自己没有 STH，返回 404
        client.headers.update({"Authorization": f"Bearer {login_a['token']}"})
        latest_resp = client.get("/api/audit/sth/latest")
        assert latest_resp.status_code == 404


# ===========================================================================
# 3. POST /api/audit/sth/issue —— 手动签发 + 锚定
# ===========================================================================
class TestIssueSthEndpoint:
    """**Validates: 周期签发 + 外部锚定的端点入口**。"""

    def test_issue_creates_sth_in_db(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """POST /sth/issue → DB 多一行 audit_tree_heads。

        注：demo_user fixture 已写一条 USER_LOGIN；再塞 2 条 → 共 3 条。
        """
        user_id = uuid.UUID(demo_user["id"])
        _seed_audit_events(sqlite_engine, user_id, count=2)

        resp = auth_client.post("/api/audit/sth/issue")
        assert resp.status_code == 201
        body = resp.json()
        # 至少 2 条（我们塞的）+ 1 条 USER_LOGIN
        assert body["tree_size"] >= 2
        assert len(body["root_hash"]) == 64
        # Phase 2: 签名带算法前缀（默认 hmac: / 生产可切换 ed25519:）
        assert body["signature"].startswith(("hmac:", "ed25519:"))
        assert len(body["signature"]) >= 64
        assert body["user_id"] == str(user_id)

        # DB 验证
        SessionLocal = sessionmaker(bind=sqlite_engine, expire_on_commit=False, future=True)
        with SessionLocal() as db:
            count = db.query(AuditTreeHead).filter(
                AuditTreeHead.user_id == user_id
            ).count()
            assert count == 1

    def test_issue_writes_to_anchor_file_when_configured(
        self,
        client: TestClient,
        sqlite_engine: Engine,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``AUDIT_STH_ANCHOR_PATH`` 配置后 → JSONL 文件被追加。

        关键：``settings.AUDIT_STH_ANCHOR_PATH`` 通过 env 注入，路由实时读取
        （``get_settings()`` 是 lru_cached——需要 ``cache_clear()``）。
        """
        from app.core.config import get_settings

        anchor_path = tmp_path / "sth.jsonl"
        monkeypatch.setenv("AUDIT_STH_ANCHOR_PATH", str(anchor_path))
        get_settings.cache_clear()
        try:
            # 登录 + 塞事件
            login = client.post("/api/auth/demo-login").json()
            user_id = uuid.UUID(login["user"]["id"])
            _seed_audit_events(sqlite_engine, user_id, count=3)

            client.headers.update({"Authorization": f"Bearer {login['token']}"})
            resp = client.post("/api/audit/sth/issue")
            assert resp.status_code == 201

            # 锚定文件存在 + 含一行合法 JSON
            assert anchor_path.exists()
            lines = [ln for ln in anchor_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            assert len(lines) >= 1
            record = json.loads(lines[-1])
            assert record["user_id"] == str(user_id)
            # demo-login 写一条 USER_LOGIN + 我们塞 3 条 → tree_size >= 3
            assert record["tree_size"] >= 3
        finally:
            get_settings.cache_clear()

    def test_issue_unauthenticated_returns_401(self, client: TestClient) -> None:
        resp = client.post("/api/audit/sth/issue")
        assert resp.status_code == 401


# ===========================================================================
# 4. GET /api/audit/events/{event_id}/inclusion —— Inclusion proof
# ===========================================================================
class TestInclusionProofEndpoint:
    """**Validates: G10**——前端可独立验证某事件确实在某 STH 中。"""

    def test_inclusion_proof_round_trip_passes_verification(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """**端到端密码学正确性**：服务端返回的 proof 用客户端 verify 函数应通过。

        这是本任务最强的整体性证据——只要任何一处的实现（leaf 哈希前缀 /
        节点哈希算法 / proof 路径计算）漂移，验证立即失败。
        """
        user_id = uuid.UUID(demo_user["id"])
        events = _seed_audit_events(sqlite_engine, user_id, count=5)
        target = events[2]  # 选中间一条做 proof

        # 先签发 STH
        sth_resp = auth_client.post("/api/audit/sth/issue")
        assert sth_resp.status_code == 201
        sth = sth_resp.json()
        tree_size = sth["tree_size"]

        # 拉 inclusion proof
        proof_resp = auth_client.get(
            f"/api/audit/events/{target.id}/inclusion?tree_size={tree_size}"
        )
        assert proof_resp.status_code == 200
        proof_body = proof_resp.json()

        # 字段完整
        assert {"event_id", "leaf_index", "leaf_hash", "proof",
                "tree_size", "root_hash"} <= proof_body.keys()
        assert 0 <= proof_body["leaf_index"] < tree_size
        assert proof_body["tree_size"] == tree_size
        assert proof_body["event_id"] == str(target.id)

        # 客户端独立验证（不需 DB）
        ok = verify_inclusion_proof(
            leaf_hex=proof_body["leaf_hash"],
            index=proof_body["leaf_index"],
            tree_size=proof_body["tree_size"],
            proof=proof_body["proof"],
            expected_root_hex=proof_body["root_hash"],
        )
        assert ok, "inclusion proof 验证失败——服务端实现与客户端 verify 不一致"

    def test_inclusion_event_not_owned_returns_404(
        self,
        client: TestClient,
        sqlite_engine: Engine,
    ) -> None:
        """**鉴权隔离**：访问别人的 event_id → 404（不暴露存在性）。"""
        # 用户 A
        login_a = client.post("/api/auth/demo-login", json={"wallet": "0xAAAFFFF000000000000000000000000000000AAA"}).json()
        # 用户 B
        login_b = client.post("/api/auth/demo-login", json={"wallet": "0xBBBFFFF000000000000000000000000000000BBB"}).json()
        user_b_id = uuid.UUID(login_b["user"]["id"])
        b_events = _seed_audit_events(sqlite_engine, user_b_id, count=2)

        # A 用 B 的 event_id 查 → 404
        client.headers.update({"Authorization": f"Bearer {login_a['token']}"})
        resp = client.get(
            f"/api/audit/events/{b_events[0].id}/inclusion?tree_size=2"
        )
        assert resp.status_code == 404

    def test_tree_size_exceeds_chain_returns_404(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """tree_size 大于实际链长 → 404（不暴露内部细节）。"""
        user_id = uuid.UUID(demo_user["id"])
        events = _seed_audit_events(sqlite_engine, user_id, count=2)

        resp = auth_client.get(
            f"/api/audit/events/{events[0].id}/inclusion?tree_size=999"
        )
        assert resp.status_code == 404

    def test_unknown_event_returns_404(
        self,
        auth_client: TestClient,
    ) -> None:
        """随机 UUID → 404。"""
        fake_id = uuid.uuid4()
        resp = auth_client.get(
            f"/api/audit/events/{fake_id}/inclusion?tree_size=1"
        )
        assert resp.status_code == 404

    def test_missing_tree_size_returns_422(
        self,
        auth_client: TestClient,
    ) -> None:
        """缺 tree_size 必填参数 → 422（FastAPI 自动校验）。"""
        fake_id = uuid.uuid4()
        resp = auth_client.get(f"/api/audit/events/{fake_id}/inclusion")
        assert resp.status_code == 422


# ===========================================================================
# 5. GET /api/audit/sth/consistency —— Consistency proof
# ===========================================================================
class TestConsistencyProofEndpoint:
    """**Validates: G11**——证明新 STH 是旧 STH 的 append-only 扩展。"""

    def test_from_size_greater_than_to_size_returns_400(
        self,
        auth_client: TestClient,
    ) -> None:
        """from_size > to_size → 400 BAD_REQUEST。"""
        resp = auth_client.get(
            "/api/audit/sth/consistency?from_size=10&to_size=5"
        )
        assert resp.status_code == 400

    def test_consistency_returns_proof_list(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """正向：链上 ≥ to_size 条事件 → 返回 proof 列表。"""
        user_id = uuid.UUID(demo_user["id"])
        _seed_audit_events(sqlite_engine, user_id, count=8)

        resp = auth_client.get(
            "/api/audit/sth/consistency?from_size=3&to_size=8"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["from_size"] == 3
        assert body["to_size"] == 8
        assert isinstance(body["proof"], list)

    def test_consistency_chain_too_short_returns_404(
        self,
        auth_client: TestClient,
        demo_user: dict[str, str],
        sqlite_engine: Engine,
    ) -> None:
        """to_size > 实际链长 → 404。"""
        user_id = uuid.UUID(demo_user["id"])
        _seed_audit_events(sqlite_engine, user_id, count=2)

        resp = auth_client.get(
            "/api/audit/sth/consistency?from_size=1&to_size=99"
        )
        assert resp.status_code == 404

    def test_consistency_unauthenticated_returns_401(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/audit/sth/consistency?from_size=0&to_size=1")
        assert resp.status_code == 401
