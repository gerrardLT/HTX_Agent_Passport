"""审计 / STH / Inclusion Proof Schemas（Phase 1 / G10-G11 跟进）。

为 ``app.routers.audit`` 暴露的 5 个端点提供请求/响应 Pydantic 模型：

- :class:`AuditEventResponse`        — GET /api/audit/events
- :class:`AuditEventListResponse`    — GET /api/audit/events 列表包装
- :class:`SthResponse`               — GET /api/audit/sth/latest, POST /api/audit/sth/issue
- :class:`InclusionProofResponse`    — GET /api/audit/events/{id}/inclusion
- :class:`ConsistencyProofResponse`  — GET /api/audit/sth/consistency

风格沿用现有 schemas（pydantic v2 + ``Field`` 描述 + ``ConfigDict(from_attributes=True)``）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models import AuditEvent, AuditTreeHead


# ---------------------------------------------------------------------------
# 单事件 + 列表
# ---------------------------------------------------------------------------
class AuditEventResponse(BaseModel):
    """单条审计事件响应。

    与前端 ``AuditEvent`` 类型镜像（手工对齐，避免引入 OpenAPI 生成）。
    所有 UUID 用字符串形式输出，便于前端不依赖 UUID 解析。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    event_type: str
    actor_type: str
    actor_id: str
    event_json: dict[str, Any]
    event_hash: str
    previous_event_hash: str | None
    trace_id: str | None
    created_at: datetime

    @classmethod
    def from_orm_event(cls, event: AuditEvent) -> "AuditEventResponse":
        """把 ORM 行转成响应模型。

        UUID 字段用 ``str()`` 转 hex；trace_id 是 nullable，None 直接透传。
        """
        return cls(
            id=str(event.id),
            event_type=event.event_type,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            event_json=event.event_json,
            event_hash=event.event_hash,
            previous_event_hash=event.previous_event_hash,
            trace_id=str(event.trace_id) if event.trace_id is not None else None,
            created_at=event.created_at,
        )


class AuditEventListResponse(BaseModel):
    """审计事件列表响应（数组 + 元信息包装）。"""

    events: list[AuditEventResponse]
    count: int = Field(..., description="本次返回的事件数（≤ limit）")


# ---------------------------------------------------------------------------
# Signed Tree Head
# ---------------------------------------------------------------------------
class SthResponse(BaseModel):
    """STH 响应（GET /sth/latest, POST /sth/issue 共用）。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    passport_id: str | None
    tree_size: int
    root_hash: str
    signature: str
    signed_at: datetime

    @classmethod
    def from_orm_sth(cls, sth: AuditTreeHead) -> "SthResponse":
        return cls(
            id=str(sth.id),
            user_id=str(sth.user_id),
            passport_id=str(sth.passport_id) if sth.passport_id is not None else None,
            tree_size=sth.tree_size,
            root_hash=sth.root_hash,
            signature=sth.signature,
            signed_at=sth.signed_at,
        )


# ---------------------------------------------------------------------------
# Inclusion / Consistency Proof
# ---------------------------------------------------------------------------
class InclusionProofResponse(BaseModel):
    """RFC 6962 Inclusion proof 响应。

    前端可以用 ``leaf_hash + leaf_index + tree_size + proof`` 在浏览器里
    用 Web Crypto API 重算 root，与 ``root_hash`` 比对——无需信任服务端。
    """

    event_id: str
    leaf_index: int = Field(..., ge=0, description="叶子在树中的 0-based 索引")
    leaf_hash: str = Field(..., description="叶子哈希 hex（已含 RFC 6962 0x00 前缀）")
    proof: list[str] = Field(..., description="兄弟节点 hash hex 列表，从底向上")
    tree_size: int = Field(..., gt=0, description="STH 承诺的事件总数")
    root_hash: str = Field(..., description="STH 的 Merkle 根 hex（验证比对目标）")


class ConsistencyProofResponse(BaseModel):
    """RFC 6962 Consistency proof 响应。

    证明：``new_tree`` 是 ``old_tree`` 的 append-only 扩展，没有任何历史
    叶子被改动 / 删除 / 重排。
    """

    from_size: int = Field(..., ge=0)
    to_size: int = Field(..., ge=0)
    proof: list[str]


__all__ = [
    "AuditEventListResponse",
    "AuditEventResponse",
    "ConsistencyProofResponse",
    "InclusionProofResponse",
    "SthResponse",
]
