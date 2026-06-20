"""任务 5.3 Passport 端点集成测试。

通过 FastAPI TestClient 走完整 HTTP 路径，验证：

- 完整生命周期：create → get → update_policy → pause → resume → revoke
- 撤销后阻止后续操作（resume / policy update 返回 409）
- 错误响应格式符合 design.md 统一错误响应结构
- 状态码映射正确（201 / 200 / 400 / 404 / 409 / 422）

复用 conftest 的 ``auth_client`` / ``demo_credential`` fixture。
"""

from __future__ import annotations

import uuid


class TestPassportFullLifecycle:
    """端到端生命周期：create → get → update_policy → pause → resume → revoke。"""

    def test_full_lifecycle(self, auth_client, demo_credential) -> None:
        """Req 3 AC1-7: 完整 Passport 生命周期走通。"""
        # ---- 1. CREATE（关联凭证 → ACTIVE） ----
        create_resp = auth_client.post(
            "/api/passports",
            json={
                "name": "lifecycle-bot",
                "agent_type": "trader",
                "api_credential_id": demo_credential["id"],
                "template_name": "small_spot_executor",
            },
        )
        assert create_resp.status_code == 201
        body = create_resp.json()
        passport_id = body["id"]
        assert body["state"] == "ACTIVE"
        assert body["version"] == 1
        assert body["name"] == "lifecycle-bot"
        assert body["agent_type"] == "trader"
        assert body["reputation_score"] == 50
        assert body["api_credential_id"] == demo_credential["id"]
        assert "policy" in body
        assert body["policy"]["version"] == "0.1"

        # ---- 2. GET（单个详情） ----
        get_resp = auth_client.get(f"/api/passports/{passport_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == passport_id
        assert get_resp.json()["state"] == "ACTIVE"

        # ---- 3. LIST（出现在列表中） ----
        list_resp = auth_client.get("/api/passports")
        assert list_resp.status_code == 200
        ids = [p["id"] for p in list_resp.json()["passports"]]
        assert passport_id in ids

        # ---- 4. UPDATE POLICY（version 递增 1→2） ----
        # 构造一份干净的 policy dict（不含 null 可选字段，避免 schema 拒绝）
        new_policy = {
            "version": "0.1",
            "capabilities": body["policy"]["capabilities"],
            "limits": {
                "allowed_symbols": ["btcusdt", "ethusdt"],
                "max_notional_usdt_per_order": 20,
                "max_daily_notional_usdt": 100,
                "max_orders_per_day": 5,
            },
            "approval": body["policy"]["approval"],
            "blocked_actions": body["policy"]["blocked_actions"],
        }
        patch_resp = auth_client.patch(
            f"/api/passports/{passport_id}/policy",
            json={"policy": new_policy},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["version"] == 2
        assert patch_resp.json()["policy"]["limits"]["max_orders_per_day"] == 5

        # ---- 5. PAUSE（ACTIVE → PAUSED） ----
        pause_resp = auth_client.post(f"/api/passports/{passport_id}/pause")
        assert pause_resp.status_code == 200
        assert pause_resp.json()["state"] == "PAUSED"

        # ---- 6. RESUME（PAUSED → ACTIVE） ----
        resume_resp = auth_client.post(f"/api/passports/{passport_id}/resume")
        assert resume_resp.status_code == 200
        assert resume_resp.json()["state"] == "ACTIVE"

        # ---- 7. REVOKE（ACTIVE → REVOKED，终态） ----
        revoke_resp = auth_client.post(f"/api/passports/{passport_id}/revoke")
        assert revoke_resp.status_code == 200
        assert revoke_resp.json()["state"] == "REVOKED"

        # ---- 8. 验证终态不可恢复 ----
        # resume 应返回 409
        resume_after_revoke = auth_client.post(
            f"/api/passports/{passport_id}/resume"
        )
        assert resume_after_revoke.status_code == 409
        error_body = resume_after_revoke.json()["error"]
        assert error_body["code"] == "ILLEGAL_STATE_TRANSITION"

        # policy update 应返回 409
        update_policy = {
            "version": "0.1",
            "capabilities": {
                "read_market": True,
                "read_account": True,
                "place_order": True,
                "cancel_order": True,
                "withdraw": False,
            },
            "limits": {
                "allowed_symbols": ["btcusdt"],
                "max_notional_usdt_per_order": 10,
                "max_daily_notional_usdt": 50,
                "max_orders_per_day": 3,
            },
            "approval": {
                "required_for_trade": True,
                "required_for_policy_change": True,
                "expires_after_seconds": 300,
            },
            "blocked_actions": ["withdraw", "borrow"],
        }
        policy_after_revoke = auth_client.patch(
            f"/api/passports/{passport_id}/policy",
            json={"policy": update_policy},
        )
        assert policy_after_revoke.status_code == 409

    def test_revoke_blocks_subsequent_state_changes(
        self, auth_client, demo_credential
    ) -> None:
        """Req 3 AC7: REVOKED 后 pause/resume/revoke 全部返回 409。"""
        # 创建并撤销
        create_resp = auth_client.post(
            "/api/passports",
            json={
                "name": "revoke-block-bot",
                "agent_type": "trader",
                "api_credential_id": demo_credential["id"],
                "template_name": "small_spot_executor",
            },
        )
        assert create_resp.status_code == 201
        passport_id = create_resp.json()["id"]

        revoke_resp = auth_client.post(f"/api/passports/{passport_id}/revoke")
        assert revoke_resp.status_code == 200

        # 所有状态变更操作都应返回 409
        assert auth_client.post(
            f"/api/passports/{passport_id}/pause"
        ).status_code == 409
        assert auth_client.post(
            f"/api/passports/{passport_id}/resume"
        ).status_code == 409
        assert auth_client.post(
            f"/api/passports/{passport_id}/revoke"
        ).status_code == 409


class TestPassportCreateValidation:
    """创建端点的输入校验。"""

    def test_create_with_invalid_policy_returns_400(
        self, auth_client, demo_credential
    ) -> None:
        """policy 含 withdraw=true → 400 POLICY_INVALID。"""
        bad_policy = {
            "version": "0.1",
            "capabilities": {
                "read_market": True,
                "read_account": True,
                "place_order": True,
                "cancel_order": True,
                "withdraw": True,  # 非法
            },
            "limits": {
                "allowed_symbols": ["btcusdt"],
                "max_notional_usdt_per_order": 20,
                "max_daily_notional_usdt": 100,
                "max_orders_per_day": 10,
            },
            "approval": {
                "required_for_trade": True,
                "expires_after_seconds": 300,
            },
            "blocked_actions": ["withdraw", "borrow"],
        }
        resp = auth_client.post(
            "/api/passports",
            json={
                "name": "bad-bot",
                "agent_type": "trader",
                "api_credential_id": demo_credential["id"],
                "policy": bad_policy,
            },
        )
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert error["code"] == "POLICY_INVALID"
        assert "errors" in error["details"]

    def test_create_with_both_policy_and_template_returns_422(
        self, auth_client, demo_credential
    ) -> None:
        """policy 与 template_name 都给 → 422 VALIDATION_ERROR。"""
        resp = auth_client.post(
            "/api/passports",
            json={
                "name": "both-bot",
                "agent_type": "trader",
                "api_credential_id": demo_credential["id"],
                "policy": {"version": "0.1"},
                "template_name": "small_spot_executor",
            },
        )
        assert resp.status_code == 422

    def test_create_with_nonexistent_credential_returns_404(
        self, auth_client
    ) -> None:
        """关联不存在的凭证 → 404。"""
        fake_id = str(uuid.uuid4())
        resp = auth_client.post(
            "/api/passports",
            json={
                "name": "ghost-cred-bot",
                "agent_type": "trader",
                "api_credential_id": fake_id,
                "template_name": "small_spot_executor",
            },
        )
        assert resp.status_code == 404


class TestPassportPolicyUpdate:
    """策略更新端点。"""

    def test_update_policy_increments_version(
        self, auth_client, demo_passport
    ) -> None:
        """Req 3 AC3: 每次 PATCH /policy → version +1。"""
        passport_id = demo_passport["id"]
        # 构造干净的 policy dict（不含 null 可选字段）
        policy = {
            "version": "0.1",
            "capabilities": {
                "read_market": True,
                "read_account": True,
                "place_order": True,
                "cancel_order": True,
                "withdraw": False,
            },
            "limits": {
                "allowed_symbols": ["btcusdt", "ethusdt"],
                "max_notional_usdt_per_order": 20,
                "max_daily_notional_usdt": 100,
                "max_orders_per_day": 3,
            },
            "approval": {
                "required_for_trade": True,
                "required_for_policy_change": True,
                "expires_after_seconds": 300,
            },
            "blocked_actions": ["withdraw", "borrow", "margin", "transfer_out"],
        }

        resp = auth_client.patch(
            f"/api/passports/{passport_id}/policy",
            json={"policy": policy},
        )
        assert resp.status_code == 200
        assert resp.json()["version"] == 2

        # 再更新一次
        policy["limits"]["max_orders_per_day"] = 7
        resp2 = auth_client.patch(
            f"/api/passports/{passport_id}/policy",
            json={"policy": policy},
        )
        assert resp2.status_code == 200
        assert resp2.json()["version"] == 3

    def test_update_nonexistent_passport_returns_404(self, auth_client) -> None:
        """不存在的 passport → 404。"""
        fake_id = str(uuid.uuid4())
        resp = auth_client.patch(
            f"/api/passports/{fake_id}/policy",
            json={"policy": {"version": "0.1"}},
        )
        assert resp.status_code == 404


class TestPassportStateTransitions:
    """状态转换端点的错误场景。"""

    def test_pause_from_draft_returns_409(self, auth_client) -> None:
        """DRAFT → PAUSED 不合法 → 409 ILLEGAL_STATE_TRANSITION。"""
        # 创建无凭证的 DRAFT passport
        resp = auth_client.post(
            "/api/passports",
            json={
                "name": "draft-bot",
                "agent_type": "trader",
                "template_name": "small_spot_executor",
            },
        )
        assert resp.status_code == 201
        passport_id = resp.json()["id"]
        assert resp.json()["state"] == "DRAFT"

        pause_resp = auth_client.post(f"/api/passports/{passport_id}/pause")
        assert pause_resp.status_code == 409
        assert pause_resp.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"

    def test_resume_from_active_returns_409(
        self, auth_client, demo_passport
    ) -> None:
        """ACTIVE → ACTIVE（resume 自循环）不合法 → 409。"""
        passport_id = demo_passport["id"]
        assert demo_passport["state"] == "ACTIVE"

        resume_resp = auth_client.post(f"/api/passports/{passport_id}/resume")
        assert resume_resp.status_code == 409

    def test_revoked_cannot_resume(
        self, auth_client, demo_passport
    ) -> None:
        """Req 3 AC7: REVOKED 是终态，不可恢复。"""
        passport_id = demo_passport["id"]
        revoke_resp = auth_client.post(f"/api/passports/{passport_id}/revoke")
        assert revoke_resp.status_code == 200

        resume_resp = auth_client.post(f"/api/passports/{passport_id}/resume")
        assert resume_resp.status_code == 409
        assert resume_resp.json()["error"]["code"] == "ILLEGAL_STATE_TRANSITION"


class TestPassportTemplates:
    """模板端点。"""

    def test_list_templates_returns_3_items(self, auth_client) -> None:
        """GET /api/passports/templates 返回 3 个内置模板。"""
        resp = auth_client.get("/api/passports/templates")
        assert resp.status_code == 200
        templates = resp.json()["templates"]
        assert len(templates) == 3
        names = [t["name"] for t in templates]
        assert "readonly_researcher" in names
        assert "small_spot_executor" in names
        assert "dao_treasury_guarded" in names


class TestPassportAccessControl:
    """认证与授权。"""

    def test_unauthenticated_request_returns_401(self, client) -> None:
        """无 token → 401。"""
        resp = client.get("/api/passports")
        assert resp.status_code == 401

    def test_get_nonexistent_passport_returns_404(self, auth_client) -> None:
        """不存在的 passport_id → 404。"""
        fake_id = str(uuid.uuid4())
        resp = auth_client.get(f"/api/passports/{fake_id}")
        assert resp.status_code == 404
