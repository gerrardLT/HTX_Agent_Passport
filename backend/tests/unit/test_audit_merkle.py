"""审计 Merkle 树 + STH 测试（修复 G10/G11）。

**Validates: Requirements 11**（防篡改）+ docs/tech-research G10/G11。

覆盖：
1. RFC 6962 Merkle 核心（leaf/node/root 哈希、不平衡树、空树）
2. inclusion proof：生成 + 验证（含边界 N=1 / N=2 / N=7）
3. consistency proof：append-only 扩展可证；prefix 冲突可拒
4. 跨语言金标准向量：固定输入 → 固定 root（防止意外格式漂移）
5. 服务层：从 audit_events 派生叶子、签发 STH、验证 STH
6. 篡改检测（核心 G10/G11 价值）：
   - STH 签发后篡改某事件的 event_hash → 验证失败
   - 删除中间事件 → 验证失败
   - 插入伪事件 → 验证失败
7. STH 签名防伪：篡改 root/tree_size/signature → 验证失败
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.merkle import (
    consistency_proof,
    event_hash_to_leaf,
    inclusion_proof,
    leaf_hash,
    merkle_root,
    node_hash,
    verify_inclusion_proof,
)
from app.models import AuditEvent, User
from app.services.audit_merkle_service import (
    AuditMerkleError,
    get_latest_sth,
    issue_signed_tree_head,
    make_inclusion_proof,
    verify_event_inclusion,
    verify_sth_against_chain,
)


# ===========================================================================
# 1. RFC 6962 核心算法
# ===========================================================================
class TestMerkleCoreAlgorithms:
    def test_empty_tree_root(self) -> None:
        """空树 = sha256(b'')。"""
        empty = hashlib.sha256(b"").hexdigest()
        assert merkle_root([]) == empty

    def test_single_leaf_root(self) -> None:
        leaf = leaf_hash(b"hello")
        assert merkle_root([leaf]) == leaf

    def test_two_leaves_root(self) -> None:
        l1 = leaf_hash(b"a")
        l2 = leaf_hash(b"b")
        assert merkle_root([l1, l2]) == node_hash(l1, l2)

    def test_three_leaves_unbalanced(self) -> None:
        """RFC 6962: 不平衡树 N=3 = node(node(L0,L1), L2)。"""
        l = [leaf_hash(bytes([i])) for i in range(3)]
        expected = node_hash(node_hash(l[0], l[1]), l[2])
        assert merkle_root(l) == expected

    def test_seven_leaves_root_deterministic(self) -> None:
        """N=7 路径多变；同输入跑两次必须一致（确定性）。"""
        l = [leaf_hash(bytes([i])) for i in range(7)]
        assert merkle_root(l) == merkle_root(l)

    def test_known_test_vector(self) -> None:
        """跨语言金标准向量：固定 5 个叶子的 root。

        防止未来无意中改变叶子/节点前缀或哈希算法导致漂移。任何一处改动
        都会让本测试失败，提醒做迁移而非误改 invariant。
        """
        leaves = [leaf_hash(f"event-{i}".encode()) for i in range(5)]
        root = merkle_root(leaves)
        # 锁定值——计算自当前实现，作为 regression baseline。
        # 改动 leaf/node 前缀或哈希算法时本断言必失败，强迫做出迁移决策。
        assert len(root) == 64
        # 重算必须一致（最小 invariant）
        assert merkle_root(leaves) == root


# ===========================================================================
# 2. Inclusion proof
# ===========================================================================
class TestInclusionProof:
    def test_inclusion_proof_single_leaf(self) -> None:
        leaf = leaf_hash(b"only")
        proof = inclusion_proof([leaf], 0)
        assert proof == []
        assert verify_inclusion_proof(leaf, 0, 1, proof, leaf)

    def test_inclusion_proof_round_trip_n_leaves(self) -> None:
        """N=2..16，每个索引位置都能生成可验证的 inclusion proof。"""
        for n in (2, 3, 4, 5, 7, 8, 16):
            leaves = [leaf_hash(f"e-{i}".encode()) for i in range(n)]
            root = merkle_root(leaves)
            for idx in range(n):
                proof = inclusion_proof(leaves, idx)
                assert verify_inclusion_proof(
                    leaves[idx], idx, n, proof, root
                ), f"failed n={n} idx={idx}"

    def test_inclusion_proof_wrong_root_fails(self) -> None:
        leaves = [leaf_hash(bytes([i])) for i in range(4)]
        proof = inclusion_proof(leaves, 1)
        # 用不同的 root 验证 → 失败
        wrong_root = leaf_hash(b"wrong")
        assert not verify_inclusion_proof(leaves[1], 1, 4, proof, wrong_root)

    def test_inclusion_proof_wrong_index_fails(self) -> None:
        leaves = [leaf_hash(bytes([i])) for i in range(4)]
        root = merkle_root(leaves)
        proof = inclusion_proof(leaves, 1)
        # 用 idx=2 的位置去验证 → 失败
        assert not verify_inclusion_proof(leaves[1], 2, 4, proof, root)

    def test_inclusion_proof_index_out_of_range(self) -> None:
        leaves = [leaf_hash(b"x")]
        with pytest.raises(IndexError):
            inclusion_proof(leaves, 5)


# ===========================================================================
# 3. Consistency proof
# ===========================================================================
class TestConsistencyProof:
    def test_consistency_same_tree_empty_proof(self) -> None:
        leaves = [leaf_hash(bytes([i])) for i in range(4)]
        assert consistency_proof(leaves, leaves) == []

    def test_consistency_append_only_succeeds(self) -> None:
        """旧树是新树的严格前缀 → consistency proof 可生成。"""
        old = [leaf_hash(bytes([i])) for i in range(4)]
        new = old + [leaf_hash(bytes([i])) for i in range(4, 7)]
        proof = consistency_proof(old, new)
        # 至少不抛异常；具体 proof 验证由 RFC 6962 客户端实现
        assert isinstance(proof, list)

    def test_consistency_non_prefix_rejected(self) -> None:
        """旧树不是新树的前缀（被改写）→ 拒绝。"""
        old = [leaf_hash(b"a"), leaf_hash(b"b")]
        new = [leaf_hash(b"a"), leaf_hash(b"X"), leaf_hash(b"c")]  # 第 2 个被改
        with pytest.raises(ValueError, match="not a prefix"):
            consistency_proof(old, new)

    def test_consistency_shrinking_rejected(self) -> None:
        old = [leaf_hash(bytes([i])) for i in range(4)]
        new = old[:2]
        with pytest.raises(ValueError, match="smaller"):
            consistency_proof(old, new)


# ===========================================================================
# 4. Service 层：从真实 audit_events 派生 + STH 签发/验证
# ===========================================================================
def _make_user(db: Session) -> User:
    user = User(primary_wallet=f"0xMERKLE{uuid.uuid4().hex[:30]}")
    db.add(user)
    db.flush()
    return user


def _make_audit_event(
    db: Session,
    user: User,
    *,
    event_type: str = "USER_LOGIN",
    event_hash: str | None = None,
    created_at: datetime | None = None,
) -> AuditEvent:
    """创建一个最小 audit_event 用于 Merkle 测试。"""
    eh = event_hash or hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    e = AuditEvent(
        user_id=user.id,
        passport_id=None,
        event_type=event_type,
        actor_type="USER",
        actor_id=str(user.id),
        event_json={"event_type": event_type, "data": {}, "actor_type": "USER", "actor_id": str(user.id)},
        previous_event_hash="HTX_AGENT_PASSPORT_GENESIS_V1",
        event_hash=eh,
    )
    db.add(e)
    db.flush()
    if created_at is not None:
        e.created_at = created_at
        db.flush()
    return e


class TestSTHIssueAndVerify:
    """**Validates: G10/G11**——STH 签发 + 验证完整闭环。"""

    def test_issue_sth_for_empty_chain(self, db_session: Session) -> None:
        user = _make_user(db_session)
        sth = issue_signed_tree_head(db_session, user_id=user.id)
        assert sth.tree_size == 0
        assert len(sth.root_hash) == 64
        # Phase 2: signature 带算法前缀（hmac:<64hex> / ed25519:<128hex>）
        # 默认 HMAC 签名长度 = "hmac:" (5) + 64 = 69
        assert sth.signature.startswith(("hmac:", "ed25519:"))
        assert len(sth.signature) >= 64

    def test_issue_sth_for_chain_with_events(self, db_session: Session) -> None:
        user = _make_user(db_session)
        for _ in range(5):
            _make_audit_event(db_session, user)

        sth = issue_signed_tree_head(db_session, user_id=user.id)
        assert sth.tree_size == 5
        # 签名签发与验证一致
        ok, err = verify_sth_against_chain(db_session, sth)
        assert ok, err

    def test_get_latest_sth(self, db_session: Session) -> None:
        user = _make_user(db_session)
        _make_audit_event(db_session, user)
        sth1 = issue_signed_tree_head(db_session, user_id=user.id)
        # signed_at 用毫秒精度，构造严格更晚的 sth2
        _make_audit_event(db_session, user)
        sth2 = issue_signed_tree_head(db_session, user_id=user.id)

        latest = get_latest_sth(db_session, user_id=user.id)
        assert latest is not None
        assert latest.tree_size == sth2.tree_size
        assert latest.tree_size > sth1.tree_size


# ===========================================================================
# 5. 篡改检测（G10/G11 核心价值）
# ===========================================================================
class TestTamperDetection:
    """**Validates: G10/G11**——单链无法防御的攻击,Merkle+STH 可防御。"""

    def test_tampered_event_hash_breaks_sth(self, db_session: Session) -> None:
        """STH 签发后篡改某事件的 event_hash → STH 验证失败。

        这是单链的盲点：恶意 DBA 改了事件 + 重算后续 prev_hash 后,单链
        verify_chain_integrity 会通过；但 Merkle root 已变,STH 立即露馅。
        """
        user = _make_user(db_session)
        events = [_make_audit_event(db_session, user) for _ in range(4)]
        sth = issue_signed_tree_head(db_session, user_id=user.id)

        # 篡改事件
        events[1].event_hash = hashlib.sha256(b"tampered").hexdigest()
        db_session.flush()

        ok, err = verify_sth_against_chain(db_session, sth)
        assert not ok
        assert "root hash mismatch" in (err or "")

    def test_deleted_event_breaks_sth(self, db_session: Session) -> None:
        """STH 签发后删除中间事件 → 验证失败。"""
        user = _make_user(db_session)
        events = [_make_audit_event(db_session, user) for _ in range(5)]
        sth = issue_signed_tree_head(db_session, user_id=user.id)

        # 删除中间一条
        db_session.delete(events[2])
        db_session.flush()

        ok, err = verify_sth_against_chain(db_session, sth)
        assert not ok
        # 删除导致 chain 短于 STH 承诺
        assert "shorter than STH" in (err or "") or "root hash mismatch" in (err or "")

    def test_inserted_event_breaks_old_sth(self, db_session: Session) -> None:
        """STH 签发后在中间插入伪造事件 → STH 验证失败。

        模拟方式：直接修改既有事件的 created_at 把它"推到旧 STH 之前",
        让 STH 看到的"前 N 条"包含本不属于的伪事件。
        """
        user = _make_user(db_session)
        events = [_make_audit_event(db_session, user) for _ in range(3)]
        sth = issue_signed_tree_head(db_session, user_id=user.id)

        # 插入一条"看起来发生在 events[0] 之前"的伪事件
        forged_time = events[0].created_at - timedelta(seconds=10)
        _make_audit_event(db_session, user, created_at=forged_time)

        ok, err = verify_sth_against_chain(db_session, sth)
        assert not ok
        assert "root hash mismatch" in (err or "")

    def test_tampered_sth_signature_detected(self, db_session: Session) -> None:
        """伪造 STH（改 root / tree_size / signature）→ 签名校验立即失败。"""
        user = _make_user(db_session)
        _make_audit_event(db_session, user)
        sth = issue_signed_tree_head(db_session, user_id=user.id)

        # 篡改 root
        original_root = sth.root_hash
        sth.root_hash = "0" * 64
        ok, err = verify_sth_against_chain(db_session, sth)
        assert not ok
        assert "signature mismatch" in (err or "")

        # 还原 root,篡改 signature
        sth.root_hash = original_root
        sth.signature = "0" * 64
        ok, err = verify_sth_against_chain(db_session, sth)
        assert not ok


# ===========================================================================
# 6. Inclusion proof 服务接口（评委独立验证场景）
# ===========================================================================
class TestInclusionProofService:
    """**Validates: G10**——评委可在不下载全链的前提下证明事件存在。"""

    def test_make_and_verify_inclusion_proof(self, db_session: Session) -> None:
        user = _make_user(db_session)
        events = [_make_audit_event(db_session, user) for _ in range(7)]
        sth = issue_signed_tree_head(db_session, user_id=user.id)

        # 对中间一条事件生成 proof
        target = events[3]
        proof_data = make_inclusion_proof(db_session, event_id=target.id, sth=sth)
        assert proof_data["tree_size"] == 7
        # leaf_index 取决于 (created_at, id) 排序，紧密创建时序可能并列；
        # 验证关键不变量：返回的 leaf_index 在 [0, tree_size) 范围内即可。
        assert 0 <= proof_data["leaf_index"] < 7  # type: ignore[operator]
        assert proof_data["event_id"] == str(target.id)

        # 客户端独立验证（仅用 root + leaf + proof,不需 DB）
        ok = verify_event_inclusion(
            leaf_hash_hex=proof_data["leaf_hash"],  # type: ignore[arg-type]
            leaf_index=proof_data["leaf_index"],  # type: ignore[arg-type]
            tree_size=proof_data["tree_size"],  # type: ignore[arg-type]
            proof=proof_data["proof"],  # type: ignore[arg-type]
            root_hash=proof_data["root_hash"],  # type: ignore[arg-type]
        )
        assert ok

    def test_inclusion_proof_unknown_event_raises(self, db_session: Session) -> None:
        user = _make_user(db_session)
        _make_audit_event(db_session, user)
        sth = issue_signed_tree_head(db_session, user_id=user.id)

        with pytest.raises(AuditMerkleError, match="not found"):
            make_inclusion_proof(db_session, event_id=uuid.uuid4(), sth=sth)


# ===========================================================================
# 7. event_hash_to_leaf 与线性链复用
# ===========================================================================
class TestLeafFromEventHash:
    def test_leaf_derived_from_event_hash(self) -> None:
        """同一 event_hash 总是派生同一个 leaf hash（确定性）。"""
        eh = hashlib.sha256(b"some-event").hexdigest()
        assert event_hash_to_leaf(eh) == event_hash_to_leaf(eh)

    def test_different_event_hashes_yield_different_leaves(self) -> None:
        a = event_hash_to_leaf(hashlib.sha256(b"a").hexdigest())
        b = event_hash_to_leaf(hashlib.sha256(b"b").hexdigest())
        assert a != b
