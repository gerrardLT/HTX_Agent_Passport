"""审计 / STH / Inclusion Proof 路由（Phase 1 / G10-G11 跟进）。

挂载到 ``/api/audit/*``，提供 5 个端点：

```
GET  /api/audit/events                              → 200 审计事件列表（带过滤 + 鉴权隔离）
GET  /api/audit/sth/latest                          → 200/404 当前最新 STH
POST /api/audit/sth/issue                           → 201 手动触发 STH 签发（演示/测试）
GET  /api/audit/events/{event_id}/inclusion         → 200/404 inclusion proof
GET  /api/audit/sth/consistency                     → 200/404 consistency proof
```

设计要点
--------
1. **鉴权隔离**：所有端点都依赖 :func:`get_current_user`；查询/proof 都强制
   ``WHERE user_id = current_user.id``——跨用户访问统一返回 404
   （避免存在性侧信道，与 passports / approvals 路由风格一致）。
2. **事务边界**：所有"写"端点（``POST /sth/issue``）由路由层 ``db.commit()``，
   服务层只 ``flush``，与 design.md「commit at router layer」一致。
3. **STH 签发后立即锚定**：``POST /sth/issue`` 在 commit 之后调用
   :func:`anchor_sth_to_file`，让"DB 持久化 + 外部锚定"对调用方表现为单一行为。
4. **inclusion proof 鉴权**：先确认 event 属于当前用户，再生成 proof——
   防止用户 A 通过 event_id 推断用户 B 的链结构。

错误码约定
----------
- 400 ``BAD_REQUEST`` —— 缺少必要过滤参数 / from_size >= to_size 等。
- 404 ``NOT_FOUND``   —— 链/事件不存在 / 不属本人 / tree_size 大于现有事件数。
- 409 ``CONFLICT``    —— 暂未使用（保留位）。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.dependencies import get_current_user, get_db
from app.core.sth_signing import get_public_key_hex, get_signing_algo
from app.models import AuditEvent, User
from app.schemas.audit import (
    AuditEventListResponse,
    AuditEventResponse,
    ConsistencyProofResponse,
    InclusionProofResponse,
    SthResponse,
)
from app.services.audit_merkle_service import (
    AuditMerkleError,
    get_latest_sth,
    issue_signed_tree_head,
    make_consistency_proof,
    make_inclusion_proof,
)
from app.services.audit_sth_anchor import anchor_sth_to_file

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /events  —— 审计事件列表（按 action / passport / user 过滤）
# ---------------------------------------------------------------------------
@router.get(
    "/events",
    response_model=AuditEventListResponse,
    status_code=status.HTTP_200_OK,
    summary="查询审计事件列表（按 action_id / passport_id / user_id 过滤；强制 user_id 鉴权隔离）",
)
def list_audit_events(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    action_id: UUID | None = Query(None, description="按 action_id 过滤"),
    passport_id: UUID | None = Query(None, description="按 passport_id 过滤"),
    # user_id 仅作语义占位——实际查询永远强制 = current_user.id
    user_id: UUID | None = Query(
        None, description="按 user_id 过滤（实际只能查自己的事件，传他人 ID 等同 404 空集）"
    ),
    limit: int = Query(200, ge=1, le=1000, description="返回上限（≤ 1000）"),
) -> AuditEventListResponse:
    """``GET /api/audit/events``

    至少给一个过滤条件（action_id / passport_id / user_id），否则 400——
    避免未限定查询拉全表。``user_id`` 参数在语义上"按用户过滤"，但实际查询
    一律强制 ``WHERE user_id = current_user.id``——传别人的 user_id 等同
    传自己的（不会越权也不会泄露存在性）。

    返回顺序按 ``created_at ASC, id ASC``——与 audit_writer / merkle service 的
    链遍历顺序一致，前端时间线可直接渲染。
    """
    if action_id is None and passport_id is None and user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": "at least one of action_id, passport_id, user_id is required",
            },
        )

    stmt = (
        select(AuditEvent)
        .where(AuditEvent.user_id == current_user.id)
        .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        .limit(limit)
    )
    if action_id is not None:
        stmt = stmt.where(AuditEvent.action_id == action_id)
    if passport_id is not None:
        stmt = stmt.where(AuditEvent.passport_id == passport_id)
    # user_id 参数被静默忽略——查询永远强制 current_user.id，避免越权。

    events = list(db.execute(stmt).scalars().all())
    return AuditEventListResponse(
        events=[AuditEventResponse.from_orm_event(e) for e in events],
        count=len(events),
    )


# ---------------------------------------------------------------------------
# GET /sth/latest —— 当前最新 STH
# ---------------------------------------------------------------------------
@router.get(
    "/sth/latest",
    response_model=SthResponse,
    status_code=status.HTTP_200_OK,
    summary="获取某条链最新一份 STH（passport_id 可选；不传 = 用户级链）",
    responses={404: {"description": "该链尚无 STH"}},
)
def get_latest_sth_endpoint(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    passport_id: UUID | None = Query(
        None, description="护照级链；不传则查 user_id+passport_id IS NULL 的用户级链"
    ),
) -> SthResponse:
    """``GET /api/audit/sth/latest?passport_id=...``

    无 STH 时 404（尚未签发——可能是首次访问、还没轮到周期签发，也可能链上无事件）。
    用户只能拿自己的链 STH——服务函数已用 ``current_user.id`` 过滤。
    """
    sth = get_latest_sth(db, user_id=current_user.id, passport_id=passport_id)
    if sth is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "NOT_FOUND",
                "message": "no STH found for this chain",
            },
        )
    return SthResponse.from_orm_sth(sth)


# ---------------------------------------------------------------------------
# POST /sth/issue —— 手动触发 STH 签发（演示/测试用）
# ---------------------------------------------------------------------------
@router.post(
    "/sth/issue",
    response_model=SthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="手动触发一次 STH 签发（不依赖 scheduler 周期；签发后立即锚定）",
)
def issue_sth_endpoint(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    passport_id: UUID | None = Query(
        None, description="护照级链；不传则签发用户级链的 STH"
    ),
) -> SthResponse:
    """``POST /api/audit/sth/issue?passport_id=...``

    路径上是 POST 但语义是"幂等签发"——同 root 多次调用会得到不同 STH 行
    （因为 ``signed_at`` 不同，签名也不同），但 root 不变。这是 RFC 6962
    可预期行为：每次调用都得到一个"截至此刻"的承诺。

    流程
    ----
    1. ``issue_signed_tree_head(...)`` 派生根 + HMAC 签名 + flush
    2. ``db.commit()`` 落库
    3. ``anchor_sth_to_file(...)`` 追加到外部 JSONL（如已配置）

    Notes
    -----
    锚定失败**不**让接口报错——锚定是辅助证据，缺失时 ``audit_sth_anchor``
    内部已经吞错并写日志，本接口对调用方而言永远成功（除非 DB 异常）。
    """
    sth = issue_signed_tree_head(
        db, user_id=current_user.id, passport_id=passport_id
    )
    db.commit()
    db.refresh(sth)

    settings = get_settings()
    # 锚定是 best-effort，函数自身吞错；这里捕获返回值仅做日志（如有需要）
    anchor_sth_to_file(sth, settings.AUDIT_STH_ANCHOR_PATH)

    return SthResponse.from_orm_sth(sth)


# ---------------------------------------------------------------------------
# GET /events/{event_id}/inclusion  —— Inclusion proof
# ---------------------------------------------------------------------------
@router.get(
    "/events/{event_id}/inclusion",
    response_model=InclusionProofResponse,
    status_code=status.HTTP_200_OK,
    summary="生成某事件在指定 tree_size 的 inclusion proof（前端可在浏览器独立验证）",
    responses={404: {"description": "事件不存在 / 不属本人 / 不在 tree_size 内"}},
)
def get_inclusion_proof_endpoint(
    event_id: UUID,
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    tree_size: int = Query(..., ge=1, description="目标 STH 的 tree_size"),
    passport_id: UUID | None = Query(
        None, description="链选择：与 STH 同一链；不传 = 用户级链"
    ),
) -> InclusionProofResponse:
    """``GET /api/audit/events/{event_id}/inclusion?tree_size=N&passport_id=...``

    流程
    ----
    1. 加载事件并校验归属（必须属于当前用户）——跨用户统一 404。
    2. 找到与 (user_id, passport_id, tree_size) 对应的 STH——若 DB 里没有
       ``tree_size`` 完全相等的 STH，则**用任意一份 ``tree_size >= N`` 的最新
       STH 替代**？不——为避免歧义，我们这里**临时构造 fake STH**（仅用于
       proof 生成），让 ``make_inclusion_proof`` 在指定 tree_size 上工作。
    3. 调用 :func:`make_inclusion_proof`。

    错误
    ----
    - 404：event 不属当前用户 / 链上事件数 < tree_size / event 不在前 tree_size 内。
    """
    # 1. 用归属过滤直接查事件
    event = db.execute(
        select(AuditEvent)
        .where(AuditEvent.id == event_id)
        .where(AuditEvent.user_id == current_user.id)
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": "audit event not found"},
        )

    # 2. 构造 proof 上下文。
    #
    # 关键点：``make_inclusion_proof`` 只用 ``sth.user_id`` / ``sth.passport_id`` /
    # ``sth.tree_size`` 决定取哪些事件计算 proof；返回的 ``root_hash`` 直接拷贝
    # ``sth.root_hash``——所以临时 STH 的 ``root_hash`` 必须**真实**才能让前端
    # verify 通过。我们这里直接用 :func:`merkle_root` 现场计算正确根。
    from datetime import UTC, datetime

    from app.core.merkle import event_hash_to_leaf, merkle_root
    from app.models import AuditTreeHead

    # 拉链上前 tree_size 条事件 → 算 root
    chain_events = list(
        db.execute(
            select(AuditEvent)
            .where(AuditEvent.user_id == current_user.id)
            .where(
                AuditEvent.passport_id.is_(None)
                if passport_id is None
                else AuditEvent.passport_id == passport_id
            )
            .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
            .limit(tree_size)
        ).scalars().all()
    )
    if len(chain_events) < tree_size:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "NOT_FOUND",
                "message": (
                    f"chain has only {len(chain_events)} events but proof requires {tree_size}"
                ),
            },
        )
    leaves = [event_hash_to_leaf(e.event_hash) for e in chain_events]
    computed_root = merkle_root(leaves)

    sth = AuditTreeHead(
        user_id=current_user.id,
        passport_id=passport_id,
        tree_size=tree_size,
        root_hash=computed_root,
        signature="<proof-only-no-signature>",
        signed_at=datetime.now(UTC),
    )

    # 3. 生成 proof
    try:
        proof_data = make_inclusion_proof(db, event_id=event_id, sth=sth)
    except AuditMerkleError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": str(exc)},
        ) from exc

    return InclusionProofResponse(
        event_id=str(proof_data["event_id"]),
        leaf_index=int(proof_data["leaf_index"]),
        leaf_hash=str(proof_data["leaf_hash"]),
        proof=list(proof_data["proof"]),  # type: ignore[arg-type]
        tree_size=int(proof_data["tree_size"]),
        root_hash=str(proof_data["root_hash"]),
    )


# ---------------------------------------------------------------------------
# GET /sth/consistency —— Consistency proof
# ---------------------------------------------------------------------------
@router.get(
    "/sth/consistency",
    response_model=ConsistencyProofResponse,
    status_code=status.HTTP_200_OK,
    summary="生成 from_size → to_size 的 consistency proof（证明 append-only 扩展）",
    responses={404: {"description": "链事件数不足 / from_size > to_size"}},
)
def get_consistency_proof_endpoint(
    db: Annotated[Session, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    from_size: int = Query(..., ge=0),
    to_size: int = Query(..., ge=0),
    passport_id: UUID | None = Query(None),
) -> ConsistencyProofResponse:
    """``GET /api/audit/sth/consistency?from_size=N&to_size=M&passport_id=...``

    用临时 STH 调用 :func:`make_consistency_proof`——演示/审计场景，
    前端用 RFC 6962 客户端实现独立验证（本任务前端不强制接入）。
    """
    if from_size > to_size:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": "from_size must be <= to_size",
            },
        )

    from datetime import UTC, datetime

    from app.models import AuditTreeHead

    old_sth = AuditTreeHead(
        user_id=current_user.id,
        passport_id=passport_id,
        tree_size=from_size,
        root_hash="<computed>",
        signature="<no-sig>",
        signed_at=datetime.now(UTC),
    )
    new_sth = AuditTreeHead(
        user_id=current_user.id,
        passport_id=passport_id,
        tree_size=to_size,
        root_hash="<computed>",
        signature="<no-sig>",
        signed_at=datetime.now(UTC),
    )
    try:
        proof = make_consistency_proof(db, old_sth=old_sth, new_sth=new_sth)
    except AuditMerkleError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": str(exc)},
        ) from exc

    return ConsistencyProofResponse(
        from_size=from_size, to_size=to_size, proof=proof
    )


# ---------------------------------------------------------------------------
# GET /sth/verifier-key —— 暴露 STH 验证公钥（Phase 2 / Ed25519 升级）
# ---------------------------------------------------------------------------
@router.get(
    "/sth/verifier-key",
    status_code=status.HTTP_200_OK,
    summary="返回当前 STH 验证公钥（Ed25519 模式）+ 签名算法标识",
)
def get_sth_verifier_key(
    current_user: Annotated[User, Depends(get_current_user)],
) -> dict[str, str | None]:
    """``GET /api/audit/sth/verifier-key``

    评委 / 外部审计员凭此端点拿到公钥后，可在浏览器或独立程序中用
    ``cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey.from_public_bytes``
    完全离线地验证任何 STH——不需要信任服务方。

    返回字段：

    - ``algo``：``hmac-sha256`` 或 ``ed25519``
    - ``public_key_hex``：32 字节 Ed25519 公钥的 hex 编码（仅 ed25519 模式有值）；
      HMAC 模式返回 None（HMAC 是对称的，没有"公钥"概念）。

    保留鉴权（登录后才能拿）的理由：避免被作匿名探测端点；公钥本身**可公开**,
    生产部署可把它复制到公开 git 仓库 / 项目 README。
    """
    _ = current_user  # 鉴权门槛而已，不参与生成
    return {
        "algo": get_signing_algo(),
        "public_key_hex": get_public_key_hex(),
    }


__all__ = ["router"]
