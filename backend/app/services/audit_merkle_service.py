"""审计 Merkle 树服务 + Signed Tree Head（修复 G10/G11）。

职责
----
1. 从某条审计链（``(user_id, passport_id)``）的全部 ``audit_events`` 派生 Merkle 树叶子。
2. 计算当前 root hash，签发 STH（Signed Tree Head）并持久化到 ``audit_tree_heads``。
3. 给定历史 STH，重算根并比对——能检测「全链重写 / 删除事件 / 插入事件」。
4. 生成 inclusion proof（O(log N)），让评委/审计方能在不下载全链的情况下
   证明某事件确实在日志中。
5. 生成 consistency proof，证明新 STH 是旧 STH 的 append-only 扩展。

设计依据：docs/tech-research/02-audit-and-secrets.md G10/G11；RFC 6962。

签名（Phase 2 升级后）
-----------------------
签名实现已从本模块抽出到 :mod:`app.core.sth_signing`，支持两种 backend：

- ``HMAC-SHA256``（默认 ``AUDIT_STH_SIGNING_ALGO=hmac-sha256``）：向后兼容，
  共享密钥（``AUDIT_STH_SIGNING_KEY``，回退 JWT_SECRET）。
- ``Ed25519``（生产推荐 ``AUDIT_STH_SIGNING_ALGO=ed25519``）：RFC 8032 非对称
  签名，公钥可对外发布，符合 RFC 6962 / C2SP signed-note 标准。

签名串带算法前缀（``hmac:<hex>`` / ``ed25519:<hex>``），同表混合存储；
:func:`verify_sth_against_chain` 自动按前缀路由验证器，零切换日即可上线 Ed25519。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.merkle import (
    HASH_HEX_LEN,
    consistency_proof,
    event_hash_to_leaf,
    inclusion_proof,
    merkle_root,
    verify_inclusion_proof,
)
from app.core.sth_signing import (
    STHSigningError,
    sign_sth,
    verify_sth_signature,
)
from app.models import AuditEvent, AuditTreeHead

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class AuditMerkleError(RuntimeError):
    """审计 Merkle 服务错误（验证失败 / 签名不匹配 / 配置错误）。"""


# ---------------------------------------------------------------------------
# 签名辅助（已迁移到 app.core.sth_signing；此处保留 thin wrapper 以兼容历史代码）
# ---------------------------------------------------------------------------
# 现在签发用 :func:`app.core.sth_signing.sign_sth`，验证用 :func:`verify_sth_signature`,
# 自动按 signature 字段前缀路由 HMAC / Ed25519。本模块不再直接处理签名 bytes。


# ---------------------------------------------------------------------------
# 内部：从 DB 拉某链的事件按时间序排
# ---------------------------------------------------------------------------
def _fetch_chain_events(
    db: Session,
    *,
    user_id: UUID,
    passport_id: UUID | None,
) -> list[AuditEvent]:
    """按 ``(user_id, passport_id)`` 分链拉取审计事件，按时间顺序。"""
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.user_id == user_id)
        .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
    )
    if passport_id is None:
        stmt = stmt.where(AuditEvent.passport_id.is_(None))
    else:
        stmt = stmt.where(AuditEvent.passport_id == passport_id)
    return list(db.execute(stmt).scalars().all())


def _compute_leaves(events: list[AuditEvent]) -> list[str]:
    """把审计事件列表转换为 Merkle 叶子哈希列表。"""
    return [event_hash_to_leaf(e.event_hash) for e in events]


# ---------------------------------------------------------------------------
# 公开 API：签发 STH
# ---------------------------------------------------------------------------
def issue_signed_tree_head(
    db: Session,
    *,
    user_id: UUID,
    passport_id: UUID | None = None,
) -> AuditTreeHead:
    """对某条链当前的全部事件签发一份 STH 并持久化。

    Notes
    -----
    设计为可周期调用（如每 5 分钟 / 每 N 条新事件），让 STH 形成历史时间序列。
    每次都从全量事件重算 root——N 不大时（demo/中小规模）开销可忽略；大规模
    可改为增量 Merkle 树（保留中间节点缓存），不影响接口。
    """
    events = _fetch_chain_events(db, user_id=user_id, passport_id=passport_id)
    leaves = _compute_leaves(events)
    root = merkle_root(leaves)
    tree_size = len(leaves)
    signed_at = datetime.now(UTC)
    signed_at_iso = signed_at.isoformat()
    try:
        signature = sign_sth(tree_size, root, signed_at_iso)
    except STHSigningError as exc:
        raise AuditMerkleError(str(exc)) from exc

    sth = AuditTreeHead(
        user_id=user_id,
        passport_id=passport_id,
        tree_size=tree_size,
        root_hash=root,
        signature=signature,
        signed_at=signed_at,
    )
    db.add(sth)
    db.flush()
    logger.info(
        "STH signed: user=%s passport=%s size=%d root=%s",
        user_id, passport_id, tree_size, root,
    )
    return sth


def get_latest_sth(
    db: Session,
    *,
    user_id: UUID,
    passport_id: UUID | None = None,
) -> AuditTreeHead | None:
    """获取某条链最新的 STH（用于客户端验证）。"""
    stmt = (
        select(AuditTreeHead)
        .where(AuditTreeHead.user_id == user_id)
        .order_by(AuditTreeHead.signed_at.desc(), AuditTreeHead.id.desc())
        .limit(1)
    )
    if passport_id is None:
        stmt = stmt.where(AuditTreeHead.passport_id.is_(None))
    else:
        stmt = stmt.where(AuditTreeHead.passport_id == passport_id)
    return db.execute(stmt).scalars().first()


# ---------------------------------------------------------------------------
# 公开 API：验证 STH（签名 + root 重算）
# ---------------------------------------------------------------------------
def verify_sth_against_chain(
    db: Session,
    sth: AuditTreeHead,
) -> tuple[bool, str | None]:
    """对一条 STH 做完整验证：签名通过 + 重算 root 与 STH 一致。

    Returns
    -------
    (ok, error_msg)
        - ``(True, None)``：STH 真实有效，从 ``signed_at`` 时刻起的"前 tree_size
          个事件"未被篡改/删除/插入。
        - ``(False, msg)``：签名失败 / root 不一致——指示链被修改或 STH 被伪造。
    """
    if not verify_sth_signature(
        sth.tree_size, sth.root_hash, sth.signed_at.isoformat(), sth.signature
    ):
        return False, "STH signature mismatch"

    events = _fetch_chain_events(db, user_id=sth.user_id, passport_id=sth.passport_id)
    if len(events) < sth.tree_size:
        return False, (
            f"chain shorter than STH: have {len(events)} events, STH committed to {sth.tree_size}"
        )

    leaves = _compute_leaves(events[: sth.tree_size])
    recomputed = merkle_root(leaves)
    if recomputed != sth.root_hash:
        return False, (
            f"root hash mismatch: STH says {sth.root_hash}, recomputed {recomputed}. "
            "Chain has been tampered (rewrite/insert/delete) since this STH."
        )
    return True, None


# ---------------------------------------------------------------------------
# 公开 API：inclusion proof（评委/审计方核心能力）
# ---------------------------------------------------------------------------
def make_inclusion_proof(
    db: Session,
    *,
    event_id: UUID,
    sth: AuditTreeHead,
) -> dict[str, object]:
    """生成某事件在指定 STH 中的 inclusion proof。

    Returns
    -------
    dict
        ``{"event_hash", "leaf_hash", "leaf_index", "tree_size", "proof": [...]}``。
        客户端可用 :func:`app.core.merkle.verify_inclusion_proof` 配合 STH 的
        ``root_hash`` 独立验证——不需要下载全链。
    """
    events = _fetch_chain_events(db, user_id=sth.user_id, passport_id=sth.passport_id)
    if len(events) < sth.tree_size:
        raise AuditMerkleError(
            f"chain has only {len(events)} events but STH committed to {sth.tree_size}"
        )

    target_idx = next(
        (i for i, e in enumerate(events[: sth.tree_size]) if e.id == event_id),
        None,
    )
    if target_idx is None:
        raise AuditMerkleError(
            f"event {event_id} not found in first {sth.tree_size} events of this chain"
        )

    leaves = _compute_leaves(events[: sth.tree_size])
    proof = inclusion_proof(leaves, target_idx)
    target_event = events[target_idx]
    return {
        "event_id": str(event_id),
        "event_hash": target_event.event_hash,
        "leaf_hash": leaves[target_idx],
        "leaf_index": target_idx,
        "tree_size": sth.tree_size,
        "root_hash": sth.root_hash,
        "proof": proof,
    }


def verify_event_inclusion(
    *,
    leaf_hash_hex: str,
    leaf_index: int,
    tree_size: int,
    proof: list[str],
    root_hash: str,
) -> bool:
    """客户端侧的 inclusion proof 验证（不需要 DB）。

    把这个函数也暴露在 service 模块是便利接口；底层就是
    :func:`app.core.merkle.verify_inclusion_proof`。
    """
    return verify_inclusion_proof(
        leaf_hex=leaf_hash_hex,
        index=leaf_index,
        tree_size=tree_size,
        proof=proof,
        expected_root_hex=root_hash,
    )


# ---------------------------------------------------------------------------
# 公开 API：consistency proof（"新 STH 是旧 STH 的扩展"）
# ---------------------------------------------------------------------------
def make_consistency_proof(
    db: Session,
    *,
    old_sth: AuditTreeHead,
    new_sth: AuditTreeHead,
) -> list[str]:
    """生成从 ``old_sth`` 到 ``new_sth`` 的 consistency proof。

    用于证明：新 STH 承诺的链是旧 STH 承诺的链的 append-only 扩展，没有任何
    历史事件被改动/删除/重排。客户端可用 RFC 6962 §2.1.2 的 verify 算法独立校验。
    """
    if old_sth.user_id != new_sth.user_id or old_sth.passport_id != new_sth.passport_id:
        raise AuditMerkleError("STH chain mismatch (different user/passport)")
    events = _fetch_chain_events(db, user_id=new_sth.user_id, passport_id=new_sth.passport_id)
    if len(events) < new_sth.tree_size:
        raise AuditMerkleError("chain shorter than new STH")
    new_leaves = _compute_leaves(events[: new_sth.tree_size])
    old_leaves = new_leaves[: old_sth.tree_size]
    return consistency_proof(old_leaves, new_leaves)


__all__ = [
    "AuditMerkleError",
    "get_latest_sth",
    "issue_signed_tree_head",
    "make_consistency_proof",
    "make_inclusion_proof",
    "verify_event_inclusion",
    "verify_sth_against_chain",
]
