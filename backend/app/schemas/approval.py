"""审批请求/响应 Schema（任务 11 / Req 8）。

定义 POST /api/actions/{action_id}/approve 的请求体与响应体。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ApprovalSubmitRequest(BaseModel):
    """审批提交请求体（Req 8 AC2）。

    - ``approved``：true=批准 / false=拒绝。
    - ``typed_confirmation``：approved=true 时必须为 "APPROVE"（Req 8 AC2）。
    - ``signature``：可选钱包签名（Req 8 AC2 可选支持）。
    """

    approved: bool
    typed_confirmation: str = Field(..., description="Must be 'APPROVE' when approved=true")
    signature: str | None = None


class ApprovalSubmitResponse(BaseModel):
    """审批提交响应体。"""

    action_id: str
    state: str
