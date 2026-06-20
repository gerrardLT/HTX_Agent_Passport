"""审批路由（任务 11 / Req 8）。

端点：
    POST /api/actions/{action_id}/approve  → 提交审批决定

设计依据：design.md「审批层」/ Req 8 AC1-9。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user, get_db
from app.models import User
from app.schemas.approval import ApprovalSubmitRequest, ApprovalSubmitResponse
from app.services.approval_service import (
    ActionNotInApprovalStateError,
    ApprovalAlreadyProcessedError,
    ApprovalExpiredError,
    ApprovalInvalidConfirmationError,
    ApprovalNotFoundError,
    ApprovalPassportRevokedError,
    MarketSlippageExceededError,
    submit_approval,
)

router = APIRouter()


@router.post(
    "/actions/{action_id}/approve",
    response_model=ApprovalSubmitResponse,
    status_code=status.HTTP_200_OK,
    summary="提交审批决定（批准或拒绝）",
    responses={
        400: {"description": "typed_confirmation 校验失败"},
        404: {"description": "Action 或 Approval 不存在"},
        409: {"description": "双重审批 / 已过期 / Passport 已撤销"},
    },
)
def approve_action(
    action_id: UUID,
    body: ApprovalSubmitRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ApprovalSubmitResponse:
    """提交审批决定。

    - approved=true + typed_confirmation="APPROVE" → 批准
    - approved=false → 拒绝（typed_confirmation 可为任意值）
    """
    try:
        action = submit_approval(
            db,
            action_id=action_id,
            user_id=current_user.id,
            approved=body.approved,
            typed_confirmation=body.typed_confirmation,
            signature=body.signature,
        )
        db.commit()
        return ApprovalSubmitResponse(
            action_id=str(action.id),
            state=action.state,
        )
    except ApprovalNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "NOT_FOUND", "message": str(exc)},
        ) from exc
    except ActionNotInApprovalStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "ACTION_STATE_INVALID", "message": str(exc)},
        ) from exc
    except ApprovalAlreadyProcessedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "APPROVAL_ALREADY_PROCESSED", "message": str(exc)},
        ) from exc
    except ApprovalExpiredError as exc:
        db.commit()  # 惰性过期已写入状态变更
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "APPROVAL_EXPIRED", "message": str(exc)},
        ) from exc
    except ApprovalPassportRevokedError as exc:
        db.commit()  # 撤销/重裁决已写入状态变更
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "PASSPORT_REVOKED", "message": str(exc)},
        ) from exc
    except ApprovalInvalidConfirmationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CONFIRMATION", "message": str(exc)},
        ) from exc
    except MarketSlippageExceededError as exc:
        db.commit()  # slippage 状态变更已写入
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": exc.reason_code or "MARKET_SLIPPAGE_EXCEEDED",
                "message": exc.message,
            },
        ) from exc
