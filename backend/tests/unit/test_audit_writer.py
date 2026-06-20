"""任务 7.2 审计写入器 + 链验证 单元测试 + PBT。

覆盖维度（对应任务 7.2 验收点 / Req 11 AC1-7）
------------------------------------------------
1. **基本写入**：首事件 prev=GENESIS、后续 prev=链尾、链分组（user × passport）、
   ``actor_id`` 默认派生、``event_hash`` 64 hex 格式。
2. **链验证 ``verify_chain_integrity``**：空链 ✓、完整链 ✓、篡改 / 删除 / 插入 /
   时间戳改动 ✗，错误消息含被破坏事件的 ID。
3. **PBT Property 2（审计链完整性）**：
   - 随机 N 条事件写入 → verify 必返 True；
   - 随机篡改任意一条 → verify 必返 False。
4. **路由集成回归**：``demo-login`` 仍然产出可被 verify 通过的链，
   首事件 prev=GENESIS。
5. **保护性**：``audit_stub`` 模块已删除（``find_spec`` 返回 None）。

PBT 用例标 ``@pytest.mark.pbt``，与 ``test_audit_chain.py`` / ``test_vault.py`` 对齐。
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from app.core.audit_chain import (
    GENESIS_HASH_DEFAULT,
    compute_event_hash,
    get_genesis_hash,
)
from app.models import AuditEvent, User
from app.models.enums import AuditEventType
from app.services.audit_writer import (
    ACTOR_TYPE_PLANNER,
    ACTOR_TYPE_USER,
    AuditWriteError,
    AuditWriter,
    write_audit_event,
)

HEX_64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_user(db_session, wallet: str = "0xAUDIT0000000000000000000000000000000001") -> User:
    """快速建一个测试用户（事务级隔离 fixture 提供）。"""
    user = User(primary_wallet=wallet)
    db_session.add(user)
    db_session.flush()
    return user


def _write_n_events(
    db_session,
    *,
    user_id: uuid.UUID,
    n: int,
    passport_id: uuid.UUID | None = None,
    event_type: str = AuditEventType.USER_LOGIN,
) -> list[AuditEvent]:
    """连续写入 N 条事件，返回 ORM 行列表。

    每条事件携带不同的 ``i`` 字段确保 event_json 各异，避免 hash 偶然相等。
    """
    writer = AuditWriter(db_session)
    out: list[AuditEvent] = []
    for i in range(n):
        evt = writer.write(
            event_type=event_type,
            user_id=user_id,
            passport_id=passport_id,
            actor_type=ACTOR_TYPE_USER,
            event_data={"i": i, "marker": f"event-{i}"},
        )
        out.append(evt)
    return out


# ---------------------------------------------------------------------------
# 1. 基本写入
# ---------------------------------------------------------------------------
class TestWriteBasic:
    """链首 / 后续 / 分链 / actor 派生 / hash 格式。"""

    def test_write_first_event_uses_genesis(self, db_session) -> None:
        """**Validates: Requirements 11**（AC2：链首事件 prev=GENESIS）。

        新建用户（链空）→ 首条事件的 ``previous_event_hash`` 必须等于
        :data:`GENESIS_HASH_DEFAULT`。
        """
        user = _make_user(db_session)
        evt = AuditWriter(db_session).write(
            event_type=AuditEventType.USER_LOGIN,
            user_id=user.id,
            actor_type=ACTOR_TYPE_USER,
            event_data={"wallet": user.primary_wallet},
        )
        assert evt.previous_event_hash == GENESIS_HASH_DEFAULT
        # genesis 也由 settings 暴露，二者必须一致（conftest 已固定为 default）
        assert evt.previous_event_hash == get_genesis_hash()

    def test_write_second_event_uses_first_hash(self, db_session) -> None:
        """**Validates: Requirements 11**（AC1：链尾继承）。

        第二条事件的 ``previous_event_hash`` 必须是第一条事件的 ``event_hash``。
        """
        user = _make_user(db_session)
        first, second = _write_n_events(db_session, user_id=user.id, n=2)
        assert second.previous_event_hash == first.event_hash
        assert first.event_hash != second.event_hash  # 不同事件不同 hash

    def test_write_third_event_uses_second_hash(self, db_session) -> None:
        """链长 3：3 条事件构成连续 prev → hash 链。"""
        user = _make_user(db_session)
        evts = _write_n_events(db_session, user_id=user.id, n=3)
        assert evts[0].previous_event_hash == GENESIS_HASH_DEFAULT
        assert evts[1].previous_event_hash == evts[0].event_hash
        assert evts[2].previous_event_hash == evts[1].event_hash

    def test_write_no_passport_chain_independent(self, db_session) -> None:
        """**Validates: Requirements 11**

        ``passport_id=None`` 的"用户级链"独立于"passport 级链"。
        """
        user = _make_user(db_session)
        passport_id = uuid.uuid4()

        # 1) 先在用户级链写一条
        user_evt = AuditWriter(db_session).write(
            event_type=AuditEventType.USER_LOGIN,
            user_id=user.id,
            actor_type=ACTOR_TYPE_USER,
        )
        # 2) 再在某 passport 链写一条——它应该看到空链 → prev=GENESIS
        passport_evt = AuditWriter(db_session).write(
            event_type=AuditEventType.PASSPORT_CREATED,
            user_id=user.id,
            passport_id=passport_id,
            actor_type=ACTOR_TYPE_USER,
        )
        assert passport_evt.previous_event_hash == GENESIS_HASH_DEFAULT
        assert passport_evt.previous_event_hash != user_evt.event_hash

    def test_write_different_passports_independent_chains(self, db_session) -> None:
        """**Validates: Requirements 11**

        同一用户两个不同 passport 各自的链互相独立——每条链的首事件
        都用 GENESIS 作 prev，互不引用。
        """
        user = _make_user(db_session)
        passport_a = uuid.uuid4()
        passport_b = uuid.uuid4()

        evt_a1 = AuditWriter(db_session).write(
            event_type=AuditEventType.PASSPORT_CREATED,
            user_id=user.id,
            passport_id=passport_a,
        )
        evt_b1 = AuditWriter(db_session).write(
            event_type=AuditEventType.PASSPORT_CREATED,
            user_id=user.id,
            passport_id=passport_b,
        )
        # 两条链各自从 genesis 起
        assert evt_a1.previous_event_hash == GENESIS_HASH_DEFAULT
        assert evt_b1.previous_event_hash == GENESIS_HASH_DEFAULT

        # 第二条 passport_a 事件应继承 passport_a 的链尾，而不是 passport_b
        evt_a2 = AuditWriter(db_session).write(
            event_type=AuditEventType.PASSPORT_PAUSED,
            user_id=user.id,
            passport_id=passport_a,
        )
        assert evt_a2.previous_event_hash == evt_a1.event_hash
        assert evt_a2.previous_event_hash != evt_b1.event_hash

    def test_event_hash_format_is_64_lowercase_hex(self, db_session) -> None:
        """**Validates: Requirements 11**（AC1：sha256 → 64 hex 字符）。"""
        user = _make_user(db_session)
        evt = AuditWriter(db_session).write(
            event_type=AuditEventType.USER_LOGIN,
            user_id=user.id,
        )
        assert HEX_64.match(evt.event_hash), f"not 64 lowercase hex: {evt.event_hash!r}"

    def test_actor_id_defaults_to_user_id_str(self, db_session) -> None:
        """未传 ``actor_id`` 时默认为 ``str(user_id)``（人类用户场景）。"""
        user = _make_user(db_session)
        evt = AuditWriter(db_session).write(
            event_type=AuditEventType.USER_LOGIN,
            user_id=user.id,
            actor_type=ACTOR_TYPE_USER,
        )
        assert evt.actor_id == str(user.id)

    def test_actor_id_explicit_non_uuid_allowed(self, db_session) -> None:
        """**Validates: Requirements 11**（AC5：actor_id 允许非 UUID 字符串）。"""
        user = _make_user(db_session)
        evt = AuditWriter(db_session).write(
            event_type=AuditEventType.MODEL_CALL_STARTED,
            user_id=user.id,
            actor_type=ACTOR_TYPE_PLANNER,
            actor_id="PLANNER",
        )
        assert evt.actor_id == "PLANNER"
        assert evt.actor_type == "PLANNER"

    def test_event_json_structure(self, db_session) -> None:
        """``event_json`` 固定结构 ``{event_type, actor_type, actor_id, data}``。"""
        user = _make_user(db_session)
        evt = AuditWriter(db_session).write(
            event_type=AuditEventType.USER_LOGIN,
            user_id=user.id,
            actor_type=ACTOR_TYPE_USER,
            event_data={"wallet": "0xABC", "trace_id": "t-123"},
        )
        assert evt.event_json == {
            "event_type": AuditEventType.USER_LOGIN,
            "actor_type": ACTOR_TYPE_USER,
            "actor_id": str(user.id),
            "data": {"wallet": "0xABC", "trace_id": "t-123"},
        }

    def test_event_data_none_becomes_empty_dict(self, db_session) -> None:
        """``event_data=None`` 时 ``event_json["data"]`` 为 ``{}``，不为 None。"""
        user = _make_user(db_session)
        evt = AuditWriter(db_session).write(
            event_type=AuditEventType.USER_LOGIN,
            user_id=user.id,
        )
        assert evt.event_json["data"] == {}

    def test_event_hash_matches_compute_event_hash(self, db_session) -> None:
        """ORM 行存储的 ``event_hash`` 必须等于显式调用 :func:`compute_event_hash`
        重算的结果——确保 writer 没在某处偷偷改字段。"""
        user = _make_user(db_session)
        evt = AuditWriter(db_session).write(
            event_type=AuditEventType.USER_LOGIN,
            user_id=user.id,
            actor_type=ACTOR_TYPE_USER,
            event_data={"k": "v"},
        )
        recomputed = compute_event_hash(
            evt.event_json,
            evt.previous_event_hash,
            evt.created_at.isoformat(),
        )
        assert recomputed == evt.event_hash

    def test_write_persists_optional_fields(self, db_session) -> None:
        """``passport_id`` / ``action_id`` / ``trace_id`` 都按入参原样持久化。"""
        user = _make_user(db_session)
        passport_id = uuid.uuid4()
        action_id = uuid.uuid4()
        trace_id = uuid.uuid4()
        evt = AuditWriter(db_session).write(
            event_type=AuditEventType.ACTION_REQUESTED,
            user_id=user.id,
            passport_id=passport_id,
            action_id=action_id,
            trace_id=trace_id,
        )
        assert evt.passport_id == passport_id
        assert evt.action_id == action_id
        assert evt.trace_id == trace_id

    def test_write_audit_event_convenience_function(self, db_session) -> None:
        """便捷函数 :func:`write_audit_event` 与 ``AuditWriter(s).write`` 等价。"""
        user = _make_user(db_session)
        evt = write_audit_event(
            db_session,
            event_type=AuditEventType.USER_LOGIN,
            user_id=user.id,
            actor_type=ACTOR_TYPE_USER,
        )
        assert evt.event_hash is not None
        assert evt.previous_event_hash == GENESIS_HASH_DEFAULT

    def test_write_failure_raises_audit_write_error(self, db_session) -> None:
        """**Validates: Requirements 11**（AC7：审计写入失败阻止业务转换）。

        模拟 flush 失败时必须抛出 :class:`AuditWriteError`（RuntimeError 子类），
        让调用方在事务中捕获并回滚业务操作。
        """
        from unittest.mock import patch

        from sqlalchemy.exc import IntegrityError

        user = _make_user(db_session)

        # 只在 session.add 之后的 flush 调用时触发异常；
        # 使用 side_effect 函数让第一次 flush（autoflush during query）正常执行，
        # 第二次 flush（显式 flush after add）抛异常。
        original_flush = db_session.flush
        call_count = {"n": 0}

        def _flush_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise IntegrityError("mock", {}, None)
            return original_flush(*args, **kwargs)

        with patch.object(db_session, "flush", side_effect=_flush_side_effect):
            with pytest.raises(AuditWriteError) as exc_info:
                AuditWriter(db_session).write(
                    event_type=AuditEventType.USER_LOGIN,
                    user_id=user.id,
                    actor_type=ACTOR_TYPE_USER,
                )
            # AuditWriteError 是 RuntimeError 的子类
            assert isinstance(exc_info.value, RuntimeError)
            # 原始异常通过 __cause__ 保留
            assert exc_info.value.__cause__ is not None


# ---------------------------------------------------------------------------
# 2. 链验证 verify_chain_integrity
# ---------------------------------------------------------------------------
class TestVerifyChainIntegrity:
    """**Validates: Requirements 11**（AC4：链完整性可重算）。"""

    def test_verify_empty_chain_returns_true(self, db_session) -> None:
        """空链视为完整（没有事件可被篡改）。"""
        user = _make_user(db_session)
        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is True
        assert err is None

    def test_verify_intact_chain_returns_true(self, db_session) -> None:
        """连续写 5 条事件后整链 verify 通过。"""
        user = _make_user(db_session)
        _write_n_events(db_session, user_id=user.id, n=5)
        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is True, f"intact chain should verify; err={err!r}"
        assert err is None

    def test_verify_intact_passport_chain(self, db_session) -> None:
        """同样适用于按 passport 分组的子链。"""
        user = _make_user(db_session)
        passport_id = uuid.uuid4()
        _write_n_events(db_session, user_id=user.id, n=3, passport_id=passport_id)
        ok, err = AuditWriter(db_session).verify_chain_integrity(
            user.id, passport_id=passport_id
        )
        assert ok is True
        assert err is None

    def test_verify_after_event_json_tampering_detects(self, db_session) -> None:
        """**Validates: Requirements 11**

        手动篡改某事件的 ``event_json`` → verify 必须返回 ``(False, msg)``，
        且 ``msg`` 包含被篡改事件的主键 ID。
        """
        user = _make_user(db_session)
        evts = _write_n_events(db_session, user_id=user.id, n=4)
        target = evts[2]

        # 篡改 event_json（绕过 SQLAlchemy 的 dirty 检测：直接重新赋值并 flush）
        original = dict(target.event_json)
        tampered = dict(original)
        tampered["data"] = {"i": 999, "marker": "TAMPERED"}
        target.event_json = tampered
        db_session.add(target)
        db_session.flush()

        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is False
        assert err is not None
        assert str(target.id) in err
        # 篡改 event_json 改的是「重算 hash != 存储 hash」
        assert "event_hash" in err

    def test_verify_after_event_deletion_detects(self, db_session) -> None:
        """删除中间一条事件 → 后续事件的 prev 不再连接到上一条 → verify 失败。"""
        user = _make_user(db_session)
        evts = _write_n_events(db_session, user_id=user.id, n=4)
        deleted = evts[1]
        deleted_id = deleted.id

        db_session.delete(deleted)
        db_session.flush()

        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is False
        assert err is not None
        # 第三条事件的 prev 仍指向"已删除事件的 hash"，但 verify 期望
        # 它指向"现在的上一条"（即原 evts[0]），所以会在 evts[2] 处报告
        # `previous_event_hash mismatch`
        assert "previous_event_hash" in err
        # 报错的事件不可能是被删的那条（它已不存在）
        assert str(deleted_id) not in err

    def test_verify_after_event_insertion_detects(self, db_session) -> None:
        """**Validates: Requirements 11**

        在中间插入一条 hash 错误的事件 → verify 失败。
        通过手工构造一条「prev_hash 错误 + event_hash 错误」的 AuditEvent 行
        模拟攻击者在数据库里偷偷加事件。
        """
        user = _make_user(db_session)
        evts = _write_n_events(db_session, user_id=user.id, n=3)

        # 在 evts[1] 之后插入一条假的事件
        # 给一个介于 evts[1] 和 evts[2] 之间的 created_at，让 ORDER BY 把它排在中间
        fake_ts = evts[1].created_at + timedelta(microseconds=1)
        forged = AuditEvent(
            user_id=user.id,
            event_type=AuditEventType.USER_LOGIN,
            actor_type=ACTOR_TYPE_USER,
            actor_id=str(user.id),
            event_json={
                "event_type": AuditEventType.USER_LOGIN,
                "actor_type": ACTOR_TYPE_USER,
                "actor_id": str(user.id),
                "data": {"forged": True},
            },
            previous_event_hash=evts[1].event_hash,  # 看似合法的 prev
            event_hash="f" * 64,  # 但 event_hash 是错的
            created_at=fake_ts,
        )
        db_session.add(forged)
        db_session.flush()

        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is False
        assert err is not None
        # 伪造行的 prev 看似合法（指向 evts[1]），但 event_hash 错误
        # → 应在 forged 处报告 event_hash mismatch
        assert str(forged.id) in err
        assert "event_hash" in err

    def test_verify_after_timestamp_change_detects(self, db_session) -> None:
        """改 ``created_at`` 但不更新 hash → verify 失败（hash 与 ts 强耦合）。

        把 ``evts[1].created_at`` 推后一段时间，时间排序 / 哈希链都会被
        破坏。具体在哪一索引处报错取决于推后多久（推后小于 evts[2] 的
        间隔仍保留原顺序、推后超过则会重排）；这里只断言「verify 必失败 +
        某一字段不一致」，不断言具体索引。
        """
        user = _make_user(db_session)
        evts = _write_n_events(db_session, user_id=user.id, n=3)
        target = evts[1]

        # 把 created_at 推后 1 微秒；event_hash 不变
        # 推后 1µs 通常不会改变排序（events 之间间隔通常 ≥ 几微秒），
        # 但会让 target 自身的 event_hash 重算与存储不一致。
        target.created_at = target.created_at + timedelta(microseconds=1)
        db_session.add(target)
        db_session.flush()

        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is False
        assert err is not None
        # 错误必然在「event_hash mismatch」或「previous_event_hash mismatch」之一
        assert "event_hash" in err or "previous_event_hash" in err

    def test_verify_after_previous_hash_tampering_detects(
        self, db_session
    ) -> None:
        """直接改某事件的 ``previous_event_hash`` → verify 在该位置失败。"""
        user = _make_user(db_session)
        evts = _write_n_events(db_session, user_id=user.id, n=3)
        target = evts[1]

        target.previous_event_hash = "0" * 64  # 错误的 prev
        db_session.add(target)
        db_session.flush()

        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is False
        assert err is not None
        assert str(target.id) in err
        assert "previous_event_hash" in err

    def test_verify_chain_does_not_leak_other_users(self, db_session) -> None:
        """两个用户各自的链互不影响——A 的链篡改不影响 B 的 verify。"""
        user_a = _make_user(db_session, wallet="0xA0000000000000000000000000000000000001")
        user_b = _make_user(db_session, wallet="0xB0000000000000000000000000000000000002")

        _write_n_events(db_session, user_id=user_a.id, n=3)
        _write_n_events(db_session, user_id=user_b.id, n=3)

        # 篡改 user_a 第一条
        evt_a = (
            db_session.execute(
                select(AuditEvent).where(AuditEvent.user_id == user_a.id)
            )
            .scalars()
            .first()
        )
        evt_a.event_json = {"forged": True}
        db_session.add(evt_a)
        db_session.flush()

        ok_a, _ = AuditWriter(db_session).verify_chain_integrity(user_a.id)
        ok_b, err_b = AuditWriter(db_session).verify_chain_integrity(user_b.id)
        assert ok_a is False
        assert ok_b is True, f"user_b chain should verify; err={err_b!r}"


# ---------------------------------------------------------------------------
# 3. PBT —— Property 2 审计链完整性
# ---------------------------------------------------------------------------
@pytest.mark.pbt
class TestPropertyChainIntegrity:
    """**Validates: Requirements 11**（Property 2：完整链 verify 必通过；
    任意篡改 verify 必失败）。

    数据库相关 PBT 用 ``function_scope_fixture`` 抑制 hypothesis 健康检查——
    每个 example 都重用同一个 ``db_session`` fixture，但通过 ``user_id``
    生成不同链来保持互相独立。
    """

    @given(n=st.integers(min_value=2, max_value=10))
    @settings(
        max_examples=15,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    def test_pbt_random_chain_intact_verifies(self, db_session, n: int) -> None:
        """**Validates: Requirements 11**

        随机生成 N（2-10）条事件，写入后整链 verify 必返 ``(True, None)``。
        """
        # 每个 example 用独立 user 避免链互相污染
        user = User(primary_wallet=f"0xPBT{uuid.uuid4().hex[:35]}")
        db_session.add(user)
        db_session.flush()

        _write_n_events(db_session, user_id=user.id, n=n)

        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is True, (
            f"intact chain of length {n} must verify; got err={err!r}"
        )
        assert err is None

    @given(
        n=st.integers(min_value=2, max_value=8),
        # 通过 data() 让我们在 generate 阶段决定篡改第几条
        data=st.data(),
    )
    @settings(
        max_examples=15,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    def test_pbt_random_tampering_detected(
        self, db_session, n: int, data: st.DataObject
    ) -> None:
        """**Validates: Requirements 11**

        随机选一条已写入事件，篡改 ``event_json`` 后 verify 必返 ``(False, _)``。
        """
        user = User(primary_wallet=f"0xPBT{uuid.uuid4().hex[:35]}")
        db_session.add(user)
        db_session.flush()

        evts = _write_n_events(db_session, user_id=user.id, n=n)

        # 选一条事件来篡改
        idx = data.draw(st.integers(min_value=0, max_value=n - 1))
        target = evts[idx]
        # 用确定性的"非原值"覆盖：在 data 里加一个独特字段
        tampered = dict(target.event_json)
        tampered["data"] = {"PBT_TAMPER_MARKER": uuid.uuid4().hex}
        target.event_json = tampered
        db_session.add(target)
        db_session.flush()

        ok, err = AuditWriter(db_session).verify_chain_integrity(user.id)
        assert ok is False, (
            f"tampering at idx={idx}/{n} should be detected; "
            f"got ok=True err={err!r}"
        )
        assert err is not None


# ---------------------------------------------------------------------------
# 4. 路由集成回归 —— demo-login 写出的链仍然有效
# ---------------------------------------------------------------------------
class TestAuthEndpointChainRegression:
    """替换 audit_stub 后路由仍能产出可被 verify 通过的链。"""

    def test_demo_login_writes_valid_chain(self, client, sqlite_engine) -> None:
        """``POST /api/auth/demo-login`` 写一条 USER_LOGIN：

        - prev=GENESIS（首事件）
        - event_hash 是 64 hex
        - verify_chain_integrity → (True, None)
        """
        resp = client.post("/api/auth/demo-login")
        assert resp.status_code == 200, resp.text
        user_id_str = resp.json()["user"]["id"]
        user_id = uuid.UUID(user_id_str)

        from sqlalchemy.orm import Session

        with Session(sqlite_engine) as session:
            evts = list(
                session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.user_id == user_id)
                    .order_by(AuditEvent.created_at.asc())
                )
                .scalars()
                .all()
            )
            assert len(evts) == 1
            evt = evts[0]
            assert evt.event_type == AuditEventType.USER_LOGIN
            assert evt.previous_event_hash == GENESIS_HASH_DEFAULT
            assert HEX_64.match(evt.event_hash)

            ok, err = AuditWriter(session).verify_chain_integrity(user_id)
            assert ok is True, f"demo-login chain must verify; err={err!r}"

    def test_demo_login_twice_chain_remains_valid(self, client, sqlite_engine) -> None:
        """同一用户连续登录两次 → 第二条事件的 prev 应等于第一条的 hash，整链 verify 通过。"""
        resp1 = client.post("/api/auth/demo-login").json()
        resp2 = client.post("/api/auth/demo-login").json()
        assert resp1["user"]["id"] == resp2["user"]["id"]
        user_id = uuid.UUID(resp1["user"]["id"])

        from sqlalchemy.orm import Session

        with Session(sqlite_engine) as session:
            evts = list(
                session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.user_id == user_id)
                    .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
                )
                .scalars()
                .all()
            )
            assert len(evts) == 2
            assert evts[0].previous_event_hash == GENESIS_HASH_DEFAULT
            assert evts[1].previous_event_hash == evts[0].event_hash

            ok, err = AuditWriter(session).verify_chain_integrity(user_id)
            assert ok is True, err


# ---------------------------------------------------------------------------
# 5. 保护性测试 —— audit_stub 模块已删除
# ---------------------------------------------------------------------------
class TestAuditStubRemoved:
    """任务 7.2 完成后 ``app.services.audit_stub`` 必须不存在。"""

    def test_audit_stub_module_removed(self) -> None:
        """``importlib.util.find_spec`` 必须返回 None。"""
        spec = importlib.util.find_spec("app.services.audit_stub")
        assert spec is None, (
            "audit_stub.py should be deleted in task 7.2 (replaced by audit_writer.py)"
        )

    def test_importing_audit_stub_raises_module_not_found(self) -> None:
        """直接 import 必须抛 ModuleNotFoundError。"""
        with pytest.raises(ModuleNotFoundError):
            __import__("app.services.audit_stub")
